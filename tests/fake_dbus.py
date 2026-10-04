"""A fake desktop on two private D-Bus daemons, for the tests of the real adapters.

``Desktop`` starts a daemon standing in for the session bus (Mutter's idle monitor, the session
manager, the screensaver) and one standing in for the system bus (login1), and serves the
methods, properties and signals the adapters use. A test drives it with plain method calls
(``fire_idle``, ``prepare_for_sleep``, ``set_inhibited`` ...) and reads what the adapters did
(``lock_calls``, ``inhibitors``, ``watches``). The services run on a loop thread of their own, so
a test thread that blocks does not stop the desktop answering.

What this is not: GNOME. It proves that the adapters speak the interfaces as written down in
``docs/architecture/dbus-state-machine.md``; whether the real shell answers the same way is the
manual test list.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

from gi.repository import Gio, GLib

from tests.fakes import LoopThread

_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><keep_umask/><listen>unix:tmpdir=/tmp</listen>
<policy context="default"><allow send_destination="*" eavesdrop="true"/>
<allow eavesdrop="true"/><allow own="*"/></policy></busconfig>
"""

IDLE_XML = """<node><interface name="org.gnome.Mutter.IdleMonitor">
<method name="GetIdletime"><arg type="t" direction="out"/></method>
<method name="AddIdleWatch"><arg type="t" direction="in"/><arg type="u" direction="out"/></method>
<method name="AddUserActiveWatch"><arg type="u" direction="out"/></method>
<method name="RemoveWatch"><arg type="u" direction="in"/></method>
<signal name="WatchFired"><arg type="u"/></signal></interface></node>"""

SESSION_MANAGER_XML = """<node><interface name="org.gnome.SessionManager">
<method name="IsInhibited"><arg type="u" direction="in"/><arg type="b" direction="out"/></method>
<method name="Inhibit"><arg type="s" direction="in"/><arg type="u" direction="in"/>
<arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="out"/></method>
<method name="Uninhibit"><arg type="u" direction="in"/></method>
<method name="GetInhibitors"><arg type="ao" direction="out"/></method>
<signal name="InhibitorAdded"><arg type="o"/></signal>
<signal name="InhibitorRemoved"><arg type="o"/></signal></interface></node>"""

SESSION_INHIBITOR_XML = """<node><interface name="org.gnome.SessionManager.Inhibitor">
<method name="GetAppId"><arg type="s" direction="out"/></method>
<method name="GetFlags"><arg type="u" direction="out"/></method></interface></node>"""

INHIBITOR_PATH = "/org/gnome/SessionManager/Inhibitor{}"

SCREEN_SAVER_XML = """<node><interface name="org.gnome.ScreenSaver">
<method name="Lock"/>
<method name="GetActive"><arg type="b" direction="out"/></method>
<signal name="ActiveChanged"><arg type="b"/></signal></interface></node>"""

LOGIN1_MANAGER_XML = """<node><interface name="org.freedesktop.login1.Manager">
<method name="Inhibit"><arg type="s" direction="in"/><arg type="s" direction="in"/>
<arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="h" direction="out"/></method>
<method name="GetSessionByPID"><arg type="u" direction="in"/>
<arg type="o" direction="out"/></method>
<property name="InhibitDelayMaxUSec" type="t" access="read"/>
<signal name="PrepareForSleep"><arg type="b"/></signal></interface></node>"""

LOGIN1_SESSION_XML = """<node><interface name="org.freedesktop.login1.Session">
<method name="Lock"/><property name="LockedHint" type="b" access="read"/></interface></node>"""

SESSION_PATH = "/org/freedesktop/login1/session/_1"


def dbus_daemon_available() -> bool:
    return shutil.which("dbus-daemon") is not None


