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

A picture is never still while it is shown: ``base_pose`` is its slow move (the Ken Burns move for
a picture that fills the window, a gentle zoom for one that does not), measured from the moment it
appears and carried by the picture through every transition, as the outgoing picture and as the
incoming one. A ``Draw`` carries it as ``pose``; the window applies it inside the picture's own
place, so every transition cuts, slides and fades that moving picture as it would a still one.

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
    INTERVAL_SHARE,
    KEN_BURNS,
    MAX_DURATION,
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

#: The slow move of a picture that fills the window (Ken Burns): it appears enlarged by this much
#: and, over the time it is shown, shrinks towards its own size, shifted to the left by
#: ``KEN_BURNS_DRIFT`` of the window's width and drifting back to the middle. The enlarged picture
#: must cover the window at every moment (no black edge shows): half of what it is larger by is at
#: least what it is shifted by, ``KEN_BURNS_DRIFT <= KEN_BURNS_ZOOM / 2`` (both shrink with the
#: same factor, so this is enough).
KEN_BURNS_ZOOM = 0.14
KEN_BURNS_DRIFT = 0.035

#: The slow move of a picture that does not fill the window: it grows by this much over the time it
#: is shown, around the middle of the window, without a shift (a shift would show its border move).
SMALL_ZOOM = 0.04

#: The slow move is redrawn this many times a second at most (the transitions run at the frame
#: clock's rate): a drift of a few pixels over many seconds needs no more, and a picture that is
#: always moving is drawn all the time, which the processor and the battery pay for.
MOTION_FPS = 30

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


class Pose(NamedTuple):
    """Where a picture stands inside its own place because of its slow move: enlarged by ``scale``
    around the middle of the window and shifted by ``dx`` window pixels (applied after the scale).
    The transition then moves, turns and cuts the result."""

    scale: float = 1.0
    dx: float = 0.0


STILL = Pose()


class Soft(NamedTuple):
    """A soft edge: the picture goes from nothing at the cut edge to whole *width* pixels inside it
    (linearly), instead of stopping at a line. *kind* says which edge (``SOFT_CLIP``,
    ``SOFT_PICTURE``, ``SOFT_CIRCLE``); *sides* which of its sides are soft, any of ``l`` ``t``
    ``r`` ``b`` (not used by the circle, whose rim is the one edge)."""

    kind: str
    width: float
    sides: str = ""


class Settle(NamedTuple):
    """How far the outgoing picture's visible area has been drawn in towards the area the incoming
    picture ends in: *share* 0 is the outgoing picture's own area, 1 is the incoming picture's
    (with its slow move *to*, which ``with_poses`` fills in). Without it a picture that does not
    fill the window, moving on under the incoming one, would stick out of it at the end of the
    transition and vanish with the plain frame that follows."""

    share: float
    to: Pose = STILL


class Draw(NamedTuple):
    """One picture to paint. ``scale``, ``angle`` (degrees, clockwise) and the translation
    ``dx, dy`` (window pixels, applied after them) work around the centre of the window; ``clip``
    and ``circle`` (centre x, centre y, radius) cut the result; ``soft`` makes one of the cuts, or
    the picture's own edge, a gradient; ``blur`` is a radius in pixels; ``pose`` is the picture's
    slow move inside its own place, done before everything else; ``settle`` (the outgoing picture
    only) cuts it to an area that goes from its own to the incoming picture's."""

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
    pose: Pose = STILL
    settle: Optional[Settle] = None


def ease(progress: float) -> float:
    """Smoothstep of *progress* clamped to 0..1: slow start and end, 0 and 1 stay where they are."""
    p = min(1.0, max(0.0, float(progress)))
    return p * p * (3.0 - 2.0 * p)


def picture_seconds(interval: float) -> float:
    """How long the longest-living picture of a slideshow with *interval* seconds per picture is on
    screen: it appears, is shown for *interval* and stays under the next transition, which is at
    most ``MAX_DURATION`` and ``INTERVAL_SHARE`` of the interval."""
    interval = max(0.0, float(interval))
    return interval + min(MAX_DURATION, INTERVAL_SHARE * interval)


def base_pose(age: float, span: float, width: float, fills: bool) -> Pose:
    """The slow move of a picture *age* seconds after it appeared, if it lives for *span* seconds
    (``picture_seconds``): one move at a constant pace over the whole of it, so that it is never
    still, and it has no end point it could rest in. A picture that fills the window (*fills*) is
    the Ken Burns one; a picture that does not only grows, around the middle."""
    left = 1.0 - min(1.0, max(0.0, float(age) / span)) if span > 0 else 1.0
    if fills:
        return Pose(1.0 + KEN_BURNS_ZOOM * left, -float(width) * KEN_BURNS_DRIFT * left)
    return Pose(1.0 + SMALL_ZOOM * (1.0 - left))


