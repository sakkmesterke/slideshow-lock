# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The service: idle -> slideshow -> lock on input, plus the lock before suspend (CORE-1).

Run it from a source checkout, no RPM and no systemd needed::

    glib-compile-schemas data/
    GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.service --folder ~/Pictures \\
        --idle-timeout 30 --grace 3

After ``--idle-timeout`` seconds without input the slideshow (``preview_app``'s windows) covers
every monitor; the first input ends it and, if more than ``--grace`` seconds have passed, locks the
session. Before the machine suspends the session is locked whatever else is going on (a
slideshow, or an application inhibiting idle). Ctrl+C or SIGTERM ends the service. The folder, the
intervals and the other options only apply to this run (the stored settings are not written).

``build_service`` is the wiring (adapters, state machine, sleep guard) and takes its connections
and its slideshow as arguments, so the tests run it on private buses; ``main`` adds the GTK
application, the picture source and the real session. Startup follows ARCH-1 section 7: a
missing idle monitor, or no way to lock, keeps the idle-triggered slideshow from starting and is
logged at ERROR; a missing ``PrepareForSleep``, delay inhibitor or lock facility for the sleep
path is fatal (exit status 1), because the service would otherwise run without the lock before
suspend and say nothing.

Not here: ``sd_notify`` readiness and the unit file (PKG-1), the unit enable/disable call
(``UnitControl``, UI-1).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Callable, List, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import (  # noqa: E402
    APP_ID,
    _,
    dbus_adapters,  # noqa: E402
    i18n,
)
from slideshow_lock.loop import Poster, current_poster  # noqa: E402
from slideshow_lock.preview import GLibClock, PreviewController, ThreadWorker  # noqa: E402
from slideshow_lock.preview_app import SessionSettings, build_source  # noqa: E402
from slideshow_lock.preview_window import (  # noqa: E402
    animations_enabled,
    open_monitor_windows,
)
from slideshow_lock.scaling import ImageScaler  # noqa: E402
from slideshow_lock.session import (  # noqa: E402
    Cancel,
    LockResult,
    OverviewControl,
    UnsupportedSessionInterface,
)
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    Settings,
)
from slideshow_lock.sleep_guard import GuardThread  # noqa: E402
from slideshow_lock.state_machine import StateMachine  # noqa: E402

_LOG = logging.getLogger(__name__)


class ServiceSettings(SessionSettings):
    """The stored settings with this run's overrides, and the two keys of the state machine."""

    def get_idle_timeout_seconds(self) -> int:
        return self._get(KEY_IDLE_TIMEOUT_SECONDS, self._settings.get_idle_timeout_seconds)

    def get_lock_grace_period_seconds(self) -> int:
        return self._get(
            KEY_LOCK_GRACE_PERIOD_SECONDS, self._settings.get_lock_grace_period_seconds
        )


