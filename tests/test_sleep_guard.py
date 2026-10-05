"""The sleep guard (CORE-1): the lock before suspend, on its own thread.

The logic is tested directly (callbacks delivered in line); the thread is tested with real GLib
loops: ``GuardThread`` for the guard and a ``LoopThread`` standing in for the main loop, which
a test can block the way a stuck network mount blocks the image source.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import threading
import time

import pytest
from gi.repository import GLib

from slideshow_lock import sleep_guard
from slideshow_lock.image_source import ImageSource
from slideshow_lock.loop import current_poster
from slideshow_lock.session import LockResult
from slideshow_lock.sleep_guard import GuardThread, SleepGuard
from slideshow_lock.state_machine import State, StateMachine
from tests.fakes import (
    FakeClock,
    FakeIdleWatcher,
    FakeInhibition,
    FakeSessionLock,
    FakeSettings,
    FakeSleepSignal,
    FakeSlideshow,
    LoopThread,
    wait_until,
)
from tests.test_image_source import make_image


class Listener:
    def __init__(self):
        self.events = []

    def sleep_started(self):
        self.events.append("started")

    def sleep_lock_finished(self, ok):
        self.events.append(("finished", ok))


class Rig:
    """A guard with fakes, everything delivered in line; ``main`` collects what was posted to
    the main loop and ``run_main()`` runs it."""

    def __init__(self, *, enable=True, to_main=None):
        self.sleep = FakeSleepSignal()
        self.order = []
        self.lock = FakeSessionLock(shared=self.order)
        self.listener = Listener()
        self.main = []
        self.clock = FakeClock()
        self.guard = SleepGuard(
            self.sleep, self.lock, self.listener, to_main or self.main.append, clock=self.clock
        )
        if enable:
            self.guard.setup()

    def run_main(self):
        queued, self.main[:] = list(self.main), []
        for fn in queued:
            fn()


# -- set up ---------------------------------------------------------------------------------------


def test_setup_takes_one_delay_inhibitor_and_subscribes():
    r = Rig()
    assert len(r.sleep.inhibitors) == 1 and r.sleep.held == 1


def test_setup_fails_loudly_if_the_delay_inhibitor_cannot_be_taken():
    r = Rig(enable=False)
    r.sleep.acquire_error = OSError("login1 refused")
    with pytest.raises(OSError, match="login1 refused"):
        r.guard.setup()


def test_a_missing_delay_ceiling_is_a_warning_not_a_failure(caplog):
    r = Rig(enable=False)
    r.sleep.inhibit_delay_max = lambda: (_ for _ in ()).throw(RuntimeError("no property"))
    with caplog.at_level(logging.WARNING):
        r.guard.setup()
    assert any("InhibitDelayMaxSec" in m for m in caplog.messages)


def test_teardown_releases_the_inhibitor_and_stops_reacting():
    r = Rig()
    r.guard.teardown()
    assert r.sleep.held == 0
    r.sleep.fire(True)
    assert r.lock.lock_calls == 0


# -- AC-3.5-1: PrepareForSleep(true) --------------------------------------------------------------


def test_ac_3_5_1_sleep_posts_the_stop_then_locks_then_releases_the_inhibitor_then_reports():
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    assert len(r.main) == 1 and r.lock.lock_calls == 1  # the stop is queued, the lock is on its way
    assert r.sleep.held == 1  # held until the round trip is over
    r.lock.complete()
    assert r.sleep.inhibitors[0].released
    r.run_main()
    assert r.listener.events == ["started", ("finished", True)]
    assert r.sleep.held == 0


def test_ac_3_5_1_the_stop_is_queued_before_the_lock_call_and_not_waited_for():
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    assert r.listener.events == []  # nothing ran in line: it was only queued
    assert r.main  # ... and the lock call did not wait for it
    assert r.order == ["lock"]


def test_ac_3_5_1_the_lock_is_made_whether_or_not_a_slideshow_was_running():
    r = Rig()
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1
    # the guard takes no slideshow, no trigger source and no inhibition query at all
    assert not any(
        "slideshow" in name or "trigger" in name or "inhibition" in name
        for name in inspect.signature(SleepGuard.__init__).parameters
    )


def test_ac_3_5_2_d28_the_guard_has_no_way_to_ask_whether_idle_is_inhibited():
    source = inspect.getsource(sleep_guard)
    names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
    assert "InhibitionQuery" not in names
    assert "is_idle_inhibited" not in attrs and "on_idle_inhibit_changed" not in attrs


def test_ac_3_5_2_d28_an_idle_inhibit_does_not_keep_the_sleep_lock_from_being_made():
    """The full pair on one fake session: an application inhibits idle, the machine does not
    start a slideshow, and PrepareForSleep still locks."""
    r = Rig()
    inhibition = FakeInhibition()
    inhibition.inhibited = True
    inhibition.error = AssertionError("the sleep path asked the inhibition query")
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1
    assert inhibition.queries == 0


def test_a_session_that_looks_locked_already_is_locked_again_before_suspend():
    r = Rig()
    r.lock.set_locked(True)
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1
    assert r.sleep.inhibitors[0].released
    r.run_main()
    assert r.listener.events == ["started", ("finished", True)]


def test_an_answer_with_no_lock_screen_behind_it_does_not_keep_later_sleeps_from_locking():
    """A lock facility can answer ``Lock()`` with success and show nothing (the login1 signal is
    a broadcast). The guard must not take that answer for a lock state: three sleeps, three
    lock calls."""
    r = Rig()
    r.lock.shows_screen = False
    for expected in (1, 2, 3):
        r.sleep.fire(True)
        assert r.lock.lock_calls == expected
        r.sleep.fire(False)
    assert not r.lock.locked  # nothing ever said the session is locked


def test_a_lock_facility_that_reports_its_screen_is_locked_before_every_sleep_too():
    """Control for the test above, with a facility shaped like the real screensaver: the screen
    comes up (``ActiveChanged`` true) and goes away (false) between the sleeps."""
    r = Rig()
    for expected in (1, 2):
        r.sleep.fire(True)
        assert r.lock.lock_calls == expected
        r.sleep.fire(False)
        r.lock.set_locked(False)


def test_a_failed_lock_is_an_error_and_still_lets_suspend_go_on(caplog):
    r = Rig()
    r.lock.result = LockResult(False, "no screensaver")
    with caplog.at_level(logging.ERROR):
        r.sleep.fire(True)
    assert any("no screensaver" in m for m in caplog.messages)
    assert r.sleep.inhibitors[0].released
    r.run_main()
    assert r.listener.events == ["started", ("finished", False)]


def test_a_lock_call_that_raises_is_a_failed_lock_and_releases_the_inhibitor(caplog):
    r = Rig()
    r.lock.raises = RuntimeError("bus gone")
    with caplog.at_level(logging.ERROR):
        r.sleep.fire(True)
    assert r.sleep.inhibitors[0].released
    assert any("bus gone" in m for m in caplog.messages)


# What a failing post may raise: the guard catches ``Exception``, not one kind of it.
_POST_ERRORS = [
    pytest.param(RuntimeError("main loop gone"), id="RuntimeError"),
    pytest.param(OSError("main loop gone"), id="OSError"),
    pytest.param(TypeError("main loop gone"), id="TypeError"),
    pytest.param(AttributeError("main loop gone"), id="AttributeError"),
    pytest.param(GLib.Error("main loop gone"), id="GLib.Error"),
]


def _rig_whose_first_post_fails(error):
    """A rig whose poster to the main loop raises *error* on its first call and queues from then
    on."""
    holder = {}
    r = Rig(to_main=lambda fn: holder["post"](fn))
    calls = []

    def post(fn):
        calls.append(fn)
        if len(calls) == 1:
            raise error
        r.main.append(fn)

    holder["post"] = post
    return r


@pytest.mark.parametrize("error", _POST_ERRORS)
def test_a_failing_post_of_the_sleep_start_does_not_keep_the_lock_back(caplog, error):
    """The post of "sleep started" to the main loop raising must not end ``_before_sleep`` before
    the lock call, and a repeated signal must not be ignored."""
    r = _rig_whose_first_post_fails(error)

    with caplog.at_level(logging.ERROR):
        r.sleep.fire(True)  # nothing escapes the signal callback

    assert r.lock.lock_calls == 1
    assert r.sleep.inhibitors[0].released  # the round trip ran its normal course
    errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith("[sleep-inhibit] could not hand the sleep start")
    assert errors[0].exc_info is not None and errors[0].exc_info[1] is error
    r.run_main()  # only the result reaches the main loop, the start never did
    assert r.listener.events == [("finished", True)]


@pytest.mark.parametrize("error", _POST_ERRORS)
def test_after_a_failing_post_the_round_ends_normally_and_the_next_suspend_is_locked(error):
    r = _rig_whose_first_post_fails(error)
    r.sleep.fire(True)
    r.sleep.fire(False)  # woke after the lock was done: no "resumed unlocked" case
    assert r.sleep.held == 1  # the inhibitor is taken again
    r.sleep.fire(True)
    assert r.lock.lock_calls == 2


@pytest.mark.parametrize("error", _POST_ERRORS)
def test_a_failing_post_of_the_sleep_start_keeps_a_repeated_signal_from_locking_twice(error):
    r = _rig_whose_first_post_fails(error)
    r.lock.mode = "manual"
    r.sleep.fire(True)
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1


@pytest.mark.parametrize("error", _POST_ERRORS)
def test_after_a_failing_post_the_inhibitor_is_held_until_the_lock_answers(error):
    """The delay inhibitor is what holds suspend back until the lock is made: the failure of the
    post must not give it up before the answer of the lock call, only after."""
    r = _rig_whose_first_post_fails(error)
    r.lock.mode = "manual"
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1
    assert not r.sleep.inhibitors[0].released
    assert r.sleep.held == 1
    r.lock.complete()
    assert r.sleep.inhibitors[0].released


def test_only_exceptions_are_caught_around_the_post_of_the_sleep_start():
    """Not ``BaseException``: a KeyboardInterrupt or SystemExit is not the guard's to swallow."""
    r = _rig_whose_first_post_fails(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        r.sleep.fire(True)


def test_a_repeated_prepare_for_sleep_true_makes_one_lock():
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    r.sleep.fire(True)
    assert r.lock.lock_calls == 1


def test_the_inhibitor_is_taken_again_after_the_wake_so_the_next_suspend_is_guarded():
    r = Rig()
    r.sleep.fire(True)
    assert r.sleep.held == 0
    r.sleep.fire(False)
    assert r.sleep.held == 1
    r.lock.set_locked(False)
    r.sleep.fire(True)
    assert r.lock.lock_calls == 2


def test_a_failure_to_take_the_inhibitor_again_is_an_error(caplog):
    r = Rig()
    r.sleep.fire(True)
    r.sleep.acquire_error = OSError("gone")
    with caplog.at_level(logging.ERROR):
        r.sleep.fire(False)
    assert any("delay inhibitor again" in m for m in caplog.messages)


# -- AC-3.5-3, D34: waking before the round trip is over ------------------------------------------


def test_ac_3_5_3_waking_before_the_lock_round_trip_ended_is_a_warning_with_the_elapsed_time(
    caplog,
):
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    r.clock.advance(7.25)  # the machine slept through more than InhibitDelayMaxSec
    with caplog.at_level(logging.WARNING):
        r.sleep.fire(False)
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert [w.getMessage() for w in warnings] == [
        "[sleep-inhibit] resume received before lock sequence completed "
        "(elapsed=7250ms, limit=InhibitDelayMaxSec); session may have resumed unlocked"
    ]


def test_ac_3_5_3_negative_control_waking_after_the_round_trip_is_no_warning(caplog):
    r = Rig()
    r.sleep.fire(True)  # answered at once
    r.clock.advance(7.25)
    with caplog.at_level(logging.WARNING):
        r.sleep.fire(False)
    assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []


def test_ac_3_5_3_a_lock_that_ends_after_the_wake_releases_and_retakes_the_inhibitor():
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    r.sleep.fire(False)
    assert r.sleep.held == 1  # the old one, still held while the round trip is open
    r.lock.complete()
    assert r.sleep.inhibitors[0].released
    assert r.sleep.held == 1  # and a new one is held now
    r.run_main()
    assert r.listener.events == ["started", ("finished", True)]


def test_an_old_answer_does_not_end_the_next_round_trip_or_release_its_inhibitor():
    r = Rig()
    r.lock.mode = "manual"
    r.sleep.fire(True)
    r.sleep.fire(False)  # woke before the first answer
    r.lock.set_locked(False)
    r.sleep.fire(True)  # and goes to sleep again
    assert r.lock.lock_calls == 2 and len(r.lock.pending) == 2
    r.lock.complete()  # the OLD answer
    assert r.sleep.held == 1  # the inhibitor of the new round is still held
    r.lock.complete()
    assert r.sleep.held == 0


def test_the_elapsed_time_counts_while_the_machine_sleeps():
    """``time.monotonic`` stops during suspend; the guard's clock must not (D34 needs the real
    time between the signal and the wake)."""
    try:
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)
    except AttributeError:
        pytest.skip("no CLOCK_BOOTTIME on this platform")
    assert abs(sleep_guard.boottime_clock() - boot) < 1.0


