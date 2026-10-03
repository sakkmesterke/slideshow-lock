"""The state machine against fake adapters (CORE-1). No bus, no GLib loop, no wall clock.

Each test name carries the acceptance criterion of ``docs/req-1-acceptance-criteria.md`` it
proves (AC-3.x-n), or the decision (D5, D11, D28) it pins. What these tests cannot say is how the
real session behaves: that is the manual test list, and the wording is "automated tests green,
live verification pending".
"""

from __future__ import annotations

import logging

import pytest

from slideshow_lock.session import LockResult
from slideshow_lock.state_machine import State, StateMachine, TriggerSource
from tests.fakes import (
    FakeClock,
    FakeGuard,
    FakeIdleWatcher,
    FakeInhibition,
    FakeSessionLock,
    FakeSettings,
    FakeSlideshow,
)


class Rig:
    def __init__(self, *, idle=120, grace=0, guard=True, enable=True):
        self.idle = FakeIdleWatcher()
        self.inhibition = FakeInhibition()
        self.lock = FakeSessionLock()
        self.slideshow = FakeSlideshow()
        self.settings = FakeSettings(idle=idle, grace=grace)
        self.clock = FakeClock()
        self.guard = FakeGuard() if guard else None
        self.machine = StateMachine(
            idle=self.idle,
            inhibition=self.inhibition,
            lock=self.lock,
            slideshow=self.slideshow,
            settings=self.settings,
            guard=self.guard,
            clock=self.clock,
        )
        if enable:
            self.machine.enable()

    @property
    def state(self):
        return self.machine.state

    def start_idle_slideshow(self):
        self.idle.fire_idle()
        assert self.state is State.SLIDESHOW_RUNNING
        assert self.machine.trigger_source is TriggerSource.IDLE

    def input_after(self, seconds):
        """The idle monitor reports the first input *seconds* after the slideshow started."""
        self.clock.advance(seconds)
        self.idle.fire_user_active()


# -- start-up, enable and disable -------------------------------------------------------------


def test_a_machine_starts_disabled_and_enable_watches_for_idle_with_the_configured_timeout():
    r = Rig(idle=300, enable=False)
    assert r.state is State.DISABLED
    assert r.idle.idle_callback is None
    r.machine.enable()
    assert r.state is State.IDLE_WATCHING
    assert r.idle.idle_timeout == 300
    assert r.guard.starts == 1


def test_enable_while_the_session_is_already_locked_starts_in_locked():
    r = Rig(enable=False)
    r.lock.locked = True
    r.machine.enable()
    assert r.state is State.LOCKED


def test_when_the_sleep_guard_cannot_start_the_machine_stays_disabled_and_says_so():
    r = Rig(enable=False)
    r.guard.start_error = OSError("logind refused the inhibitor")
    with pytest.raises(OSError, match="logind refused"):
        r.machine.enable()
    assert r.state is State.DISABLED
    assert r.idle.idle_callback is None  # no idle watch without the pre-sleep lock


def test_disable_drops_the_watches_stops_a_running_slideshow_and_releases_the_guard():
    r = Rig()
    r.start_idle_slideshow()
    r.machine.disable()
    assert r.state is State.DISABLED
    assert r.slideshow.stops == 1 and not r.slideshow.running
    assert r.idle.idle_callback is None
    assert r.idle.active_cancels == 1
    assert r.guard.stops == 1
    assert r.lock.lock_calls == 0
    r.machine.disable()  # idempotent
    assert r.guard.stops == 1


def test_a_disabled_machine_ignores_every_event():
    r = Rig()
    r.machine.disable()
    r.machine._on_idle()
    r.inhibition.set_inhibited(True)
    r.lock.set_locked(True)
    r.machine.sleep_started()
    r.machine.sleep_lock_finished(True)
    assert r.state is State.DISABLED
    assert r.slideshow.starts == 0 and r.lock.lock_calls == 0


def test_a_changed_idle_timeout_takes_effect_without_a_restart():
    r = Rig(idle=120)
    r.settings.set("idle-timeout-seconds", 45)
    assert r.idle.idle_registrations == [120, 45]
    r.settings.set("lock-grace-period-seconds", 3)  # another key: no new watch
    assert r.idle.idle_registrations == [120, 45]