class PreviewSlideshow:
    """``SlideshowControl`` on the CORE-2 preview controller and the image source.

    ``start`` refuses, with the reason, when there is no picture to show or no monitor (3.7); the
    controller itself would open empty windows. When it refuses because the folder is still being
    read, ``connect_ready`` callbacks hear about the first picture the scan finds. A stop the state
    machine asked for is not reported back as input.

    With an *overview* (``OverviewControl``) ``start`` closes the shell's overview first, once it
    knows it will open the windows: a slideshow that opens under an open overview is a third
    window in it, not a full screen one."""

    def __init__(
        self, controller: PreviewController, source, overview: Optional[OverviewControl] = None
    ) -> None:
        self._controller = controller
        self._source = source
        self._overview = overview
        self._listeners: List[Callable[[str], None]] = []
        self._ready_listeners: List[Callable[[], None]] = []
        self._waiting_for_scan = False
        self._asked = False
        controller.connect_stopped(self._on_stopped)
        source.connect_current_changed(self._on_current_changed)

    def start(self) -> Optional[str]:
        self._waiting_for_scan = False
        if self._source.current() is None:
            scanning = "" if self._source.scan_complete else " (the folder is still being read)"
            self._waiting_for_scan = bool(scanning)
            return (
                f"no picture to show: the folder '{self._source.folder}' is missing or has no "
                f"valid image{scanning}"
            )
        if self._overview is not None:
            self._overview.close_if_open()
        self._controller.start()
        if not self._controller.running:
            return "no monitor found"
        return None

    def stop(self) -> None:
        self._asked = True
        try:
            self._controller.stop("requested")
        finally:
            self._asked = False

    def connect_stopped(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    def connect_ready(self, callback: Callable[[], None]) -> None:
        self._ready_listeners.append(callback)

    def _on_current_changed(self, path: Optional[str]) -> None:
        if path is None or not self._waiting_for_scan:
            return
        self._waiting_for_scan = False
        for callback in list(self._ready_listeners):
            callback()

    def _on_stopped(self, reason: str) -> None:
        if self._asked:
            return
        for callback in list(self._listeners):
            callback(reason)


class _NoIdle:
    """Stands in for the idle monitor when there is none: nothing ever fires. The state machine
    then never starts a slideshow; the sleep lock is not affected (ARCH-1 section 7)."""

    def on_idle(self, timeout_s, callback) -> None:
        pass

    def cancel_idle(self) -> None:
        pass

    def on_user_active(self, callback) -> Cancel:
        return lambda: None

    def close(self) -> None:
        pass


class _NoLock:
    """Stands in for the lock facility on the main thread when there is none."""

    def lock(self, on_done) -> None:
        on_done(LockResult(False, "no way to lock the session on this desktop"))

    def is_active(self) -> bool:
        return False

    def on_active_changed(self, callback) -> None:
        pass


class _NoInhibition:
    def is_idle_inhibited(self) -> bool:
        return True  # cannot be asked: do not start a slideshow on a guess

    def on_idle_inhibit_changed(self, callback) -> None:
        pass

    def hold_idle_inhibit(self) -> None:
        pass

    def release_idle_inhibit(self) -> None:
        pass


class _LateListener:
    """The state machine needs the guard and the guard needs the state machine: this is the
    second one, filled in once the machine exists."""

    target = None

    def sleep_started(self) -> None:
        if self.target is not None:
            self.target.sleep_started()

    def sleep_lock_finished(self, ok: bool) -> None:
        if self.target is not None:
            self.target.sleep_lock_finished(ok)


class Service:
    def __init__(self, machine: StateMachine, guard: GuardThread, closers: List[Callable]) -> None:
        self.machine = machine
        self.guard = guard
        self._closers = closers

    def close(self) -> None:
        """Stop the slideshow, release the guard and every watch. Idempotent."""
        self.machine.disable()
        closers, self._closers = self._closers, []
        for close in closers:
            try:
                close()
            except Exception:
                _LOG.exception("closing the service failed")


def build_service(
    session: "Gio.DBusConnection",
    system: "Gio.DBusConnection",
    settings,
    slideshow,
    to_main: Optional[Poster] = None,
) -> Service:
    """Probe the interfaces, make the adapters, wire the state machine and the sleep guard, and
    enable the machine. Call it on the thread of the main loop. Raises if the sleep path cannot
    be set up (``UnsupportedSessionInterface``, ``OSError``, a bus error): the service must not
    run without it.
    """
    to_main = to_main or current_poster()
    closers: List[Callable] = []

    idle_ok = True
    try:
        idle = dbus_adapters.MutterIdleWatcher(session)
        closers.append(idle.close)
    except UnsupportedSessionInterface as exc:
        _LOG.error(
            "[idle-trigger] %s is missing (%s): the idle-triggered slideshow is disabled",
            exc.interface,
            exc.detail,
        )
        idle, idle_ok = _NoIdle(), False
    try:
        inhibition = dbus_adapters.SessionManagerInhibition(session)
        closers.append(inhibition.close)
    except UnsupportedSessionInterface as exc:
        _LOG.error(
            "[idle-trigger] %s is missing (%s): the idle-triggered slideshow is disabled",
            exc.interface,
            exc.detail,
        )
        inhibition, idle = _NoInhibition(), _NoIdle()
        idle_ok = False
    try:
        lock = dbus_adapters.make_session_lock(session, system)
        closers.append(lock.close)
    except UnsupportedSessionInterface as exc:
        _LOG.error(
            "[lock] %s is missing (%s): the idle-triggered slideshow is disabled",
            exc.interface,
            exc.detail,
        )
        lock, idle = _NoLock(), _NoIdle()
        idle_ok = False

    listener = _LateListener()

    def build_guard():
        # made on the guard's thread: Gio calls these back on that thread's loop
        sleep = dbus_adapters.Login1Sleep(system)
        return sleep, dbus_adapters.make_session_lock(session, system)

    guard = GuardThread(build_guard, listener, to_main)
    machine = StateMachine(
        idle=idle,
        inhibition=inhibition,
        lock=lock,
        slideshow=slideshow,
        settings=settings,
        guard=guard,
    )
    listener.target = machine
    service = Service(machine, guard, closers)
    try:
        machine.enable()
    except BaseException:
        service.close()
        raise
    if not idle_ok:
        _LOG.error("[idle-trigger] running without the idle-triggered slideshow (see above)")
    return service


# -- the program ---------------------------------------------------------------------------------


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m slideshow_lock.service",
        description=_(
            "Start the slideshow when the session is idle, lock it on the first input after "
            "the grace period, and lock before the machine suspends."
        ),
    )
    parser.add_argument("--folder", help=_("picture folder (this run only)"))
    parser.add_argument("--idle-timeout", type=int, help=_("idle seconds before the slideshow"))
    parser.add_argument("--grace", type=int, help=_("no lock for input within this many seconds"))
    parser.add_argument("--interval", type=int, help=_("seconds per picture (this run only)"))
    parser.add_argument("--order", choices=("random", "name"), help=_("picture order"))
    parser.add_argument("--scaling", choices=("fit", "fill"), help=_("fit or fill the monitor"))
    parser.add_argument(
        "--pan", action="store_true", help=_("scroll portrait pictures slowly (fill mode)")
    )
    parser.add_argument("--debug", action="store_true", help=_("log every step"))
    return parser.parse_args(argv)


