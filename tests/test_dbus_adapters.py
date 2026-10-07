"""The real adapters against a fake desktop on private D-Bus daemons (CORE-1).

What this proves: the adapters speak the interfaces of ``docs/architecture/dbus-state-machine.md``
the way that document says (method names, argument types, flag 8, signals, the unix fd of the
delay inhibitor). What it does not: that GNOME's shell, session manager and mutter answer the same
way. That is the manual test list, and the wording stays "automated tests green, live
verification pending".

The tests start ``dbus-daemon`` and are marked ``spawns_processes`` for the whole module (the
tripwire of ``conftest.py`` stands down; ``test_tripwire.py`` lists this file). They are skipped
where there is no ``dbus-daemon``.
"""

from __future__ import annotations

import logging
import os
import time

import pytest
from gi.repository import Gio, GLib

from slideshow_lock import dbus_adapters as adapters
from slideshow_lock.session import LockResult, UnsupportedSessionInterface
from tests.fake_dbus import Desktop, dbus_daemon_available, wait_for

pytestmark = [
    pytest.mark.spawns_processes,
    # CI installs it and checks for it; a skip there would be a silent loss (the no-skip gate)
    pytest.mark.skipif(
        not dbus_daemon_available() and not os.environ.get("CI"),
        reason="no dbus-daemon on this machine",
    ),
]


@pytest.fixture
def desktop():
    with Desktop() as d:
        yield d


# -- the idle monitor -----------------------------------------------------------------------------


def test_the_idle_watch_is_added_in_milliseconds_and_its_signal_calls_back(desktop):
    idle = adapters.MutterIdleWatcher(desktop.session)
    fired = []
    idle.on_idle(2.5, lambda: fired.append("idle"))
    assert desktop.idle_timeouts == [2500]
    desktop.fire_idle()
    assert wait_for(lambda: fired == ["idle"])
    desktop.fire_idle()  # the idle watch keeps firing until it is cancelled
    assert wait_for(lambda: fired == ["idle", "idle"])
    idle.close()


def test_a_new_idle_watch_replaces_the_old_one_and_cancel_removes_it(desktop):
    idle = adapters.MutterIdleWatcher(desktop.session)
    idle.on_idle(10, lambda: None)
    first = next(iter(desktop.watches))
    idle.on_idle(20, lambda: None)
    assert first in desktop.removed
    assert list(desktop.watches.values()) == ["idle"]
    idle.cancel_idle()
    assert desktop.watches == {}
    idle.close()


def test_a_user_active_watch_calls_back_once_and_can_be_cancelled(desktop):
    idle = adapters.MutterIdleWatcher(desktop.session)
    seen = []
    idle.on_user_active(lambda: seen.append(1))
    assert list(desktop.watches.values()) == ["active"]
    desktop.fire_user_active()
    assert wait_for(lambda: seen == [1])
    desktop.fire_user_active()  # nothing is registered any more
    assert not wait_for(lambda: len(seen) > 1, timeout=0.3)
    cancel = idle.on_user_active(lambda: seen.append(2))
    watch = next(iter(desktop.watches))
    cancel()
    assert watch in desktop.removed and desktop.watches == {}
    idle.close()


def test_a_missing_idle_monitor_is_named_by_the_probe():
    with Desktop(idle_monitor=False) as d:
        with pytest.raises(UnsupportedSessionInterface) as caught:
            adapters.MutterIdleWatcher(d.session)
    assert caught.value.interface == "org.gnome.Mutter.IdleMonitor"


# -- the session manager --------------------------------------------------------------------------


