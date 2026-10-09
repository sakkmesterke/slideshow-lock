# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The real adapters: the protocols of ``session.py`` on ``Gio.DBusConnection`` (CORE-1, ARCH-1).

Which bus carries what (ARCH-1 section 3):

========================  ===========  ==========================================================
protocol                  bus          interface
========================  ===========  ==========================================================
``IdleWatcher``           session      ``org.gnome.Mutter.IdleMonitor`` (``AddIdleWatch``,
                                       ``AddUserActiveWatch``, ``RemoveWatch``, ``WatchFired``)
``InhibitionQuery``       session      ``org.gnome.SessionManager`` (``IsInhibited(8)``,
                                       ``GetInhibitors`` and, per inhibitor, ``GetAppId`` and
                                       ``GetFlags``; ``Inhibit(..., 8)`` and ``Uninhibit`` for the
                                       one the service holds while its slideshow shows;
                                       ``InhibitorAdded``, ``InhibitorRemoved``)
``SessionLock``           session      ``org.gnome.ScreenSaver`` (``Lock``, ``GetActive``,
                                       ``ActiveChanged``); fallback: system bus,
                                       ``org.freedesktop.login1.Session.Lock`` and ``LockedHint``
``SleepSignal``           system       ``org.freedesktop.login1.Manager`` (``PrepareForSleep``,
                                       ``Inhibit("sleep", ..., "delay")``, ``InhibitDelayMaxUSec``)
``OverviewControl``       session      ``org.gnome.Shell`` (the property ``OverviewActive``, read
                                       and written through ``org.freedesktop.DBus.Properties``)
(unit start)              session      ``org.freedesktop.systemd1.Manager`` of the user's systemd
                                       (``ResetFailedUnit``, ``StartUnit``)
========================  ===========  ==========================================================

Each constructor probes its interface once, with a cheap read-only call, and raises
``UnsupportedSessionInterface`` naming what is missing (ARCH-1 section 7): the service never
runs silently degraded. The adapters take the connection as an argument, so a test can point
them at a private bus; ``session_bus()`` and ``system_bus()`` are the production ones.

Threads. Gio delivers a signal, and the answer of an asynchronous call, on the main context that
was the thread's default one when the subscription or the call was made. So an adapter made on
the main thread calls back on the main loop, and one made on the sleep guard's thread (see
``GuardThread``) on the guard's loop. An instance is used from the thread that made it.

This module is the lock side of the package: it names the screensaver, the login manager and the
bus, which the preview modules must never do (``tests/test_preview.py`` scans them, and checks
that none of them imports this one).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, List, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from slideshow_lock import APP_ID  # noqa: E402
from slideshow_lock.session import (  # noqa: E402
    Cancel,
    LockResult,
    UnsupportedSessionInterface,
)

_LOG = logging.getLogger(__name__)

#: Milliseconds before a D-Bus call is given up. Every call here answers at once on a healthy
#: session; a long wait means the other side is stuck, and then an error is the useful answer.
CALL_TIMEOUT_MS = 5000

#: ``flags`` of ``org.gnome.SessionManager.IsInhibited``: 8 is "inhibit the session going idle".
INHIBIT_IDLE = 8

#: The application id and the reason of the idle inhibitor the service holds while its slideshow
#: shows. The id is how the service tells its own inhibitor from somebody else's (see
#: ``SessionManagerInhibition.is_idle_inhibited``). The reason is for diagnostics, not for the
#: user interface, so it is not translated.
INHIBIT_APP_ID = APP_ID
INHIBIT_REASON = "A slideshow is showing"

