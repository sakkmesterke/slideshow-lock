"""``slideshow_lock.settings_app``: what the lock side gives the settings window's Preview button.
The window itself, and that it calls the callback, is ``test_preferences.py``; the callback against
a fake ``org.gnome.Shell`` is in ``test_dbus_adapters.py``."""

from __future__ import annotations

import logging

from slideshow_lock import settings_app


def test_main_starts_the_settings_window_with_the_overview_step(monkeypatch):
    seen = []
    monkeypatch.setattr(
        settings_app,
        "preferences_main",
        lambda argv=None, before_preview=None: seen.append((argv, before_preview)) or 0,
    )
    assert settings_app.main(["--debug"]) == 0
    assert seen == [(["--debug"], settings_app.close_overview)]


def test_close_overview_asks_the_overview_adapter_on_the_session_bus(monkeypatch):
    calls = []
    bus = object()

    class Overview:
        def __init__(self, conn):
            calls.append(("made", conn))

        def close_if_open(self):
            calls.append("close_if_open")

    monkeypatch.setattr(settings_app.dbus_adapters, "session_bus", lambda: bus)
    monkeypatch.setattr(settings_app.dbus_adapters, "GnomeShellOverview", Overview)
    settings_app.close_overview()
    assert calls == [("made", bus), "close_if_open"]


def test_without_a_session_bus_the_step_is_one_warning_and_no_exception(monkeypatch, caplog):
    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(settings_app.dbus_adapters, "session_bus", no_bus)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings_app"):
        settings_app.close_overview()
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "the overview is not closed before the preview" in warning.getMessage()
