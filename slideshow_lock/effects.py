"""Whether the slideshow may draw its effects. No GTK.

The effects are the soft edges of the transitions, the slow move every picture has and the stronger
Ken Burns zoom (``transition_draw.compose``). They cost drawing time that a GPU has and a CPU
renderer does not, so they are drawn only on a machine that is *known* to draw with a GPU; on every
other, and on one that cannot be told, the plain drawing is used (``transition_draw.compose_plain``,
what 1.0.1 drew).

The user has a switch as well (``hardware-acceleration`` in the settings): off, the plain drawing is
used whatever the machine is. It can only take the effects away too; on a machine that is not known
to have a GPU the switch changes nothing (the settings window greys it out and shows it off).

Two layers decide, the second can only take the effects away:

1. ``decide``: from the name of GTK's renderer class and the OpenGL renderer string (what Mesa or
   the vendor driver calls itself: ``llvmpipe`` is Mesa drawing with the CPU, which a machine
   without a GPU, or with a broken driver, falls back to by itself and which GTK does not report).
   Anything that is not positively a GPU - Cairo, a software or a virtual GPU, a string that could
   not be read - is a no.
2. ``Effects.frame``: the drawing time itself. The interval between the frame clock's ticks, while
   the effects are drawn, is measured in windows of ``GUARD_FRAMES``; if the median of a window is
   over the budget (``frame_budget_ms``) the effects are taken away for the rest of the process.

``Effects`` keeps both and the switch; the window asks it (``full``) before every picture and hands
it the frame clock's time (``frame``). The switch is read once per picture (``apply_switch``), so a
change applies from the next one and the move of the picture on screen is not cut short. Everything
it decides and measures is logged.
"""

from __future__ import annotations

import logging
import statistics
from typing import Callable, List, Mapping, NamedTuple, Optional, Tuple

from slideshow_lock.transition_draw import software_renderer

_LOG = logging.getLogger(__name__)

#: GTK's renderer classes that draw with a GPU (through OpenGL or Vulkan), by lower case type name:
#: ``GskGLRenderer`` (GTK 4.0 to 4.12), ``GskNglRenderer`` (4.14 on), ``GskVulkanRenderer``.
GPU_RENDERERS = ("gskglrenderer", "gsknglrenderer", "gskvulkanrenderer")

#: Parts of a GL renderer string (lower case) that say the CPU draws: Mesa's software rasterisers
#: and the software Vulkan/GL implementations.
SOFTWARE_MARKERS = ("llvmpipe", "softpipe", "swrast", "lavapipe", "swiftshader", "software")

#: Parts of a GL renderer string (lower case) of a virtual GPU. What it does is up to the host, and
#: it is often the CPU there too, so it is not taken for a GPU.
VIRTUAL_MARKERS = (
    "svga3d",
    "vmware",
    "virgl",
    "virtio",
    "vbox",
    "virtualbox",
    "qxl",
    "bochs",
    "cirrus",
    "parallels",
)

#: The interval between two frames, in milliseconds, that the median of a window of frames may not
#: pass while the effects are drawn (25 ms is 40 frames a second; a display that keeps up shows
#: 16.7 ms at 60 Hz). PROVISIONAL: the value is to come from measurements on a CPU renderer and a
#: GPU; ``FRAME_BUDGET_ENV`` changes it without a new build.
FRAME_BUDGET_MS = 25.0
FRAME_BUDGET_ENV = "SLIDESHOW_FRAME_BUDGET_MS"

#: The guard judges a window of this many frame intervals at a time.
GUARD_FRAMES = 20

#: An interval longer than this (milliseconds) is a pause, not slow drawing (the display was off,
#: the window hidden): it is not counted and starts the window again.
GUARD_GAP_MS = 1000.0


class Decision(NamedTuple):
    """The answer of ``decide``: *full* (draw the effects) and the *reason*, for the log."""

    full: bool
    reason: str


