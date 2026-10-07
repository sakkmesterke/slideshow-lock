"""What each of the ten transitions looks like at a given moment, and which one comes next. No GTK.

``compose(name, progress, width, height)`` answers with a list of ``Draw``: the pictures to paint,
from the bottom up, on top of a black window. The preview window only turns each ``Draw`` into
``Gtk.Snapshot`` calls (clip, opacity, blur, translate/rotate/scale around the window's centre),
so the shape of every transition is decided and tested here, without a display. Where the new
picture meets the old one the edge is soft (``Soft``): a gradient, which the window makes a mask.

Progress runs from 0 (the old picture alone) to 1 (the new one alone) and is eased here, so the
window passes the plain fraction of the time that has gone. The new picture is always painted over
the old one, never beside it in the same layer, so it never makes the old one less opaque than the
pixels below it would allow: a transition cannot dip towards black unless it is the "fade-black".

``TransitionChooser`` picks the transition for every change of picture from the stored list.
"""

from __future__ import annotations

import math
import random
from typing import List, NamedTuple, Optional, Sequence, Tuple

from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BLUR,
    CIRCLE,
    CROSSFADE,
    FADE_BLACK,
    KEN_BURNS,
    NEW,
    OLD,
    ORDER_SEQUENCE,
    PUSH,
    ROTATE,
    SLIDE_IN,
    WIPE,
    ZOOM,
    clean,
)

#: The Ken Burns picture comes in enlarged by this much and over the whole run shrinks to its own
#: size, and starts shifted to the left by ``KEN_BURNS_DRIFT`` of the window's width and drifts back
#: to the middle: the last frame of the run is the picture as the plain drawing shows it, so
#: nothing jumps when the run ends. The enlarged picture must cover the window at every moment of
#: the run (no black edge shows): half of what it is larger by is at least what it is shifted by,
#: ``KEN_BURNS_DRIFT <= KEN_BURNS_ZOOM / 2`` (both shrink with the same factor, so this is enough).
KEN_BURNS_ZOOM = 0.14
KEN_BURNS_DRIFT = 0.035

#: The zoom: the old picture grows by this much while it fades out, the new one comes in from
#: ``1 - ZOOM_IN`` of its size.
ZOOM_OUT = 0.15
ZOOM_IN = 0.15

#: The rotation: the new picture turns in from this many degrees, enlarged by ``ROTATE_SCALE``.
ROTATE_DEGREES = 12.0
ROTATE_SCALE = 0.25

#: The most the blur reaches, in pixels, on the (reduced) pictures the window blurs.
BLUR_RADIUS = 8.0

#: The opacity of the new picture on the very first frame of a transition, at a point of one pixel:
#: it makes the window upload the new texture where nobody can see it (a texture that is not
#: drawn at all is not uploaded until the frame it is first seen on).
UPLOAD_OPACITY = 1.0 / 255.0

#: The soft edge of the circle, wipe, slide-in, push, zoom and rotate: a band this wide (as a share
#: of the window's shorter side) over which the new picture fades in, instead of a line. Where the
#: edge has no room to run its whole width (it comes in from, or ends at, the window's border) the
#: band narrows, so the first and the last frame are the plain pictures with no band left behind.
SOFT_EDGE_SHARE = 0.12

#: What a ``Soft`` edge is measured on: the ``clip`` rectangle (window pixels), the picture's own
#: rectangle (its own pixels, moved/turned/scaled with it) or the rim of the ``circle``.
SOFT_CLIP = "clip"
SOFT_PICTURE = "picture"
SOFT_CIRCLE = "circle"

Rect = Tuple[float, float, float, float]  # x, y, width, height in window pixels


class Soft(NamedTuple):
    """A soft edge: the picture goes from nothing at the cut edge to whole *width* pixels inside it
    (linearly), instead of stopping at a line. *kind* says which edge (``SOFT_CLIP``,
    ``SOFT_PICTURE``, ``SOFT_CIRCLE``); *sides* which of its sides are soft, any of ``l`` ``t``
    ``r`` ``b`` (not used by the circle, whose rim is the one edge)."""

    kind: str
    width: float
    sides: str = ""


