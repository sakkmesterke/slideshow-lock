"""What each of the ten transitions looks like at a given moment, and which one comes next. No GTK.

``compose(name, progress, width, height)`` answers with a list of ``Draw``: the pictures to paint,
from the bottom up, on top of a black window. The preview window only turns each ``Draw`` into
``Gtk.Snapshot`` calls (clip, opacity, blur, translate/rotate/scale around the window's centre),
so the shape of every transition is decided and tested here, without a display. The cuts are hard
edges, as they were in 1.0.1.

Progress runs from 0 (the old picture alone) to 1 (the new one alone) and is eased here, so the
window passes the plain fraction of the time that has gone. The new picture is always painted over
the old one, never beside it in the same layer, so it never makes the old one less opaque than the
pixels below it would allow: a transition cannot dip towards black unless it is the "fade-black".

Only the Ken Burns picture moves. ``base_pose`` is its slow move (a zoom and a drift), measured from
the moment the picture appears, with the Ken Burns transition (or, on a window that had nothing on
screen, as the first picture, when Ken Burns is the choice for it and no transition is drawn), and
carried by the picture through the whole time it is on screen: as the incoming picture of that
transition, while it is shown, and as the outgoing picture of whichever transition takes it away,
so it does not stop before the next picture comes. A ``Draw`` carries it as ``pose``; the window
applies it inside the picture's own place, so every transition cuts, slides and fades that moving
picture as it would a still one. Every other picture stands still (it is drawn once, 1:1), as in
1.0.1.

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

#: The slow move of a Ken Burns picture: it appears enlarged by this much and, over the time it is
#: on screen, shrinks towards its own size, shifted to the left by ``KEN_BURNS_DRIFT`` of the
#: window's width and drifting back to the middle. The enlarged picture must cover the window at
#: every moment (no black edge shows): half of what it is larger by is at least what it is shifted
#: by, ``KEN_BURNS_DRIFT <= KEN_BURNS_ZOOM / 2`` (both shrink with the same factor, so this is
#: enough).
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

Rect = Tuple[float, float, float, float]  # x, y, width, height in window pixels


class Pose(NamedTuple):
    """Where a picture stands inside its own place because of its slow move: enlarged by ``scale``
    around the middle of the window and shifted by ``dx`` window pixels (applied after the scale).
    The transition then moves, turns and cuts the result."""

    scale: float = 1.0
    dx: float = 0.0


STILL = Pose()


class Draw(NamedTuple):
    """One picture to paint. ``scale``, ``angle`` (degrees, clockwise) and the translation
    ``dx, dy`` (window pixels, applied after them) work around the centre of the window; ``clip``
    and ``circle`` (centre x, centre y, radius) cut the result; ``blur`` is a radius in pixels;
    ``pose`` is the picture's slow move inside its own place, done before everything else."""

    layer: str
    opacity: float = 1.0
    scale: float = 1.0
    angle: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    clip: Optional[Rect] = None
    circle: Optional[Tuple[float, float, float]] = None
    blur: float = 0.0
    pose: Pose = STILL


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


def base_pose(age: float, span: float, width: float) -> Pose:
    """The slow move of a Ken Burns picture *age* seconds after it appeared, if it lives for
    *span* seconds (``picture_seconds``): one move at a constant pace over the whole of it, so that
    it does not stop before the next picture comes."""
    left = 1.0 - min(1.0, max(0.0, float(age) / span)) if span > 0 else 1.0
    return Pose(1.0 + KEN_BURNS_ZOOM * left, -float(width) * KEN_BURNS_DRIFT * left)


def fills_window(frame_size: Tuple[int, int], window_size: Tuple[int, int]) -> bool:
    """True if a picture of *frame_size* covers a window of *window_size*."""
    return frame_size[0] >= window_size[0] and frame_size[1] >= window_size[1]


def compose(name: str, progress: float, width: float, height: float) -> List[Draw]:
    """The pictures to paint for transition *name* at *progress* (0 to 1, clamped) in a window of
    *width* x *height* pixels, bottom first. An empty list for a name that is not one of the ten.

    The slow move of the Ken Burns picture is not in it (``Draw.pose`` is ``STILL``):
    ``with_poses`` adds it. Ken Burns is the cross fade: its move is the move its pictures have."""
    p = min(1.0, max(0.0, float(progress)))
    e = ease(p)
    w, h = float(width), float(height)
    window: Rect = (0.0, 0.0, w, h)

    if name in (CROSSFADE, KEN_BURNS):
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


def with_poses(draws: Sequence[Draw], old: Pose = STILL, new: Pose = STILL) -> List[Draw]:
    """*draws* with the slow move of the outgoing picture (*old*) and the incoming one (*new*)."""
    return [draw._replace(pose=old if draw.layer == OLD else new) for draw in draws]


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
    itself (a virtual machine without a GPU) is not recognised here. Only the blur uses this: on
    software rendering it is drawn as a cross fade."""
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

    def peek(self, names: Sequence, order: str) -> Optional[str]:
        """The choice ``next`` would make, without remembering it: the picture it is made for
        comes in over nothing, so it must not use up a place of the ``sequence`` order or change
        what the first real change may not repeat."""
        last = self._last
        try:
            return self.next(names, order)
        finally:
            self._last = last


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