# -- 3.1 the idle-triggered start ----------------------------------------------------------------


def test_ac_3_1_1_the_idle_timeout_starts_the_slideshow_when_nothing_stands_in_the_way():
    r = Rig()
    r.idle.fire_idle()
    assert r.state is State.SLIDESHOW_RUNNING
    assert r.machine.trigger_source is TriggerSource.IDLE
    assert r.slideshow.starts == 1
    assert len(r.idle.active_callbacks) == 1  # input is watched through the idle monitor (D22)


def test_ac_3_1_1_a_second_idle_event_while_the_slideshow_runs_starts_nothing_more():
    r = Rig()
    r.start_idle_slideshow()
    r.idle.fire_idle()
    assert r.slideshow.starts == 1


# -- 3.2 input stops the slideshow; the grace period ----------------------------------------------


def test_ac_3_2_1_the_first_input_stops_the_slideshow_at_once():
    r = Rig(grace=30)  # even when no lock follows
    r.start_idle_slideshow()
    r.input_after(1)
    assert r.slideshow.stops == 1 and not r.slideshow.running


def test_ac_3_2_1_a_touch_on_a_slideshow_window_is_input_too():
    r = Rig()
    r.start_idle_slideshow()
    r.slideshow.end_by_input("input")
    assert r.state is State.LOCKED
    assert r.lock.lock_calls == 1
    assert len(r.idle.active_callbacks) == 0  # the idle monitor's watch was cancelled


@pytest.mark.parametrize(
    "grace,elapsed,locks",
    [
        (0, 0, True),  # G = 0: one boundary point, every input locks
        (0, 5, True),
        (5, 0, False),
        (5, 4, False),
        (5, 4.999, False),
        (5, 5, True),  # t = G: lock-eligible (strict less-than, D16)
        (5, 6, True),
        (86400, 86399, False),
    ],
)
def test_ac_3_2_2_the_grace_period_boundary_is_a_strict_less_than(grace, elapsed, locks):
    r = Rig(grace=grace)
    r.start_idle_slideshow()
    r.input_after(elapsed)
    assert (r.lock.lock_calls == 1) is locks
    assert r.state is (State.LOCKED if locks else State.IDLE_WATCHING)


def test_the_grace_period_is_read_when_the_input_comes_not_when_the_machine_started():
    r = Rig(grace=0)
    r.start_idle_slideshow()
    r.settings.set("lock-grace-period-seconds", 60)
    r.input_after(10)
    assert r.lock.lock_calls == 0


# -- 3.3 lock only for a running, idle-triggered slideshow ----------------------------------------


def test_ac_3_3_1_input_after_an_idle_slideshow_that_is_lock_eligible_locks():
    r = Rig()
    r.start_idle_slideshow()
    r.input_after(2)
    assert r.lock.lock_calls == 1
    assert r.state is State.LOCKED


@pytest.mark.parametrize("grace", [0, 1, 30])
@pytest.mark.parametrize("elapsed", [0, 1, 30, 3600])
def test_ac_3_3_2_a_manual_preview_is_never_locked_by_input_whatever_the_grace_and_timing(
    grace, elapsed
):
    r = Rig(grace=grace)
    assert r.machine.start_preview()
    assert r.machine.trigger_source is TriggerSource.MANUAL_PREVIEW
    assert len(r.idle.active_callbacks) == 0  # a preview has no idle -> active transition (D22)
    r.clock.advance(elapsed)
    r.slideshow.end_by_input("input")
    assert r.lock.lock_calls == 0
    assert r.state is State.IDLE_WATCHING


def test_ac_3_3_2_idle_monitor_activity_does_not_end_or_lock_a_manual_preview():
    r = Rig()
    r.machine.start_preview()
    r.idle.fire_user_active()
    assert r.state is State.SLIDESHOW_RUNNING and r.lock.lock_calls == 0


def test_ac_3_3_3_input_with_no_slideshow_running_never_locks():
    r = Rig()
    r.idle.fire_user_active()
    r.machine._on_user_active()
    r.machine._on_slideshow_stopped("input")
    assert r.lock.lock_calls == 0
    assert r.state is State.IDLE_WATCHING