def fills_window(frame_size: Tuple[int, int], window_size: Tuple[int, int]) -> bool:
    """True if a picture of *frame_size* covers a window of *window_size*."""
    return frame_size[0] >= window_size[0] and frame_size[1] >= window_size[1]


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


def compose(name: str, progress: float, width: float, height: float) -> List[Draw]:
    """The pictures to paint for transition *name* at *progress* (0 to 1, clamped) in a window of
    *width* x *height* pixels, bottom first. An empty list for a name that is not one of the ten.

    The slow move of the pictures is not in it (``Draw.pose`` is ``STILL``): ``with_poses`` adds it.
    Ken Burns is the cross fade: its move is the move every picture has."""
    p = min(1.0, max(0.0, float(progress)))
    e = ease(p)
    w, h = float(width), float(height)
    window: Rect = (0.0, 0.0, w, h)
    soft = soft_width(w, h)
    settle = Settle(e)  # the outgoing picture that stays under the incoming one to the end

    if name in (CROSSFADE, KEN_BURNS):
        return [Draw(OLD, settle=settle), Draw(NEW, opacity=e)]
    if name == FADE_BLACK:
        if e < 0.5:
            return [Draw(OLD, opacity=1.0 - 2.0 * e)]
        return [Draw(NEW, opacity=2.0 * e - 1.0)]
    if name == SLIDE_IN:
        dx = w * (1.0 - e)
        edge = min(soft, dx) if dx < w else 0.0  # narrower as the edge nears the window's border
        return [
            Draw(OLD, settle=settle),
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
    if name == ZOOM:
        edge = soft * (1.0 - e)
        return [
            Draw(OLD, scale=1.0 + ZOOM_OUT * e, settle=settle),
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
            return [Draw(OLD, settle=settle), Draw(NEW)]
        return [
            Draw(OLD, settle=settle),
            Draw(NEW, clip=(0.0, 0.0, (w + soft) * e, h), soft=Soft(SOFT_CLIP, soft, "r")),
        ]
    if name == CIRCLE:
        if e >= 1.0:
            return [Draw(OLD, settle=settle), Draw(NEW)]
        radius = (math.hypot(w, h) / 2.0 + soft) * e  # the same: the band ends past the corners
        return [
            Draw(OLD, settle=settle),
            Draw(NEW, circle=(w / 2.0, h / 2.0, radius), soft=Soft(SOFT_CIRCLE, soft)),
        ]
    if name == BLUR:
        radius = BLUR_RADIUS * math.sin(math.pi * e)
        radius = radius if radius > 1e-9 else 0.0
        return [Draw(OLD, blur=radius)] if e < 0.5 else [Draw(NEW, blur=radius)]
    if name == ROTATE:
        edge = soft * (1.0 - e)
        return [
            Draw(OLD, settle=settle),
            Draw(
                NEW,
                opacity=e,
                angle=-ROTATE_DEGREES * (1.0 - e),
                scale=1.0 + ROTATE_SCALE * (1.0 - e),
                soft=Soft(SOFT_PICTURE, edge, "ltrb") if edge > 0 else None,
            ),
        ]
    return []


def with_poses(draws: Sequence[Draw], old: Pose = STILL, new: Pose = STILL) -> List[Draw]:
    """*draws* with the slow move of the outgoing picture (*old*) and the incoming one (*new*)."""
    posed = []
    for draw in draws:
        if draw.layer == OLD:
            settle = None if draw.settle is None else draw.settle._replace(to=new)
            draw = draw._replace(pose=old, settle=settle)
        else:
            draw = draw._replace(pose=new)
        posed.append(draw)
    return posed


def first_frame(name: str, width: float, height: float) -> List[Draw]:
    """The frame drawn before the transition's clock starts: ``compose`` at 0 with the new picture
    added at a single pixel and almost no opacity, so that it is drawn, hence uploaded, here."""
    draws = compose(name, 0.0, width, height)
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
    pixels in a window of *window_size*: Ken Burns, which is the cross fade of pictures that move,
    stays a cross fade for a picture that does not fill the window or scrolls; the blur is too
    heavy for software rendering and becomes the cross fade too."""
    if name == KEN_BURNS:
        if not fills_window(frame_size, window_size) or tuple(pan_range) != (0, 0):
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
    clock; the clock starts at the second tick, so the upload is not counted in the time."""

    def __init__(self, name: str, seconds: float) -> None:
        self.name = name
        self.seconds = float(seconds)
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

    def draws(
        self, width: float, height: float, old: Pose = STILL, new: Pose = STILL
    ) -> List[Draw]:
        """The pictures to paint now, the outgoing one with the slow move *old*, the incoming one
        with *new* (see ``with_poses``)."""
        if self.first:
            return with_poses(first_frame(self.name, width, height), old, new)
        return with_poses(compose(self.name, self.progress, width, height), old, new)