# -- the thread: the lock does not wait for the main loop (the security condition) -----------------


def _guard_thread(main, *, lock_mode="auto"):
    """A GuardThread whose fakes are made on the guard's thread and deliver there."""
    made = {}

    def build():
        deliver = current_poster()
        made["thread"] = threading.current_thread()
        made["sleep"] = FakeSleepSignal(deliver)
        made["lock"] = FakeSessionLock(deliver)
        made["lock"].mode = lock_mode
        return made["sleep"], made["lock"]

    return GuardThread(build, made.setdefault("listener", Listener()), main.post), made


def test_the_guard_runs_on_a_thread_of_its_own_and_its_adapters_deliver_there():
    main = LoopThread()
    guard, made = _guard_thread(main)
    try:
        guard.start()
        assert made["thread"] is guard.thread and made["thread"] is not threading.current_thread()
        made["sleep"].fire(True)
        assert wait_until(lambda: made["lock"].lock_calls == 1)
        assert wait_until(lambda: made["sleep"].held == 0)
    finally:
        guard.stop()
        main.stop()
    assert not guard.thread or not guard.thread.is_alive()


def test_a_guard_that_cannot_start_raises_in_start_and_leaves_no_thread_behind():
    main = LoopThread()

    def build():
        raise OSError("no login1")

    guard = GuardThread(build, Listener(), main.post)
    try:
        with pytest.raises(OSError, match="no login1"):
            guard.start()
    finally:
        main.stop()
    assert guard.thread is None or not guard.thread.is_alive()


