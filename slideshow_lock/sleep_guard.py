"""The lock before suspend (CORE-1, ARCH-1 sections 3.5 and 3.6, decisions D1, D10, D28, D32, D34).

``SleepGuard`` is the whole sleep path: it holds logind's delay inhibitor, and when
``PrepareForSleep(true)`` comes it locks the session and lets suspend go on. It runs on a thread
of its own (``GuardThread``) with its own GLib main context, and that is the security condition
of CORE-1: the slideshow's image source reads folders and file headers on the main loop, and a
stuck network mount can block one such call for minutes. If the sleep handler ran on that loop,
the lock would miss the ``InhibitDelayMaxSec`` window (5 s by default) and the machine would sleep
unlocked, without any error. Nothing here touches the image source, the slideshow windows, the
settings or the main loop's state; the two messages to the state machine are posted (queued),
never waited for.

The sequence on ``PrepareForSleep(true)``, in this order:

1. post "sleep started" to the main loop (the slideshow stops there; not waited for; if the post
   itself fails it is logged at ERROR and the lock goes on);
2. start the lock round trip (asynchronous, so this thread stays free to see the wake signal);
3. when the round trip is over, success or not, release the delay inhibitor (``_on_lock_done``);
4. post the result to the main loop.

Nothing else runs between the signal and the lock call. The guard keeps no picture of the lock
state and never skips the call because the session looks locked already: the answer to ``Lock()``
is not proof that a lock screen is up (on the login1 fallback it only means that a signal was
sent, with nobody necessarily listening), and a remembered "locked" would keep every later
suspend from being locked. The idle inhibition query is not an argument of this class and is
never asked here (D28): an application that inhibits idle, a video call say, does not keep the
lock from being made.

``PrepareForSleep(false)`` before step 3 has finished is the D34 case: the machine woke while
the lock was still on its way, so it may have resumed unlocked. That is logged at WARNING with
the elapsed time. The inhibitor is taken again after every sleep.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Tuple

from slideshow_lock.loop import Poster, poster_for
from slideshow_lock.session import (
    DelayInhibitor,
    LockResult,
    SessionLock,
    SleepSignal,
)

_LOG = logging.getLogger(__name__)

#: How long ``GuardThread.start`` waits for the guard to be up (the inhibitor is the slow part).
STARTUP_TIMEOUT_SECONDS = 15.0


def boottime_clock() -> float:
    """Seconds that keep counting while the machine sleeps, so the time between
    ``PrepareForSleep(true)`` and the wake signal is the real one. (``time.monotonic`` stops.)"""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):  # pragma: no cover - not Linux
        return time.monotonic()


class SleepListener:
    """What the state machine offers the guard (see ``StateMachine``). Called on the main loop,
    through a post."""

    def sleep_started(self) -> None: ...

    def sleep_lock_finished(self, ok: bool) -> None: ...


class _Round:
    __slots__ = ("started", "done")

    def __init__(self, started: float) -> None:
        self.started = started
        self.done = False


class SleepGuard:
    """The sleep path. Every method runs on the guard's own thread, and the adapters passed in
    were created there. *to_main* queues a function on the main loop."""

    def __init__(
        self,
        sleep: SleepSignal,
        lock: SessionLock,
        listener: SleepListener,
        to_main: Poster,
        clock: Callable[[], float] = boottime_clock,
    ) -> None:
        self._sleep = sleep
        self._lock = lock
        self._listener = listener
        self._to_main = to_main
        self._clock = clock

        self._inhibitor: Optional[DelayInhibitor] = None
        self._sleeping = False
        self._round: Optional[_Round] = None
        self._running = False

    # -- set up and tear down --------------------------------------------------------

    def setup(self) -> None:
        """Subscribe, take the delay inhibitor, read the delay ceiling. Raises if the inhibitor
        cannot be had: the service must not run without it (ARCH-1 section 7)."""
        self._sleep.on_prepare_for_sleep(self._on_prepare_for_sleep)
        self._inhibitor = self._sleep.acquire_delay_inhibitor()
        self._running = True
        try:
            limit = self._sleep.inhibit_delay_max()
        except Exception as exc:
            _LOG.warning("[sleep-inhibit] could not read InhibitDelayMaxSec (%s)", exc)
        else:
            _LOG.info("[sleep-inhibit] delay inhibitor held (InhibitDelayMaxSec=%.1fs)", limit)

    def teardown(self) -> None:
        self._running = False
        self._release()

    # -- the sleep signal -----------------------------------------------------------------

    def _on_prepare_for_sleep(self, going_to_sleep: bool) -> None:
        if not self._running:
            return
        if going_to_sleep:
            self._before_sleep()
        else:
            self._after_wake()

    def _before_sleep(self) -> None:
        if self._sleeping:
            return  # a repeated signal: the round trip is on its way already
        self._sleeping = True
        current = self._round = _Round(self._clock())
        _LOG.info("[sleep-inhibit] PrepareForSleep(true): locking before suspend")
        try:
            self._to_main(self._listener.sleep_started)
        except Exception:  # the stop of the slideshow is not what suspend waits for: lock anyway
            _LOG.exception(
                "[sleep-inhibit] could not hand the sleep start to the main loop: "
                "the slideshow is not told, locking anyway"
            )
        try:
            self._lock.lock(lambda result: self._on_lock_done(current, result))
        except Exception as exc:  # the protocol says it does not raise; do not hold suspend back
            self._on_lock_done(current, LockResult(False, str(exc)))

    def _on_lock_done(self, current: _Round, result: LockResult) -> None:
        if current.done:
            return  # one answer per round trip, and an old one never ends a newer round
        current.done = True
        elapsed_ms = (self._clock() - current.started) * 1000
        if current is not self._round:
            _LOG.info("[lock] a late answer of an earlier round trip (elapsed=%dms)", elapsed_ms)
            return  # the inhibitor held now belongs to the newer round
        self._release()  # always: holding it longer only blocks suspend for everybody
        if result.ok:
            _LOG.info("[lock] session locked before suspend (elapsed=%dms)", elapsed_ms)
        else:
            _LOG.error(
                "[lock] locking before suspend failed (elapsed=%dms): %s",
                elapsed_ms,
                result.error or "unknown error",
            )
        ok = result.ok
        self._to_main(lambda: self._listener.sleep_lock_finished(ok))
        if not self._sleeping:  # the machine woke before this round trip ended
            self._reacquire()

    def _after_wake(self) -> None:
        self._sleeping = False
        current = self._round
        if current is not None and not current.done:
            elapsed_ms = (self._clock() - current.started) * 1000
            _LOG.warning(
                "[sleep-inhibit] resume received before lock sequence completed "
                "(elapsed=%dms, limit=InhibitDelayMaxSec); session may have resumed unlocked",
                elapsed_ms,
            )
            return  # the inhibitor is still held; it is taken again when the round trip ends
        self._reacquire()

    # -- the delay inhibitor ---------------------------------------------------------------

    def _release(self) -> None:
        inhibitor, self._inhibitor = self._inhibitor, None
        if inhibitor is not None:
            try:
                inhibitor.release()
            except Exception:
                _LOG.exception("[sleep-inhibit] releasing the delay inhibitor failed")

    def _reacquire(self) -> None:
        if self._inhibitor is not None or not self._running:
            return
        try:
            self._inhibitor = self._sleep.acquire_delay_inhibitor()
        except Exception as exc:
            _LOG.error(
                "[sleep-inhibit] could not take the delay inhibitor again (%s): the next "
                "suspend will not wait for the lock",
                exc,
            )


def _close_adapter(adapter: object) -> None:
    # ``close`` is not part of the two protocols; the real adapters have it, a stand-in may not
    close = getattr(adapter, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        _LOG.exception("[sleep-inhibit] closing an adapter of the sleep guard failed")


class GuardThread:
    """A ``SleepGuardControl``: runs a ``SleepGuard`` on a thread with its own GLib main context.

    *build* is called on that thread, so the adapters it returns deliver their callbacks on its
    loop; it returns ``(SleepSignal, SessionLock)``. *listener* is the state machine and *to_main*
    posts to the main loop. The thread is a daemon: nothing it waits on can keep the process from
    exiting.
    """

    def __init__(
        self,
        build: Callable[[], Tuple[SleepSignal, SessionLock]],
        listener: SleepListener,
        to_main: Poster,
        clock: Callable[[], float] = boottime_clock,
    ) -> None:
        self._build = build
        self._listener = listener
        self._to_main = to_main
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._context = None
        self._guard: Optional[SleepGuard] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    @property
    def post(self) -> Poster:
        """Queue a function on the guard's loop (for tests; raises before ``start``)."""
        return poster_for(self._context)

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="slideshow-lock-sleep", daemon=True)
        self._thread.start()
        if not self._ready.wait(STARTUP_TIMEOUT_SECONDS):
            raise TimeoutError("the sleep guard did not come up in time")
        if self._error is not None:
            error, self._error = self._error, None
            self._thread.join(2)
            raise error

    def stop(self) -> None:
        thread, loop, context = self._thread, self._loop, self._context
        if thread is None or loop is None:
            return
        # queued on the loop itself: a plain ``loop.quit()`` from here is lost if the loop has
        # not started running yet (stop right after start)
        poster_for(context)(loop.quit)
        thread.join(5)
        self._thread = None

    def _run(self) -> None:
        from gi.repository import GLib

        context = GLib.MainContext.new()
        context.push_thread_default()
        loop = GLib.MainLoop.new(context, False)
        guard: Optional[SleepGuard] = None
        adapters: Tuple[object, ...] = ()
        try:
            try:
                sleep, lock = self._build()
                adapters = (sleep, lock)
                guard = SleepGuard(sleep, lock, self._listener, self._to_main, self._clock)
                guard.setup()
            except BaseException as exc:
                self._error = exc
                return
            self._context, self._loop, self._guard = context, loop, guard
            self._ready.set()
            loop.run()
        finally:
            if guard is not None:
                guard.teardown()
            # the adapters are this thread's: their signal subscriptions end here, or every
            # disable/enable cycle would leave two more on the bus connection
            for adapter in adapters:
                _close_adapter(adapter)
            context.pop_thread_default()
            self._ready.set()
