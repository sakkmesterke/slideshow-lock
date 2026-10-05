"""The parts of ``slideshow_lock.service`` that sit between the state machine and the preview:
``PreviewSlideshow`` (the real class, on a fake controller and a fake picture source),
``ServiceSettings`` (this run's overrides) and the limits of the two command line switches.

Nothing here needs a bus or a display. The wording is "automated tests green, live verification
pending".
"""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import service
from slideshow_lock.service import PreviewSlideshow, ServiceSettings, overrides_from_args
from slideshow_lock.settings import KEY_IDLE_TIMEOUT_SECONDS, KEY_LOCK_GRACE_PERIOD_SECONDS
from slideshow_lock.state_machine import State, StateMachine
from tests.fakes import (
    FakeClock,
    FakeIdleWatcher,
    FakeInhibition,
    FakeSessionLock,
    FakeSettings,
)
from tests.timeout_guard import per_test_deadline  # noqa: F401  (autouse fixture)


class FakeSource:
    """The picture source as ``PreviewSlideshow`` asks it."""

    def __init__(self, current="/pictures/a.jpg", folder="/pictures", scan_complete=True):
        self._current = current
        self.folder = folder
        self.scan_complete = scan_complete
        self._listeners = []

    def current(self):
        return self._current

    def connect_current_changed(self, callback):
        self._listeners.append(callback)

    def finds(self, path="/pictures/a.jpg"):
        """The scan found its first picture (or, with ``None``, the queue ran empty)."""
        self._current = path
        for callback in list(self._listeners):
            callback(path)


class FakeController:
    """The preview controller as ``PreviewSlideshow`` sees it. ``monitors=0`` is a session with
    no monitor: ``start`` leaves it not running, like the real one."""

    def __init__(self, monitors=1):
        self.monitors = monitors
        self.running = False
        self.starts = 0
        self.stop_reasons = []
        self._listeners = []

    def connect_stopped(self, callback):
        self._listeners.append(callback)

    def start(self):
        self.starts += 1
        self.running = self.monitors > 0

    def stop(self, reason="requested"):
        if not self.running:
            return
        self.running = False
        self.stop_reasons.append(reason)
        self._emit(reason)

    def input(self):
        """The user touched a preview window: the controller stops itself, reason ``input``."""
        assert self.running
        self.stop("input")

    def _emit(self, reason):
        for callback in list(self._listeners):
            callback(reason)


def make_slideshow(source=None, controller=None):
    controller = controller or FakeController()
    slideshow = PreviewSlideshow(controller, source or FakeSource())
    heard = []
    slideshow.connect_stopped(heard.append)
    return slideshow, controller, heard


# -- start: the two reasons for refusing (3.7) ----------------------------------------------------


def test_a_start_without_a_picture_is_refused_with_the_folder_in_the_reason_and_opens_nothing():
    slideshow, controller, _ = make_slideshow(FakeSource(current=None, folder="/nowhere"))
    reason = slideshow.start()
    assert reason is not None and "no picture to show" in reason and "/nowhere" in reason
    assert "still being read" not in reason
    assert controller.starts == 0


def test_a_start_while_the_folder_is_still_being_read_says_so():
    slideshow, controller, _ = make_slideshow(FakeSource(current=None, scan_complete=False))
    reason = slideshow.start()
    assert reason is not None and "still being read" in reason
    assert controller.starts == 0


def test_a_start_without_a_monitor_is_refused_after_the_controller_was_asked():
    slideshow, controller, _ = make_slideshow(controller=FakeController(monitors=0))
    assert slideshow.start() == "no monitor found"
    assert controller.starts == 1


def test_a_start_with_a_picture_and_a_monitor_runs_and_gives_no_reason():
    slideshow, controller, _ = make_slideshow()
    assert slideshow.start() is None
    assert controller.running and controller.starts == 1


def test_the_state_machine_stays_idle_and_warns_when_the_real_slideshow_refuses(caplog):
    """The refusal of the real class, end to end through the state machine (3.7)."""
    source = FakeSource(current=None, folder="/nowhere")
    slideshow, controller, _ = make_slideshow(source)
    idle = FakeIdleWatcher()
    machine = StateMachine(
        idle=idle,
        inhibition=FakeInhibition(),
        lock=FakeSessionLock(),
        slideshow=slideshow,
        settings=FakeSettings(),
        guard=None,
        clock=FakeClock(),
    )
    machine.enable()
    with caplog.at_level(logging.WARNING):
        idle.fire_idle()
    assert machine.state is State.IDLE_WATCHING
    assert controller.starts == 0
    assert any("/nowhere" in m for m in caplog.messages)
    source._current = "/pictures/a.jpg"  # a picture turned up
    idle.fire_idle()
    assert machine.state is State.SLIDESHOW_RUNNING and controller.running


