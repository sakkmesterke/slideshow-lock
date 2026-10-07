"""``slideshow_lock.control``: ``slideshowlock`` starts the service, then opens the settings window.

The wiring is run here, not only set: the real ``main`` and ``start_service`` run against a fake
systemd manager and the real ``Gio.Settings`` (the memory backend of ``conftest.py``); only the
settings window (``settings_app.main``, it needs a display) and the bus are stand-ins. The same
code against the fake ``org.freedesktop.systemd1`` on a private bus is ``test_control_dbus.py``.
"""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import control
from slideshow_lock.settings import KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER, Settings

UNIT = "slideshow-lock.service"


class FakeManager:
    """What ``SystemdUserManager`` is to ``control``: the calls it gets, in order, and the
    exceptions it is told to raise."""

    def __init__(self, calls, fail_reset=None, fail_start=None):
        self.calls = calls
        self.fail_reset = fail_reset
        self.fail_start = fail_start

    def reset_failed(self, unit):
        self.calls.append(("reset_failed", unit))
        if self.fail_reset:
            raise self.fail_reset

    def start(self, unit):
        self.calls.append(("start", unit))
        if self.fail_start:
            raise self.fail_start
        return "/job/1"


@pytest.fixture
def settings():
    """The real settings, with the two keys the first start reads back at their defaults."""
    settings = Settings()
    for key in (KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER):
        settings._settings.reset(key)
    yield settings
    for key in (KEY_FIRST_RUN_DONE, KEY_PICTURE_FOLDER):
        settings._settings.reset(key)


@pytest.fixture
def env(monkeypatch, settings):
    """``control`` on stand-ins: ``env.calls`` is every step in order (the unit calls and
    ``window``), ``env.window_argv`` what the window was given, ``env.first_run_at_window`` the
    value of ``first-run-done`` at the moment the window was opened."""

    class Env:
        calls = []
        window_argv = None
        first_run_at_window = None
        window_status = 0
        manager_kwargs = {}

    env = Env()
    env.calls = []
    env.manager_kwargs = {}

    monkeypatch.setattr(control.dbus_adapters, "session_bus", lambda: "the session bus")
    monkeypatch.setattr(
        control.dbus_adapters,
        "SystemdUserManager",
        lambda conn: FakeManager(env.calls, **env.manager_kwargs),
    )

    def window(argv=None):
        env.calls.append("window")
        env.window_argv = argv
        env.first_run_at_window = Settings().get_first_run_done()
        return env.window_status

    monkeypatch.setattr(control.settings_app, "main", window)
    env.settings = settings
    return env


# -- the service -----------------------------------------------------------------------------------


def test_the_service_is_reset_and_then_started(env):
    assert control.start_service() is True
    assert env.calls == [("reset_failed", UNIT), ("start", UNIT)]


def test_a_unit_that_cannot_be_reset_is_still_started(env):
    env.manager_kwargs = {"fail_reset": RuntimeError("Unit slideshow-lock.service not loaded")}
    assert control.start_service() is True
    assert env.calls == [("reset_failed", UNIT), ("start", UNIT)]


def test_a_refused_start_is_one_warning_and_no_exception(env, caplog):
    env.manager_kwargs = {"fail_start": RuntimeError("Access denied")}
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.control"):
        assert control.start_service() is False
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "systemd refused StartUnit(slideshow-lock.service) (Access denied)" in (
        warning.getMessage()
    )


def test_without_a_session_bus_the_service_is_not_started_and_it_is_one_warning(
    env, monkeypatch, caplog
):
    def no_bus():
        raise OSError("no session bus")

    monkeypatch.setattr(control.dbus_adapters, "session_bus", no_bus)
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.control"):
        assert control.start_service() is False
    assert env.calls == []
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "no session bus" in warning.getMessage()


# -- the menu start: the service, then the window --------------------------------------------------


def test_slideshowlock_starts_the_service_and_then_opens_the_window(env):
    assert control.main([]) == 0
    assert env.calls == [("reset_failed", UNIT), ("start", UNIT), "window"]