def test_a_preview_can_only_start_from_idle_watching():
    r = Rig()
    r.start_idle_slideshow()
    assert not r.machine.start_preview()
    r.slideshow.end_by_input()  # locks
    assert not r.machine.start_preview()
    assert r.slideshow.starts == 1


def test_a_failed_lock_is_logged_at_error_and_the_machine_watches_again(caplog):
    r = Rig()
    r.lock.result = LockResult(False, "no screensaver")
    r.start_idle_slideshow()
    with caplog.at_level(logging.ERROR):
        r.input_after(1)
    assert r.state is State.IDLE_WATCHING
    assert any("no screensaver" in m for m in caplog.messages)


def test_a_lock_that_raises_is_a_failed_lock_not_a_crash(caplog):
    r = Rig()
    r.lock.raises = RuntimeError("bus gone")
    r.start_idle_slideshow()
    with caplog.at_level(logging.ERROR):
        r.input_after(1)
    assert r.state is State.IDLE_WATCHING
    assert any("bus gone" in m for m in caplog.messages)


def test_the_lock_is_in_flight_in_locking_until_the_answer_comes():
    r = Rig()
    r.lock.mode = "manual"
    r.start_idle_slideshow()
    r.input_after(1)
    assert r.state is State.LOCKING
    r.lock.complete()
    assert r.state is State.LOCKED


def test_the_session_reporting_locked_before_the_answer_arrives_ends_in_locked():
    r = Rig()
    r.lock.mode = "manual"
    r.start_idle_slideshow()
    r.input_after(1)
    r.lock.set_locked(True)
    assert r.state is State.LOCKED
    r.lock.complete()
    assert r.state is State.LOCKED


# -- 3.4 idle inhibition ------------------------------------------------------------------------


def test_ac_3_4_1_an_idle_inhibit_at_the_threshold_keeps_the_slideshow_from_starting():
    r = Rig()
    r.inhibition.inhibited = True
    r.idle.fire_idle()
    assert r.state is State.IDLE_WATCHING and r.slideshow.starts == 0


def test_ac_3_4_1_a_session_manager_that_cannot_be_asked_keeps_the_slideshow_from_starting(caplog):
    r = Rig()
    r.inhibition.error = RuntimeError("no session manager")
    with caplog.at_level(logging.WARNING):
        r.idle.fire_idle()
    assert r.slideshow.starts == 0
    assert any("no session manager" in m for m in caplog.messages)


def test_ac_3_4_2_d5_an_inhibit_raised_during_the_run_stops_the_slideshow_without_a_lock():
    r = Rig()
    r.start_idle_slideshow()
    r.inhibition.set_inhibited(True)
    assert r.state is State.IDLE_WATCHING
    assert r.slideshow.stops == 1
    assert r.lock.lock_calls == 0
    assert len(r.idle.active_callbacks) == 0


def test_an_inhibit_that_goes_away_again_changes_nothing():
    r = Rig()
    r.start_idle_slideshow()
    r.inhibition.set_inhibited(False)
    assert r.state is State.SLIDESHOW_RUNNING


def test_an_inhibit_does_not_stop_a_manual_preview():
    r = Rig()
    r.machine.start_preview()
    r.inhibition.set_inhibited(True)
    assert r.state is State.SLIDESHOW_RUNNING


# -- 3.5 sleep: the guard locks, the machine follows ----------------------------------------------


def test_ac_3_5_1_sleep_stops_a_running_idle_slideshow_and_the_lock_is_the_guards():
    r = Rig()
    r.start_idle_slideshow()
    r.machine.sleep_started()
    assert r.slideshow.stops == 1 and not r.slideshow.running
    assert r.state is State.LOCKING
    assert r.lock.lock_calls == 0  # the guard's own session lock does it, on its own thread
    r.machine.sleep_lock_finished(True)
    assert r.state is State.LOCKED


def test_ac_3_5_1_sleep_stops_a_manual_preview_too_independent_of_how_it_started():
    r = Rig()
    r.machine.start_preview()
    r.machine.sleep_started()
    assert r.slideshow.stops == 1
    assert r.state is State.LOCKING


def test_ac_3_5_1_sleep_with_no_slideshow_running_still_follows_to_locked():
    r = Rig()
    r.machine.sleep_started()
    r.machine.sleep_lock_finished(True)
    assert r.state is State.LOCKED
    assert r.slideshow.stops == 0


