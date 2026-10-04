"""Fake adapters for the protocols of ``slideshow_lock.session`` (CORE-1): every event a bus could
bring is a method call, nothing is connected to a bus.

Callbacks are delivered directly by default (the state machine tests are single threaded). A fake
made with a *deliver* poster (``slideshow_lock.loop.current_poster()`` on the thread that
creates it) hands its callbacks to that thread's loop instead, which is how the sleep guard's own
thread is exercised for real.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from slideshow_lock.session import LockResult


def _direct(fn: Callable[[], None]) -> None:
    fn()


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.t = now

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeIdleWatcher:
    def __init__(self) -> None:
        self.idle_timeout: Optional[float] = None
        self.idle_callback: Optional[Callable[[], None]] = None
        self.active_callbacks: List[Callable[[], None]] = []
        self.idle_registrations: List[float] = []
        self.active_cancels = 0
        self.closed = False

    def on_idle(self, timeout_s, callback) -> None:
        self.idle_timeout = timeout_s
        self.idle_callback = callback
        self.idle_registrations.append(timeout_s)

    def cancel_idle(self) -> None:
        self.idle_callback = None
        self.idle_timeout = None

    def on_user_active(self, callback):
        self.active_callbacks.append(callback)

        def cancel() -> None:
            if callback in self.active_callbacks:
                self.active_callbacks.remove(callback)
                self.active_cancels += 1

        return cancel

    def close(self) -> None:
        self.closed = True
        self.cancel_idle()
        self.active_callbacks.clear()

    # test helpers
    def fire_idle(self) -> None:
        assert self.idle_callback is not None, "no idle watch registered"
        self.idle_callback()

    def fire_user_active(self) -> None:
        callbacks, self.active_callbacks = self.active_callbacks, []  # one-shot, like the monitor
        for callback in callbacks:
            callback()


class FakeInhibition:
    """``inhibited`` is what other applications hold. The machine's own idle inhibitor is
    ``holding``: like the real adapter, it is never part of ``inhibited`` or of the callbacks.
    ``holds`` and ``releases`` count the calls, ``hold_error`` and ``release_error`` make them
    raise."""

    def __init__(self) -> None:
        self.inhibited = False
        self.queries = 0
        self.error: Optional[Exception] = None
        self.holding = False
        self.holds = 0
        self.releases = 0
        self.hold_error: Optional[Exception] = None
        self.release_error: Optional[Exception] = None
        self._callbacks: List[Callable[[bool], None]] = []

    def hold_idle_inhibit(self) -> None:
        self.holds += 1
        if self.hold_error is not None:
            raise self.hold_error
        self.holding = True

    def release_idle_inhibit(self) -> None:
        self.releases += 1
        if self.release_error is not None:
            raise self.release_error
        self.holding = False

    def is_idle_inhibited(self) -> bool:
        self.queries += 1
        if self.error is not None:
            raise self.error
        return self.inhibited

    def on_idle_inhibit_changed(self, callback) -> None:
        self._callbacks.append(callback)

    def set_inhibited(self, value: bool) -> None:
        self.inhibited = value
        for callback in list(self._callbacks):
            callback(value)


class FakeSessionLock:
    """``mode``: ``"auto"`` answers at once with ``result``; ``"manual"`` keeps the callbacks in
    ``pending`` until ``complete()``. ``lock_calls`` counts every lock request.
    ``shows_screen``: a successful answer also makes the session locked (``ActiveChanged`` and
    ``is_active``); False is the answer of a facility with nobody behind it, as the login1
    ``Lock`` signal can be."""

    def __init__(self, deliver: Callable = _direct, shared: Optional[list] = None) -> None:
        self._deliver = deliver
        self.locked = False
        self.mode = "auto"
        self.result = LockResult(True)
        self.shows_screen = True
        self.raises: Optional[Exception] = None
        self.is_active_error: Optional[Exception] = None
        self.calls = shared if shared is not None else []
        self.pending: List[Callable[[LockResult], None]] = []
        self._callbacks: List[Callable[[bool], None]] = []

    @property
    def lock_calls(self) -> int:
        return len(self.calls)

    def lock(self, on_done) -> None:
        self.calls.append("lock")
        if self.raises is not None:
            raise self.raises
        if self.mode == "manual":
            self.pending.append(on_done)
            return
        self._answer(on_done)

    def _answer(self, on_done) -> None:
        if self.result.ok and self.shows_screen:
            self.set_locked(True)
        self._deliver(lambda: on_done(self.result))

    def complete(self) -> None:
        """Answer the oldest pending lock request."""
        self._answer(self.pending.pop(0))

    def is_active(self) -> bool:
        if self.is_active_error is not None:
            raise self.is_active_error
        return self.locked

    def on_active_changed(self, callback) -> None:
        self._callbacks.append(callback)

    def set_locked(self, value: bool) -> None:
        if self.locked == value:
            return
        self.locked = value
        for callback in list(self._callbacks):
            self._deliver(lambda cb=callback: cb(value))


class FakeInhibitor:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeSleepSignal:
    def __init__(self, deliver: Callable = _direct) -> None:
        self._deliver = deliver
        self._callbacks: List[Callable[[bool], None]] = []
        self.inhibitors: List[FakeInhibitor] = []
        self.delay_max = 5.0
        self.acquire_error: Optional[Exception] = None

    def on_prepare_for_sleep(self, callback) -> None:
        self._callbacks.append(callback)

    def acquire_delay_inhibitor(self) -> FakeInhibitor:
        if self.acquire_error is not None:
            raise self.acquire_error
        inhibitor = FakeInhibitor()
        self.inhibitors.append(inhibitor)
        return inhibitor

    def inhibit_delay_max(self) -> float:
        return self.delay_max

    @property
    def held(self) -> int:
        return sum(1 for i in self.inhibitors if not i.released)

    def fire(self, going_to_sleep: bool) -> None:
        for callback in list(self._callbacks):
            self._deliver(lambda cb=callback: cb(going_to_sleep))


class FakeSlideshow:
    def __init__(self) -> None:
        self.running = False
        self.refuse: Optional[str] = None
        self.starts = 0
        self.stops = 0
        self._callbacks: List[Callable[[str], None]] = []
        self.start_error: Optional[Exception] = None

    def start(self) -> Optional[str]:
        if self.start_error is not None:
            raise self.start_error
        if self.refuse is not None:
            return self.refuse
        self.starts += 1
        self.running = True
        return None

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    def connect_stopped(self, callback) -> None:
        self._callbacks.append(callback)

    # test helper: the user touched a slideshow window
    def end_by_input(self, reason: str = "input") -> None:
        assert self.running, "the slideshow is not running"
        self.running = False
        for callback in list(self._callbacks):
            callback(reason)


class FakeSettings:
    def __init__(self, idle: int = 120, grace: int = 0) -> None:
        self.values = {"idle-timeout-seconds": idle, "lock-grace-period-seconds": grace}
        self._callbacks: List[Callable[[str], None]] = []

    def get_idle_timeout_seconds(self) -> int:
        return self.values["idle-timeout-seconds"]

    def get_lock_grace_period_seconds(self) -> int:
        return self.values["lock-grace-period-seconds"]

    def connect_changed(self, callback) -> None:
        self._callbacks.append(callback)

    def set(self, key: str, value) -> None:
        self.values[key] = value
        for callback in list(self._callbacks):
            callback(key)


class FakeGuard:
    """The sleep guard as the state machine sees it."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.start_error: Optional[Exception] = None

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class LoopThread:
    """A thread with a GLib main context of its own: the stand-in for the service's main loop
    (and, in ``GuardThread``, for the sleep guard's). ``post`` queues a function on it; a job
    that blocks keeps everything behind it from running, as a stuck file system call would."""

    def __init__(self, name: str = "test-main-loop") -> None:
        import threading

        from gi.repository import GLib

        from slideshow_lock.loop import poster_for

        self._context = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(self._context, False)
        self.post = poster_for(self._context)
        started = threading.Event()

        def run() -> None:
            self._context.push_thread_default()
            started.set()
            self._loop.run()
            self._context.pop_thread_default()

        self.thread = threading.Thread(target=run, name=name, daemon=True)
        self.thread.start()
        assert started.wait(5)

    def call(self, fn: Callable, timeout: float = 5.0):
        """Run *fn* on this loop and wait for its result (raises what it raised)."""
        import threading

        done = threading.Event()
        box: dict = {}

        def run() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:
                box["error"] = exc
            done.set()

        self.post(run)
        assert done.wait(timeout), "the loop did not run the function in time"
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def stop(self) -> None:
        self._loop.quit()
        self.thread.join(5)


def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.005
) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
