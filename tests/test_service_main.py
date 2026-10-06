"""``service.main`` with the wiring replaced: what the program does when the lock before suspend
cannot be set up. The GTK application and its main loop are the real ones; the buses, the picture
source, the preview controller and ``build_service`` are stand-ins, so nothing here needs a
desktop.

The wording is "automated tests green, live verification pending".
"""

from __future__ import annotations

import logging

import gi
import pytest

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import service  # noqa: E402
from tests.timeout_guard import per_test_deadline  # noqa: E402,F401

_REAL_APPLICATION = Gtk.Application

#: A program that does not end by itself is stopped after this long. The main loop is C code, so
#: a signal alarm would not reach Python while it runs: the test quits the application itself.
HANG_SECONDS = 5


class _Source:
    def __init__(self):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _Program:
    def __init__(self):
        self.source = _Source()
        self.hung = False
        self.watchdogs = []


@pytest.fixture
def program(monkeypatch):
    """``service.main`` runnable in a test: no session bus, no windows, a source that records."""
    prog = _Program()
    monkeypatch.setattr(service.dbus_adapters, "session_bus", lambda: object())
    monkeypatch.setattr(service.dbus_adapters, "system_bus", lambda: object())
    monkeypatch.setattr(service, "build_source", lambda settings: prog.source)
    monkeypatch.setattr(service, "PreviewController", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        service, "PreviewSlideshow", lambda controller, source, overview=None: object()
    )

    def application(application_id):
        # NON_UNIQUE: the test must not claim the application id on a session bus that may exist
        app = _REAL_APPLICATION(
            application_id=application_id, flags=Gio.ApplicationFlags.NON_UNIQUE
        )

        def hang():
            prog.hung = True
            app.quit()
            return GLib.SOURCE_REMOVE

        prog.watchdogs.append(GLib.timeout_add_seconds(HANG_SECONDS, hang))
        return app

    monkeypatch.setattr(service.Gtk, "Application", application)
    yield prog
    for watchdog in prog.watchdogs:
        source = GLib.MainContext.default().find_source_by_id(watchdog)
        if source is not None:
            source.destroy()


@pytest.mark.parametrize(
    "error",
    [
        GLib.Error("the bus call failed"),
        OSError("no logind"),
        TimeoutError("the guard did not come up"),
        RuntimeError("anything else"),
    ],
    ids=["glib-error", "os-error", "timeout", "other"],
)
def test_a_failed_sleep_path_setup_ends_the_program_with_status_1_and_an_error(
    program, monkeypatch, caplog, error
):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(service, "build_service", broken)
    with caplog.at_level(logging.ERROR):
        status = service.main(["--folder", "/nonexistent"])
    assert not program.hung, "the application kept running after the failed set-up"
    assert status == 1
    assert any("the lock before suspend cannot be set up" in m for m in caplog.messages)
    assert program.source.started and program.source.stopped


def test_a_working_set_up_keeps_the_program_running_until_it_is_asked_to_stop(program, monkeypatch):
    """Control: the same program with a wiring that succeeds does not end by itself, so the exit
    above is the failure path's doing. SIGTERM ends it, with status 0."""
    closed = []

    class Stub:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(service, "build_service", lambda *args, **kwargs: Stub())
    GLib.timeout_add(300, lambda: service.signal.raise_signal(service.signal.SIGTERM) or False)
    status = service.main(["--folder", "/nonexistent"])
    assert not program.hung
    assert status == 0
    assert closed == [True]
    assert program.source.stopped