def renderer_is_gpu(renderer_class: str) -> bool:
    """True if *renderer_class* (``GskNglRenderer``, ``GskVulkanRenderer``, ...) is one that draws
    with a GPU. The Cairo renderer, a name that is not known and an empty one are not."""
    return (renderer_class or "").strip().lower() in GPU_RENDERERS


def decide(
    renderer_class: Optional[str],
    gl_renderer: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> Decision:
    """Whether the effects may be drawn, from GTK's renderer class *renderer_class*, the OpenGL
    renderer string *gl_renderer* (None if it could not be read) and the environment *environ*.
    True only for a GPU renderer class with a string that names neither software nor a virtual GPU,
    and with nothing in the environment that asks for software drawing."""
    renderer_class = (renderer_class or "").strip()
    gl_renderer = (gl_renderer or "").strip()
    if not renderer_class:
        return Decision(False, "the renderer is not known")
    if software_renderer("", environ):
        return Decision(False, "the environment asks for software drawing")
    if software_renderer(renderer_class):
        return Decision(False, "%s draws with the CPU" % renderer_class)
    if not renderer_is_gpu(renderer_class):
        return Decision(False, "%s is not known to draw with a GPU" % renderer_class)
    if not gl_renderer:
        return Decision(False, "the OpenGL renderer string could not be read")
    lowered = gl_renderer.lower()
    for marker in SOFTWARE_MARKERS:
        if marker in lowered:
            return Decision(False, "OpenGL renderer %r draws with the CPU" % gl_renderer)
    for marker in VIRTUAL_MARKERS:
        if marker in lowered:
            return Decision(False, "OpenGL renderer %r is a virtual GPU" % gl_renderer)
    return Decision(True, "%s on OpenGL renderer %r" % (renderer_class, gl_renderer))


def frame_budget_ms(environ: Optional[Mapping[str, str]] = None) -> float:
    """The budget for the median frame interval: ``FRAME_BUDGET_ENV`` if it holds a positive number,
    else ``FRAME_BUDGET_MS`` (a value that is not a positive number is logged and ignored)."""
    text = (environ or {}).get(FRAME_BUDGET_ENV, "")
    if not text.strip():
        return FRAME_BUDGET_MS
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not value > 0.0 or value != value or value == float("inf"):
        _LOG.warning(
            "[effects] %s=%r is not a positive number; using %s",
            FRAME_BUDGET_ENV,
            text,
            FRAME_BUDGET_MS,
        )
        return FRAME_BUDGET_MS
    return value


class FrameTimer:
    """The median interval, in milliseconds, of every window of *frames* frame intervals.

    ``add`` gets the frame clock's time (microseconds) of a frame. Two callbacks of the same frame
    (the clock gives both the same time) count once; a gap of *gap_ms* or more is a pause: it is not
    an interval and the window starts again."""

    def __init__(self, frames: int = GUARD_FRAMES, gap_ms: float = GUARD_GAP_MS) -> None:
        self._frames = max(1, int(frames))
        self._gap = float(gap_ms) * 1000.0
        self._last: Optional[int] = None
        self._intervals: List[float] = []

    def add(self, now_microseconds: int) -> Optional[float]:
        """The median of the window this frame completes, in milliseconds; None otherwise."""
        last, self._last = self._last, now_microseconds
        if last is None or now_microseconds == last:  # the first frame, or the same one again
            self._last = now_microseconds if last is None else last
            return None
        if now_microseconds < last:  # the clock went back: start again
            self._intervals = []
            return None
        interval = now_microseconds - last
        if interval >= self._gap:
            self._intervals = []
            return None
        self._intervals.append(interval / 1000.0)
        if len(self._intervals) < self._frames:
            return None
        median = statistics.median(self._intervals)
        self._intervals = []
        return median


Reading = Tuple[str, Optional[str]]  # GTK's renderer class, the OpenGL renderer string


class Effects:
    """Whether the effects are drawn, for the life of the process: the decision from the renderer
    (made at the first ``full`` that can read it) and the guard of the drawing time.

    *environ* is the environment ``decide`` reads; *budget_ms* the budget of the guard."""

    def __init__(
        self,
        environ: Optional[Mapping[str, str]] = None,
        budget_ms: Optional[float] = None,
        frames: int = GUARD_FRAMES,
    ) -> None:
        self._environ = environ if environ is not None else {}
        self._budget = budget_ms if budget_ms is not None else frame_budget_ms(self._environ)
        self._timer = FrameTimer(frames)
        self._decision: Optional[Decision] = None
        self._tripped: Optional[str] = None
        self._switch: Callable[[], bool] = lambda: True
        self._wanted = True

    @property
    def budget_ms(self) -> float:
        return self._budget

    @property
    def decision(self) -> Optional[Decision]:
        """What ``decide`` said, None until the renderer could be read."""
        return self._decision

    @property
    def tripped(self) -> Optional[str]:
        """Why the guard took the effects away, None while it has not."""
        return self._tripped

    def follow(self, switch: Callable[[], bool]) -> None:
        """Take the user's switch from *switch* (``Settings.get_hardware_acceleration``) from now
        on, and read it at once."""
        self._switch = switch
        self.apply_switch()

    def apply_switch(self) -> bool:
        """Read the user's switch (once per picture). A switch that cannot be read leaves the last
        value; the switch changing is logged. Returns True while the user wants the effects."""
        try:
            wanted = bool(self._switch())
        except Exception as error:  # noqa: BLE001 - a settings failure must never stop the slideshow
            _LOG.debug("[effects] could not read the hardware acceleration switch: %s", error)
            return self._wanted
        if wanted != self._wanted:
            _LOG.info(
                "[effects] hardware acceleration switched %s in the settings",
                "on" if wanted else "off: plain drawing (as in 1.0.1)",
            )
        self._wanted = wanted
        return wanted

    def _decide(self, read: Callable[[], Optional[Reading]]) -> Optional[Decision]:
        """The decision from the renderer, made at the first call that can read it (None while the
        window has no renderer yet); anything *read* raises is an unknown renderer, which is off."""
        if self._decision is None:
            try:
                reading = read()
            except Exception as error:  # noqa: BLE001 - a failed probe must never stop the slideshow
                _LOG.debug("[effects] could not read the renderer: %s", error)
                reading = ("", None)
            if reading is None:
                return None
            self._decision = decide(reading[0], reading[1], self._environ)
            _LOG.info(
                "[effects] %s: %s",
                "full effects" if self._decision.full else "plain drawing (as in 1.0.1)",
                self._decision.reason,
            )
        return self._decision

    def available(self, read: Callable[[], Optional[Reading]]) -> bool:
        """True if this machine is known to draw with a GPU: what the settings window needs to know
        to offer the switch. The switch itself and the guard of the drawing time are not asked."""
        decision = self._decide(read)
        return decision is not None and decision.full

    def full(self, read: Callable[[], Optional[Reading]]) -> bool:
        """True if the effects may be drawn now: the machine is known to have a GPU, the user's
        switch is on and the guard has not tripped. *read* is asked, until it answers, for
        ``(renderer class, OpenGL renderer string)``; it answers None while the window has no
        renderer yet (the effects are off then, and it is asked again), and anything it raises is
        an unknown renderer, which is off for good."""
        decision = self._decide(read)
        if decision is None:
            return False
        return decision.full and self._wanted and self._tripped is None

    def frame(self, now_microseconds: int) -> None:
        """The frame clock's time of a frame drawn with the effects: the guard of the drawing time.
        Not called, or no effect, while the effects are off."""
        if self._decision is None or not self._decision.full or self._tripped is not None:
            return
        median = self._timer.add(now_microseconds)
        if median is None:
            return
        _LOG.debug("[effects] median frame interval %.1f ms (budget %.1f ms)", median, self._budget)
        if median > self._budget:
            self._tripped = "median frame interval %.1f ms is over the %.1f ms budget" % (
                median,
                self._budget,
            )
            _LOG.warning("[effects] plain drawing from now on (as in 1.0.1): %s", self._tripped)
