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

import pytest

from slideshow_lock import dbus_adapters as adapters
from slideshow_lock.session import LockResult, UnsupportedSessionInterface
from tests.fake_dbus import Desktop, dbus_daemon_available, wait_for

pytestmark = [
    pytest.mark.spawns_processes,
    pytest.mark.skipif(not dbus_daemon_available(), reason="no dbus-daemon on this machine"),
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
    assert desktop.held == 0
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