IDLE_MONITOR = ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core")
IDLE_MONITOR_IFACE = "org.gnome.Mutter.IdleMonitor"
SESSION_MANAGER = ("org.gnome.SessionManager", "/org/gnome/SessionManager")
SESSION_MANAGER_IFACE = "org.gnome.SessionManager"
SESSION_INHIBITOR_IFACE = "org.gnome.SessionManager.Inhibitor"
SCREEN_SAVER = ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver")
SCREEN_SAVER_IFACE = "org.gnome.ScreenSaver"
LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1")
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
SHELL = ("org.gnome.Shell", "/org/gnome/Shell")
SHELL_IFACE = "org.gnome.Shell"
SYSTEMD = ("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
SYSTEMD_MANAGER_IFACE = "org.freedesktop.systemd1.Manager"


def session_bus() -> "Gio.DBusConnection":
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def system_bus() -> "Gio.DBusConnection":
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def _call_sync(
    conn: "Gio.DBusConnection",
    target,
    iface: str,
    method: str,
    args: Optional["GLib.Variant"] = None,
    reply: Optional[str] = None,
    timeout_ms: int = CALL_TIMEOUT_MS,
):
    name, path = target
    return conn.call_sync(
        name,
        path,
        iface,
        method,
        args,
        GLib.VariantType(reply) if reply else None,
        Gio.DBusCallFlags.NONE,
        timeout_ms,
        None,
    )


def _probe(what: str, fn: Callable[[], object]) -> object:
    try:
        return fn()
    except GLib.Error as exc:
        raise UnsupportedSessionInterface(what, exc.message) from exc


class _Subscriptions:
    """The signal subscriptions of one adapter, so that ``close()`` can drop them."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        self._ids: List[int] = []

    def add(self, target, iface: str, signal: str, handler: Callable, arg0: Optional[str] = None):
        name, path = target
        self._ids.append(
            self._conn.signal_subscribe(
                name,
                iface,
                signal,
                path,
                arg0,
                Gio.DBusSignalFlags.NONE,
                lambda _c, _sender, _path, _iface, _signal, params: handler(params),
            )
        )
        # ``signal_subscribe`` only queues the match rule for the bus daemon. A round trip on the
        # same connection comes after it, so once ``add`` returns no signal can be missed.
        self._conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetId",
            None,
            GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS,
            None,
        )

    def close(self) -> None:
        ids, self._ids = self._ids, []
        for sub in ids:
            self._conn.signal_unsubscribe(sub)


# -- idle ---------------------------------------------------------------------------------------


class MutterIdleWatcher:
    """``IdleWatcher`` on the compositor's idle monitor. Event driven: no polling, no timer."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        self._idle_id: Optional[int] = None
        self._idle_callback: Optional[Callable[[], None]] = None
        self._active = {}  # watch id -> callback
        _probe(
            IDLE_MONITOR_IFACE,
            lambda: _call_sync(conn, IDLE_MONITOR, IDLE_MONITOR_IFACE, "GetIdletime", None, "(t)"),
        )
        self._subs = _Subscriptions(conn)
        self._subs.add(IDLE_MONITOR, IDLE_MONITOR_IFACE, "WatchFired", self._on_watch_fired)

    def on_idle(self, timeout_s: float, callback: Callable[[], None]) -> None:
        self.cancel_idle()
        millis = max(1, int(timeout_s * 1000))
        result = _call_sync(
            self._conn,
            IDLE_MONITOR,
            IDLE_MONITOR_IFACE,
            "AddIdleWatch",
            GLib.Variant("(t)", (millis,)),
            "(u)",
        )
        self._idle_id = result.unpack()[0]
        self._idle_callback = callback
        _LOG.debug("[idle-trigger] idle watch %d added (%d ms)", self._idle_id, millis)

    def cancel_idle(self) -> None:
        watch, self._idle_id = self._idle_id, None
        self._idle_callback = None
        if watch is not None:
            self._remove(watch)

    def on_user_active(self, callback: Callable[[], None]) -> Cancel:
        result = _call_sync(
            self._conn, IDLE_MONITOR, IDLE_MONITOR_IFACE, "AddUserActiveWatch", None, "(u)"
        )
        watch = result.unpack()[0]
        self._active[watch] = callback
        _LOG.debug("[idle-trigger] user-active watch %d added", watch)

        def cancel() -> None:
            if self._active.pop(watch, None) is not None:
                self._remove(watch)

        return cancel

    def close(self) -> None:
        self.cancel_idle()
        for watch in list(self._active):
            self._active.pop(watch, None)
            self._remove(watch)
        self._subs.close()

    def _remove(self, watch: int) -> None:
        try:
            _call_sync(
                self._conn,
                IDLE_MONITOR,
                IDLE_MONITOR_IFACE,
                "RemoveWatch",
                GLib.Variant("(u)", (watch,)),
            )
        except GLib.Error as exc:  # already gone (a user-active watch removes itself)
            _LOG.debug("[idle-trigger] removing watch %d: %s", watch, exc.message)

    def _on_watch_fired(self, params: "GLib.Variant") -> None:
        watch = params.unpack()[0]
        if watch == self._idle_id:
            callback = self._idle_callback
            if callback is not None:
                callback()
            return
        callback = self._active.pop(watch, None)  # the monitor drops a user-active watch itself
        if callback is not None:
            callback()


# -- idle inhibition ------------------------------------------------------------------------------


class SessionManagerInhibition:
    """``InhibitionQuery`` on ``org.gnome.SessionManager``, and the holder of the one idle
    inhibitor the service takes while its own slideshow shows.

    Without it GNOME's own idle delay would blank and lock the screen while the slideshow runs.
    The service's inhibitor must not count as "an application inhibits idle", or the state
    machine would end its own slideshow, and a foreign one added next to it would pass unnoticed
    (the answer would stay True). So ``is_idle_inhibited`` and the change callback speak about
    the inhibitors of other applications only: those whose application id is not ours."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        self._callbacks: List[Callable[[bool], None]] = []
        self._cookie: Optional[int] = None
        self._last = bool(_probe(SESSION_MANAGER_IFACE, self.is_idle_inhibited))
        self._subs = _Subscriptions(conn)
        self._subs.add(SESSION_MANAGER, SESSION_MANAGER_IFACE, "InhibitorAdded", self._changed)
        self._subs.add(SESSION_MANAGER, SESSION_MANAGER_IFACE, "InhibitorRemoved", self._changed)

    def is_idle_inhibited(self) -> bool:
        """True if an application other than this one holds an idle inhibitor."""
        result = _call_sync(
            self._conn,
            SESSION_MANAGER,
            SESSION_MANAGER_IFACE,
            "IsInhibited",
            GLib.Variant("(u)", (INHIBIT_IDLE,)),
            "(b)",
        )
        if not result.unpack()[0]:
            return False  # nobody inhibits idle, ours included
        return self._another_application_inhibits_idle()

    def _another_application_inhibits_idle(self) -> bool:
        for path in self._list_inhibitors():
            try:
                flags = self._inhibitor_property(path, "GetFlags", "(u)")
                app_id = self._inhibitor_property(path, "GetAppId", "(s)")
            except GLib.Error as exc:
                if self._inhibitor_is_gone(exc, path):
                    continue  # it went away between the list and the question
                raise  # any other failure is "cannot be asked", never "it is not there"
            if flags & INHIBIT_IDLE and app_id != INHIBIT_APP_ID:
                return True
        return False

    def _list_inhibitors(self) -> List[str]:
        return _call_sync(
            self._conn, SESSION_MANAGER, SESSION_MANAGER_IFACE, "GetInhibitors", None, "(ao)"
        ).unpack()[0]

    def _inhibitor_is_gone(self, exc: "GLib.Error", path: str) -> bool:
        """True if *exc*, the answer to a question about the inhibitor at *path*, means that it
        went away after it was listed. Decided by the error NAME, never by its text: the text is
        translated by the session manager's own library (measured with GDBus: the Hungarian and
        German catalogs of GLib translate "Object does not exist at path").

        ``UnknownObject`` (libdbus services) says it. ``UnknownMethod`` (GDBus services, for an
        object path that is not there) says it only if a fresh list no longer has the path: the
        same name also answers a missing method on an object that is there. Every other error,
        and a fresh list that cannot be read, say nothing: False (the question stays unanswered,
        never "it is gone")."""
        name = Gio.DBusError.get_remote_error(exc)
        if name == "org.freedesktop.DBus.Error.UnknownObject":
            return True
        if name != "org.freedesktop.DBus.Error.UnknownMethod":
            return False
        try:
            return path not in self._list_inhibitors()
        except GLib.Error:
            return False

    def _inhibitor_property(self, path: str, method: str, reply: str):
        return _call_sync(
            self._conn,
            (SESSION_MANAGER[0], path),
            SESSION_INHIBITOR_IFACE,
            method,
            None,
            reply,
        ).unpack()[0]

    def hold_idle_inhibit(self) -> None:
        """Take the idle inhibitor (``Inhibit`` with flag 8). A second call while it is held does
        nothing. Raises ``GLib.Error`` if the session manager refuses."""
        if self._cookie is not None:
            return
        result = _call_sync(
            self._conn,
            SESSION_MANAGER,
            SESSION_MANAGER_IFACE,
            "Inhibit",
            GLib.Variant("(susu)", (INHIBIT_APP_ID, 0, INHIBIT_REASON, INHIBIT_IDLE)),
            "(u)",
        )
        self._cookie = result.unpack()[0]

    def release_idle_inhibit(self) -> None:
        """Give the idle inhibitor back. Nothing happens if it is not held. Raises
        ``GLib.Error`` if the session manager refuses. The cookie is then kept, so that the next
        call tries again, unless the session manager no longer lists an inhibitor of ours (then
        it is gone, and the error is still raised once)."""
        cookie = self._cookie
        if cookie is None:
            return
        try:
            _call_sync(
                self._conn,
                SESSION_MANAGER,
                SESSION_MANAGER_IFACE,
                "Uninhibit",
                GLib.Variant("(u)", (cookie,)),
            )
        except GLib.Error:
            if self._own_inhibitor_is_gone():
                self._cookie = None
            raise
        self._cookie = None

    def _own_inhibitor_is_gone(self) -> bool:
        """True only if the session manager answers, and lists no inhibitor of our application id.
        Any doubt (it cannot be asked, an inhibitor cannot be read) is False."""
        try:
            for path in self._list_inhibitors():
                try:
                    if self._inhibitor_property(path, "GetAppId", "(s)") == INHIBIT_APP_ID:
                        return False
                except GLib.Error as exc:
                    if not self._inhibitor_is_gone(exc, path):
                        return False
        except GLib.Error:
            return False
        return True

    def on_idle_inhibit_changed(self, callback: Callable[[bool], None]) -> None:
        self._callbacks.append(callback)

    def close(self) -> None:
        try:
            self.release_idle_inhibit()
        except GLib.Error as exc:
            _LOG.warning("[slideshow] releasing the idle inhibitor failed (%s)", exc.message)
        self._subs.close()

    def _changed(self, _params) -> None:
        try:
            now = self.is_idle_inhibited()
        except GLib.Error as exc:
            _LOG.warning("[idle-trigger] could not ask the session manager (%s)", exc.message)
            return
        if now != self._last:
            self._last = now
            for callback in list(self._callbacks):
                callback(now)


# -- locking --------------------------------------------------------------------------------------


class _LockBase:
    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        self._callbacks: List[Callable[[bool], None]] = []
        self._subs = _Subscriptions(conn)

    def on_active_changed(self, callback: Callable[[bool], None]) -> None:
        self._callbacks.append(callback)

    def close(self) -> None:
        self._subs.close()

    def _notify(self, locked: bool) -> None:
        for callback in list(self._callbacks):
            callback(locked)

    def _lock_async(self, target, iface: str, on_done: Callable[[LockResult], None]) -> None:
        name, path = target

        def finished(conn, res, _data=None) -> None:
            try:
                conn.call_finish(res)
            except GLib.Error as exc:
                on_done(LockResult(False, exc.message))
            else:
                on_done(LockResult(True))

        try:
            self._conn.call(
                name,
                path,
                iface,
                "Lock",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS,
                None,
                finished,
                None,
            )
        except GLib.Error as exc:
            on_done(LockResult(False, exc.message))


class ScreenSaverLock(_LockBase):
    """``SessionLock`` on ``org.gnome.ScreenSaver``."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        super().__init__(conn)
        _probe(SCREEN_SAVER_IFACE, self.is_active)
        self._subs.add(
            SCREEN_SAVER,
            SCREEN_SAVER_IFACE,
            "ActiveChanged",
            lambda params: self._notify(bool(params.unpack()[0])),
        )

    def lock(self, on_done: Callable[[LockResult], None]) -> None:
        self._lock_async(SCREEN_SAVER, SCREEN_SAVER_IFACE, on_done)

    def is_active(self) -> bool:
        result = _call_sync(self._conn, SCREEN_SAVER, SCREEN_SAVER_IFACE, "GetActive", None, "(b)")
        return bool(result.unpack()[0])


