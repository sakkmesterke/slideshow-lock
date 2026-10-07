"""``slideshow_lock.control``: ``slideshowlock`` starts the service, then opens the settings window.

The wiring is run here, not only set: the real ``main`` and ``start_service`` run against a fake
systemd manager and the real ``Gio.Settings`` (the memory backend of ``conftest.py``); only the
settings window (``settings_app.main``, it needs a display) and the bus are stand-ins. The same
code against the fake ``org.freedesktop.systemd1`` on a private bus is ``test_control_dbus.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

from slideshow_lock import control, sample_pictures
from slideshow_lock.settings import (
    KEY_FIRST_RUN_DONE,
    KEY_PICTURE_FOLDER,
    Settings,
    default_picture_folder,
)
from tests.sample_fixtures import isolate_sample_pictures, make_source

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
def env(monkeypatch, settings, tmp_path):
    """``control`` on stand-ins: ``env.calls`` is every step in order (the unit calls and
    ``window``), ``env.window_argv`` what the window was given, ``env.first_run_at_window`` the
    value of ``first-run-done`` at the moment the window was opened."""

    class Env:
        share = None
        root = None
        calls = []
        window_argv = None
        first_run_at_window = None
        window_status = 0
        manager_kwargs = {}

    env = Env()
    env.calls = []
    env.manager_kwargs = {}

    # no real home, no real /usr/share: the package of the sample pictures is an empty folder, so
    # the copy is a no-op by construction (a test that wants one makes it, see ``copying``)
    env_root = tmp_path / "env"
    env.share = isolate_sample_pictures(monkeypatch, env_root)
    env.root = env_root
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


# -- the sample pictures ---------------------------------------------------------------------------


def test_the_tests_of_the_command_never_see_the_real_home_or_the_real_package(env):
    assert os.environ["HOME"].startswith(str(env.root))
    assert os.environ["XDG_STATE_HOME"].startswith(str(env.root))
    assert sample_pictures.find_source_dir() is None


class Copy:
    """What the copy of the sample pictures is given and when: ``install`` is a stand-in that notes
    its arguments and, when told to, waits on an event."""

    def __init__(self):
        self.calls = []
        self.threads = []
        self.first_run_done_at_start = []
        self.finished = []
        self.go = threading.Event()
        self.started = threading.Event()
        self.block = False
        self.fail = None
        self.stopped_seen = threading.Event()

    def install(self, source_dir, pictures_dir, state_file, **kwargs):
        self.calls.append((source_dir, pictures_dir, state_file))
        self.threads.append(threading.current_thread())
        self.first_run_done_at_start.append(Settings().get_first_run_done())
        self.started.set()
        should_stop = kwargs["should_stop"]
        if self.block:
            while not should_stop() and not self.go.is_set():
                time.sleep(0.001)
            if should_stop():
                self.stopped_seen.set()
        if self.fail:
            raise self.fail
        self.finished.append(True)
        return sample_pictures.Result(sample_pictures.DONE)


@pytest.fixture
def copying(env, monkeypatch):
    """A package with pictures in the (isolated) data dir, and a stand-in for ``install``."""
    make_source(env.root)
    copy = Copy()
    monkeypatch.setattr(control.sample_pictures, "install", copy.install)
    window = control.settings_app.main

    def window_once_the_copy_runs(argv=None):
        copy.started.wait(5)  # a real window takes time: the copy thread has started by then
        return window(argv)

    monkeypatch.setattr(control.settings_app, "main", window_once_the_copy_runs)
    yield copy
    copy.go.set()  # a thread that waits does not outlive the test


def test_the_copy_gets_three_plain_strings_and_runs_on_a_daemon_thread_of_its_own(copying):
    thread = control.ensure_sample_pictures()
    thread.join(5)
    [(source, pictures, state)] = copying.calls
    assert source == sample_pictures.find_source_dir()
    assert pictures == default_picture_folder()
    assert state == sample_pictures.state_path()
    assert all(type(value) is str for value in (source, pictures, state))
    assert copying.threads == [thread] and thread is not threading.main_thread()
    assert thread.daemon and thread.name == "sample-pictures"


def test_without_the_schema_nothing_is_touched_and_no_thread_starts(copying, monkeypatch):
    monkeypatch.setattr(control, "schema_installed", lambda: False)

    def boom(*args, **kwargs):
        raise AssertionError("Gio.Settings aborts the process when the schema is missing")

    monkeypatch.setattr(control, "Settings", boom)
    assert control.ensure_sample_pictures() is None
    assert copying.calls == []


def test_another_picture_folder_means_no_copy(copying, env):
    env.settings.set_picture_folder("/home/user/Pictures/Holiday")
    assert control.ensure_sample_pictures() is None
    assert copying.calls == []


def test_the_default_folder_written_out_is_still_the_default_and_is_copied_into(copying, env):
    env.settings.set_picture_folder(default_picture_folder() + "/")
    control.ensure_sample_pictures().join(5)
    assert copying.calls[0][1] == default_picture_folder()


def test_no_package_means_no_thread(env, monkeypatch):
    copy = Copy()
    monkeypatch.setattr(control.sample_pictures, "install", copy.install)
    assert control.ensure_sample_pictures() is None
    assert copy.calls == []


def test_the_copy_never_writes_the_picture_folder_key_and_the_first_run_answer_is_the_same(
    copying, env
):
    answer_without = control.first_run_window_wanted()
    env.settings._settings.reset(KEY_FIRST_RUN_DONE)
    control.ensure_sample_pictures().join(5)
    assert env.settings._settings.get_user_value(KEY_PICTURE_FOLDER) is None
    assert env.settings.has_chosen_picture_folder() is False
    assert control.first_run_window_wanted() == answer_without is True


def test_the_menu_start_copies_too_and_opens_the_window(copying, env):
    assert control.main([]) == 0
    assert env.calls[-1] == "window" and len(copying.calls) == 1


def test_the_login_start_without_a_window_waits_for_the_copy(copying, env):
    env.settings.set_first_run_done(True)
    copying.block = True
    threading.Timer(0.05, copying.go.set).start()
    assert control.main(["autostart"]) == 0
    assert copying.finished == [True]  # the process does not end with the copy half done
    assert "window" not in env.calls


def test_the_first_login_decides_the_window_before_the_copy_starts(copying, env):
    assert control.main(["autostart"]) == 0
    assert env.calls[-1] == "window"
    assert copying.first_run_done_at_start == [True]  # first-run-done was set before the copy ran


def test_a_copy_that_never_ends_does_not_hold_the_login_start_or_change_its_status(
    copying, env, monkeypatch, caplog
):
    env.settings.set_first_run_done(True)
    copying.block = True
    monkeypatch.setattr(control, "COPY_JOIN_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
        assert control.main(["autostart"]) == 0  # the service was started: 0
    assert any("[samples] the copy is still running" in m for m in caplog.messages)
    assert copying.stopped_seen.wait(5)  # and it was told to stop


def test_the_exit_status_does_not_depend_on_the_copy(copying, env, caplog):
    env.settings.set_first_run_done(True)
    env.manager_kwargs = {"fail_start": RuntimeError("no such unit")}
    assert control.main(["autostart"]) == 1  # not started, copied
    assert copying.finished == [True]
    env.manager_kwargs = {}
    copying.fail = OSError("the copy broke")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
        assert control.main(["autostart"]) == 0  # started, the copy failed
    assert any(
        "[samples] the sample pictures were not copied (OSError)" in m for m in caplog.messages
    )


class NoThread(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


def _find_source_fails():
    raise OSError("no package")


def _uses_default_folder_fails(self):
    raise RuntimeError("no settings")


@pytest.mark.parametrize("window", [True, False], ids=["with the window", "without the window"])
@pytest.mark.parametrize(
    "target, name, broken, error",
    [
        (
            control.Settings,
            "uses_default_picture_folder",
            _uses_default_folder_fails,
            "RuntimeError",
        ),
        (sample_pictures, "find_source_dir", _find_source_fails, "OSError"),
        (control.threading, "Thread", NoThread, "RuntimeError"),
    ],
    ids=["the settings", "the source", "the thread"],
)
def test_a_copy_that_cannot_even_start_is_one_warning_and_the_command_goes_on(
    copying, env, monkeypatch, caplog, window, target, name, broken, error
):
    env.settings.set_first_run_done(True)
    env.window_status = 2
    monkeypatch.setattr(target, name, broken)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.control"):
        status = control.main([] if window else ["autostart"])
    assert status == (2 if window else 0)  # the window's, or the service's: never the copy's
    assert ("window" in env.calls) is window
    assert [m for m in caplog.messages if m.startswith("[samples]")] == [
        f"[samples] the sample pictures were not copied ({error})"
    ]


def test_the_window_opens_and_the_command_returns_while_the_copy_is_still_running(copying, env):
    copying.block = True
    assert control.main([]) == 0  # the window is the stand-in: it returns, the copy has not ended
    assert env.calls[-1] == "window"
    assert copying.finished == []
    assert copying.stopped_seen.wait(5)  # the closed window tells the copy to stop


def test_the_window_status_is_the_exit_status_whatever_the_copy_does(copying, env):
    env.window_status = 2
    copying.fail = RuntimeError("x")
    assert control.main([]) == 2


def test_the_copy_starts_after_the_window_decision_and_before_the_window_opens(
    copying, env, monkeypatch
):
    order = []
    real_decision = control.first_run_window_wanted
    real_install = copying.install

    def decision():
        order.append("decision")
        return real_decision()

    monkeypatch.setattr(control, "first_run_window_wanted", decision)
    monkeypatch.setattr(
        control.sample_pictures,
        "install",
        lambda *args, **kwargs: order.append("copy") or real_install(*args, **kwargs),
    )
    monkeypatch.setattr(
        control.settings_app,
        "main",
        lambda argv=None: copying.started.wait(5) and order.append("window") or 0,
    )
    control.main(["autostart"])
    assert order == ["decision", "copy", "window"]