def test_stop_releases_the_inhibitor_on_the_guards_thread():
    main = LoopThread()
    guard, made = _guard_thread(main)
    guard.start()
    assert made["sleep"].held == 1
    guard.stop()
    main.stop()
    assert made["sleep"].held == 0


class _ClosingSleepSignal(FakeSleepSignal):
    def __init__(self, deliver, closed):
        super().__init__(deliver)
        self._closed = closed

    def close(self):
        self._closed.append(("sleep", threading.current_thread()))


class _ClosingSessionLock(FakeSessionLock):
    def __init__(self, deliver, closed):
        super().__init__(deliver)
        self._closed = closed

    def close(self):
        self._closed.append(("lock", threading.current_thread()))


def _closing_guard_thread(main, closed, *, setup_error=None):
    def build():
        deliver = current_poster()
        sleep = _ClosingSleepSignal(deliver, closed)
        if setup_error is not None:
            sleep.acquire_delay_inhibitor = lambda: (_ for _ in ()).throw(setup_error)
        return sleep, _ClosingSessionLock(deliver, closed)

    return GuardThread(build, Listener(), main.post)


def test_stop_closes_both_adapters_on_the_guards_thread():
    main = LoopThread()
    closed = []
    guard = _closing_guard_thread(main, closed)
    try:
        guard.start()
        thread = guard.thread
        assert closed == []
        guard.stop()
    finally:
        main.stop()
    assert sorted(kind for kind, _ in closed) == ["lock", "sleep"]
    assert all(where is thread for _, where in closed)