class Login1SessionLock(_LockBase):
    """``SessionLock`` on ``org.freedesktop.login1.Session`` (the fallback of ARCH-1 section 3.3,
    when there is no ``org.gnome.ScreenSaver``): ``Lock()`` on this process's session and its
    ``LockedHint`` for the state, on the system bus."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        super().__init__(conn)
        self._path = self._session_path()
        self._target = (LOGIN1[0], self._path)
        _probe(LOGIN1_SESSION_IFACE, self.is_active)
        self._subs.add(
            self._target,
            PROPERTIES_IFACE,
            "PropertiesChanged",
            self._properties_changed,
            arg0=LOGIN1_SESSION_IFACE,
        )

    def _session_path(self) -> str:
        def ask() -> str:
            result = _call_sync(
                self._conn,
                LOGIN1,
                LOGIN1_MANAGER_IFACE,
                "GetSessionByPID",
                GLib.Variant("(u)", (os.getpid(),)),
                "(o)",
            )
            return result.unpack()[0]

        return str(_probe("login1.Session", ask))

    def lock(self, on_done: Callable[[LockResult], None]) -> None:
        self._lock_async(self._target, LOGIN1_SESSION_IFACE, on_done)

    def is_active(self) -> bool:
        result = _call_sync(
            self._conn,
            self._target,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (LOGIN1_SESSION_IFACE, "LockedHint")),
            "(v)",
        )
        return bool(result.unpack()[0])

    def _properties_changed(self, params: "GLib.Variant") -> None:
        _iface, changed, _invalidated = params.unpack()
        if "LockedHint" in changed:
            self._notify(bool(changed["LockedHint"]))


def make_session_lock(session: "Gio.DBusConnection", system: "Gio.DBusConnection"):
    """The screensaver if there is one, else logind's session lock, else
    ``UnsupportedSessionInterface("ScreenSaver+login1.Session")`` (ARCH-1 sections 3.3 and 7)."""
    try:
        return ScreenSaverLock(session)
    except UnsupportedSessionInterface as first:
        _LOG.warning(
            "[lock] no org.gnome.ScreenSaver (%s), trying login1 Session.Lock", first.detail
        )
    try:
        return Login1SessionLock(system)
    except UnsupportedSessionInterface as second:
        raise UnsupportedSessionInterface("ScreenSaver+login1.Session", second.detail) from second


# -- overview -------------------------------------------------------------------------------------

#: The most the overview adapter waits in all: the calls and the polling together. The closing
#: animation of the shell takes 250 ms (``ANIMATION_TIME`` in its ``overview.js``).
OVERVIEW_WAIT_S = 0.5
OVERVIEW_POLL_S = 0.05

#: D-Bus errors that say the shell is not on this session (not GNOME): not a problem to report.
_NO_SHELL_ERRORS = (
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
)


def _one_line(text: str) -> str:
    """A message that came from the peer, on one line and at most 200 characters, for the log."""
    return text.replace("\n", " ")[:200]


class GnomeShellOverview:
    """``OverviewControl`` on the ``OverviewActive`` property of ``org.gnome.Shell``.

    A slideshow window that opens while the overview is up comes out as a third window in it
    instead of full screen. The property is true until the closing animation is over, so after
    setting it to false the adapter reads it again until it is false.

    It does not raise and it waits about ``OVERVIEW_WAIT_S`` in all (each call gets what is left
    of that time as its timeout; one more call still runs after the last 50 ms sleep). Without a
    shell on the bus it does nothing and says nothing above DEBUG; any other failure is a WARNING,
    once, then DEBUG. It does not probe anything when it is made: the shell may come up later than
    this service."""

    def __init__(
        self,
        conn: "Gio.DBusConnection",
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._sleep = sleep
        self._warned = False

    def close_if_open(self) -> None:
        try:
            self._close_if_open()
        except GLib.Error as exc:
            remote = (
                Gio.DBusError.get_remote_error(exc) if Gio.DBusError.is_remote_error(exc) else ""
            )
            if remote in _NO_SHELL_ERRORS:
                _LOG.debug("[slideshow] no org.gnome.Shell on this session: the overview stays")
            else:
                self._warn("the overview cannot be closed (%s)", _one_line(exc.message))
        except Exception as exc:  # the adapter must not stop a slideshow, whatever it was
            self._warn("the overview cannot be closed (%s)", _one_line(str(exc)))

    def _warn(self, message: str, *args) -> None:
        if self._warned:
            _LOG.debug("[slideshow] " + message, *args)
            return
        self._warned = True
        _LOG.warning("[slideshow] " + message + ": the slideshow starts anyway", *args)

    def _is_active(self, deadline: float) -> bool:
        result = _call_sync(
            self._conn,
            SHELL,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (SHELL_IFACE, "OverviewActive")),
            "(v)",
            timeout_ms=self._left_ms(deadline),
        )
        return bool(result.unpack()[0])

    def _left_ms(self, deadline: float) -> int:
        return max(1, int((deadline - self._clock()) * 1000))

    def _close_if_open(self) -> None:
        deadline = self._clock() + OVERVIEW_WAIT_S
        if not self._is_active(deadline):
            return
        _LOG.info("[slideshow] the overview is open: closing it before the slideshow starts")
        _call_sync(
            self._conn,
            SHELL,
            PROPERTIES_IFACE,
            "Set",
            GLib.Variant("(ssv)", (SHELL_IFACE, "OverviewActive", GLib.Variant("b", False))),
            "()",
            timeout_ms=self._left_ms(deadline),
        )
        while self._clock() < deadline:
            self._sleep(OVERVIEW_POLL_S)
            try:
                if not self._is_active(deadline):
                    return
            except GLib.Error as exc:
                # the last read gets what is left of the half second, which may be a millisecond
                if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
                    raise
                break
        self._warn("the overview is still open after %.1f s", OVERVIEW_WAIT_S)


# -- the user's systemd ------------------------------------------------------------------------


class SystemdUserManager:
    """Two calls on the ``org.freedesktop.systemd1.Manager`` of the user's systemd, which is on the
    session bus: what ``slideshowlock`` needs to start the service unit. Unlike the adapters above
    it raises ``GLib.Error`` as it is; the caller (``control``) decides what a failure means.
    Nothing is probed when it is made."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn

    def reset_failed(self, unit: str) -> None:
        """``ResetFailedUnit``: clears the ``failed`` state, which a unit that ran out of its
        start limit stays in and that makes ``StartUnit`` refuse it. Not an error for a unit that
        has not failed."""
        _call_sync(
            self._conn,
            SYSTEMD,
            SYSTEMD_MANAGER_IFACE,
            "ResetFailedUnit",
            GLib.Variant("(s)", (unit,)),
            "()",
        )

    def start(self, unit: str) -> str:
        """``StartUnit(unit, "replace")``: queues the start job and returns its object path
        without waiting for the unit to be up. A unit that is already running is left running."""
        result = _call_sync(
            self._conn,
            SYSTEMD,
            SYSTEMD_MANAGER_IFACE,
            "StartUnit",
            GLib.Variant("(ss)", (unit, "replace")),
            "(o)",
        )
        return result.unpack()[0]