# -- ready: the scan found its first picture after a refused start -------------------------------


def _ready_counter(slideshow):
    heard = []
    slideshow.connect_ready(lambda: heard.append(1))
    return heard


def test_a_picture_found_after_a_start_refused_while_scanning_says_ready_once():
    source = FakeSource(current=None, scan_complete=False)
    slideshow, controller, _ = make_slideshow(source)
    heard = _ready_counter(slideshow)
    assert "still being read" in slideshow.start()
    source.finds("/pictures/a.jpg")
    source.finds("/pictures/b.jpg")  # a second change is not a second signal
    assert len(heard) == 1
    assert controller.starts == 0  # saying so does not start anything: the machine does that


def test_a_picture_that_turns_up_without_a_refused_start_says_nothing():
    source = FakeSource(current=None, scan_complete=False)
    slideshow, _, _ = make_slideshow(source)
    heard = _ready_counter(slideshow)
    source.finds("/pictures/a.jpg")
    assert heard == []


def test_a_start_refused_for_another_reason_does_not_say_ready_later():
    source = FakeSource(current=None, scan_complete=True)  # an empty folder, not a scan
    slideshow, _, _ = make_slideshow(source)
    heard = _ready_counter(slideshow)
    assert "still being read" not in slideshow.start()
    source.finds("/pictures/a.jpg")
    assert heard == []


def test_the_queue_running_empty_is_not_ready():
    source = FakeSource(current=None, scan_complete=False)
    slideshow, _, _ = make_slideshow(source)
    heard = _ready_counter(slideshow)
    slideshow.start()
    source.finds(None)
    assert heard == []


def test_an_idle_event_before_the_first_picture_starts_the_slideshow_when_the_scan_finds_one():
    """The real class and the state machine together (the card: idle before the scan's first
    picture)."""
    source = FakeSource(current=None, scan_complete=False)
    slideshow, controller, _ = make_slideshow(source)
    idle = FakeIdleWatcher()
    machine = StateMachine(
        idle=idle,
        inhibition=FakeInhibition(),
        lock=FakeSessionLock(),
        slideshow=slideshow,
        settings=FakeSettings(),
        guard=None,
        clock=FakeClock(),
    )
    machine.enable()
    idle.fire_idle()
    assert machine.state is State.IDLE_WATCHING and controller.starts == 0
    source.finds("/pictures/a.jpg")
    assert machine.state is State.SLIDESHOW_RUNNING and controller.running


def test_input_before_the_scan_finds_a_picture_means_no_slideshow_afterwards():
    source = FakeSource(current=None, scan_complete=False)
    slideshow, controller, _ = make_slideshow(source)
    idle = FakeIdleWatcher()
    machine = StateMachine(
        idle=idle,
        inhibition=FakeInhibition(),
        lock=FakeSessionLock(),
        slideshow=slideshow,
        settings=FakeSettings(),
        guard=None,
        clock=FakeClock(),
    )
    machine.enable()
    idle.fire_idle()
    idle.fire_user_active()  # the user is back before the scan is done
    source.finds("/pictures/a.jpg")
    assert machine.state is State.IDLE_WATCHING
    assert controller.starts == 0 and not controller.running


# -- stop: the slideshow's own stop is not input --------------------------------------------------


def test_a_stop_the_state_machine_asked_for_is_not_reported_as_input():
    slideshow, controller, heard = make_slideshow()
    slideshow.start()
    slideshow.stop()
    assert controller.stop_reasons == ["requested"]
    assert heard == []


def test_a_stop_by_input_is_reported_with_its_reason():
    slideshow, controller, heard = make_slideshow()
    slideshow.start()
    controller.input()
    assert heard == ["input"]


def test_input_after_an_own_stop_is_reported_again():
    """The "asked" flag is only for the length of the call, not for the rest of the run."""
    slideshow, controller, heard = make_slideshow()
    slideshow.start()
    slideshow.stop()
    slideshow.start()
    controller.input()
    assert heard == ["input"]


def test_the_flag_is_cleared_when_the_controller_raises_while_stopping():
    slideshow, controller, heard = make_slideshow()
    slideshow.start()

    def boom(reason="requested"):
        raise RuntimeError("window closed twice")

    real_stop, controller.stop = controller.stop, boom
    with pytest.raises(RuntimeError):
        slideshow.stop()
    controller.stop = real_stop
    controller.input()
    assert heard == ["input"]


