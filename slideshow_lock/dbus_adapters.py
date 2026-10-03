"""The real adapters: the protocols of ``session.py`` on ``Gio.DBusConnection`` (CORE-1, ARCH-1).

Which bus carries what (ARCH-1 section 3):

========================  ===========  ==========================================================
protocol                  bus          interface
========================  ===========  ==========================================================
``IdleWatcher``           session      ``org.gnome.Mutter.IdleMonitor`` (``AddIdleWatch``,
                                       ``AddUserActiveWatch``, ``RemoveWatch``, ``WatchFired``)
``InhibitionQuery``       session      ``org.gnome.SessionManager`` (``IsInhibited(8)``,
                                       ``InhibitorAdded``, ``InhibitorRemoved``)
``SessionLock``           session      ``org.gnome.ScreenSaver`` (``Lock``, ``GetActive``,
                                       ``ActiveChanged``); fallback: system bus,
                                       ``org.freedesktop.login1.Session.Lock`` and ``LockedHint``
``SleepSignal``           system       ``org.freedesktop.login1.Manager`` (``PrepareForSleep``,
                                       ``Inhibit("sleep", ..., "delay")``, ``InhibitDelayMaxUSec``)
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
from typing import Callable, List, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

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

IDLE_MONITOR = ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core")
IDLE_MONITOR_IFACE = "org.gnome.Mutter.IdleMonitor"
SESSION_MANAGER = ("org.gnome.SessionManager", "/org/gnome/SessionManager")
SESSION_MANAGER_IFACE = "org.gnome.SessionManager"
SCREEN_SAVER = ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver")
SCREEN_SAVER_IFACE = "org.gnome.ScreenSaver"
LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1")
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


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
        CALL_TIMEOUT_MS,
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
    """``InhibitionQuery`` on ``org.gnome.SessionManager``."""

    def __init__(self, conn: "Gio.DBusConnection") -> None:
        self._conn = conn
        self._callbacks: List[Callable[[bool], None]] = []
        self._last = bool(_probe(SESSION_MANAGER_IFACE, self.is_idle_inhibited))
        self._subs = _Subscriptions(conn)
        self._subs.add(SESSION_MANAGER, SESSION_MANAGER_IFACE, "InhibitorAdded", self._changed)
        self._subs.add(SESSION_MANAGER, SESSION_MANAGER_IFACE, "InhibitorRemoved", self._changed)

    def is_idle_inhibited(self) -> bool:
        result = _call_sync(
            self._conn,
            SESSION_MANAGER,
            SESSION_MANAGER_IFACE,
            "IsInhibited",
            GLib.Variant("(u)", (INHIBIT_IDLE,)),
            "(b)",
        )
        return bool(result.unpack()[0])

    def on_idle_inhibit_changed(self, callback: Callable[[bool], None]) -> None:
        self._callbacks.append(callback)

    def close(self) -> None:
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
            fd = fds.get(index)
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
