"""The whole service wiring (``build_service``) on the real adapters against the fake desktop.

State machine, sleep guard on its own thread, and the D-Bus adapters, with a fake slideshow in
place of the windows. "Simulated bus" in the sense of the acceptance criteria: nothing here is a
GNOME session, so the claim is "automated tests green, live verification pending".

Marked ``spawns_processes`` for the module (it starts ``dbus-daemon``; see ``test_tripwire.py``).
"""

from __future__ import annotations

import logging
import os
import threading

import pytest

from slideshow_lock.image_source import ImageSource
from slideshow_lock.service import build_service
from slideshow_lock.session import UnsupportedSessionInterface
from slideshow_lock.state_machine import State
from tests.fake_dbus import Desktop, dbus_daemon_available, wait_for
from tests.fakes import FakeSettings, FakeSlideshow, LoopThread, wait_until
from tests.test_image_source import make_image

pytestmark = [
    pytest.mark.spawns_processes,
    # CI installs it and checks for it; a skip there would be a silent loss (the no-skip gate)
    pytest.mark.skipif(
        not dbus_daemon_available() and not os.environ.get("CI"),
        reason="no dbus-daemon on this machine",
    ),
]


class Run:
    """A service on a fake desktop. The test thread is the main loop (``wait_for`` turns it)."""

    def __init__(self, desktop, *, idle=120, grace=0):
        self.desktop = desktop
        self.slideshow = FakeSlideshow()
        self.settings = FakeSettings(idle=idle, grace=grace)
        self.service = build_service(desktop.session, desktop.system, self.settings, self.slideshow)
        self.machine = self.service.machine

    def settle(self, predicate, timeout=5.0):
        return wait_for(predicate, timeout)

    def close(self):
        self.service.close()


@pytest.fixture
def run():
    with Desktop() as desktop:
        r = Run(desktop)
        yield r
        r.close()


def test_the_service_watches_idle_and_holds_the_delay_inhibitor_once_enabled(run):
    assert run.machine.state is State.IDLE_WATCHING
    assert run.desktop.idle_timeouts == [120_000]
    assert run.desktop.held == 1
    assert run.desktop.inhibit_calls[0][0::3] == ("sleep", "delay")


def test_idle_then_input_after_the_grace_period_locks_the_session(run):
    run.desktop.fire_idle()
    assert run.settle(lambda: run.machine.state is State.SLIDESHOW_RUNNING)
    assert run.slideshow.running
    assert list(run.desktop.watches.values()).count("active") == 1
    run.desktop.fire_user_active()
    assert run.settle(lambda: run.machine.state is State.LOCKED)
    assert len(run.desktop.lock_calls) == 1
    assert not run.slideshow.running
    run.desktop.set_locked(False)
    assert run.settle(lambda: run.machine.state is State.IDLE_WATCHING)


def test_input_within_the_grace_period_stops_the_slideshow_without_a_lock():
    with Desktop() as desktop:
        r = Run(desktop, grace=3600)
        try:
            desktop.fire_idle()
            assert r.settle(lambda: r.machine.state is State.SLIDESHOW_RUNNING)
            desktop.fire_user_active()
            assert r.settle(lambda: r.machine.state is State.IDLE_WATCHING)
            assert desktop.lock_calls == []
        finally:
            r.close()


def test_an_idle_inhibit_at_the_threshold_keeps_the_slideshow_down_and_one_during_the_run_stops_it(
    run,
):
    run.desktop.set_inhibited(True)
    run.desktop.fire_idle()
    assert not run.settle(lambda: run.slideshow.starts, timeout=0.4)
    run.desktop.set_inhibited(False)
    assert run.settle(lambda: not run.machine._inhibition.is_idle_inhibited())
    run.desktop.fire_idle()
    assert run.settle(lambda: run.machine.state is State.SLIDESHOW_RUNNING)
    run.desktop.set_inhibited(True)
    assert run.settle(lambda: run.machine.state is State.IDLE_WATCHING)
    assert run.desktop.lock_calls == []


def test_sleep_stops_the_slideshow_locks_releases_the_inhibitor_and_takes_it_again(run):
    run.desktop.fire_idle()
    assert run.settle(lambda: run.machine.state is State.SLIDESHOW_RUNNING)
    run.desktop.prepare_for_sleep(True)
    assert run.settle(lambda: len(run.desktop.lock_calls) == 1)
    assert run.settle(lambda: not run.slideshow.running)
    assert run.settle(lambda: run.machine.state is State.LOCKED)
    assert run.settle(lambda: run.desktop.held == 0)
    run.desktop.prepare_for_sleep(False)
    assert run.settle(lambda: run.desktop.held == 1)


def test_d28_an_idle_inhibit_does_not_keep_the_lock_before_suspend_from_being_made(run):
    run.desktop.set_inhibited(True)  # a video call
    run.desktop.prepare_for_sleep(True)
    assert run.settle(lambda: len(run.desktop.lock_calls) == 1)


def test_a_manual_preview_is_locked_for_sleep_but_never_for_input(run):
    assert run.machine.start_preview()
    run.slideshow.end_by_input()
    assert run.desktop.lock_calls == []
    assert run.machine.start_preview()
    run.desktop.prepare_for_sleep(True)
    assert run.settle(lambda: len(run.desktop.lock_calls) == 1)


