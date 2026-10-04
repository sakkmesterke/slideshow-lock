"""The state machine of the service (CORE-1, ARCH-1 section 4).

It decides when the slideshow starts, when the session locks and when nothing happens. It
depends on the protocols of ``session.py`` and on nothing else: no ``Gio``, no bus name, no
systemd. Every transition of the table in ``docs/architecture/dbus-state-machine.md`` is a
method here, and the three reasons to lock are three separate paths, not one branch:

* **input after an idle-triggered slideshow** (``_input_detected``): locks if the grace period
  has run out (strict ``<``, D16). Only a slideshow with ``TriggerSource.IDLE`` can reach the
  lock call; a manual preview cannot (D11, by construction: the call is not on its path). An
  application that starts inhibiting idle while such a slideshow runs ends it the same way
  (``_on_inhibit_changed``): inside the grace period it just stops, after it the session locks at
  once, through the same method (``_end_slideshow_and_lock_if_due``), because "the next move"
  may be somebody else's.
* **sleep** (``sleep_started`` / ``sleep_lock_finished``): the lock itself is made by the sleep
  guard on its own thread (``sleep_guard.py``), not here. This object only follows: it stops
  the slideshow and records the result. It never asks the inhibition query (D28).
* **somebody else locked** (``_on_active_changed``): the state follows, nothing is called.

The idle path asks ``InhibitionQuery`` in exactly one place, ``_on_idle``, and hears about a new
inhibitor through ``_on_inhibit_changed``; the sleep path never does. The inhibitor only ever
holds back the idle-triggered slideshow, or ends one: it never holds back a lock that is due.

Threads: every method is called on the main loop's thread. The sleep guard hands its two
messages over with a main-loop post; it never calls this object from its own thread.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Callable, Optional

from slideshow_lock.session import (
    Cancel,
    IdleWatcher,
    InhibitionQuery,
    LockResult,
    SessionLock,
    SleepGuardControl,
    SlideshowControl,
)

_LOG = logging.getLogger(__name__)


class State(enum.Enum):
    DISABLED = "disabled"
    IDLE_WATCHING = "idle_watching"
    SLIDESHOW_RUNNING = "slideshow_running"
    LOCKING = "locking"
    LOCKED = "locked"


class TriggerSource(enum.Enum):
    IDLE = "idle"
    MANUAL_PREVIEW = "manual_preview"


#: The only sources whose input may lock the session. A preview is not on the list (D11).
_LOCKING_SOURCES = frozenset({TriggerSource.IDLE})


class StateMachine:
    """*settings* has ``get_idle_timeout_seconds()``, ``get_lock_grace_period_seconds()`` and
    ``connect_changed(cb)``. *guard* is the sleep guard (started and stopped with the machine);
    None for a machine without one (tests of the idle path only)."""

    def __init__(
        self,
        *,
        idle: IdleWatcher,
        inhibition: InhibitionQuery,
        lock: SessionLock,
        slideshow: SlideshowControl,
        settings,
        guard: Optional[SleepGuardControl] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle = idle
        self._inhibition = inhibition
        self._lock = lock
        self._slideshow = slideshow
        self._settings = settings
        self._guard = guard
        self._clock = clock

        self._state = State.DISABLED
        self._trigger: Optional[TriggerSource] = None
        self._started_at = 0.0
        self._cancel_active: Optional[Cancel] = None
        self._holding_inhibit = False

        slideshow.connect_stopped(self._on_slideshow_stopped)
        lock.on_active_changed(self._on_active_changed)
        inhibition.on_idle_inhibit_changed(self._on_inhibit_changed)
        settings.connect_changed(self._on_settings_changed)

    # -- public API ----------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def trigger_source(self) -> Optional[TriggerSource]:
        """Who started the slideshow that is running, or None."""
        return self._trigger

    def enable(self) -> None:
        """DISABLED -> IDLE_WATCHING (or LOCKED, if the session is locked already). Takes the
        sleep guard first: if that fails the exception goes to the caller and the machine
        stays disabled, because running without the pre-sleep lock is not an option."""
        if self._state is not State.DISABLED:
            return
        if self._guard is not None:
            self._guard.start()
        try:
            locked = self._lock.is_active()
        except Exception:
            _LOG.warning("[lock] could not ask whether the session is locked, assuming not")
            locked = False
        self._state = State.LOCKED if locked else State.IDLE_WATCHING
        self._idle.on_idle(self._idle_timeout(), self._on_idle)
        _LOG.info(
            "[idle-trigger] watching for idle (timeout=%ds, state=%s)",
            self._idle_timeout(),
            self._state.value,
        )

    def disable(self) -> None:
        """Any state -> DISABLED: stop the slideshow, drop every watch, release the guard."""
        if self._state is State.DISABLED:
            return
        if self._state is State.SLIDESHOW_RUNNING:
            self._finish_slideshow()
        self._state = State.DISABLED
        self._trigger = None
        self._release_idle_inhibit()  # also when no slideshow ran: nothing may outlive the machine
        self._idle.cancel_idle()
        if self._guard is not None:
            self._guard.stop()
        _LOG.info("[idle-trigger] disabled")

    def start_preview(self) -> bool:
        """The manual preview (D11): a slideshow whose input never locks. True if it is up."""
        if self._state is not State.IDLE_WATCHING:
            _LOG.info("[slideshow] preview not started (state=%s)", self._state.value)
            return False
        return self._start_slideshow(TriggerSource.MANUAL_PREVIEW)

    # -- the idle path (3.1, 3.4) ------------------------------------------------------

    def _idle_timeout(self) -> int:
        return int(self._settings.get_idle_timeout_seconds())

    def _on_idle(self) -> None:
        if self._state is State.LOCKED:
            if self._still_locked():
                _LOG.debug("[idle-trigger] idle while the session is locked: nothing to do")
                return
            _LOG.info("[lock] the session is no longer locked, watching for idle again")
            self._state = State.IDLE_WATCHING
        if self._state is not State.IDLE_WATCHING:
            _LOG.debug("[idle-trigger] idle ignored (state=%s)", self._state.value)
            return
        try:
            inhibited = self._inhibition.is_idle_inhibited()
        except Exception as exc:
            _LOG.warning(
                "[idle-trigger] could not ask whether idle is inhibited (%s): "
                "slideshow not started",
                exc,
            )
            return
        if inhibited:
            _LOG.info("[idle-trigger] idle, but an application inhibits it: slideshow not started")
            return
        self._start_slideshow(TriggerSource.IDLE)

    def _still_locked(self) -> bool:
        try:
            return self._lock.is_active()
        except Exception:
            return True  # cannot tell: do nothing rather than act on a guess

    def _start_slideshow(self, trigger: TriggerSource) -> bool:
        started_at = self._clock()
        try:
            reason = self._slideshow.start()
        except Exception:
            _LOG.exception("[slideshow] starting the slideshow failed")
            reason = "the slideshow could not be started"
        if reason is not None:
            _LOG.warning("[slideshow-dir] slideshow not started: %s", reason)
            return False
        self._state = State.SLIDESHOW_RUNNING
        self._trigger = trigger
        self._started_at = started_at
        if trigger is TriggerSource.IDLE:
            # input is the idle monitor's active transition (D22); a preview has none, the
            # windows' own input controllers report it through the slideshow
            self._cancel_active = self._idle.on_user_active(self._on_user_active)
            self._hold_idle_inhibit()
        _LOG.info("[slideshow] started (trigger=%s)", trigger.value)
        return True

    def _on_inhibit_changed(self, inhibited: bool) -> None:
        if (
            inhibited
            and self._state is State.SLIDESHOW_RUNNING
            and self._trigger is TriggerSource.IDLE
        ):
            _LOG.info("[idle-trigger] an application inhibits idle: stopping the slideshow")
            self._end_slideshow_and_lock_if_due("an application that inhibits idle")

    # -- input (3.2, 3.3) -----------------------------------------------------------------

    def _on_user_active(self) -> None:
        self._cancel_active = None  # one-shot: it fired
        self._input_detected("idle monitor")

    def _on_slideshow_stopped(self, reason: str) -> None:
        self._input_detected(f"window ({reason})")

    def _input_detected(self, where: str) -> None:
        if self._state is not State.SLIDESHOW_RUNNING:
            return
        self._end_slideshow_and_lock_if_due(f"input from the {where}")

    def _end_slideshow_and_lock_if_due(self, cause: str) -> None:
        """Stop the running slideshow and lock the session if it ran for the grace period or
        longer (strict ``<`` for "within": a grace of 0 never skips the lock). The one place
        where a slideshow ends with a possible lock, for input and for a new idle inhibitor
        alike; a slideshow that is not idle-triggered (the preview) never locks."""
        trigger = self._trigger
        elapsed = self._clock() - self._started_at
        self._finish_slideshow()
        if trigger not in _LOCKING_SOURCES:
            _LOG.info(
                "[slideshow] stopped by %s (trigger=%s): a preview never locks",
                cause,
                trigger.value if trigger else "?",
            )
            return
        grace = int(self._settings.get_lock_grace_period_seconds())
        if elapsed < grace:  # strict less-than (D16): G = 0 never skips the lock
            _LOG.info(
                "[slideshow] stopped by %s after %.1fs, within the grace period of %ds: no lock",
                cause,
                elapsed,
                grace,
            )
            return
        _LOG.info(
            "[slideshow] stopped by %s after %.1fs: locking the session",
            cause,
            elapsed,
        )
        self._lock_session()

    def _lock_session(self) -> None:
        self._state = State.LOCKING
        try:
            self._lock.lock(self._on_lock_done)
        except Exception as exc:
            self._on_lock_done(LockResult(False, str(exc)))

    def _on_lock_done(self, result: LockResult) -> None:
        if self._state is not State.LOCKING:
            return
        if result.ok:
            _LOG.info("[lock] session locked")
            self._state = State.LOCKED
        else:
            _LOG.error("[lock] locking the session failed: %s", result.error or "unknown error")
            self._state = State.IDLE_WATCHING

    def _finish_slideshow(self) -> None:
        """The slideshow is over by our decision: no lock call here, just tear it down."""
        cancel, self._cancel_active = self._cancel_active, None
        if cancel is not None:
            cancel()
        self._state = State.IDLE_WATCHING
        self._trigger = None
        self._release_idle_inhibit()
        try:
            self._slideshow.stop()
        except Exception:
            _LOG.exception("[slideshow] stopping the slideshow failed")

    def _hold_idle_inhibit(self) -> None:
        """An idle-triggered slideshow keeps the desktop's own idle delay from blanking or locking
        the screen under it. A manual preview does not (nothing starts it from idle, and it ends
        at the first input anyway). A refusal is logged and the slideshow goes on."""
        try:
            self._inhibition.hold_idle_inhibit()
        except Exception as exc:
            _LOG.warning(
                "[slideshow] could not take the idle inhibitor (%s): the desktop's own idle "
                "delay may blank or lock the screen during the slideshow",
                exc,
            )
        else:
            self._holding_inhibit = True

    def _release_idle_inhibit(self) -> None:
        """Called on every way a slideshow ends. If it stayed, the next idle would find idle
        inhibited and no slideshow would ever start again."""
        if not self._holding_inhibit:
            return
        self._holding_inhibit = False
        try:
            self._inhibition.release_idle_inhibit()
        except Exception as exc:
            _LOG.warning("[slideshow] could not give back the idle inhibitor (%s)", exc)

    # -- the lock state (3.6) -----------------------------------------------------------------

    def _on_active_changed(self, locked: bool) -> None:
        if self._state is State.DISABLED:
            return
        if locked:
            if self._state is State.SLIDESHOW_RUNNING:
                _LOG.info(
                    "[lock] the session was locked while the slideshow ran: slideshow stopped"
                )
                self._finish_slideshow()
            if self._state is not State.LOCKED:
                _LOG.info("[lock] session is locked")
            self._state = State.LOCKED
        elif self._state is State.LOCKED:
            _LOG.info("[lock] session unlocked")
            self._state = State.IDLE_WATCHING

    # -- sleep (3.5): the guard locks, this object follows --------------------------------------

    def sleep_started(self) -> None:
        """``PrepareForSleep(true)`` reached the sleep guard, which is locking on its own
        thread. Stop the slideshow, whatever started it (D10: independent of how it began)."""
        if self._state in (State.DISABLED, State.LOCKED):
            return
        if self._state is State.SLIDESHOW_RUNNING:
            _LOG.info("[slideshow] stopped before sleep")
            self._finish_slideshow()
        self._state = State.LOCKING

    def sleep_lock_finished(self, ok: bool) -> None:
        """The guard's lock round trip is over."""
        if self._state is not State.LOCKING:
            return
        self._state = State.LOCKED if ok else State.IDLE_WATCHING

    # -- settings ---------------------------------------------------------------------------------

    def _on_settings_changed(self, key: str) -> None:
        from slideshow_lock.settings import KEY_IDLE_TIMEOUT_SECONDS

        if key == KEY_IDLE_TIMEOUT_SECONDS and self._state is not State.DISABLED:
            self._idle.on_idle(self._idle_timeout(), self._on_idle)
            _LOG.info("[config] idle timeout is now %ds", self._idle_timeout())