def test_adapters_are_closed_in_every_cycle_of_start_and_stop():
    """Five disable/enable cycles close ten adapters, not none: each one left open keeps its
    signal subscriptions on the bus connection."""
    main = LoopThread()
    closed = []
    guard = _closing_guard_thread(main, closed)
    try:
        for cycle in range(1, 6):
            guard.start()
            guard.stop()
            assert len(closed) == 2 * cycle
    finally:
        main.stop()


def test_adapters_are_closed_when_the_guard_cannot_come_up():
    main = LoopThread()
    closed = []
    guard = _closing_guard_thread(main, closed, setup_error=OSError("logind refused"))
    try:
        with pytest.raises(OSError, match="logind refused"):
            guard.start()
    finally:
        main.stop()
    assert wait_until(lambda: len(closed) == 2, 3)


def test_an_adapter_that_fails_to_close_is_logged_and_does_not_keep_the_other_open(caplog):
    main = LoopThread()
    closed = []

    def build():
        deliver = current_poster()
        sleep = _ClosingSleepSignal(deliver, closed)

        def broken_close():
            raise RuntimeError("connection is gone")

        sleep.close = broken_close
        return sleep, _ClosingSessionLock(deliver, closed)

    guard = GuardThread(build, Listener(), main.post)
    try:
        with caplog.at_level(logging.ERROR):
            guard.start()
            guard.stop()
    finally:
        main.stop()
    assert [kind for kind, _ in closed] == ["lock"]
    assert any("closing an adapter" in m for m in caplog.messages)