# -- sleep ----------------------------------------------------------------------------------------


class _FdInhibitor:
    """The file descriptor of a logind inhibitor. Closing it releases the inhibitor."""

    def __init__(self, fd: int) -> None:
        self._fd: Optional[int] = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


class Login1Sleep:
    """``SleepSignal`` on ``org.freedesktop.login1.Manager`` (system bus)."""

    WHO = "slideshow-lock"
    WHY = "Lock the screen before the system suspends"

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        _probe(LOGIN1_MANAGER_IFACE, self.inhibit_delay_max)
        self._subs = _Subscriptions(conn)

    def on_prepare_for_sleep(self, callback: Callable[[bool], None]) -> None:
        self._subs.add(
            LOGIN1,
            LOGIN1_MANAGER_IFACE,
            "PrepareForSleep",
            lambda params: callback(bool(params.unpack()[0])),
        )

    def acquire_delay_inhibitor(self) -> _FdInhibitor:
        name, path = LOGIN1
        try:
            result, fds = self._conn.call_with_unix_fd_list_sync(
                name,
                path,
                LOGIN1_MANAGER_IFACE,
                "Inhibit",
                GLib.Variant("(ssss)", ("sleep", self.WHO, self.WHY, "delay")),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS,
                None,
                None,
            )
            index = result.get_child_value(0).get_handle()
            fd = fds.steal_fds()[index]  # ours alone: ``get`` would leave a copy in the list
        except GLib.Error as exc:
            raise UnsupportedSessionInterface("login1.Manager.Inhibit", exc.message) from exc
        return _FdInhibitor(fd)

    def inhibit_delay_max(self) -> float:
        result = _call_sync(
            self._conn,
            LOGIN1,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (LOGIN1_MANAGER_IFACE, "InhibitDelayMaxUSec")),
            "(v)",
        )
        return int(result.unpack()[0]) / 1_000_000

    def close(self) -> None:
        self._subs.close()