class Draw(NamedTuple):
    """One picture to paint. ``scale``, ``angle`` (degrees, clockwise) and the translation
    ``dx, dy`` (window pixels, applied after them) work around the centre of the window; ``clip``
    and ``circle`` (centre x, centre y, radius) cut the result; ``soft`` makes one of the cuts, or
    the picture's own edge, a gradient; ``blur`` is a radius in pixels."""

    layer: str
    opacity: float = 1.0
    scale: float = 1.0
    angle: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    clip: Optional[Rect] = None
    circle: Optional[Tuple[float, float, float]] = None
    blur: float = 0.0
    soft: Optional[Soft] = None


def ease(progress: float) -> float:
    """Smoothstep of *progress* clamped to 0..1: slow start and end, 0 and 1 stay where they are."""
    p = min(1.0, max(0.0, float(progress)))
    return p * p * (3.0 - 2.0 * p)


def soft_width(width: float, height: float) -> float:
    """The full width, in pixels, of a soft edge in a window of *width* x *height*."""
    return SOFT_EDGE_SHARE * min(float(width), float(height))


def edge_stops(size: float, width: float, near: bool, far: bool) -> List[Tuple[float, float]]:
    """The gradient across *size* pixels along one axis, as ``(offset 0..1, alpha)`` stops: the
    near end (``near``) and/or the far end (``far``) goes from nothing to whole over *width*
    pixels (the shorter *size* is, the less whole it gets: the two ramps meet at their crossing).
    A ramp is measured from the end, also when it would begin before the start."""
    if size <= 0.0:
        return [(0.0, 0.0), (1.0, 0.0)]
    if width <= 0.0:
        return [(0.0, 1.0), (1.0, 1.0)]

    def alpha(x: float) -> float:
        value = 1.0
        if near:
            value = min(value, x / width)
        if far:
            value = min(value, (size - x) / width)
        return max(0.0, value)

    points = {0.0, size}
    if near:
        points.add(width)
    if far:
        points.add(size - width)
    if near and far and width * 2.0 > size:
        points.add(size / 2.0)
    kept = sorted(x for x in points if 0.0 <= x <= size)
    return [(x / size, alpha(x)) for x in kept]


def rim_stops(radius: float, width: float) -> List[Tuple[float, float]]:
    """The gradient from the centre to the *radius* of a soft circle, as ``(offset 0..1, alpha)``
    stops: whole up to *width* pixels inside the rim, nothing at the rim (a circle smaller than
    *width* is not whole even in the middle)."""
    if radius <= 0.0:
        return [(0.0, 0.0), (1.0, 0.0)]
    if width <= 0.0:
        return [(0.0, 1.0), (1.0, 1.0)]
    inner = radius - width
    if inner <= 0.0:
        return [(0.0, radius / width), (1.0, 0.0)]
    return [(0.0, 1.0), (inner / radius, 1.0), (1.0, 0.0)]


def stops_alpha(stops: Sequence[Tuple[float, float]], offset: float) -> float:
    """The alpha of *stops* at *offset* (0..1): linear between them, the end value past them."""
    if offset <= stops[0][0]:
        return stops[0][1]
    for (o0, a0), (o1, a1) in zip(stops, stops[1:]):
        if offset <= o1:
            return a0 if o1 == o0 else a0 + (a1 - a0) * (offset - o0) / (o1 - o0)
    return stops[-1][1]