class _BlockingScandir:
    """``os.scandir`` that blocks, as a hard NFS mount does: one listing, for as long as the
    test says."""

    def __init__(self, real):
        self.real = real
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, path):
        self.entered.set()
        self.release.wait(30)
        return self.real(path)


def _blocked_machine(tmp_path, monkeypatch, main, guard_control):
    """A real ImageSource walking on *main*'s loop, stuck inside ``os.scandir``, and the state
    machine around it, owned by that loop."""
    make_image(tmp_path / "a.png")
    blocker = _BlockingScandir(os.scandir)
    monkeypatch.setattr("slideshow_lock.image_source.os.scandir", blocker)
    source = ImageSource(
        str(tmp_path),
        scheduler=lambda step: (main.post(lambda: step()), lambda: None)[1],
        watcher=lambda path, callback: lambda: None,
    )
    slideshow = FakeSlideshow()
    machine = main.call(
        lambda: StateMachine(
            idle=FakeIdleWatcher(),
            inhibition=FakeInhibition(),
            lock=FakeSessionLock(),
            slideshow=slideshow,
            settings=FakeSettings(),
            guard=guard_control,
        )
    )
    main.call(machine.enable)
    main.post(source.start)  # the walk: blocks the loop inside scandir
    assert blocker.entered.wait(5), "the source did not start walking"
    return machine, blocker


def test_the_lock_before_suspend_goes_out_while_the_main_loop_is_stuck_in_the_image_source(
    tmp_path, monkeypatch
):
    """The security condition of CORE-1: a stuck folder on the main loop must not keep the
    pre-sleep lock back. The main loop here is blocked inside ``os.scandir`` of a real
    ImageSource; the lock must still be made, and the inhibitor released, while it is."""
    main = LoopThread()
    machine_box = {}

    class ToMachine:
        """The guard's listener: the state machine, which is made later on the main loop."""

        def sleep_started(self):
            machine_box["machine"].sleep_started()

        def sleep_lock_finished(self, ok):
            machine_box["machine"].sleep_lock_finished(ok)

    guard_control, made = _guard_thread(main)
    guard_control._listener = ToMachine()
    try:
        machine, blocker = _blocked_machine(tmp_path, monkeypatch, main, guard_control)
        machine_box["machine"] = machine
        made["sleep"].fire(True)
        assert wait_until(lambda: made["lock"].lock_calls == 1, 3), (
            "no lock while the loop is stuck"
        )
        assert wait_until(lambda: made["sleep"].held == 0, 3)
        # and the main loop really was stuck the whole time: it has not seen the stop yet
        assert machine.state is State.IDLE_WATCHING
        blocker.release.set()
        assert wait_until(lambda: main.call(lambda: machine.state) is State.LOCKED, 5)
    finally:
        guard_control.stop()
        main.stop()


def test_negative_control_the_same_lock_on_the_stuck_main_loop_does_not_go_out(
    tmp_path, monkeypatch
):
    """The test above measures what it claims: with the sleep path wired on the main loop (the
    design CORE-1 must not have) the lock waits for the stuck call."""
    main = LoopThread()
    made = {}
    try:

        def build_on_main():
            deliver = current_poster()
            made["sleep"] = FakeSleepSignal(deliver)
            made["lock"] = FakeSessionLock(deliver)
            guard = SleepGuard(made["sleep"], made["lock"], Listener(), main.post)
            guard.setup()
            return guard

        main.call(build_on_main)
        blocker = _BlockingScandir(os.scandir)
        make_image(tmp_path / "a.png")
        monkeypatch.setattr("slideshow_lock.image_source.os.scandir", blocker)
        source = ImageSource(
            str(tmp_path),
            scheduler=lambda step: (main.post(lambda: step()), lambda: None)[1],
            watcher=lambda path, callback: lambda: None,
        )
        main.post(source.start)
        assert blocker.entered.wait(5)
        made["sleep"].fire(True)
        time.sleep(0.4)
        assert made["lock"].lock_calls == 0  # waiting behind the stuck call
        blocker.release.set()
        assert wait_until(lambda: made["lock"].lock_calls == 1, 5)
    finally:
        blocker.release.set()
        main.stop()