def test_ac_3_5_3_d34_waking_before_the_lock_answer_is_a_warning_with_the_elapsed_time(run, caplog):
    run.desktop.lock_delay = 0.8  # the artificial delay
    with caplog.at_level(logging.WARNING):
        run.desktop.prepare_for_sleep(True)
        assert wait_until(lambda: len(run.desktop.lock_calls) == 1)
        run.desktop.prepare_for_sleep(False)
        assert wait_until(
            lambda: any(
                "resume received before lock sequence completed" in m for m in caplog.messages
            )
        )
    warning = next(m for m in caplog.messages if "resume received" in m)
    assert "limit=InhibitDelayMaxSec" in warning and "session may have resumed unlocked" in warning
    # the inhibitor is held until the answer comes, then released and taken again
    assert run.settle(lambda: run.desktop.last_lock_reply_at > 0)
    assert run.settle(lambda: run.desktop.released(0) and run.desktop.held == 1)


def test_ac_3_5_3_negative_control_an_answer_before_the_wake_is_no_warning(run, caplog):
    with caplog.at_level(logging.WARNING):
        run.desktop.prepare_for_sleep(True)
        assert wait_until(lambda: len(run.desktop.lock_calls) == 1)
        assert wait_until(lambda: run.desktop.held == 0)
        run.desktop.prepare_for_sleep(False)
        assert wait_until(lambda: run.desktop.held == 1)
    assert not [m for m in caplog.messages if "resume received" in m]


def test_a_failed_screensaver_lock_before_suspend_is_an_error_and_suspend_is_not_held_back(
    run, caplog
):
    run.desktop.lock_error = "no shield"
    with caplog.at_level(logging.ERROR):
        run.desktop.prepare_for_sleep(True)
        assert wait_until(lambda: any("no shield" in m for m in caplog.messages))
    assert wait_until(lambda: run.desktop.held == 0)


def test_the_lock_before_suspend_goes_out_while_the_main_loop_is_stuck_in_the_image_source(
    tmp_path, monkeypatch
):
    """The security condition, end to end: the main loop is blocked inside ``os.scandir`` of a real
    ImageSource while PrepareForSleep arrives on the (fake) system bus. The lock call reaches the
    screensaver and the inhibitor is released while the main loop is still stuck."""
    release = threading.Event()
    entered = threading.Event()
    real_scandir = os.scandir

    def stuck_scandir(path):
        entered.set()
        release.wait(30)
        return real_scandir(path)

    make_image(tmp_path / "a.png")
    monkeypatch.setattr("slideshow_lock.image_source.os.scandir", stuck_scandir)
    main = LoopThread()
    with Desktop() as desktop:
        try:
            source = ImageSource(
                str(tmp_path),
                scheduler=lambda step: (main.post(lambda: step()), lambda: None)[1],
                watcher=lambda path, callback: lambda: None,
            )
            holder = {}

            def build():
                holder["service"] = build_service(
                    desktop.session,
                    desktop.system,
                    FakeSettings(),
                    FakeSlideshow(),
                    main.post,
                )

            main.call(build)
            machine = holder["service"].machine
            main.post(source.start)  # the walk: the loop is stuck from here on
            assert entered.wait(5), "the image source did not start walking"
            desktop.prepare_for_sleep(True)
            assert wait_until(lambda: len(desktop.lock_calls) == 1, 3), "no lock while stuck"
            assert wait_until(lambda: desktop.held == 0, 3)
            assert machine.state is State.IDLE_WATCHING  # the main loop has not moved
            release.set()
            assert wait_until(lambda: main.call(lambda: machine.state) is State.LOCKED, 5)
        finally:
            release.set()
            main.call(lambda: holder["service"].close())
            main.stop()


def test_without_an_idle_monitor_the_sleep_lock_still_works_and_the_error_is_logged(caplog):
    with Desktop(idle_monitor=False) as desktop:
        with caplog.at_level(logging.ERROR):
            r = Run(desktop)
        try:
            assert any("IdleMonitor" in m for m in caplog.messages)
            assert desktop.watches == {}
            desktop.prepare_for_sleep(True)
            assert r.settle(lambda: len(desktop.lock_calls) == 1)
        finally:
            r.close()


def test_without_login1_the_service_refuses_to_start_and_leaves_no_guard_thread_behind():
    with Desktop() as desktop:
        before = {t.name for t in threading.enumerate()}
        with pytest.raises(UnsupportedSessionInterface):
            # the "system bus" here has no org.freedesktop.login1
            build_service(desktop.session, desktop.session, FakeSettings(), FakeSlideshow())
        assert wait_until(lambda: {t.name for t in threading.enumerate()} <= before, 3)


def test_the_lock_falls_back_to_login1_when_there_is_no_screensaver_and_a_sleep_locks_again():
    with Desktop(screensaver=False) as desktop:
        r = Run(desktop)
        try:
            desktop.fire_idle()
            assert r.settle(lambda: r.machine.state is State.SLIDESHOW_RUNNING)
            desktop.fire_user_active()
            assert r.settle(lambda: len(desktop.session_lock_calls) == 1)
            assert desktop.lock_calls == []
            assert r.settle(lambda: r.machine.state is State.LOCKED)
            # a sleep while the session is locked is locked again: the answer of the login1
            # signal is not proof of a lock screen, so nothing is remembered between the calls
            desktop.prepare_for_sleep(True)
            assert r.settle(lambda: len(desktop.session_lock_calls) == 2)
            assert r.settle(lambda: desktop.held == 0)
        finally:
            r.close()


def test_close_disables_the_machine_drops_the_watches_and_releases_the_inhibitor():
    with Desktop() as desktop:
        r = Run(desktop)
        r.close()
        assert r.machine.state is State.DISABLED
        assert desktop.held == 0
        assert wait_for(lambda: desktop.watches == {})