def test_idle_inhibition_is_asked_with_flag_8_and_changes_are_reported_once(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    seen = []
    inhibition.on_idle_inhibit_changed(seen.append)
    assert inhibition.is_idle_inhibited() is False
    desktop.set_inhibited(True)
    assert wait_for(lambda: seen == [True])
    assert inhibition.is_idle_inhibited() is True
    desktop.set_inhibited(True)  # another inhibitor, no change in the answer
    desktop.set_inhibited(False)
    assert wait_for(lambda: seen == [True, False])
    inhibition.close()


# -- the idle inhibitor the service holds while its slideshow shows -------------------------------


def _quiet(predicate, timeout=0.4):
    """True if *predicate* stays False for *timeout* seconds while the main loop turns."""
    return not wait_for(predicate, timeout)


def test_the_idle_inhibitor_is_taken_with_flag_8_under_the_application_id_and_given_back(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    assert len(desktop.inhibit_requests) == 1
    app_id, xid, reason, flags = desktop.inhibit_requests[0]
    assert (app_id, xid, flags) == (adapters.INHIBIT_APP_ID, 0, 8)
    assert reason
    assert len(desktop.inhibitors_of(adapters.INHIBIT_APP_ID)) == 1
    inhibition.release_idle_inhibit()
    assert desktop.uninhibit_calls == [100]  # the cookie the session manager handed out
    assert desktop.inhibitors_of(adapters.INHIBIT_APP_ID) == []
    inhibition.close()


def test_holding_twice_takes_one_inhibitor_and_releasing_when_not_held_does_nothing(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.release_idle_inhibit()
    assert desktop.uninhibit_calls == []
    inhibition.hold_idle_inhibit()
    inhibition.hold_idle_inhibit()
    assert len(desktop.inhibit_requests) == 1
    inhibition.release_idle_inhibit()
    inhibition.release_idle_inhibit()
    assert len(desktop.uninhibit_calls) == 1
    inhibition.close()


def test_the_own_idle_inhibitor_is_neither_counted_nor_reported(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    seen = []
    inhibition.on_idle_inhibit_changed(seen.append)
    inhibition.hold_idle_inhibit()
    assert len(desktop.inhibitors_of(adapters.INHIBIT_APP_ID)) == 1
    assert _quiet(lambda: seen)  # the InhibitorAdded of our own is no change for the machine
    assert inhibition.is_idle_inhibited() is False
    inhibition.release_idle_inhibit()
    assert _quiet(lambda: seen)
    assert inhibition.is_idle_inhibited() is False
    inhibition.close()


def test_another_application_next_to_the_own_inhibitor_is_still_a_change_both_ways(desktop):
    """The own inhibitor makes ``IsInhibited(8)`` true for good; a foreign one next to it must
    still turn the answer to True, and its removal back to False."""
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    seen = []
    inhibition.on_idle_inhibit_changed(seen.append)
    inhibition.hold_idle_inhibit()
    foreign = desktop.add_inhibitor("video.call")
    assert wait_for(lambda: seen == [True])
    assert inhibition.is_idle_inhibited() is True
    desktop.remove_inhibitor(foreign)
    assert wait_for(lambda: seen == [True, False])
    assert inhibition.is_idle_inhibited() is False
    assert len(desktop.inhibitors_of(adapters.INHIBIT_APP_ID)) == 1  # ours stayed all along
    inhibition.close()


def test_a_foreign_inhibitor_that_was_there_first_stays_true_through_hold_and_release(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    seen = []
    inhibition.on_idle_inhibit_changed(seen.append)
    desktop.add_inhibitor("video.call")
    assert wait_for(lambda: seen == [True])
    inhibition.hold_idle_inhibit()
    inhibition.release_idle_inhibit()
    assert _quiet(lambda: len(seen) > 1)
    assert inhibition.is_idle_inhibited() is True
    inhibition.close()


@pytest.mark.parametrize("flags", [1, 2, 4])
def test_a_foreign_inhibitor_that_does_not_inhibit_idle_is_not_counted(desktop, flags):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    desktop.add_inhibitor("editor", flags=flags)
    assert _quiet(lambda: inhibition.is_idle_inhibited())
    inhibition.close()


def test_a_foreign_inhibitor_with_both_idle_and_other_flags_is_counted(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    desktop.add_inhibitor("video.call", flags=4 | 8)
    assert inhibition.is_idle_inhibited() is True
    inhibition.close()


def test_an_own_inhibitor_the_service_lost_track_of_never_counts_as_foreign(desktop):
    """If giving the inhibitor back failed once, it may still be on the bus: it must not keep the
    next slideshow from starting."""
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    inhibition._cookie = 424242  # the cookie the session manager does not know
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()
    assert len(desktop.inhibitors_of(adapters.INHIBIT_APP_ID)) == 1  # still there
    assert inhibition.is_idle_inhibited() is False
    inhibition.close()


def test_a_refused_release_keeps_the_cookie_so_that_the_next_call_gives_it_back(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    desktop.uninhibit_failures = 1
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()
    assert len(desktop.inhibitors_of(adapters.INHIBIT_APP_ID)) == 1  # still on the bus
    inhibition.hold_idle_inhibit()  # still held: no second inhibitor
    assert len(desktop.inhibit_requests) == 1
    inhibition.release_idle_inhibit()
    assert desktop.uninhibit_calls == [100, 100]  # the same cookie, twice
    assert desktop.inhibitors_of(adapters.INHIBIT_APP_ID) == []
    inhibition.close()


def test_a_refused_release_of_an_inhibitor_the_session_manager_no_longer_lists_is_done(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    (path,) = desktop.inhibitors_of(adapters.INHIBIT_APP_ID)
    desktop.remove_inhibitor(path)  # gone on its own (the session manager dropped it)
    desktop.uninhibit_failures = 1
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()
    inhibition.release_idle_inhibit()  # nothing is held any more: no further call
    assert len(desktop.uninhibit_calls) == 1
    inhibition.close()


def test_a_refused_release_stays_held_when_the_session_manager_cannot_be_asked_either(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    desktop.uninhibit_failures = 1
    desktop.inhibitor_errors["GetAppId"] = "org.freedesktop.DBus.Error.Failed"
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()
    desktop.inhibitor_errors.clear()
    inhibition.release_idle_inhibit()  # the cookie was kept: this one goes through
    assert desktop.uninhibit_calls == [100, 100]
    assert desktop.inhibitors_of(adapters.INHIBIT_APP_ID) == []
    inhibition.close()


def test_a_refused_release_stays_held_when_the_inhibitor_list_cannot_be_read(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    desktop.uninhibit_failures = 1
    desktop.session_manager_errors["GetInhibitors"] = "org.freedesktop.DBus.Error.Failed"
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()
    desktop.session_manager_errors.clear()
    inhibition.release_idle_inhibit()  # the cookie was kept: this one goes through
    assert desktop.uninhibit_calls == [100, 100]
    assert desktop.inhibitors_of(adapters.INHIBIT_APP_ID) == []
    inhibition.close()


def test_an_inhibitor_that_cannot_be_read_is_not_taken_for_gone(desktop):
    """Fail closed: if the flags of a listed inhibitor cannot be read, the question is not
    answered (the state machine then starts no slideshow), it is not answered with False."""
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    desktop.add_inhibitor("video.call")
    desktop.inhibitor_errors["GetFlags"] = "org.freedesktop.DBus.Error.Failed"
    with pytest.raises(GLib.Error):
        inhibition.is_idle_inhibited()
    desktop.inhibitor_errors["GetFlags"] = "org.freedesktop.DBus.Error.UnknownMethod"
    with pytest.raises(GLib.Error):  # a missing method on an object that is there
        inhibition.is_idle_inhibited()
    inhibition.close()


@pytest.mark.parametrize("how", ["unknown_object_error", "no_such_object_on_the_bus"])
def test_an_inhibitor_that_went_away_between_the_list_and_the_question_is_skipped(desktop, how):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    if how == "unknown_object_error":
        desktop.add_inhibitor("video.call")
        desktop.inhibitor_errors["GetFlags"] = "org.freedesktop.DBus.Error.UnknownObject"
    else:  # listed once, then gone: GDBus answers UnknownMethod for the path that is not there
        desktop.ghost_inhibitors["/org/gnome/SessionManager/Inhibitor9"] = 8
    assert inhibition.is_idle_inhibited() is False
    inhibition.close()


def _dbus_error(name: str, text: str) -> GLib.Error:
    """An error as a D-Bus call raises it (the remote name in front of the text)."""
    return Gio.DBusError.new_for_dbus_error(name, text)


#: What GDBus of GLib answers for an object path that is not there, in the languages measured
#: (``LANGUAGE`` of the process that owns the object; the English text is the one the fake gives
#: in a test run).
GONE_TEXTS = {
    "en": "Object does not exist at path \u201c/org/gnome/SessionManager/Inhibitor9\u201d",
    "hu": (
        "Az objektum nem l\u00e9tezik a(z) \u201e/org/gnome/SessionManager/Inhibitor9\u201d "
        "\u00fatvonalon"
    ),
    "de": "Das Objekt existiert nicht am Pfad \u00bb/org/gnome/SessionManager/Inhibitor9\u00ab",
}
GONE_PATH = "/org/gnome/SessionManager/Inhibitor9"


@pytest.mark.parametrize("language", sorted(GONE_TEXTS))
def test_a_vanished_inhibitor_is_recognised_by_the_error_name_in_any_language(desktop, language):
    """The session manager translates the text ("Az objektum nem l\u00e9tezik ..." with a Hungarian
    session: measured), so the text decides nothing: UnknownMethod and a path that a fresh
    list no longer has."""
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    error = _dbus_error("org.freedesktop.DBus.Error.UnknownMethod", GONE_TEXTS[language])
    assert inhibition._inhibitor_is_gone(error, GONE_PATH) is True
    inhibition.close()


def test_unknown_method_for_an_inhibitor_that_is_still_listed_is_not_gone(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    path = desktop.add_inhibitor("video.call")
    for text in GONE_TEXTS.values():  # whatever it says: the path is there, the method is not
        error = _dbus_error("org.freedesktop.DBus.Error.UnknownMethod", text)
        assert inhibition._inhibitor_is_gone(error, path) is False
    inhibition.close()


def test_unknown_method_with_a_list_that_cannot_be_read_is_not_gone(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    desktop.session_manager_errors["GetInhibitors"] = "org.freedesktop.DBus.Error.Failed"
    error = _dbus_error("org.freedesktop.DBus.Error.UnknownMethod", GONE_TEXTS["hu"])
    assert inhibition._inhibitor_is_gone(error, GONE_PATH) is False
    desktop.session_manager_errors.clear()
    inhibition.close()


@pytest.mark.parametrize(
    "name",
    ["org.freedesktop.DBus.Error.Failed", "org.freedesktop.DBus.Error.NoReply", "x.y.Odd"],
)
def test_any_other_error_name_is_not_gone_whatever_the_text_says(desktop, name):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    for text in GONE_TEXTS.values():
        assert inhibition._inhibitor_is_gone(_dbus_error(name, text), GONE_PATH) is False
    inhibition.close()


def test_unknown_object_is_gone_without_asking_for_a_list(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    desktop.session_manager_errors["GetInhibitors"] = "org.freedesktop.DBus.Error.Failed"
    error = _dbus_error("org.freedesktop.DBus.Error.UnknownObject", "no such object")
    assert inhibition._inhibitor_is_gone(error, GONE_PATH) is True
    desktop.session_manager_errors.clear()
    inhibition.close()


def test_a_refused_release_drops_the_cookie_when_the_only_listed_inhibitor_vanished(desktop):
    """The check of ``_own_inhibitor_is_gone`` skips a vanished entry by the same rule: ours is
    not listed any more, the other entry is listed once and its object is gone."""
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    (own,) = desktop.inhibitors_of(adapters.INHIBIT_APP_ID)
    desktop.uninhibit_failures = 1
    desktop.remove_inhibitor(own)
    desktop.ghost_inhibitors[GONE_PATH] = 8
    with pytest.raises(GLib.Error):
        inhibition.release_idle_inhibit()  # the refusal is still raised once
    inhibition.release_idle_inhibit()  # the cookie was dropped: nothing more to give back
    assert desktop.uninhibit_calls == [100]
    inhibition.close()


def test_close_gives_the_idle_inhibitor_back(desktop):
    inhibition = adapters.SessionManagerInhibition(desktop.session)
    inhibition.hold_idle_inhibit()
    inhibition.close()
    assert desktop.inhibitors_of(adapters.INHIBIT_APP_ID) == []


# -- locking --------------------------------------------------------------------------------------


def test_the_screensaver_lock_is_asynchronous_and_reports_the_answer(desktop):
    lock = adapters.make_session_lock(desktop.session, desktop.system)
    assert isinstance(lock, adapters.ScreenSaverLock)
    results, states = [], []
    lock.on_active_changed(states.append)
    assert lock.is_active() is False
    desktop.lock_delay = 0.3
    lock.lock(results.append)
    assert results == []  # it did not wait for the answer
    assert wait_for(lambda: results == [LockResult(True)])
    assert desktop.lock_calls and lock.is_active() is True
    assert wait_for(lambda: states == [True])
    desktop.set_locked(False)
    assert wait_for(lambda: states == [True, False])
    lock.close()


def test_a_screensaver_that_refuses_is_a_failed_lock_result_not_an_exception(desktop):
    lock = adapters.ScreenSaverLock(desktop.session)
    desktop.lock_error = "screen shield not ready"
    results = []
    lock.lock(results.append)
    assert wait_for(lambda: len(results) == 1)
    assert results[0].ok is False and "screen shield not ready" in results[0].error
    lock.close()


def test_without_a_screensaver_login1_session_lock_is_the_fallback():
    with Desktop(screensaver=False) as d:
        lock = adapters.make_session_lock(d.session, d.system)
        assert isinstance(lock, adapters.Login1SessionLock)
        results, states = [], []
        lock.on_active_changed(states.append)
        assert lock.is_active() is False
        lock.lock(results.append)
        assert wait_for(lambda: results == [LockResult(True)])
        assert len(d.session_lock_calls) == 1 and d.lock_calls == []
        assert wait_for(lambda: states == [True]) and lock.is_active() is True
        lock.close()


def test_with_neither_facility_the_probe_names_both():
    with Desktop(screensaver=False) as d:
        # the "system bus" given here has no login1 either
        with pytest.raises(UnsupportedSessionInterface) as caught:
            adapters.make_session_lock(d.session, d.session)
    assert caught.value.interface == "ScreenSaver+login1.Session"


# -- logind ---------------------------------------------------------------------------------------


def test_the_delay_inhibitor_is_a_sleep_delay_lock_whose_fd_release_lets_go(desktop):
    sleep = adapters.Login1Sleep(desktop.system)
    assert sleep.inhibit_delay_max() == 5.0
    inhibitor = sleep.acquire_delay_inhibitor()
    what, who, why, mode = desktop.inhibit_calls[0]
    assert (what, mode) == ("sleep", "delay") and who and why
    assert desktop.held == 1
    inhibitor.release()
    assert wait_for(lambda: desktop.held == 0)  # the fake's own copy goes when its message does
    inhibitor.release()  # idempotent
    sleep.close()


def test_the_delay_ceiling_is_read_from_the_running_logind(desktop):
    desktop.delay_max_usec = 12_500_000
    assert adapters.Login1Sleep(desktop.system).inhibit_delay_max() == 12.5


def test_prepare_for_sleep_true_and_false_reach_the_callback(desktop):
    sleep = adapters.Login1Sleep(desktop.system)
    seen = []
    sleep.on_prepare_for_sleep(seen.append)
    desktop.prepare_for_sleep(True)
    desktop.prepare_for_sleep(False)
    assert wait_for(lambda: seen == [True, False])
    sleep.close()


# -- the shell's overview -------------------------------------------------------------------------


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_open_overview_is_set_to_false_and_waited_for_until_it_is_closed(desktop):
    desktop.overview_active = True
    desktop.overview_close_delay = 0.15  # the closing animation: the property stays true
    overview = adapters.GnomeShellOverview(desktop.session)
    started = time.monotonic()
    overview.close_if_open()
    elapsed = time.monotonic() - started
    assert desktop.overview_active is False
    assert 0.15 <= elapsed < adapters.OVERVIEW_WAIT_S + 0.2
    log = desktop.overview_log
    assert log[0] == ("Get", True) and log[1] == ("Set", False)
    assert log[-1] == ("Get", False)
    assert len(log) > 3  # it read the property again while the animation ran


def test_a_closed_overview_costs_one_read_and_no_write_and_no_wait(desktop):
    overview = adapters.GnomeShellOverview(desktop.session)
    started = time.monotonic()
    overview.close_if_open()
    assert time.monotonic() - started < 0.4
    assert desktop.overview_log == [("Get", False)]


def test_without_a_shell_on_the_bus_nothing_happens_and_nothing_is_warned(caplog):
    with Desktop(shell=False) as d:
        overview = adapters.GnomeShellOverview(d.session)
        with caplog.at_level(logging.DEBUG, logger="slideshow_lock.dbus_adapters"):
            overview.close_if_open()
            overview.close_if_open()
    assert _warnings(caplog) == []
    assert any("no org.gnome.Shell" in m for m in caplog.messages)


def test_a_refused_set_is_one_warning_and_no_exception_however_often_it_happens(desktop, caplog):
    desktop.overview_active = True
    desktop.overview_set_refused = True
    overview = adapters.GnomeShellOverview(desktop.session)
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.dbus_adapters"):
        overview.close_if_open()
        overview.close_if_open()
    assert desktop.overview_active is True
    [warning] = _warnings(caplog)
    assert "[slideshow]" in warning.getMessage() and "starts anyway" in warning.getMessage()


def test_an_overview_that_never_closes_ends_the_wait_at_half_a_second(desktop, caplog):
    desktop.overview_active = True
    desktop.overview_stuck = True
    overview = adapters.GnomeShellOverview(desktop.session)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.dbus_adapters"):
        overview.close_if_open()
    elapsed = time.monotonic() - started
    assert adapters.OVERVIEW_WAIT_S <= elapsed < adapters.OVERVIEW_WAIT_S + 0.4
    [warning] = _warnings(caplog)
    assert "still open" in warning.getMessage()


def test_a_shell_that_does_not_answer_holds_the_caller_for_half_a_second_at_most(desktop, caplog):
    desktop.overview_get_delay = 1.5  # the shell is stuck: it answers long after the deadline
    overview = adapters.GnomeShellOverview(desktop.session)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.dbus_adapters"):
        overview.close_if_open()
    elapsed = time.monotonic() - started
    assert elapsed < adapters.OVERVIEW_WAIT_S + 0.4
    assert len(_warnings(caplog)) == 1


def test_the_overview_step_of_the_settings_window_closes_the_overview_of_the_fake_shell(
    desktop, monkeypatch
):
    """``settings_app.close_overview`` end to end on the fake session bus: the step the Preview
    button of the settings window runs. (Not a real GNOME Shell.)"""
    from slideshow_lock import settings_app

    monkeypatch.setattr(settings_app.dbus_adapters, "session_bus", lambda: desktop.session)
    desktop.overview_active = True
    desktop.overview_close_delay = 0.1
    settings_app.close_overview()
    assert desktop.overview_active is False
    assert desktop.overview_log[:2] == [("Get", True), ("Set", False)]


class _FailingConnection:
    """A connection whose every call fails with *error*."""

    def __init__(self, error):
        self._error = error

    def call_sync(self, *args):
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        GLib.Error("first line\nsecond line " + "x" * 400),
        RuntimeError("first line\nsecond line " + "x" * 400),
    ],
    ids=["glib-error", "other"],
)
def test_the_message_of_a_failed_overview_call_is_logged_on_one_line_and_cut(caplog, error):
    """What the peer says is not trusted: no line break (a forged log line), no 400 characters."""
    overview = adapters.GnomeShellOverview(_FailingConnection(error))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.dbus_adapters"):
        overview.close_if_open()
    [warning] = _warnings(caplog)
    text = warning.getMessage()
    assert "\n" not in text and "first line second line " in text
    assert "x" * 200 not in text and "x" * 100 in text


def test_the_overview_adapter_does_not_raise_whatever_the_connection_is(caplog):
    overview = adapters.GnomeShellOverview(object())  # not a bus connection at all
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.dbus_adapters"):
        overview.close_if_open()
    assert len(_warnings(caplog)) == 1


# -- the user's systemd (the unit start of ``control``) --------------------------------------------


def test_the_unit_is_reset_and_started_on_the_manager_interface_in_replace_mode(desktop):
    manager = adapters.SystemdUserManager(desktop.session)
    manager.reset_failed("slideshow-lock.service")
    job = manager.start("slideshow-lock.service")
    assert desktop.systemd_calls == [
        ("ResetFailedUnit", ("slideshow-lock.service",)),
        ("StartUnit", ("slideshow-lock.service", "replace")),
    ]
    assert job == "/org/freedesktop/systemd1/job/1"


@pytest.mark.parametrize(
    "method, call", [("ResetFailedUnit", "reset_failed"), ("StartUnit", "start")]
)
def test_a_refused_unit_call_raises_the_bus_error_for_the_caller_to_judge(desktop, method, call):
    desktop.systemd_errors[method] = "org.freedesktop.systemd1.NoSuchUnit"
    manager = adapters.SystemdUserManager(desktop.session)
    with pytest.raises(GLib.Error) as raised:
        getattr(manager, call)("slideshow-lock.service")
    assert "NoSuchUnit" in raised.value.message or Gio.DBusError.is_remote_error(raised.value)


def test_without_systemd_on_the_bus_the_call_raises_and_is_not_probed_at_construction():
    with Desktop(systemd=False) as d:
        manager = adapters.SystemdUserManager(d.session)  # nothing is probed when it is made
        with pytest.raises(GLib.Error):
            manager.start("slideshow-lock.service")