def test_a_sleep_lock_that_failed_leaves_the_machine_watching_for_idle():
    r = Rig()
    r.machine.sleep_started()
    r.machine.sleep_lock_finished(False)
    assert r.state is State.IDLE_WATCHING


def test_ac_3_5_2_d28_the_sleep_path_never_asks_the_inhibition_query():
    r = Rig()
    r.inhibition.inhibited = True  # a video call
    r.inhibition.error = AssertionError("the sleep path asked the inhibition query")
    r.machine.sleep_started()
    r.machine.sleep_lock_finished(True)
    assert r.state is State.LOCKED
    assert r.inhibition.queries == 0


def test_d28_an_inhibit_that_stops_a_slideshow_is_not_a_sleep_inhibit():
    """The inhibit-triggered stop and the sleep follow-up are separate paths: after an inhibit
    stop the machine still follows a sleep to LOCKED."""
    r = Rig()
    r.start_idle_slideshow()
    r.inhibition.set_inhibited(True)
    r.machine.sleep_started()
    r.machine.sleep_lock_finished(True)
    assert r.state is State.LOCKED


# -- 3.6 already locked ---------------------------------------------------------------------------


def test_ac_3_6_1_while_locked_idle_input_and_inhibit_events_do_nothing():
    r = Rig()
    r.lock.set_locked(True)
    assert r.state is State.LOCKED
    r.idle.fire_idle()
    r.idle.fire_user_active()
    r.machine._on_slideshow_stopped("input")
    r.inhibition.set_inhibited(True)
    r.inhibition.set_inhibited(False)
    assert r.slideshow.starts == 0 and r.slideshow.stops == 0
    assert r.lock.lock_calls == 0
    assert r.state is State.LOCKED


def test_unlocking_returns_to_idle_watching_and_the_next_idle_starts_a_slideshow():
    r = Rig()
    r.lock.set_locked(True)
    r.lock.set_locked(False)
    assert r.state is State.IDLE_WATCHING
    r.idle.fire_idle()
    assert r.state is State.SLIDESHOW_RUNNING


def test_a_stale_locked_state_is_corrected_at_the_next_idle_event(caplog):
    r = Rig()
    r.lock.mode = "manual"
    r.start_idle_slideshow()
    r.input_after(1)
    r.lock.complete()  # the answer says locked, then the unlock signal was lost
    r.lock.locked = False  # (no ActiveChanged delivered)
    assert r.state is State.LOCKED
    r.idle.fire_idle()
    assert r.state is State.SLIDESHOW_RUNNING


def test_when_the_lock_state_cannot_be_asked_a_locked_machine_does_nothing():
    r = Rig()
    r.lock.set_locked(True)
    r.lock.is_active_error = RuntimeError("no screensaver")
    r.idle.fire_idle()
    assert r.state is State.LOCKED and r.slideshow.starts == 0


def test_somebody_else_locking_while_the_slideshow_runs_stops_it_without_a_lock_call():
    r = Rig()
    r.start_idle_slideshow()
    r.lock.set_locked(True)
    assert r.state is State.LOCKED
    assert r.slideshow.stops == 1
    assert r.lock.lock_calls == 0
    assert len(r.idle.active_callbacks) == 0


# -- 3.7 no pictures ------------------------------------------------------------------------------


def test_ac_3_7_1_a_slideshow_that_cannot_start_is_a_warning_and_the_service_keeps_going(caplog):
    r = Rig()
    r.slideshow.refuse = "the picture folder '/x' is missing or has no valid image"
    with caplog.at_level(logging.WARNING):
        r.idle.fire_idle()
    assert r.state is State.IDLE_WATCHING
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1 and "/x" in warnings[0].getMessage()
    assert r.lock.lock_calls == 0
    r.slideshow.refuse = None  # a picture turned up
    r.idle.fire_idle()
    assert r.state is State.SLIDESHOW_RUNNING


def test_a_slideshow_that_raises_while_starting_is_a_warning_not_a_crash(caplog):
    r = Rig()
    r.slideshow.start_error = RuntimeError("boom")
    with caplog.at_level(logging.WARNING):
        r.idle.fire_idle()
    assert r.state is State.IDLE_WATCHING
    assert any("not started" in m for m in caplog.messages)
