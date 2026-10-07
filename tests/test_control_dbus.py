"""``slideshow_lock.control`` through the real ``SystemdUserManager`` to a fake
``org.freedesktop.systemd1`` on a private bus (``tests/fake_dbus.py``).

This proves the calls on the wire: the method names, the unit, the mode. It does not prove what
the user's real systemd does with them (``StartUnit`` of a disabled unit, a unit that is not
loaded, the user manager on the session bus): that is the manual test list. Marked
``spawns_processes`` for the module (``dbus-daemon``; see ``test_tripwire.py``).
"""

from __future__ import annotations

import logging
import os

import pytest

from slideshow_lock import control
from slideshow_lock.settings import KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER, Settings
from tests.fake_dbus import Desktop, dbus_daemon_available
from tests.sample_fixtures import isolate_sample_pictures

pytestmark = [
    pytest.mark.spawns_processes,
    pytest.mark.skipif(
        not dbus_daemon_available() and not os.environ.get("CI"),
        reason="no dbus-daemon on this machine",
    ),
]

UNIT = "slideshow-lock.service"


@pytest.fixture
def window(monkeypatch, tmp_path):
    """The settings window stands in (it needs a display): the arguments of each call. No real
    home and no real package folder either: the sample pictures are never copied from here."""
    isolate_sample_pictures(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(control.settings_app, "main", lambda argv=None: calls.append(argv) or 0)
    settings = Settings()
    for key in (KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER):
        settings._settings.reset(key)
    yield calls
    for key in (KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER):
        settings._settings.reset(key)


def _on_bus(monkeypatch, desktop):
    monkeypatch.setattr(control.dbus_adapters, "session_bus", lambda: desktop.session)


def test_slideshowlock_resets_and_starts_the_unit_on_the_user_manager_and_opens_the_window(
    monkeypatch, window
):
    with Desktop() as desktop:
        _on_bus(monkeypatch, desktop)
        assert control.main([]) == 0
        assert desktop.systemd_calls == [
            ("ResetFailedUnit", (UNIT,)),
            ("StartUnit", (UNIT, "replace")),
        ]
    assert window == [[]]


def test_the_login_start_does_the_same_calls_and_opens_the_window_only_the_first_time(
    monkeypatch, window
):
    with Desktop() as desktop:
        _on_bus(monkeypatch, desktop)
        assert control.main(["autostart"]) == 0
        assert control.main(["autostart"]) == 0
        assert [c[0] for c in desktop.systemd_calls] == ["ResetFailedUnit", "StartUnit"] * 2
    assert window == [[]]


def test_a_unit_that_is_not_loaded_is_still_started(monkeypatch, window):
    with Desktop() as desktop:
        desktop.systemd_errors["ResetFailedUnit"] = "org.freedesktop.systemd1.NoSuchUnit"
        _on_bus(monkeypatch, desktop)
        assert control.start_service() is True
        assert [c[0] for c in desktop.systemd_calls] == ["ResetFailedUnit", "StartUnit"]


def test_a_refused_start_is_a_warning_and_the_window_opens_all_the_same(
    monkeypatch, window, caplog
):
    with Desktop() as desktop:
        desktop.systemd_errors["StartUnit"] = "org.freedesktop.DBus.Error.AccessDenied"
        _on_bus(monkeypatch, desktop)
        with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
            assert control.main([]) == 0
    assert window == [[]]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "the service is not started: systemd refused StartUnit(slideshow-lock.service)" in (
        warning.getMessage()
    )


def test_without_a_user_manager_on_the_bus_the_login_start_fails_and_says_so(
    monkeypatch, window, caplog
):
    Settings().set_first_run_done(True)  # nothing to show: only the service matters
    with Desktop(systemd=False) as desktop:
        _on_bus(monkeypatch, desktop)
        with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
            assert control.main(["autostart"]) == 1
    assert window == []
    assert any("the service is not started" in m for m in caplog.messages)