# -- the settings of this run ----------------------------------------------------------------------


def stored(idle=120, grace=0):
    return FakeSettings(idle=idle, grace=grace)


def test_the_stored_settings_are_used_when_nothing_is_replaced():
    settings = ServiceSettings(stored(idle=77, grace=9), {})
    assert settings.get_idle_timeout_seconds() == 77
    assert settings.get_lock_grace_period_seconds() == 9


def test_a_replaced_idle_timeout_wins_over_the_stored_one_and_leaves_the_grace_alone():
    settings = ServiceSettings(stored(idle=77, grace=9), {KEY_IDLE_TIMEOUT_SECONDS: 5})
    assert settings.get_idle_timeout_seconds() == 5
    assert settings.get_lock_grace_period_seconds() == 9


def test_a_replaced_grace_wins_over_the_stored_one_and_leaves_the_idle_timeout_alone():
    settings = ServiceSettings(stored(idle=77, grace=9), {KEY_LOCK_GRACE_PERIOD_SECONDS: 3})
    assert settings.get_lock_grace_period_seconds() == 3
    assert settings.get_idle_timeout_seconds() == 77


def test_a_replaced_grace_of_zero_is_a_value_not_an_absence():
    settings = ServiceSettings(stored(grace=9), {KEY_LOCK_GRACE_PERIOD_SECONDS: 0})
    assert settings.get_lock_grace_period_seconds() == 0


def test_the_state_machine_registers_the_replaced_idle_timeout_and_locks_by_the_replaced_grace():
    idle, lock = FakeIdleWatcher(), FakeSessionLock()
    clock = FakeClock()
    settings = ServiceSettings(
        stored(idle=120, grace=0), {KEY_IDLE_TIMEOUT_SECONDS: 4, KEY_LOCK_GRACE_PERIOD_SECONDS: 10}
    )
    slideshow, controller, _ = make_slideshow()
    machine = StateMachine(
        idle=idle,
        inhibition=FakeInhibition(),
        lock=lock,
        slideshow=slideshow,
        settings=settings,
        guard=None,
        clock=clock,
    )
    machine.enable()
    assert idle.idle_timeout == 4
    idle.fire_idle()
    clock.advance(9.9)
    idle.fire_user_active()
    assert lock.lock_calls == 0  # inside the replaced grace of 10 s (the stored one is 0)
    assert machine.state is State.IDLE_WATCHING


# -- the command line: the limits of the two switches ----------------------------------------------


def parsed(*argv):
    return service._parse(list(argv))


def test_no_switch_replaces_neither_timing():
    assert overrides_from_args(parsed()) == {}


@pytest.mark.parametrize("value", ["1", "86400"])
def test_the_ends_of_the_idle_timeout_range_are_accepted(value):
    assert overrides_from_args(parsed("--idle-timeout", value)) == {
        KEY_IDLE_TIMEOUT_SECONDS: int(value)
    }


@pytest.mark.parametrize("value", ["0", "-1", "86401"])
def test_an_idle_timeout_outside_one_to_86400_is_refused(value):
    with pytest.raises(ValueError, match="between 1 and 86400"):
        overrides_from_args(parsed("--idle-timeout", value))


@pytest.mark.parametrize("value", ["0", "86400"])
def test_the_ends_of_the_grace_range_are_accepted(value):
    assert overrides_from_args(parsed("--grace", value)) == {
        KEY_LOCK_GRACE_PERIOD_SECONDS: int(value)
    }


@pytest.mark.parametrize("value", ["-1", "86401"])
def test_a_grace_outside_zero_to_86400_is_refused(value):
    with pytest.raises(ValueError, match="between 0 and 86400"):
        overrides_from_args(parsed("--grace", value))


def test_both_switches_together_replace_both_timings():
    assert overrides_from_args(parsed("--idle-timeout", "30", "--grace", "3")) == {
        KEY_IDLE_TIMEOUT_SECONDS: 30,
        KEY_LOCK_GRACE_PERIOD_SECONDS: 3,
    }


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--idle-timeout", "0"], "between 1 and 86400"),
        (["--grace", "86401"], "between 0 and 86400"),
    ],
)
def test_main_prints_the_reason_and_returns_2_for_a_timing_out_of_range(argv, message, capsys):
    assert service.main(argv) == 2
    assert message in capsys.readouterr().err
