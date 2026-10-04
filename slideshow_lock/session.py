"""The contract between the state machine and the desktop session (CORE-1, ARCH-1 section 2).

Every D-Bus interaction is a small ``Protocol`` here. The state machine and the sleep guard
depend on these and on nothing else: no ``Gio``, no ``GLib``, no bus name appears in
``state_machine.py`` or ``sleep_guard.py``. There are two implementations of each protocol: the
real adapters of ``dbus_adapters.py`` and the fakes of the tests (``tests/fakes.py``), which let
a test inject every event without a bus.

Threads. The sleep path and the idle path live on different threads on purpose (the security
condition of CORE-1: a picture folder on a stuck network mount can block the main loop for
minutes, and the lock before suspend must not wait for it):

* ``IdleWatcher``, ``InhibitionQuery`` and the state machine's own ``SessionLock`` belong to the
  main thread; their callbacks run on the main loop.
* ``SleepSignal`` and the sleep guard's own ``SessionLock`` belong to the guard thread; their
  callbacks run on the guard's loop. An adapter delivers its callbacks on the thread that was
  current when it was created, and an instance must be used from that thread only.

Callbacks are never called from inside the call that registered them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

try:  # Protocol is in typing from 3.8; the project's floor is 3.9
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore

Cancel = Callable[[], None]


class UnsupportedSessionInterface(RuntimeError):
    """A D-Bus interface the service needs is not on this session (ARCH-1 section 7).

    Raised at startup by the capability probe, with the name of what is missing, so the service
    can say so at ERROR level and refuse to run silently degraded."""

    def __init__(self, interface: str, detail: str = "") -> None:
        self.interface = interface
        self.detail = detail
        super().__init__(f"{interface}: {detail}" if detail else interface)


@dataclass(frozen=True)
class LockResult:
    """How a ``SessionLock.lock()`` round trip ended. ``ok`` False carries the error text."""

    ok: bool
    error: str = ""


class IdleWatcher(Protocol):
    """The compositor's idle monitor (``org.gnome.Mutter.IdleMonitor``). Event driven."""

    def on_idle(self, timeout_s: float, callback: Callable[[], None]) -> None:
        """Call *callback()* each time the session has been idle for *timeout_s* seconds,
        until ``cancel_idle()``. Replaces an earlier watch."""

    def cancel_idle(self) -> None:
        """Remove the idle watch, if there is one."""

    def on_user_active(self, callback: Callable[[], None]) -> Cancel:
        """Call *callback()* once, at the next idle -> active transition. Returns a cancel."""

    def close(self) -> None:
        """Remove every watch."""


class InhibitionQuery(Protocol):
    """Whether an application holds an idle inhibitor (``org.gnome.SessionManager``), and the one
    idle inhibitor this service holds itself while its slideshow shows.

    "An application" means another one: the inhibitor taken by ``hold_idle_inhibit`` is never
    reported by ``is_idle_inhibited`` or ``on_idle_inhibit_changed``, whatever order things
    happen in (see ``dbus_adapters.SessionManagerInhibition``)."""

    def is_idle_inhibited(self) -> bool:
        """Raises if the session manager cannot be asked."""

    def on_idle_inhibit_changed(self, callback: Callable[[bool], None]) -> None:
        """Call *callback(inhibited)* when an inhibitor of another application is added or
        removed."""

    def hold_idle_inhibit(self) -> None:
        """Take this service's idle inhibitor, so that the desktop's own idle delay does not
        blank or lock the screen while the slideshow shows. Idempotent. Raises if the session
        manager refuses."""

    def release_idle_inhibit(self) -> None:
        """Give it back. Idempotent. Raises if the session manager refuses."""


class SessionLock(Protocol):
    """Lock the session, and know whether it is locked."""

    def lock(self, on_done: Callable[[LockResult], None]) -> None:
        """Start the lock round trip; *on_done(result)* is called when it is over, on the
        creating thread's loop. Never raises: a failure is a ``LockResult(ok=False)``."""

    def is_active(self) -> bool:
        """True if the session is locked now. Raises if it cannot be asked."""

    def on_active_changed(self, callback: Callable[[bool], None]) -> None:
        """Call *callback(locked)* whenever the lock state changes."""


class DelayInhibitor(Protocol):
    """The fd of a logind delay inhibitor. Holding it delays suspend (up to
    ``InhibitDelayMaxSec``); ``release()`` lets suspend go on. Idempotent."""

    def release(self) -> None: ...


class SleepSignal(Protocol):
    """logind's suspend notification and the delay inhibitor that makes it useful (D1, D32)."""

    def on_prepare_for_sleep(self, callback: Callable[[bool], None]) -> None:
        """Call *callback(going_to_sleep)* for ``PrepareForSleep``: True before, False after."""

    def acquire_delay_inhibitor(self) -> DelayInhibitor:
        """Take a "sleep" delay inhibitor. Raises if logind refuses."""

    def inhibit_delay_max(self) -> float:
        """``InhibitDelayMaxUSec`` of the running logind, in seconds (read, not assumed)."""


class SlideshowControl(Protocol):
    """Starts and stops the slideshow windows. Main thread."""

    def start(self) -> Optional[str]:
        """Show the slideshow. Returns None if it is up, else the reason it could not start
        (no picture, no monitor): the state machine logs it and goes on."""

    def stop(self) -> None:
        """Close the slideshow. Idempotent. Must not lock anything."""

    def connect_stopped(self, callback: Callable[[str], None]) -> None:
        """Call *callback(reason)* when the slideshow ends by itself (``"input"``: a key, the
        pointer, a click or a scroll on a window; ``"close"`` is reported as ``"input"`` too).
        Not called for a ``stop()`` the state machine asked for."""


class SleepGuardControl(Protocol):
    """The sleep guard as the state machine sees it: on or off (D4, the preferences toggle)."""

    def start(self) -> None:
        """Subscribe and take the delay inhibitor. Raises ``UnsupportedSessionInterface`` or
        ``OSError`` if that cannot be done: the service must not run without it."""

    def stop(self) -> None:
        """Release the inhibitor and the subscription."""