def compose(
    name: str, progress: float, width: float, height: float, fade_share: float = 1.0
) -> List[Draw]:
    """The pictures to paint for transition *name* at *progress* (0 to 1, clamped) in a window of
    *width* x *height* pixels, bottom first. An empty list for a name that is not one of the ten.

    ``fade_share`` only matters for Ken Burns, whose slow move lasts for the whole run (its last
    frame is the picture as it is) while the cross fade into it takes this share of that time
    (0 to 1)."""
    p = min(1.0, max(0.0, float(progress)))
    e = ease(p)
    w, h = float(width), float(height)
    window: Rect = (0.0, 0.0, w, h)
    soft = soft_width(w, h)

    if name == CROSSFADE:
        return [Draw(OLD), Draw(NEW, opacity=e)]
    if name == FADE_BLACK:
        if e < 0.5:
            return [Draw(OLD, opacity=1.0 - 2.0 * e)]
        return [Draw(NEW, opacity=2.0 * e - 1.0)]
    if name == SLIDE_IN:
        dx = w * (1.0 - e)
        edge = min(soft, dx) if dx < w else 0.0  # narrower as the edge nears the window's border
        return [
            Draw(OLD),
            Draw(NEW, dx=dx, clip=window, soft=Soft(SOFT_PICTURE, edge, "l") if edge > 0 else None),
        ]
    if name == PUSH:
        # The old picture ends at *seam*. The new one comes in over the last *overlap* pixels of
        # it, soft, so that the seam is two pictures dissolving into each other, not a dark line.
        seam = w * (1.0 - e)
        overlap = min(soft, seam, w - seam)
        return [
            Draw(OLD, dx=-w * e, clip=window),
            Draw(
                NEW,
                dx=seam - overlap,
                clip=window,
                soft=Soft(SOFT_PICTURE, overlap, "l") if overlap > 0 else None,
            ),
        ]
    if name == KEN_BURNS:
        share = min(1.0, max(1e-6, float(fade_share)))
        fade = ease(min(1.0, p / share))
        left = 1.0 - p  # the part of the slow move still to go: 1 at the start, 0 at the end
        return [
            Draw(OLD),
            Draw(
                NEW,
                opacity=fade,
                scale=1.0 + KEN_BURNS_ZOOM * left,
                dx=-w * KEN_BURNS_DRIFT * left,
            ),
        ]
    if name == ZOOM:
        edge = soft * (1.0 - e)
        return [
            Draw(OLD, scale=1.0 + ZOOM_OUT * e),
            Draw(
                NEW,
                opacity=e,
                scale=(1.0 - ZOOM_IN) + ZOOM_IN * e,
                soft=Soft(SOFT_PICTURE, edge, "ltrb") if edge > 0 else None,
            ),
        ]
    if name == WIPE:
        # The front runs *soft* pixels past the window, so that the last of the band leaves it.
        if e >= 1.0:
            return [Draw(OLD), Draw(NEW)]
        return [
            Draw(OLD),
            Draw(NEW, clip=(0.0, 0.0, (w + soft) * e, h), soft=Soft(SOFT_CLIP, soft, "r")),
        ]
    if name == CIRCLE:
        if e >= 1.0:
            return [Draw(OLD), Draw(NEW)]
        radius = (math.hypot(w, h) / 2.0 + soft) * e  # the same: the band ends past the corners
        return [
            Draw(OLD),
            Draw(NEW, circle=(w / 2.0, h / 2.0, radius), soft=Soft(SOFT_CIRCLE, soft)),
        ]
    if name == BLUR:
        radius = BLUR_RADIUS * math.sin(math.pi * e)
        radius = radius if radius > 1e-9 else 0.0
        return [Draw(OLD, blur=radius)] if e < 0.5 else [Draw(NEW, blur=radius)]
    if name == ROTATE:
        edge = soft * (1.0 - e)
        return [
            Draw(OLD),
            Draw(
                NEW,
                opacity=e,
                angle=-ROTATE_DEGREES * (1.0 - e),
                scale=1.0 + ROTATE_SCALE * (1.0 - e),
                soft=Soft(SOFT_PICTURE, edge, "ltrb") if edge > 0 else None,
            ),
        ]
    return []


def first_frame(name: str, width: float, height: float, fade_share: float = 1.0) -> List[Draw]:
    """The frame drawn before the transition's clock starts: ``compose`` at 0 with the new picture
    added at a single pixel and almost no opacity, so that it is drawn, hence uploaded, here."""
    draws = compose(name, 0.0, width, height, fade_share)
    if not draws and name not in ALL_TRANSITIONS:
        return draws
    return draws + [Draw(NEW, opacity=UPLOAD_OPACITY, clip=(0.0, 0.0, 1.0, 1.0))]


def effective_name(
    name: str,
    frame_size: Tuple[int, int],
    window_size: Tuple[int, int],
    pan_range: Tuple[int, int],
    software_gl: bool = False,
) -> str:
    """The transition that is really drawn for *name* when the new picture is *frame_size*
    pixels in a window of *window_size*: Ken Burns needs a picture that fills the window and does
    not scroll (a smaller one would show its edges move, a scrolling one already moves), the blur
    is too heavy for software rendering. Both become the cross fade."""
    if name == KEN_BURNS:
        fills = frame_size[0] >= window_size[0] and frame_size[1] >= window_size[1]
        if not fills or tuple(pan_range) != (0, 0):
            return CROSSFADE
    if name == BLUR and software_gl:
        return CROSSFADE
    return name