def overrides_from_args(args: argparse.Namespace) -> dict:
    """The settings this run replaces. Raises ``ValueError`` (with the message to print) for a
    value outside its range. Nothing is replaced unless it was given."""
    from slideshow_lock.preview_app import overrides_from_args as preview_overrides

    overrides = preview_overrides(args)
    if args.idle_timeout is not None:
        if not 1 <= args.idle_timeout <= 86400:
            raise ValueError(_("The idle timeout must be between 1 and 86400 seconds."))
        overrides[KEY_IDLE_TIMEOUT_SECONDS] = args.idle_timeout
    if args.grace is not None:
        if not 0 <= args.grace <= 86400:
            raise ValueError(_("The grace period must be between 0 and 86400 seconds."))
        overrides[KEY_LOCK_GRACE_PERIOD_SECONDS] = args.grace
    return overrides


def main(argv: Optional[List[str]] = None) -> int:
    language = i18n.setup()  # before the command line is parsed: --help is translated too
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    i18n.log_status(language)
    schema = Gio.SettingsSchemaSource.get_default()
    if schema is None or schema.lookup(APP_ID, True) is None:
        print(
            _(
                "The settings schema is not installed. Compile it and point GSettings at it:\n"
                "  glib-compile-schemas data/\n"
                "  GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.service"
            ),
            file=sys.stderr,
        )
        return 2
    try:
        overrides = overrides_from_args(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    settings = ServiceSettings(Settings(), overrides)

    app = Gtk.Application(application_id=APP_ID + ".Service")
    state = {"service": None, "source": None, "worker": None, "status": 0}

    def fail(message: str, *args) -> None:
        _LOG.error(message, *args)
        state["status"] = 1
        app.quit()

    def on_activate(application: Gtk.Application) -> None:
        if state["service"] is not None:
            return
        application.hold()
        try:
            session, system = dbus_adapters.session_bus(), dbus_adapters.system_bus()
        except GLib.Error as exc:
            fail("[config] cannot connect to the D-Bus session or system bus: %s", exc.message)
            return
        source = build_source(settings)
        source.start()
        worker = ThreadWorker()
        controller = PreviewController(
            source,
            settings,
            open_monitor_windows,
            ImageScaler(),
            clock=GLibClock(),
            worker=worker,
            animations=animations_enabled,
        )
        state["source"], state["worker"] = source, worker
        try:
            state["service"] = build_service(
                session,
                system,
                settings,
                PreviewSlideshow(controller, source, dbus_adapters.GnomeShellOverview(session)),
            )
        except Exception as exc:
            # Any failure, not a list of known ones: a GLib.Error from a bus call that is not an
            # UnsupportedSessionInterface would otherwise leave the held application running
            # with no sleep guard, exit status 0 and nothing in the log.
            fail("[sleep-inhibit] the lock before suspend cannot be set up (%s): not running", exc)
            return
        _LOG.info("[config] service running (stored settings are not written)")

    app.connect("activate", on_activate)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signum, lambda: app.quit() or GLib.SOURCE_REMOVE
        )
    status = app.run([sys.argv[0]])
    if state["service"] is not None:
        state["service"].close()
    if state["source"] is not None:
        state["source"].stop()
    if state["worker"] is not None:
        state["worker"].close()
    _LOG.info("[config] service stopped")
    return state["status"] or status


if __name__ == "__main__":
    sys.exit(main())
