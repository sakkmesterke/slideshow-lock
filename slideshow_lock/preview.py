"""Slideshow preview controller (CORE-2): one fullscreen window per monitor.

The controller decides *what* is shown *when*. It draws nothing, imports no GTK and has
no lock, session or D-Bus call of any kind: a preview never locks (D11), by construction
and not by a runtime check, and the tests prove it (``tests/test_preview.py``). The
windows, the scaler, the clock and the worker thread are handed in, so the whole control
flow runs under test without a display.

How it works:

* The same picture is shown on every monitor, switched together. Each monitor gets a
  frame scaled to its own pixel size.
* The next picture is chosen with ``source.advance()`` and decoded and scaled on a worker
  thread while the current one is on screen, so the cost is hidden (MEAS-1, section 7).
  The GLib main loop never decodes or scales anything: the part of the service that must
  lock the session before sleep does not wait for picture I/O (the CORE-1 safety
  condition rests on this).
* The slide interval counts from the moment a picture appears. If the next picture is not
  ready when the interval ends, it is shown the moment it is.
* A picture that cannot be shown (``ImageSkipped``) is logged and skipped at once, to the
  next one. If as many pictures in a row fail as the queue holds, the controller stops
  trying until the next interval and keeps what is on screen (or the empty-state message
  if nothing was shown yet). No tight loop, no crash.
* An empty source (``current() is None``) is a defined state: the windows show a short
  message, one line is logged, and the preview carries on by itself when a picture turns up.
* Any input on any window ends the preview. Nothing else does.
* Live settings: the interval, ``scaling`` and ``pan-portrait-images`` take effect without a
  restart (the folder and the order are handled by ``source_from_settings``).

The source cursor runs one picture ahead of the screen because of the prefetch. A picture
that is shown stays on screen even if its file is deleted meanwhile (the pixels are in
memory). A single picture is decoded once and reused for every cycle.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from slideshow_lock import _
from slideshow_lock.image_source import _BurstLog
from slideshow_lock.scaling import Frame, ImageSkipped

_LOG = logging.getLogger(__name__)

TRIGGER_PREVIEW = "preview"

#: The pan animation takes this share of the slide interval; the rest the picture rests.
PAN_FRACTION = 0.9

#: If a window has not reported its size after this long, the preview starts without it.
SIZE_WAIT_SECONDS = 2.0

#: Window input kinds, for the log only.
INPUT_MOTION = "motion"
INPUT_BUTTON = "button"
INPUT_KEY = "key"
INPUT_SCROLL = "scroll"

_JOB_SHOW = "show"  # nothing is on screen (or it must be redone): show as soon as ready
_JOB_REFRESH = "refresh"  # redo what is on screen after a settings or size change
_JOB_NEXT = "next"  # prefetch the picture that follows

Cancel = Callable[[], None]
_WorkItem = Tuple[Callable[[], Any], Callable[[Any, Any], None]]
WindowFactory = Callable[[], Sequence[Any]]


class _Job:
    __slots__ = ("path", "purpose", "order", "sizes")

    def __init__(self, path: str, purpose: str, order: List[int], sizes: List[Tuple[int, int]]):
        self.path = path
        self.purpose = purpose
        self.order = order  # window indexes, parallel to sizes
        self.sizes = sizes


class PreviewController:
    """Runs one preview at a time; ``start()`` may be called again after it stopped.

    *source* is an ``ImageSource``; *settings* a ``Settings``; *open_windows* returns
    one window per monitor (see ``slideshow_lock.preview_window``); *scaler* has
    ``prepare(path, sizes, mode, pan) -> [Frame]``; *clock* has ``now()`` and
    ``call_later(seconds, fn) -> cancel``; *worker* has ``submit(fn, done)`` where
    ``done(result, error)`` runs on the main loop.

    A window has ``device_size()``, ``show_frame(frame, pan_seconds)``,
    ``show_message(text)``, ``connect_input(cb)``, ``connect_size_changed(cb)`` and
    ``close()``.
    """

    def __init__(
        self,
        source,
        settings,
        open_windows: WindowFactory,
        scaler,
        *,
        clock,
        worker,
    ) -> None:
        self._source = source
        self._settings = settings
        self._open_windows = open_windows
        self._scaler = scaler
        self._clock = clock
        self._worker = worker

        self._running = False
        self._windows: List[Any] = []
        self._stopped_listeners: List[Callable[[str], None]] = []
        self._skip_log = _BurstLog("pictures that could not be shown")

        source.connect_current_changed(self._on_source_changed)
        settings.connect_changed(self._on_settings_changed)
        self._reset_state()

    # -- public API ----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def shown_path(self) -> Optional[str]:
        """The picture on screen now, or None (nothing shown yet, or the empty state)."""
        return self._shown_path

    def connect_stopped(self, callback: Callable[[str], None]) -> None:
        """Call *callback(reason)* when a running preview stops (``"input"`` or ``"requested"``)."""
        self._stopped_listeners.append(callback)

    def start(self) -> None:
        """Open one window per monitor and begin. The source must be started by the caller."""
        if self._running:
            return
        self._reset_state()
        windows = list(self._open_windows())
        if not windows:
            _LOG.warning("[slideshow] preview not started: no monitor found")
            return
        self._running = True
        self._windows = windows
        for index, window in enumerate(windows):
            window.connect_input(lambda kind, i=index: self._on_input(i, kind))
            window.connect_size_changed(lambda i=index: self._on_window_size(i))
        _LOG.info("[slideshow] started (source=%s, monitors=%d)", TRIGGER_PREVIEW, len(windows))
        self._size_wait = self._clock.call_later(SIZE_WAIT_SECONDS, self._on_size_wait_over)
        self._begin()

    def stop(self, reason: str = "requested") -> None:
        """End the preview: cancel everything, close every window. Idempotent. Never locks."""
        if not self._running:
            return
        self._running = False
        self._cancel_timers()
        self._job = None
        windows, self._windows = self._windows, []
        for window in windows:
            try:
                window.close()
            except Exception:
                _LOG.exception("[slideshow] closing a preview window failed")
        self._skip_log.reset()
        _LOG.info("[slideshow] stopped (source=%s, reason=%s)", TRIGGER_PREVIEW, reason)
        for callback in list(self._stopped_listeners):
            try:
                callback(reason)
            except Exception:
                _LOG.exception("[slideshow] stop listener failed")

    # -- state -----------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._shown_path: Optional[str] = None
        self._shown_frames: Dict[int, Frame] = {}
        self._shown_stale = False  # settings or sizes changed since these frames were made
        self._shown_at = 0.0
        self._next_path: Optional[str] = None
        self._next_frames: Optional[Dict[int, Frame]] = None
        self._swap_due = False
        self._want: Optional[Tuple[str, str]] = None
        self._job: Optional[_Job] = None
        self._timer: Optional[Cancel] = None
        self._size_wait: Optional[Cancel] = None
        self._size_wait_over = False
        self._failures = 0
        self._failed_out = False
        self._empty_logged = False

    def _cancel_timers(self) -> None:
        for name in ("_timer", "_size_wait"):
            cancel = getattr(self, name)
            if cancel is not None:
                cancel()
                setattr(self, name, None)

    # -- choosing and preparing pictures -----------------------------------------------

    def _begin(self) -> None:
        """Nothing is on screen: start from the source's current picture."""
        path = self._source.current()
        if path is None:
            self._enter_empty()
            return
        self._empty_logged = False
        self._want = (path, _JOB_SHOW)
        self._dispatch()

    def _enter_empty(self) -> None:
        self._job = None
        self._want = None
        self._next_path = None
        self._next_frames = None
        self._swap_due = False
        self._shown_path = None
        self._shown_frames = {}
        if self._timer is not None:
            self._timer()
            self._timer = None
        for window in self._windows:
            window.show_message(_("No pictures to show"))
        if not self._empty_logged:
            self._empty_logged = True
            complete = getattr(self._source, "scan_complete", True)
            (_LOG.warning if complete else _LOG.info)(
                "[slideshow-dir] no picture to show, the preview waits for one%s",
                "" if complete else " (the folder scan is still running)",
            )

    def _window_sizes(self) -> Dict[int, Tuple[int, int]]:
        sizes = {}
        for index, window in enumerate(self._windows):
            size = window.device_size()
            if size is not None:
                sizes[index] = size
        return sizes

    def _dispatch(self) -> None:
        """Hand the wanted picture to the worker, once the windows know their sizes."""
        if self._want is None or not self._running:
            return
        sizes = self._window_sizes()
        if not sizes or (len(sizes) < len(self._windows) and not self._size_wait_over):
            return  # a size event or the size timeout calls this again
        path, purpose = self._want
        self._want = None
        order = sorted(sizes)
        job = _Job(path, purpose, order, [sizes[i] for i in order])
        self._job = job
        _LOG.debug("[slideshow] preparing %r (%s) for %s", path, purpose, job.sizes)
        mode = self._settings.get_scaling()
        pan = self._settings.get_pan_portrait_images()
        scaler = self._scaler

        def work() -> Optional[List[Frame]]:
            if self._job is not job:  # superseded while it waited in the queue
                return None
            return scaler.prepare(path, job.sizes, mode, pan)

        self._worker.submit(work, lambda result, error: self._on_job_done(job, result, error))

    def _on_job_done(self, job: _Job, result, error) -> None:
        if not self._running or self._job is not job:
            return  # stopped, or replaced by newer wishes
        self._job = None
        try:
            if error is not None:
                if not isinstance(error, ImageSkipped):
                    _LOG.error("[slideshow] unexpected error while preparing a picture: %r", error)
                    error = ImageSkipped(f"{type(error).__name__}: {error}")
                self._on_skipped(job, error)
                return
            frames = dict(zip(job.order, result))
            self._failures = 0
            self._failed_out = False
            if job.purpose == _JOB_NEXT:
                self._next_frames = frames
                if self._swap_due:
                    self._swap()
            else:
                self._display(job.path, frames, fresh=job.purpose == _JOB_SHOW)
        except Exception:
            _LOG.exception("[slideshow] preview step failed")

    def _on_skipped(self, job: _Job, error: ImageSkipped) -> None:
        self._skip_log.warn("[slideshow-dir] skipping %r: %s", job.path, error)
        self._failures += 1
        if self._failures >= max(1, len(self._source)):
            if not self._failed_out:
                self._failed_out = True
                _LOG.warning(
                    "[slideshow-dir] none of the %d pictures could be shown, trying again "
                    "at the next interval",
                    max(1, len(self._source)),
                )
            if job.purpose == _JOB_NEXT:
                self._next_path = None
            elif self._shown_path is None:
                for window in self._windows:
                    window.show_message(_("No pictures to show"))
            if self._timer is None:
                self._arm_timer(self._interval())
            return
        following = self._source.advance()
        if following is None:
            return  # the source emptied; it reports that itself
        if job.purpose == _JOB_NEXT:
            self._next_path = following
        self._want = (following, job.purpose)
        self._dispatch()

    # -- on screen ----------------------------------------------------------------------

    def _display(self, path: str, frames: Dict[int, Frame], *, fresh: bool) -> None:
        interval = self._interval()
        _LOG.debug(
            "[slideshow] showing %r (%s)",
            path,
            ", ".join(f"{f.width}x{f.height} {f.method}" for f in frames.values()),
        )
        for index, window in enumerate(self._windows):
            frame = frames.get(index)
            if frame is None:
                window.show_message("")
            else:
                window.show_frame(frame, interval * PAN_FRACTION)
        self._shown_path = path
        self._shown_frames = frames
        self._shown_stale = False
        if fresh:
            self._shown_at = self._clock.now()
            self._arm_timer(interval)
            self._prefetch()
        elif self._next_path is not None:  # refresh: the timer keeps running
            self._want = (self._next_path, _JOB_NEXT)
            self._dispatch()
        if any(
            i not in frames and w.device_size() is not None for i, w in enumerate(self._windows)
        ):
            self._invalidate_and_rerender()  # a window reported its size after the job started

    def _prefetch(self) -> None:
        """Pick the picture that follows the one on screen and get it ready."""
        path = self._source.advance()
        self._next_frames = None
        self._next_path = path
        if path is None:
            return  # empty; the source notifies the controller itself
        if path == self._shown_path and not self._shown_stale and self._shown_frames:
            self._next_frames = self._shown_frames  # a single picture is not decoded again
            return
        self._want = (path, _JOB_NEXT)
        self._dispatch()

    def _swap(self) -> None:
        path, frames = self._next_path, self._next_frames
        self._next_path = None
        self._next_frames = None
        self._swap_due = False
        if path is not None and frames is not None:
            self._display(path, frames, fresh=True)

    # -- timer ---------------------------------------------------------------------------

    def _interval(self) -> float:
        return float(self._settings.get_slide_interval_seconds())

    def _arm_timer(self, delay: float) -> None:
        if self._timer is not None:
            self._timer()
        self._timer = self._clock.call_later(delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if not self._running:
            return
        try:
            if self._shown_path is None:  # nothing was shown: try again from the source
                self._failures = 0
                self._failed_out = False
                self._begin()
            elif self._next_frames is not None:
                self._swap()
            elif self._next_path is None:  # the last prefetch failed out, or the queue was empty
                self._failures = 0
                self._failed_out = False
                self._prefetch()
                if self._next_frames is not None:
                    self._swap()
                elif self._timer is None:
                    self._arm_timer(self._interval())
            else:
                self._swap_due = True  # still being prepared: show it the moment it is ready
        except Exception:
            _LOG.exception("[slideshow] preview step failed")

    def _on_size_wait_over(self) -> None:
        self._size_wait = None
        self._size_wait_over = True
        self._dispatch()

    # -- events ----------------------------------------------------------------------------

    def _on_input(self, index: int, kind: str) -> None:
        if self._running:
            _LOG.debug("[slideshow] input (%s) on monitor %d", kind, index)
            self.stop("input")

    def _on_window_size(self, index: int) -> None:
        if not self._running:
            return
        try:
            if self._shown_path is not None:
                self._invalidate_and_rerender()
            else:
                self._dispatch()
        except Exception:
            _LOG.exception("[slideshow] preview step failed")

    def _on_source_changed(self, path: Optional[str]) -> None:
        if not self._running:
            return
        try:
            if path is None:
                self._enter_empty()
            elif self._shown_path is None:
                # empty, failed out, or the first picture was replaced: start again from here
                self._job = None
                self._failures = 0
                self._failed_out = False
                self._begin()
            elif path != self._next_path:
                # the picture being prepared was deleted and the cursor moved on
                if self._job is not None and self._job.purpose == _JOB_NEXT:
                    self._job = None
                self._next_path = path
                self._next_frames = None
                self._want = (path, _JOB_NEXT)
                self._dispatch()
        except Exception:
            _LOG.exception("[slideshow] preview step failed")

    def _on_settings_changed(self, key: str) -> None:
        if not self._running:
            return
        from slideshow_lock.settings import (
            KEY_PAN_PORTRAIT_IMAGES,
            KEY_SCALING,
            KEY_SLIDE_INTERVAL_SECONDS,
        )

        try:
            if key == KEY_SLIDE_INTERVAL_SECONDS:
                if self._timer is not None:
                    left = self._interval() - (self._clock.now() - self._shown_at)
                    self._arm_timer(max(0.0, left))
            elif key in (KEY_SCALING, KEY_PAN_PORTRAIT_IMAGES) and self._shown_path is not None:
                self._invalidate_and_rerender()
        except Exception:
            _LOG.exception("[slideshow] preview step failed")

    def _invalidate_and_rerender(self) -> None:
        """Settings or a window size changed: redo the shown picture and the prefetched one."""
        self._shown_stale = True
        self._next_frames = None
        self._swap_due = False
        self._job = None
        self._want = (self._shown_path, _JOB_REFRESH)
        self._dispatch()


# -- default backends (GLib, imported lazily) -------------------------------------------------


class GLibClock:
    """``now()`` and one-shot timers on the GLib main loop."""

    @staticmethod
    def now() -> float:
        import time

        return time.monotonic()

    @staticmethod
    def call_later(delay: float, callback: Callable[[], None]) -> Cancel:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        state = {"id": 0}

        def run() -> bool:
            state["id"] = 0
            callback()
            return GLib.SOURCE_REMOVE

        state["id"] = GLib.timeout_add(max(0, int(delay * 1000)), run)

        def cancel() -> None:
            if state["id"]:
                GLib.source_remove(state["id"])
                state["id"] = 0

        return cancel


class ThreadWorker:
    """One daemon thread that runs jobs in order; each result is delivered on the main loop.

    A daemon thread, so a picture on a hung mount (the only thing that can block it) can
    never keep the process from exiting. Jobs that were superseded return immediately.
    """

    def __init__(self) -> None:
        self._jobs: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def submit(self, fn: Callable[[], Any], done: Callable[[Any, Any], None]) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="slideshow-preview-worker", daemon=True
            )
            self._thread.start()
        self._jobs.put((fn, done))

    def close(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, done = item
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(self._deliver, done, result, error)

    @staticmethod
    def _deliver(done, result, error) -> bool:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        done(result, error)
        return GLib.SOURCE_REMOVE
