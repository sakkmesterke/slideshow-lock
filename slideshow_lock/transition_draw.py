"""What each of the ten transitions looks like at a given moment, and which one comes next. No GTK.

``compose(name, progress, width, height)`` answers with a list of ``Draw``: the pictures to paint,
from the bottom up, on top of a black window. The preview window only turns each ``Draw`` into
``Gtk.Snapshot`` calls (clip, opacity, blur, translate/rotate/scale around the window's centre),
so the shape of every transition is decided and tested here, without a display.

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
#: nothing jumps when the run ends.
KEN_BURNS_ZOOM = 0.08
KEN_BURNS_DRIFT = 0.02

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

Rect = Tuple[float, float, float, float]  # x, y, width, height in window pixels


class Draw(NamedTuple):
    """One picture to paint. ``scale``, ``angle`` (degrees, clockwise) and the translation
    ``dx, dy`` (window pixels, applied after them) work around the centre of the window; ``clip``
    and ``circle`` (centre x, centre y, radius) cut the result; ``blur`` is a radius in pixels."""

    layer: str
    opacity: float = 1.0
    scale: float = 1.0
    angle: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    clip: Optional[Rect] = None
    circle: Optional[Tuple[float, float, float]] = None
    blur: float = 0.0


def ease(progress: float) -> float:
    """Smoothstep of *progress* clamped to 0..1: slow start and end, 0 and 1 stay where they are."""
    p = min(1.0, max(0.0, float(progress)))
    return p * p * (3.0 - 2.0 * p)


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

    if name == CROSSFADE:
        return [Draw(OLD), Draw(NEW, opacity=e)]
    if name == FADE_BLACK:
        if e < 0.5:
            return [Draw(OLD, opacity=1.0 - 2.0 * e)]
        return [Draw(NEW, opacity=2.0 * e - 1.0)]
    if name == SLIDE_IN:
        return [Draw(OLD), Draw(NEW, dx=w * (1.0 - e), clip=window)]
    if name == PUSH:
        return [
            Draw(OLD, dx=-w * e, clip=window),
            Draw(NEW, dx=w * (1.0 - e), clip=window),
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
        return [
            Draw(OLD, scale=1.0 + ZOOM_OUT * e),
            Draw(NEW, opacity=e, scale=(1.0 - ZOOM_IN) + ZOOM_IN * e),
        ]
    if name == WIPE:
        return [Draw(OLD), Draw(NEW, clip=(0.0, 0.0, w * e, h))]
    if name == CIRCLE:
        return [Draw(OLD), Draw(NEW, circle=(w / 2.0, h / 2.0, e * math.hypot(w, h) / 2.0))]
    if name == BLUR:
        radius = BLUR_RADIUS * math.sin(math.pi * e)
        radius = radius if radius > 1e-9 else 0.0
        return [Draw(OLD, blur=radius)] if e < 0.5 else [Draw(NEW, blur=radius)]
    if name == ROTATE:
        return [
            Draw(OLD),
            Draw(
                NEW,
                opacity=e,
                angle=-ROTATE_DEGREES * (1.0 - e),
                scale=1.0 + ROTATE_SCALE * (1.0 - e),
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


def first_picture_name(names: Sequence) -> Optional[str]:
    """The transition for a picture that comes in over nothing (the first one of a preview): Ken
    Burns when it is the only one chosen, None (a plain picture) otherwise. Ken Burns is a slow
    move that goes on while the picture is shown, so it has something to show even with no old
    picture; the other nine are a change *from* one. A list of several is left alone here: the
    chooser has not been asked, so the order of the mix starts, as it always did, with the second
    picture."""
    return KEN_BURNS if clean(names) == [KEN_BURNS] else None


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