def test_the_window_opens_when_the_service_cannot_be_started(env):
    env.manager_kwargs = {"fail_start": RuntimeError("no such unit")}
    assert control.main([]) == 0
    assert env.calls[-1] == "window"


def test_the_exit_status_is_the_windows_not_the_services(env):
    env.window_status = 2  # the window's "schema not installed"
    env.manager_kwargs = {"fail_start": RuntimeError("no such unit")}
    assert control.main([]) == 2


def test_debug_goes_on_to_the_window_and_nothing_else_does(env):
    control.main(["--debug"])
    assert env.window_argv == ["--debug"]
    control.main([])
    assert env.window_argv == []


def test_the_menu_start_neither_reads_nor_sets_first_run_done(env):
    assert control.main([]) == 0
    assert env.settings.get_first_run_done() is False
    assert env.first_run_at_window is False
    env.settings.set_first_run_done(True)
    control.main([])  # opens the window again, whatever the key says
    assert env.calls.count("window") == 2


# -- the login start -------------------------------------------------------------------------------


def test_the_first_login_starts_the_service_and_opens_the_window_once(env):
    assert control.main(["autostart"]) == 0
    assert env.calls == [("reset_failed", UNIT), ("start", UNIT), "window"]
    assert env.settings.get_first_run_done() is True
    env.calls.clear()
    assert control.main(["autostart"]) == 0  # the second login: the service only
    assert env.calls == [("reset_failed", UNIT), ("start", UNIT)]


def test_first_run_done_is_set_before_the_window_opens(env):
    control.main(["autostart"])
    assert env.first_run_at_window is True  # a window closed without a choice is not shown again


def test_a_chosen_picture_folder_means_no_window_and_the_key_stays_false(env):
    env.settings.set_picture_folder("/home/user/Pictures/Holiday")
    assert control.main(["autostart"]) == 0
    assert "window" not in env.calls
    assert env.settings.get_first_run_done() is False


def test_an_empty_stored_folder_is_a_choice_too(env):
    env.settings.set_picture_folder("")
    assert control.main(["autostart"]) == 0
    assert "window" not in env.calls


def test_the_login_start_without_a_window_fails_when_the_service_could_not_be_started(env):
    env.settings.set_first_run_done(True)
    env.manager_kwargs = {"fail_start": RuntimeError("no such unit")}
    assert control.main(["autostart"]) == 1
    assert "window" not in env.calls


def test_the_first_login_opens_the_window_when_the_service_cannot_be_started(env):
    env.manager_kwargs = {"fail_start": RuntimeError("no such unit")}
    env.window_status = 0
    assert control.main(["autostart"]) == 0
    assert env.calls[-1] == "window"


def test_a_key_that_cannot_be_stored_is_a_warning_and_the_window_opens(env, monkeypatch, caplog):
    monkeypatch.setattr(Settings, "set_first_run_done", lambda self, value: False)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
        assert control.main(["autostart"]) == 0
    assert env.calls[-1] == "window"
    assert any("first-run-done cannot be stored" in m for m in caplog.messages)


def test_without_the_schema_the_login_start_opens_no_window(env, monkeypatch, caplog):
    monkeypatch.setattr(control.Gio.SettingsSchemaSource, "get_default", staticmethod(lambda: None))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
        assert control.main(["autostart"]) == 0
    assert "window" not in env.calls
    assert any("the settings schema is not installed" in m for m in caplog.messages)


# -- the command line ------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["help"], ["start"], ["--folder", "/x"]])
def test_an_unknown_mode_or_option_is_an_error_and_starts_nothing(env, argv):
    with pytest.raises(SystemExit) as stopped:
        control.main(argv)
    assert stopped.value.code == 2
    assert env.calls == []


def test_help_names_the_command_and_starts_nothing(env, capsys):
    with pytest.raises(SystemExit) as stopped:
        control.main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "usage: slideshowlock" in out
    assert "autostart" in out
    assert env.calls == []