def software_renderer(renderer_class: str, environ=None) -> bool:
    """True when drawing is known to be done by the CPU: GTK's Cairo renderer (also asked for with
    ``GSK_RENDERER=cairo``), or Mesa told to use its software rasteriser (``LIBGL_ALWAYS_SOFTWARE``
    set, ``GALLIUM_DRIVER`` ``llvmpipe`` or ``softpipe``). Not known to be: everything else. GTK
    does not say which OpenGL driver draws, so a machine whose Mesa falls back to ``llvmpipe`` by
    itself (a virtual machine without a GPU) is not recognised here."""
    environ = environ if environ is not None else {}
    if "cairo" in renderer_class.lower() or environ.get("GSK_RENDERER", "").lower() == "cairo":
        return True
    if environ.get("LIBGL_ALWAYS_SOFTWARE", "") not in ("", "0", "false"):
        return True
    return environ.get("GALLIUM_DRIVER", "").lower() in ("llvmpipe", "softpipe")


class TransitionChooser:
    """Picks the transition for each change of picture from the list in the settings.

    One name in the list: that one. Several, ``random``: any but the one used last, so the same
    transition never comes twice in a row; ``sequence``: the next one after the last, in the order
    of ``ALL_TRANSITIONS``, round and round. None in the list: None (the picture is cut). A name
    that is not one of the ten is left out (``clean`` logs it once). *rng* is a ``random.Random``
    (a seeded one makes a test deterministic)."""

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng if rng is not None else random.Random()
        self._last: Optional[str] = None

    def next(self, names: Sequence, order: str) -> Optional[str]:
        names = [name for name in clean(names) if name in ALL_TRANSITIONS]
        if not names:
            self._last = None
            return None
        if len(names) == 1:
            choice = names[0]
        elif order == ORDER_SEQUENCE:
            canonical = [name for name in ALL_TRANSITIONS if name in names]
            position = ALL_TRANSITIONS.index(self._last) if self._last is not None else -1
            later = [name for name in canonical if ALL_TRANSITIONS.index(name) > position]
            choice = later[0] if later else canonical[0]
        else:
            pool = [name for name in names if name != self._last]
            choice = self._rng.choice(pool)
        self._last = choice
        return choice


class TransitionRun:
    """One running transition: the clock and what to draw. No GTK; the window gives it the frame
    clock's time at every tick and asks it for the pictures to paint.

    Its end is a moment (``seconds`` after its clock started), not a number of frames, so a slow
    machine draws fewer frames and not a longer transition. The first tick only shows the first
    frame (``first_frame``: the old picture, the new texture uploaded out of sight) and starts no
    clock; the clock starts at the second tick, so the upload is not counted in the time.
    *fade_share* is for Ken Burns (see ``compose``)."""

    def __init__(self, name: str, seconds: float, fade_share: float = 1.0) -> None:
        self.name = name
        self.seconds = float(seconds)
        self.fade_share = fade_share
        self.progress = 0.0
        self._ticks = 0
        self._t0: Optional[int] = None

    @property
    def first(self) -> bool:
        """True until the clock has started (the first frame is the one to draw)."""
        return self._t0 is None

    def tick(self, now_microseconds: int) -> bool:
        """The frame clock's time at this tick. True while the transition goes on, False when it
        is over (the window then draws the new picture plainly)."""
        self._ticks += 1
        if self._ticks == 1:
            return True
        if self._t0 is None:
            self._t0 = now_microseconds
        self.progress = (now_microseconds - self._t0) / 1_000_000.0 / self.seconds
        if self.progress >= 1.0:
            self.progress = 1.0
            return False
        self.progress = max(0.0, self.progress)
        return True

    def draws(self, width: float, height: float) -> List[Draw]:
        if self.first:
            return first_frame(self.name, width, height, self.fade_share)
        return compose(self.name, self.progress, width, height, self.fade_share)