class _Daemon:
    def __init__(self, workdir: str, name: str) -> None:
        config = os.path.join(workdir, f"{name}.conf")
        with open(config, "w") as handle:
            handle.write(_CONFIG)
        self.process = subprocess.Popen(
            ["dbus-daemon", f"--config-file={config}", "--print-address=1", "--nofork"],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.address = self.process.stdout.readline().strip()
        assert self.address, "dbus-daemon printed no address"

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(5)


def _connect(address: str) -> "Gio.DBusConnection":
    return Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


def _own(conn: "Gio.DBusConnection", name: str) -> None:
    conn.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (name, 0)),
        GLib.VariantType("(u)"),
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    )


class Desktop:
    """``with Desktop() as d:`` -> ``d.session`` and ``d.system`` are connections for the
    adapters (their own, separate from the ones the services use)."""

    def __init__(
        self,
        *,
        screensaver: bool = True,
        idle_monitor: bool = True,
        session_address: Optional[str] = None,
    ) -> None:
        """*session_address*: serve on an existing session bus (a compositor's, in the smoke
        tool) instead of a private daemon; give ``idle_monitor=False`` there, the compositor has
        its own."""
        self._session_address = session_address
        self._with_screensaver = screensaver
        self._with_idle_monitor = idle_monitor
        self.watches: Dict[int, str] = {}  # id -> "idle" | "active"
        self.idle_timeouts: List[int] = []  # ms, of every AddIdleWatch
        self.removed: List[int] = []
        self.inhibited = False  # the one foreign inhibitor of ``set_inhibited``
        # every inhibitor the session manager knows: path -> {"app_id", "flags", "reason"}
        self.inhibitors: Dict[str, dict] = {}
        self.inhibit_requests: List[tuple] = []  # (app_id, toplevel_xid, reason, flags)
        self.uninhibit_calls: List[int] = []  # cookies
        # the next *n* ``Uninhibit`` calls fail with this error and leave the inhibitor in place
        self.uninhibit_failures = 0
        self.uninhibit_error = "org.freedesktop.DBus.Error.Failed"
        # inhibitor method ("GetFlags", "GetAppId") -> the D-Bus error it answers with
        self.inhibitor_errors: Dict[str, str] = {}
        # session manager method ("GetInhibitors") -> the D-Bus error it answers with
        self.session_manager_errors: Dict[str, str] = {}
        # path -> flags of inhibitors that ``IsInhibited`` counts and the next ``GetInhibitors``
        # lists once, and that have no object on the bus (they went away right after): the race
        # between the list and the question about one of its entries
        self.ghost_inhibitors: Dict[str, int] = {}
        self._cookies: Dict[int, str] = {}  # cookie -> path, of the inhibitors clients took
        self._inhibitor_regs: Dict[str, int] = {}
        self._next_cookie = 100
        self._next_foreign = 1000
        self.locked = False
        self.lock_calls: List[float] = []
        self.session_lock_calls: List[float] = []  # login1 Session.Lock
        self.lock_delay = 0.0  # seconds the screensaver takes to answer Lock
        self.lock_error: Optional[str] = None
        self.inhibit_calls: List[tuple] = []
        self._pipes: List[tuple] = []  # (write_fd) per inhibitor handed out
        self.locked_hint = False
        self._next_watch = 1
        self.delay_max_usec = 5_000_000
        self.last_lock_reply_at = 0.0

    # -- life cycle --------------------------------------------------------------------------

    def __enter__(self) -> "Desktop":
        self._dir = tempfile.mkdtemp(prefix="slideshow-lock-dbus-")
        self._session_daemon = None if self._session_address else _Daemon(self._dir, "session")
        self._system_daemon = _Daemon(self._dir, "system")
        self.session_address = self._session_address or self._session_daemon.address
        self.system_address = self._system_daemon.address
        self.service_loop = LoopThread("fake-desktop")
        self.service_loop.call(self._serve)
        self.session = _connect(self.session_address)
        self.system = _connect(self.system_address)
        return self

    def __exit__(self, *exc) -> None:
        for conn in (self.session, self.system):
            conn.close_sync(None)
        self.service_loop.call(self._close_services)
        self.service_loop.stop()
        for write_fd in self._pipes:
            try:
                os.close(write_fd)
            except OSError:
                pass
        if self._session_daemon is not None:
            self._session_daemon.stop()
        self._system_daemon.stop()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _close_services(self) -> None:
        for conn in (self._svc_session, self._svc_system):
            conn.close_sync(None)

    def _serve(self) -> None:
        self._svc_session = _connect(self.session_address)
        self._svc_system = _connect(self.system_address)
        if self._with_idle_monitor:
            self._register(
                self._svc_session,
                "/org/gnome/Mutter/IdleMonitor/Core",
                IDLE_XML,
                self._idle_call,
            )
            _own(self._svc_session, "org.gnome.Mutter.IdleMonitor")
        self._register(
            self._svc_session,
            "/org/gnome/SessionManager",
            SESSION_MANAGER_XML,
            self._session_manager_call,
        )
        _own(self._svc_session, "org.gnome.SessionManager")
        if self._with_screensaver:
            self._register(
                self._svc_session,
                "/org/gnome/ScreenSaver",
                SCREEN_SAVER_XML,
                self._screensaver_call,
            )
            _own(self._svc_session, "org.gnome.ScreenSaver")
        self._register(
            self._svc_system,
            "/org/freedesktop/login1",
            LOGIN1_MANAGER_XML,
            self._login1_call,
            self._login1_property,
        )
        self._register(
            self._svc_system,
            SESSION_PATH,
            LOGIN1_SESSION_XML,
            self._login1_session_call,
            self._login1_session_property,
        )
        _own(self._svc_system, "org.freedesktop.login1")

    @staticmethod
    def _register(conn, path, xml, on_call, on_property=None) -> int:
        info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        return conn.register_object(path, info, on_call, on_property, None)

    # -- the services ------------------------------------------------------------------------------

    def _emit(self, conn, path, iface, signal, params) -> None:
        conn.emit_signal(None, path, iface, signal, params)

    def _idle_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method == "GetIdletime":
            invocation.return_value(GLib.Variant("(t)", (0,)))
        elif method in ("AddIdleWatch", "AddUserActiveWatch"):
            watch = self._next_watch
            self._next_watch += 1
            if method == "AddIdleWatch":
                self.watches[watch] = "idle"
                self.idle_timeouts.append(params.unpack()[0])
            else:
                self.watches[watch] = "active"
            invocation.return_value(GLib.Variant("(u)", (watch,)))
        elif method == "RemoveWatch":
            watch = params.unpack()[0]
            self.removed.append(watch)
            if self.watches.pop(watch, None) is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "no watch")
            else:
                invocation.return_value(None)

    def _session_manager_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method in self.session_manager_errors:
            invocation.return_dbus_error(self.session_manager_errors[method], "refused")
        elif method == "IsInhibited":
            flags = params.unpack()[0]
            inhibited = any(i["flags"] & flags for i in self.inhibitors.values()) or any(
                ghost & flags for ghost in self.ghost_inhibitors.values()
            )
            invocation.return_value(GLib.Variant("(b)", (inhibited,)))
        elif method == "GetInhibitors":
            listed = sorted(self.inhibitors) + sorted(self.ghost_inhibitors)
            self.ghost_inhibitors = {}  # listed once, gone for the next question
            invocation.return_value(GLib.Variant("(ao)", (listed,)))
        elif method == "Inhibit":
            app_id, xid, reason, flags = params.unpack()
            self.inhibit_requests.append((app_id, xid, reason, flags))
            cookie = self._next_cookie
            self._next_cookie += 1
            self._cookies[cookie] = self._add_inhibitor(
                INHIBITOR_PATH.format(cookie), app_id, flags, reason
            )
            invocation.return_value(GLib.Variant("(u)", (cookie,)))
        elif method == "Uninhibit":
            cookie = params.unpack()[0]
            self.uninhibit_calls.append(cookie)
            if self.uninhibit_failures > 0:
                self.uninhibit_failures -= 1
                invocation.return_dbus_error(self.uninhibit_error, "refused")
                return
            inhibitor_path = self._cookies.pop(cookie, None)
            if inhibitor_path is None:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.InvalidArgs", "no such inhibitor"
                )
                return
            self._remove_inhibitor(inhibitor_path)
            invocation.return_value(None)

    def _inhibitor_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        inhibitor = self.inhibitors[path]
        if method in self.inhibitor_errors:
            invocation.return_dbus_error(self.inhibitor_errors[method], "refused")
        elif method == "GetAppId":
            invocation.return_value(GLib.Variant("(s)", (inhibitor["app_id"],)))
        else:
            invocation.return_value(GLib.Variant("(u)", (inhibitor["flags"],)))

    def _add_inhibitor(self, path: str, app_id: str, flags: int, reason: str = "") -> str:
        """On the service loop. A real session manager emits ``InhibitorAdded`` for each."""
        self.inhibitors[path] = {"app_id": app_id, "flags": flags, "reason": reason}
        self._inhibitor_regs[path] = self._register(
            self._svc_session, path, SESSION_INHIBITOR_XML, self._inhibitor_call
        )
        self._emit(
            self._svc_session,
            "/org/gnome/SessionManager",
            "org.gnome.SessionManager",
            "InhibitorAdded",
            GLib.Variant("(o)", (path,)),
        )
        return path

    def _remove_inhibitor(self, path: str) -> None:
        self.inhibitors.pop(path, None)
        self._svc_session.unregister_object(self._inhibitor_regs.pop(path))
        self._emit(
            self._svc_session,
            "/org/gnome/SessionManager",
            "org.gnome.SessionManager",
            "InhibitorRemoved",
            GLib.Variant("(o)", (path,)),
        )

    def _screensaver_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method == "GetActive":
            invocation.return_value(GLib.Variant("(b)", (self.locked,)))
            return
        self.lock_calls.append(time.monotonic())

        def answer() -> bool:
            if self.lock_error:
                invocation.return_dbus_error("org.gnome.Error.Failed", self.lock_error)
            else:
                self._set_locked(True)
                invocation.return_value(None)
            self.last_lock_reply_at = time.monotonic()
            return GLib.SOURCE_REMOVE

        if self.lock_delay:
            GLib.timeout_add(int(self.lock_delay * 1000), answer)
        else:
            answer()

    def _set_locked(self, value: bool) -> None:
        if self.locked != value:
            self.locked = value
            self._emit(
                self._svc_session,
                "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver",
                "ActiveChanged",
                GLib.Variant("(b)", (value,)),
            )

    def _login1_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method == "GetSessionByPID":
            invocation.return_value(GLib.Variant("(o)", (SESSION_PATH,)))
            return
        self.inhibit_calls.append(params.unpack())
        read_fd, write_fd = os.pipe()
        self._pipes.append(write_fd)
        fds = Gio.UnixFDList.new()
        index = fds.append(read_fd)
        os.close(read_fd)  # the list holds its own copy
        invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (index,)), fds)

    def _login1_property(self, conn, sender, path, iface, name):
        if name == "InhibitDelayMaxUSec":
            return GLib.Variant("t", self.delay_max_usec)
        return None

    def _login1_session_call(self, conn, sender, path, iface, method, params, invocation) -> None:
        self.session_lock_calls.append(time.monotonic())
        self._set_locked_hint(True)
        invocation.return_value(None)

    def _login1_session_property(self, conn, sender, path, iface, name):
        if name == "LockedHint":
            return GLib.Variant("b", self.locked_hint)
        return None

    def _set_locked_hint(self, value: bool) -> None:
        if self.locked_hint != value:
            self.locked_hint = value
            self._emit(
                self._svc_system,
                SESSION_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant(
                    "(sa{sv}as)",
                    (
                        "org.freedesktop.login1.Session",
                        {"LockedHint": GLib.Variant("b", value)},
                        [],
                    ),
                ),
            )

    # -- what a test does to the desktop ----------------------------------------------------------

    def _on_service_loop(self, fn: Callable) -> None:
        self.service_loop.call(fn)

    def fire_idle(self) -> None:
        def go() -> None:
            for watch, kind in list(self.watches.items()):
                if kind == "idle":
                    self._emit(
                        self._svc_session,
                        "/org/gnome/Mutter/IdleMonitor/Core",
                        "org.gnome.Mutter.IdleMonitor",
                        "WatchFired",
                        GLib.Variant("(u)", (watch,)),
                    )

        self._on_service_loop(go)

    def fire_user_active(self) -> None:
        def go() -> None:
            for watch, kind in list(self.watches.items()):
                if kind == "active":
                    del self.watches[watch]  # the monitor removes it when it fires
                    self._emit(
                        self._svc_session,
                        "/org/gnome/Mutter/IdleMonitor/Core",
                        "org.gnome.Mutter.IdleMonitor",
                        "WatchFired",
                        GLib.Variant("(u)", (watch,)),
                    )

        self._on_service_loop(go)

    def set_inhibited(self, value: bool) -> None:
        """One foreign idle inhibitor (application id ``other.application``) comes or goes."""

        def go() -> None:
            self.inhibited = value
            path = INHIBITOR_PATH.format(1)
            if value:
                if path in self.inhibitors:  # already there: a signal, as for a second one
                    self._emit(
                        self._svc_session,
                        "/org/gnome/SessionManager",
                        "org.gnome.SessionManager",
                        "InhibitorAdded",
                        GLib.Variant("(o)", (path,)),
                    )
                else:
                    self._add_inhibitor(path, "other.application", 8, "a video call")
            elif path in self.inhibitors:
                self._remove_inhibitor(path)
            else:
                self._emit(
                    self._svc_session,
                    "/org/gnome/SessionManager",
                    "org.gnome.SessionManager",
                    "InhibitorRemoved",
                    GLib.Variant("(o)", (path,)),
                )

        self._on_service_loop(go)

    def add_inhibitor(self, app_id: str = "another.application", flags: int = 8) -> str:
        """Another application takes an inhibitor with *flags*. Returns its object path."""

        def go() -> str:
            path = INHIBITOR_PATH.format(self._next_foreign)
            self._next_foreign += 1
            return self._add_inhibitor(path, app_id, flags, "a test")

        return self.service_loop.call(go)

    def remove_inhibitor(self, path: str) -> None:
        self.service_loop.call(lambda: self._remove_inhibitor(path))

    def inhibitors_of(self, app_id: str) -> List[str]:
        """The paths of the inhibitors that were taken under *app_id* (read on the service loop,
        so the answer is not half way through a change)."""
        return self.service_loop.call(
            lambda: sorted(p for p, i in self.inhibitors.items() if i["app_id"] == app_id)
        )

    def set_locked(self, value: bool) -> None:
        self._on_service_loop(lambda: self._set_locked(value))

    def set_locked_hint(self, value: bool) -> None:
        self._on_service_loop(lambda: self._set_locked_hint(value))

    def prepare_for_sleep(self, going_to_sleep: bool) -> None:
        self._on_service_loop(
            lambda: self._emit(
                self._svc_system,
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                GLib.Variant("(b)", (going_to_sleep,)),
            )
        )

    def released(self, index: int) -> bool:
        """True once the client closed the fd of the *index*-th inhibitor it was handed."""
        poller = select.poll()
        poller.register(self._pipes[index], select.POLLERR)
        return bool(poller.poll(0))

    @property
    def held(self) -> int:
        return sum(1 for i in range(len(self._pipes)) if not self.released(i))


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Wait for *predicate* while keeping the calling thread's default main context turning (the
    adapters made on it call back there)."""
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


_ = threading  # (kept for tests that start guard threads next to a Desktop)
