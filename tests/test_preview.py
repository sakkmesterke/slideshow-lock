"""Tests for the CORE-2 preview controller: order, interval, switching, skipping, input.

The controller is driven without a display: fake windows, a fake clock, a manual worker and
a fake scaler, around the real ``ImageSource`` (real files, fake monitor backend) and real
``Settings``-style change notifications. What the real GTK windows, the real decoder and
the real scaler do is covered by ``tests/test_scaling*.py`` and ``tools/wayland-smoke``.

Test names carry the CORE-2 acceptance criterion they prove (AC1 ... AC8).
"""

from __future__ import annotations

import ast
import logging
import os
import re
import threading
import time

import pytest
from gi.repository import GLib

from slideshow_lock import preview as preview_module
from slideshow_lock.preview import (
    INPUT_BUTTON,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
    GLibClock,
    PreviewController,
    ThreadWorker,
)
from slideshow_lock.scaling import Frame, ImageSkipped, read_image_file
from slideshow_lock.settings import (
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
)
from tests.jpeg_fixtures import fake_jpeg
from tests.test_image_source import (
    FakeWatcher,
    ManualScheduler,
    make_image,
    make_source,
    started,
)
from tests.timeout_guard import (
    hard_timeout,
    per_test_deadline,  # noqa: F401  (autouse fixture)
)


@pytest.fixture
def backends():
    return FakeWatcher(), ManualScheduler()


# -- fakes ----------------------------------------------------------------------------------


class FakeClock:
    """Time that moves only when the test says so."""

    def __init__(self):
        self.t = 0.0
        self._timers = []
        self._seq = 0

    def now(self):
        return self.t

    def call_later(self, delay, fn):
        self._seq += 1
        entry = [self.t + delay, self._seq, fn]
        self._timers.append(entry)

        def cancel():
            if entry in self._timers:
                self._timers.remove(entry)

        return cancel

    @property
    def pending(self):
        return len(self._timers)

    def advance(self, seconds):
        target = self.t + seconds
        while True:
            due = [e for e in self._timers if e[0] <= target]
            if not due:
                break
            entry = min(due, key=lambda e: (e[0], e[1]))
            self._timers.remove(entry)
            self.t = entry[0]
            entry[2]()
        self.t = target


class ManualWorker:
    """Jobs wait until the test runs them: the way a result arrives later on the main loop."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, done):
        self.jobs.append((fn, done))

    def run_one(self):
        """Run the oldest waiting job only: the others are still being prepared."""
        fn, done = self.jobs.pop(0)
        try:
            result, error = fn(), None
        except Exception as exc:
            result, error = None, exc
        done(result, error)

    def run_all(self):
        ran = 0
        while self.jobs:
            fn, done = self.jobs.pop(0)
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            done(result, error)
            ran += 1
        return ran


class FakeWindow:
    def __init__(self, size=(1920, 1080)):
        self.size = size
        self.frames = []
        self.messages = []
        self.closed = 0
        self.input_callbacks = []
        self.size_callbacks = []

    def device_size(self):
        return self.size

    def show_frame(self, frame, pan_seconds):
        self.frames.append((frame, pan_seconds))
        self.messages.append(None)

    def show_message(self, text):
        self.messages.append(text)

    def connect_input(self, callback):
        self.input_callbacks.append(callback)

    def connect_size_changed(self, callback):
        self.size_callbacks.append(callback)

    def close(self):
        self.closed += 1

    # test helpers
    def fire_input(self, kind=INPUT_KEY):
        for callback in list(self.input_callbacks):
            callback(kind)

    def resize(self, size):
        self.size = size
        for callback in list(self.size_callbacks):
            callback()

    def shown(self):
        return [os.path.basename(frame.path) for frame, _pan in self.frames]


class FakeScaler:
    """Returns one frame per requested size. Paths in *bad* raise ImageSkipped."""

    def __init__(self, bad=()):
        self.bad = set(bad)
        self.fail_next = 0  # this many of the next calls raise, whatever the picture
        self.calls = []
        self.threads = []

    def prepare(self, path, sizes, mode, pan=False):
        self.calls.append((os.path.basename(path), list(sizes), mode, pan))
        self.threads.append(threading.get_ident())
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ImageSkipped("test: fails this time")
        if os.path.basename(path) in self.bad:
            raise ImageSkipped("test: damaged")
        return [
            Frame(path, w, h, w * 3, b"", f"fake-{mode}-{int(pan)}", (0, 0)) for (w, h) in sizes
        ]

    def calls_for(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeSettings:
    def __init__(self, interval=10, scaling="fill", pan=False):
        self.values = {
            KEY_SLIDE_INTERVAL_SECONDS: interval,
            KEY_SCALING: scaling,
            KEY_PAN_PORTRAIT_IMAGES: pan,
        }
        self._callbacks = []

    def get_slide_interval_seconds(self):
        return self.values[KEY_SLIDE_INTERVAL_SECONDS]

    def get_scaling(self):
        return self.values[KEY_SCALING]

    def get_pan_portrait_images(self):
        return self.values[KEY_PAN_PORTRAIT_IMAGES]

    def connect_changed(self, callback):
        self._callbacks.append(callback)

    def set(self, key, value):
        self.values[key] = value
        for callback in list(self._callbacks):
            callback(key)


class Rig:
    """One controller with everything around it."""

    def __init__(
        self,
        tmp_path,
        backends,
        files,
        *,
        windows=2,
        settings=None,
        scaler=None,
        order="name",
        sizes=None,
        scan=True,
        **source_kwargs,
    ):
        self.root = tmp_path
        for name in files:
            make_image(tmp_path / name)
        self.backends = backends
        if scan:
            self.source = started(tmp_path, backends, order=order, **source_kwargs)
        else:  # the walk is under way: the test runs its steps (``backends[1].run_all()``)
            self.source = make_source(tmp_path, backends, order=order, **source_kwargs)
            self.source.start()
        self.settings = settings or FakeSettings()
        self.scaler = scaler or FakeScaler()
        self.clock = FakeClock()
        self.worker = ManualWorker()
        sizes = sizes or [(1920, 1080), (1080, 1920), (2560, 1440)][:windows]
        self.windows = [FakeWindow(size) for size in sizes]
        self.stops = []
        self.controller = PreviewController(
            self.source,
            self.settings,
            lambda: self.windows,
            self.scaler,
            clock=self.clock,
            worker=self.worker,
        )
        self.controller.connect_stopped(self.stops.append)

    def start(self):
        self.controller.start()
        self.worker.run_all()
        return self

    def tick(self, seconds):
        """Let *seconds* of slideshow time pass, finishing the picture work as it arrives."""
        self.clock.advance(seconds)
        self.worker.run_all()


def rig(tmp_path, backends, files=("a.png", "b.png", "c.png"), **kwargs):
    return Rig(tmp_path, backends, files, **kwargs).start()


# -- AC1: one window per monitor, interval and order come from the settings --------------------


def test_ac1_one_window_per_monitor_each_gets_a_frame_of_its_own_size(tmp_path, backends):
    r = rig(tmp_path, backends, windows=3)
    assert len(r.windows) == 3
    for window in r.windows:
        ((frame, _pan),) = window.frames
        assert (frame.width, frame.height) == window.size
    # the picture is decoded once for all monitors, not once per monitor
    assert len(r.scaler.calls_for("a.png")) == 1
    assert r.scaler.calls_for("a.png")[0][1] == [w.size for w in r.windows]


def test_ac1_name_order_and_the_interval_decide_what_is_on_screen_when(tmp_path, backends):
    r = rig(tmp_path, backends, settings=FakeSettings(interval=10))
    assert r.windows[0].shown() == ["a.png"]
    r.tick(9.9)
    assert r.windows[0].shown() == ["a.png"]  # the interval is not over yet
    r.tick(0.2)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.tick(10)
    r.tick(10)
    r.tick(10)
    assert r.windows[0].shown() == ["a.png", "b.png", "c.png", "a.png", "b.png"]
    assert r.windows[1].shown() == r.windows[0].shown()  # monitors switch together


def test_ac1_random_order_shows_every_picture_once_per_cycle(tmp_path, backends):
    files = [f"p{i}.png" for i in range(6)]
    r = rig(tmp_path, backends, files, order="random")
    for _ in range(5):
        r.tick(10)
    assert sorted(r.windows[0].shown()) == sorted(files)


def test_ac1_a_changed_interval_takes_effect_without_a_restart(tmp_path, backends):
    r = rig(tmp_path, backends, settings=FakeSettings(interval=10))
    r.tick(1)
    r.settings.set(KEY_SLIDE_INTERVAL_SECONDS, 4)  # 1 s already shown, 3 s to go
    r.tick(2.9)
    assert r.windows[0].shown() == ["a.png"]
    r.tick(0.2)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.tick(4)  # and the next picture follows at the new interval
    assert r.windows[0].shown() == ["a.png", "b.png", "c.png"]


def test_ac1_a_changed_order_applies_to_the_pictures_after_the_prepared_one(tmp_path, backends):
    files = [f"p{i}.png" for i in range(8)]
    r = rig(tmp_path, backends, files, order="name")
    r.source.set_order("random")  # what source_from_settings does on a settings change
    new_order = [os.path.basename(p) for p in r.source.images()]  # starts with the prepared p1
    for _ in range(7):
        r.tick(10)
    shown = r.windows[0].shown()
    assert shown[0] == "p0.png"
    assert shown[1:] == new_order[:7]  # the prepared picture is kept, then the new order rules


# -- AC2/AC3: scaling mode and pan come from the settings, live ----------------------------------


def test_ac2_the_scaling_mode_and_pan_flag_reach_the_scaler(tmp_path, backends):
    r = rig(tmp_path, backends, settings=FakeSettings(scaling="fit", pan=True))
    assert r.scaler.calls[0][2:] == ("fit", True)


def test_ac2_changing_scaling_redoes_the_picture_on_screen_and_the_prepared_one(tmp_path, backends):
    r = rig(tmp_path, backends, settings=FakeSettings(scaling="fill"))
    r.settings.set(KEY_SCALING, "fit")
    r.worker.run_all()
    assert [os.path.basename(f.path) for f, _ in r.windows[0].frames] == ["a.png", "a.png"]
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"
    r.tick(10)  # the prepared next picture was redone too: it is not a stale "fill" frame
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"
    assert r.windows[0].shown()[-1] == "b.png"


def test_ac4_pan_is_off_unless_switched_on_and_a_change_applies_live(tmp_path, backends):
    r = rig(tmp_path, backends)
    assert r.scaler.calls[0][3] is False
    r.settings.set(KEY_PAN_PORTRAIT_IMAGES, True)
    r.worker.run_all()
    assert r.scaler.calls[-1][3] is True
    assert r.windows[0].frames[-1][0].method == "fake-fill-1"


def test_ac4_the_pan_animation_is_given_a_share_of_the_interval(tmp_path, backends):
    r = rig(tmp_path, backends, settings=FakeSettings(interval=20))
    assert r.windows[0].frames[0][1] == pytest.approx(18.0)  # 90 % of 20 s


def test_ac2_a_window_resize_redoes_the_picture_at_the_new_size(tmp_path, backends):
    r = rig(tmp_path, backends, windows=1)
    r.windows[0].resize((2560, 1440))
    r.worker.run_all()
    frame = r.windows[0].frames[-1][0]
    assert (frame.width, frame.height) == (2560, 1440)


def test_ac2_a_window_that_reports_its_size_late_still_gets_a_frame(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=2)
    r.windows[1].size = None  # the compositor has not sized the second window yet
    r.controller.start()
    r.worker.run_all()
    assert r.windows[0].frames == []  # the preview waits for every window first
    r.windows[1].resize((1080, 1920))
    r.worker.run_all()
    assert [w.shown() for w in r.windows] == [["a.png"], ["a.png"]]


def test_ac2_a_window_that_never_reports_a_size_does_not_hold_the_others_back(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=2)
    r.windows[1].size = None
    r.controller.start()
    r.clock.advance(2.5)  # past the wait for sizes
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png"]
    assert r.windows[1].frames == []


def test_ac2_a_window_sized_while_the_first_picture_is_being_prepared_gets_its_frame_too(
    tmp_path, backends
):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=2)
    r.windows[1].size = None
    r.controller.start()
    r.clock.advance(2.5)  # the wait for sizes is over: the job goes out with one window only
    r.windows[1].resize((1080, 1920))  # ... and the second window reports while it runs
    r.worker.run_all()
    assert [w.shown() for w in r.windows][1][:1] == ["a.png"]
    assert [w.shown() for w in r.windows][0][:1] == ["a.png"]


# A window that the compositor has not sized yet can report a placeholder size (measured on
# GTK 4.8 under mutter: 1x1, then the real size a few tens of milliseconds later). The first
# picture is then prepared for that size. These tests give the first window exactly that.

PLACEHOLDER = (1, 1)


def _sizes_shown(window):
    return [(frame.width, frame.height) for frame, _pan in window.frames]


def test_a_first_picture_prepared_for_a_placeholder_size_is_redone_at_the_real_size(
    tmp_path, backends
):
    """The service always starts like this (the picture is known, the windows are new); a preview
    does when its walk is faster than the compositor. The size arrives while the job runs."""
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1, sizes=[PLACEHOLDER])
    r.controller.start()  # the job goes out with the placeholder size
    r.windows[0].resize((1920, 1080))  # the compositor sizes the window while it runs
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png"]
    assert _sizes_shown(r.windows[0]) == [(1920, 1080)]  # never the 1x1 frame


def test_a_first_picture_is_redone_when_the_walk_ends_before_the_window_is_sized(
    tmp_path, backends
):
    """The preview of the settings window: the empty state first, the first picture of the walk
    arrives while the window still has its placeholder size."""
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1, sizes=[PLACEHOLDER], scan=False)
    r.controller.start()
    assert backends[1].run_all()  # the walk finds the pictures: a.png goes out at 1x1
    r.windows[0].resize((1920, 1080))
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png"]
    assert _sizes_shown(r.windows[0]) == [(1920, 1080)]


def test_a_first_picture_is_redone_even_if_no_size_event_arrives(tmp_path, backends):
    """The window reports an event only when its geometry differs from the last reported one, so
    a size can change without one reaching the controller: the result must be checked itself."""
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1, sizes=[PLACEHOLDER])
    r.controller.start()
    r.windows[0].size = (1920, 1080)  # no event
    r.worker.run_all()
    assert _sizes_shown(r.windows[0]) == [(1920, 1080)]


def test_a_redone_first_picture_still_gets_a_full_interval(tmp_path, backends):
    r = Rig(
        tmp_path,
        backends,
        ["a.png", "b.png"],
        windows=1,
        sizes=[PLACEHOLDER],
        settings=FakeSettings(interval=10),
    )
    r.controller.start()
    r.windows[0].resize((1920, 1080))
    r.worker.run_all()
    r.tick(9.9)
    assert r.windows[0].shown() == ["a.png"]
    r.tick(0.2)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    assert set(_sizes_shown(r.windows[0])) == {(1920, 1080)}


@pytest.mark.parametrize("late", [0, 1])
def test_every_window_of_a_first_picture_gets_its_real_size(tmp_path, backends, late):
    real = [(2560, 1440), (1080, 1920)]
    sizes = list(real)
    sizes[late] = PLACEHOLDER  # the window that the compositor has not sized yet
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=2, sizes=sizes)
    r.controller.start()
    r.windows[late].resize(real[late])
    r.worker.run_all()
    assert [_sizes_shown(w) for w in r.windows] == [[real[0]], [real[1]]]
    assert r.scaler.calls_for("a.png")[-1][1] == real


def test_a_prepared_next_picture_made_for_an_outdated_size_is_redone(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1, settings=FakeSettings(interval=10))
    r.controller.start()
    r.worker.run_one()  # a.png is up; b.png is being prepared
    r.windows[0].size = (2560, 1440)  # no event
    r.worker.run_all()
    r.tick(10)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    assert _sizes_shown(r.windows[0])[-1] == (2560, 1440)


def test_a_stable_window_size_prepares_the_first_picture_once(tmp_path, backends):
    """No redo without a reason: with an unchanged size one job makes the first picture."""
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1)
    r.controller.start()
    r.worker.run_all()
    assert len(r.scaler.calls_for("a.png")) == 1
    assert len(r.scaler.calls_for("b.png")) == 1


def test_a_window_that_lost_its_size_while_a_job_runs_keeps_what_the_job_made(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1)
    r.controller.start()
    r.windows[0].size = None  # not reported at the moment: no reason to throw the result away
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png"]


def test_a_changed_interval_counts_from_when_the_picture_appeared_not_from_the_start(
    tmp_path, backends
):
    r = rig(tmp_path, backends, settings=FakeSettings(interval=10))
    r.tick(10)  # b.png appears at t = 10, not at 0
    r.tick(2)
    r.settings.set(KEY_SLIDE_INTERVAL_SECONDS, 5)  # 2 s of b.png are over, 3 s to go
    r.tick(2.9)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.tick(0.2)
    assert r.windows[0].shown() == ["a.png", "b.png", "c.png"]


def test_a_picture_that_is_late_when_the_interval_ends_is_shown_the_moment_it_is_ready(
    tmp_path, backends
):
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()  # a.png is on screen, b.png is being prepared
    r.clock.advance(10)  # the interval is over and b.png is not ready
    assert r.windows[0].shown() == ["a.png"]
    assert len(r.worker.jobs) == 1  # b.png is waited for, not asked for a second time
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png", "b.png"]


def test_a_settings_change_while_the_next_picture_is_late_does_not_lose_the_swap(
    tmp_path, backends
):
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()
    r.clock.advance(10)  # the interval is over and b.png is not ready ...
    r.settings.set(KEY_SCALING, "fit")  # ... and the scaling changes meanwhile
    r.worker.run_all()
    assert r.windows[0].shown()[-1] == "b.png"  # b.png still comes, without waiting a second round
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"
    r.tick(10)
    assert r.windows[0].shown()[-1] == "c.png"


def test_a_window_resize_while_the_next_picture_is_late_does_not_lose_the_swap(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()
    r.clock.advance(10)
    r.windows[0].resize((2560, 1440))
    r.worker.run_all()
    assert r.windows[0].shown()[-1] == "b.png"
    frame = r.windows[0].frames[-1][0]
    assert (frame.width, frame.height) == (2560, 1440)


@pytest.mark.parametrize("animations", [True, False])
@pytest.mark.parametrize("pan_setting", [True, False])
def test_pan_is_asked_of_the_scaler_only_when_it_is_set_and_animations_are_on(
    tmp_path, backends, animations, pan_setting
):
    """A panning frame is up to six monitors' pixels. With animations off the window would show
    only its middle, so the controller does not ask for the tall frame: every call of the scaler
    (first picture, next picture, a redo) gets ``pan`` False then."""
    answers = [animations]
    r = Rig(
        tmp_path,
        backends,
        ["a.png", "b.png"],
        windows=1,
        settings=FakeSettings(pan=pan_setting),
    )
    controller = PreviewController(
        r.source,
        r.settings,
        lambda: r.windows,
        r.scaler,
        clock=r.clock,
        worker=r.worker,
        animations=lambda: answers[0],
    )
    controller.start()
    r.worker.run_all()
    r.settings.set(KEY_SCALING, "fit")  # a redo
    r.worker.run_all()
    assert len(r.scaler.calls) >= 3
    assert {call[3] for call in r.scaler.calls} == {pan_setting and animations}
    answers[0] = not animations  # asked for every picture: the next call follows the new answer
    r.clock.advance(10)
    r.worker.run_all()
    assert r.scaler.calls[-1][3] is (pan_setting and not animations)
    controller.stop()


@pytest.mark.parametrize("change", ["scaling", "resize"])
def test_a_picture_prepared_for_the_old_mode_is_never_shown_when_the_interval_ends_first(
    tmp_path, backends, change
):
    """The settings or size change comes, the redo has not arrived, and the interval ends: the
    picture that was already prepared (in the old mode or size) must not be shown, the redone
    one is."""
    r = rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)  # b.png is ready, in fill
    if change == "scaling":
        r.settings.set(KEY_SCALING, "fit")
    else:
        r.windows[0].resize((2560, 1440))
    r.clock.advance(10)
    assert r.windows[0].shown() == ["a.png"]  # not b.png from before the change
    r.worker.run_all()
    shown_b = [frame for frame, _pan in r.windows[0].frames if frame.path.endswith("b.png")]
    assert len(shown_b) == 1
    if change == "scaling":
        assert shown_b[0].method == "fake-fit-0"
    else:
        assert (shown_b[0].width, shown_b[0].height) == (2560, 1440)


def test_a_swap_is_over_once_done_the_picture_after_it_waits_for_its_own_interval(
    tmp_path, backends
):
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()
    r.clock.advance(10)  # b.png is late ...
    r.worker.run_all()  # ... arrives, is shown, and c.png is prepared
    assert r.windows[0].shown() == ["a.png", "b.png"]  # c.png is ready but not yet due
    r.tick(9.9)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.tick(0.2)
    assert r.windows[0].shown() == ["a.png", "b.png", "c.png"]


def test_a_due_swap_does_not_survive_the_source_running_empty(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1)
    r.controller.start()
    r.worker.run_one()
    r.clock.advance(10)  # b.png is late: the swap is due
    for name in ("a.png", "b.png"):
        os.unlink(tmp_path / name)
        r.backends[0].emit(str(tmp_path / name), _fs("deleted"))
    backends[1].run_all()
    assert r.controller.shown_path is None  # the empty state
    for name in ("new1.png", "new2.png"):
        make_image(tmp_path / name)
        r.backends[0].emit(str(tmp_path / name), _fs("created"))
    backends[1].run_all()
    r.worker.run_all()
    assert r.windows[0].shown()[-1] == "new1.png"  # the first picture of the new start ...
    assert r.windows[0].shown().count("new2.png") == 0  # ... and not also the second at once
    r.tick(10)
    assert r.windows[0].shown()[-1] == "new2.png"


def test_a_refresh_still_running_is_not_lost_when_the_prepared_picture_is_replaced(
    tmp_path, backends
):
    r = rig(tmp_path, backends, ["a.png", "b.png", "c.png", "d.png"], windows=1)
    r.settings.set(KEY_SCALING, "fit")  # a.png is being redone in the new mode ...
    refresh_fn, refresh_done = r.worker.jobs.pop(0)
    refresh_result = refresh_fn()  # ... the worker has it, its answer is on the way ...
    os.unlink(tmp_path / "b.png")  # ... and the prepared picture is deleted meanwhile
    r.backends[0].emit(str(tmp_path / "b.png"), _fs("deleted"))
    backends[1].run_all()
    refresh_done(refresh_result, None)
    r.worker.run_all()
    assert [c[2] for c in r.scaler.calls_for("a.png")] == ["fill", "fit"]  # redone once, not twice
    assert r.windows[0].shown()[-1] == "a.png"
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"  # the refresh result was used
    r.tick(10)
    assert r.windows[0].shown()[-1] == "c.png"
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"


def test_a_refresh_waiting_for_a_window_size_is_not_lost_to_a_new_next_picture(tmp_path, backends):
    r = rig(tmp_path, backends, ["a.png", "b.png", "c.png", "d.png"], windows=2)
    r.windows[1].size = None  # the compositor withdraws a size ...
    r.settings.set(KEY_SCALING, "fit")  # ... so the redo of a.png has to wait for it
    os.unlink(tmp_path / "b.png")
    r.backends[0].emit(str(tmp_path / "b.png"), _fs("deleted"))
    backends[1].run_all()
    r.clock.advance(2.5)  # the wait for sizes is over
    r.worker.run_all()
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"
    assert r.windows[0].shown()[-1] == "a.png"


@pytest.mark.parametrize("files", [("a.png",), ("a.png", "b.png")], ids=["one", "two"])
def test_a_refresh_that_fails_once_does_not_leave_the_picture_in_the_old_mode(
    tmp_path, backends, files
):
    """The redo of the shown picture after a settings change fails out (every picture failed
    once): the controller keeps what is on screen and says it will try again at the next
    interval. It has to: otherwise the shown picture stays in the old mode for good."""
    r = rig(tmp_path, backends, files, windows=1)
    r.scaler.fail_next = len(files)  # the redo of a.png (and of the next one) fails once
    r.settings.set(KEY_SCALING, "fit")
    r.worker.run_all()
    assert r.windows[0].frames[-1][0].method == "fake-fill-0"  # still the old mode, as promised
    r.tick(10)  # the next interval
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"
    r.tick(10)
    r.tick(10)
    assert {f.method for f, _pan in r.windows[0].frames[-2:]} == {"fake-fit-0"}


def test_a_refresh_that_keeps_failing_is_tried_once_per_interval_not_in_a_loop(
    tmp_path, backends, caplog
):
    r = rig(tmp_path, backends, ("a.png",), windows=1)
    r.scaler.fail_next = 10**6
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r.settings.set(KEY_SCALING, "fit")
        assert r.worker.run_all() == 1
        assert any("trying again at the next interval" in m for m in caplog.messages)
        for round_ in range(1, 4):
            before = len(r.scaler.calls)
            r.clock.advance(10)
            assert r.worker.run_all() == 1, f"round {round_}"  # one job, not a chain of them
            assert len(r.scaler.calls) == before + 1
            assert r.clock.pending == 1  # and the next try is on the clock
    assert r.windows[0].frames[-1][0].method == "fake-fill-0"  # nothing was shown meanwhile
    r.scaler.fail_next = 0  # the file turns readable again
    r.tick(10)
    assert r.windows[0].frames[-1][0].method == "fake-fit-0"


def test_a_timer_that_fires_while_a_failed_redo_waits_for_sizes_does_not_overwrite_its_wish(
    tmp_path, backends
):
    """The redo of the shown picture failed out, and its follow-up request waits because no
    window knows its size yet. The interval timer fires in that gap: the request that waits must
    not be replaced by "the next picture" (``_on_timer`` asks only when nothing is pending).
    Replaced, the picture that just failed is asked for again, and ``c.png`` is skipped."""
    r = rig(tmp_path, backends, windows=1, settings=FakeSettings(interval=1))
    r.settings.set(KEY_SCALING, "fit")  # the redo of a.png is on the worker
    r.windows[0].size = None  # and now no window knows its size
    r.scaler.fail_next = 1
    r.worker.run_one()  # it fails out: the request for c.png (redo) waits for sizes
    assert not r.worker.jobs
    r.clock.advance(1.0)  # the interval timer fires in that gap
    assert not r.worker.jobs  # still waiting for sizes
    r.windows[0].size = (1920, 1080)  # known again, but no size event: the wait runs out
    r.clock.advance(1.5)
    r.worker.run_all()
    r.tick(1)
    r.tick(1)
    assert r.windows[0].shown()[:3] == ["a.png", "c.png", "b.png"]


# -- AC5: damaged pictures are skipped, empty source is a defined state --------------------------


def test_ac5_a_damaged_picture_is_skipped_and_logged_and_the_next_one_is_shown(
    tmp_path, backends, caplog
):
    scaler = FakeScaler(bad={"b.png"})
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, scaler=scaler)
        r.tick(10)
    assert r.windows[0].shown() == ["a.png", "c.png"]  # b.png never appears
    skipped = [m for m in caplog.messages if "skipping" in m]
    assert len(skipped) == 1 and "b.png" in skipped[0] and "[slideshow-dir]" in skipped[0]
    assert any(
        rec.levelno == logging.WARNING for rec in caplog.records if "skipping" in rec.message
    )


def test_ac5_negative_control_the_same_picture_undamaged_is_shown(tmp_path, backends):
    r = rig(tmp_path, backends, scaler=FakeScaler(bad=set()))
    r.tick(10)
    assert r.windows[0].shown() == ["a.png", "b.png"]


def test_ac5_a_truncated_file_with_a_valid_header_is_skipped_through_the_real_file_checks(
    tmp_path, backends
):
    """Real files and the real file checks of the scaler, only the decoding is faked."""

    class CheckingScaler(FakeScaler):
        def prepare(self, path, sizes, mode, pan=False):
            read_image_file(path, settle_seconds=0)  # raises ImageSkipped for a damaged file
            return super().prepare(path, sizes, mode, pan)

    whole = fake_jpeg()
    for name, data in (("a.jpg", whole), ("b.jpg", whole[: len(whole) // 2]), ("c.jpg", whole)):
        make_image(tmp_path / name, data)
        os.utime(tmp_path / name, (time.time() - 3600, time.time() - 3600))
    r = Rig(tmp_path, backends, [], scaler=CheckingScaler()).start()
    r.tick(10)
    r.tick(10)
    assert r.windows[0].shown() == ["a.jpg", "c.jpg", "a.jpg"]
    assert "b.jpg" not in r.windows[0].shown()


def test_ac5_a_file_still_being_copied_is_skipped_now_and_shown_once_it_settled(tmp_path, backends):
    class CheckingScaler(FakeScaler):
        def prepare(self, path, sizes, mode, pan=False):
            read_image_file(path, settle_seconds=60)
            return super().prepare(path, sizes, mode, pan)

    whole = fake_jpeg()
    make_image(tmp_path / "a.jpg", whole)
    os.utime(tmp_path / "a.jpg", (time.time() - 3600, time.time() - 3600))
    make_image(tmp_path / "b.jpg", whole)  # just written: modified a moment ago
    r = Rig(tmp_path, backends, [], scaler=CheckingScaler()).start()
    r.tick(10)
    assert r.windows[0].shown() == ["a.jpg", "a.jpg"]  # b.jpg skipped, a.jpg again
    os.utime(tmp_path / "b.jpg", (time.time() - 3600, time.time() - 3600))  # the copy finished
    for _ in range(3):
        r.tick(10)
    assert "b.jpg" in r.windows[0].shown()


def test_ac5_when_every_picture_fails_there_is_no_busy_loop_and_it_retries_later(
    tmp_path, backends, caplog
):
    bad = {"a.png", "b.png", "c.png"}
    scaler = FakeScaler(bad=bad)
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, scaler=scaler)
    assert len(scaler.calls) == 3  # one attempt per picture, then it stops trying
    assert r.windows[0].frames == []  # nothing could be shown
    assert r.windows[0].messages[-1]  # a message instead of a frozen black screen
    assert any("none of the 3 pictures could be shown" in m for m in caplog.messages)
    scaler.bad = set()  # for example the copy finished meanwhile
    r.tick(10)
    assert r.windows[0].shown() == ["c.png"]  # the next interval tries again, from where it was


def test_ac5_when_every_next_picture_fails_the_current_one_stays_on_screen(tmp_path, backends):
    scaler = FakeScaler()
    r = rig(tmp_path, backends, ["a.png", "b.png"], scaler=scaler)
    scaler.bad = {"a.png", "b.png"}
    r.tick(10)
    r.tick(10)
    assert r.windows[0].shown() == ["a.png", "b.png"]
    assert r.controller.shown_path.endswith("b.png")  # no blank screen, no crash
    assert len(scaler.calls) < 12  # bounded work, not a spin


def test_ac5_an_unexpected_error_in_the_scaler_is_a_skip_not_a_crash(tmp_path, backends, caplog):
    class Exploding(FakeScaler):
        def prepare(self, path, sizes, mode, pan=False):
            if path.endswith("b.png"):
                raise MemoryError("boom")
            return super().prepare(path, sizes, mode, pan)

    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, scaler=Exploding())
        r.tick(10)
    assert r.windows[0].shown() == ["a.png", "c.png"]
    assert any("unexpected error" in m for m in caplog.messages)


def test_ac5_an_empty_source_is_a_defined_state_logged_once_and_not_an_error(
    tmp_path, backends, caplog
):
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, files=())
    assert r.controller.running
    assert r.windows[0].messages and r.windows[0].messages[-1]
    assert r.scaler.calls == []
    waiting = [m for m in caplog.messages if "no picture to show" in m]
    assert len(waiting) == 1
    r.tick(60)
    assert len([m for m in caplog.messages if "no picture to show" in m]) == 1  # not repeated
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_ac5_the_empty_message_names_the_folder_that_was_searched(tmp_path, backends):
    r = rig(tmp_path, backends, files=())
    message = r.windows[0].messages[-1]
    assert message.splitlines() == ["No pictures to show", str(tmp_path)]


def test_ac5_a_picture_that_appears_in_an_empty_source_starts_the_preview(tmp_path, backends):
    r = rig(tmp_path, backends, files=())
    make_image(tmp_path / "new.png")
    r.backends[0].emit(str(tmp_path / "new.png"), _fs("created"))
    backends[1].run_all()
    r.worker.run_all()
    assert r.windows[0].shown() == ["new.png"]


def test_ac5_when_the_last_picture_is_deleted_the_windows_show_the_empty_message(
    tmp_path, backends
):
    r = rig(tmp_path, backends, files=("only.png",))
    assert r.windows[0].shown() == ["only.png"]
    os.unlink(tmp_path / "only.png")
    r.backends[0].emit(str(tmp_path / "only.png"), _fs("deleted"))
    backends[1].run_all()
    assert r.windows[0].messages[-1]
    assert r.controller.shown_path is None
    assert r.controller.running


def test_ac5_a_single_picture_is_decoded_once_and_reused(tmp_path, backends):
    r = rig(tmp_path, backends, files=("only.png",))
    for _ in range(4):
        r.tick(10)
    assert len(r.scaler.calls_for("only.png")) == 1
    assert len(r.windows[0].frames) == 5  # but it is shown again each interval (pan restarts)


def test_the_prepared_picture_being_deleted_moves_on_to_its_follower(tmp_path, backends):
    r = rig(tmp_path, backends, files=("a.png", "b.png", "c.png"))
    os.unlink(tmp_path / "b.png")  # b.png is the prepared next picture
    r.backends[0].emit(str(tmp_path / "b.png"), _fs("deleted"))
    backends[1].run_all()
    r.worker.run_all()
    r.tick(10)
    assert r.windows[0].shown() == ["a.png", "c.png"]


def test_a_prepared_picture_deleted_before_its_follower_is_ready_is_not_shown_in_its_place(
    tmp_path, backends
):
    """b.png is prepared and then deleted; the interval ends before c.png, its follower, is
    ready. The frames of b.png must be gone by then: shown under the name of c.png they would
    put the deleted picture on screen."""
    r = rig(tmp_path, backends, files=("a.png", "b.png", "c.png"), windows=1)
    os.unlink(tmp_path / "b.png")
    r.backends[0].emit(str(tmp_path / "b.png"), _fs("deleted"))
    backends[1].run_all()  # c.png is now the next picture, and waits for the worker
    r.clock.advance(10)  # the interval is over, c.png is not ready
    assert r.windows[0].shown() == ["a.png"]
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png", "c.png"]


def test_a_picture_shown_once_is_not_shown_again_when_its_follower_is_late(tmp_path, backends):
    """The swap hands over the prepared frames and forgets them: a second interval that ends
    before the next picture is ready leaves the screen alone."""
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()  # a.png is on screen, b.png is being prepared
    r.worker.run_one()
    r.clock.advance(10)  # b.png is shown, c.png is being prepared
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.clock.advance(10)  # the interval is over and c.png is not ready
    assert r.windows[0].shown() == ["a.png", "b.png"]
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png", "b.png", "c.png"]


def test_the_picture_on_screen_being_deleted_does_not_blank_the_screen(tmp_path, backends):
    r = rig(tmp_path, backends, files=("a.png", "b.png", "c.png"))
    os.unlink(tmp_path / "a.png")
    r.backends[0].emit(str(tmp_path / "a.png"), _fs("deleted"))
    backends[1].run_all()
    r.worker.run_all()
    assert r.windows[0].shown() == ["a.png"]
    assert r.controller.shown_path.endswith("a.png")  # the pixels are in memory
    r.tick(10)
    assert r.windows[0].shown()[-1] == "b.png"


def test_failures_that_are_not_in_a_row_do_not_add_up_to_a_total_failure(
    tmp_path, backends, caplog
):
    """b.png and d.png are damaged, a.png and c.png are fine: never two in a row, so many
    cycles later the failure count must still be 0 or 1, not "all four pictures failed"."""
    files = ["a.png", "b.png", "c.png", "d.png"]
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, files, scaler=FakeScaler(bad={"b.png", "d.png"}))
        for _ in range(12):
            r.tick(10)
    assert r.windows[0].shown() == (["a.png", "c.png"] * 7)[:13]  # on time, every interval
    assert not any("none of the" in m for m in caplog.messages)


def test_a_job_that_was_replaced_while_it_waited_in_the_queue_is_never_decoded(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png", "c.png"], windows=1)
    r.controller.start()
    r.worker.run_one()  # a.png is on screen; b.png waits in the queue
    r.settings.set(KEY_SCALING, "fit")  # the queued b.png job is out of date now
    r.worker.run_all()
    assert [c[2] for c in r.scaler.calls_for("b.png")] == ["fit"]  # no "fill" decode of it
    assert [c[2] for c in r.scaler.calls_for("a.png")] == ["fill", "fit"]


def test_unexpected_errors_are_burst_limited_like_every_other_skip_line(tmp_path, backends, caplog):
    from slideshow_lock.image_source import LOG_FIRST_N

    class Exploding(FakeScaler):
        def prepare(self, path, sizes, mode, pan=False):
            raise RuntimeError("boom")

    files = [f"p{i:02d}.png" for i in range(LOG_FIRST_N + 20)]
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends, files, scaler=Exploding(), windows=1)
        r.controller.stop()
    errors = [rec for rec in caplog.records if "unexpected error while" in rec.message]
    assert len(errors) == LOG_FIRST_N  # not one line per picture
    assert all(rec.levelno == logging.ERROR for rec in errors)  # still ERROR, only fewer of them
    assert any("20 more unexpected errors" in rec.message for rec in caplog.records)


# -- AC6: input ends the preview, nothing else does; it never locks ------------------------------


@pytest.mark.parametrize("kind", [INPUT_MOTION, INPUT_BUTTON, INPUT_KEY, INPUT_SCROLL])
@pytest.mark.parametrize("which", [0, 1, 2])
def test_ac6_any_input_on_any_monitors_window_ends_the_preview(tmp_path, backends, kind, which):
    r = rig(tmp_path, backends, windows=3)
    r.windows[which].fire_input(kind)
    assert not r.controller.running
    assert [w.closed for w in r.windows] == [1, 1, 1]  # every window closed, exactly once
    assert r.stops == ["input"]
    assert r.clock.pending == 0  # no timer left behind


def test_ac6_without_input_the_preview_keeps_going(tmp_path, backends):
    r = rig(tmp_path, backends)
    for _ in range(6):
        r.tick(10)
    assert r.controller.running and r.stops == [] and [w.closed for w in r.windows] == [0, 0]


def test_ac6_after_the_stop_late_results_and_repeated_input_do_nothing(tmp_path, backends):
    r = Rig(tmp_path, backends, ["a.png", "b.png"])
    r.controller.start()  # the first picture is still being prepared
    r.windows[1].fire_input(INPUT_KEY)
    r.worker.run_all()  # its result arrives after the stop
    r.windows[0].fire_input(INPUT_MOTION)
    r.windows[1].fire_input(INPUT_BUTTON)
    r.clock.advance(100)
    assert r.windows[0].frames == [] and r.windows[1].frames == []
    assert r.stops == ["input"] and [w.closed for w in r.windows] == [1, 1]


def test_ac6_an_explicit_stop_is_idempotent_and_the_preview_can_start_again(tmp_path, backends):
    r = rig(tmp_path, backends)
    r.controller.stop()
    r.controller.stop()
    assert r.stops == ["requested"]
    r.windows = [FakeWindow(), FakeWindow()]
    r.controller.start()
    r.worker.run_all()
    assert r.controller.running
    assert all(w.shown() for w in r.windows)


class Spy:
    """Wraps a collaborator and records the name of every attribute the controller uses."""

    def __init__(self, inner, log, owner):
        self._inner, self._log, self._owner = inner, log, owner

    def __getattr__(self, name):
        self._log.add((self._owner, name))
        return getattr(self._inner, name)

    def __len__(self):
        self._log.add((self._owner, "__len__"))
        return len(self._inner)


ALLOWED_CALLS = {
    ("source", "current"),
    ("source", "advance"),
    ("source", "connect_current_changed"),
    ("source", "scan_complete"),
    ("source", "__len__"),
    ("settings", "get_scaling"),
    ("settings", "get_pan_portrait_images"),
    ("settings", "get_slide_interval_seconds"),
    ("settings", "connect_changed"),
    ("window", "device_size"),
    ("window", "show_frame"),
    ("window", "show_message"),
    ("window", "connect_input"),
    ("window", "connect_size_changed"),
    ("window", "close"),
    ("scaler", "prepare"),
    ("clock", "now"),
    ("clock", "call_later"),
    ("worker", "submit"),
}


def test_ac6_d11_a_whole_preview_touches_nothing_but_the_picture_interfaces(tmp_path, backends):
    """The controller gets no session, lock or D-Bus object, and everything it does call is
    on the list of picture-and-timing methods. A preview with input, settings changes and a
    stop included. (CORE-1 owns locking; there is no lock call in here to find.)"""
    log = set()
    r = Rig(tmp_path, backends, ["a.png", "b.png"])
    spies = [Spy(w, log, "window") for w in r.windows]
    controller = PreviewController(
        Spy(r.source, log, "source"),
        Spy(r.settings, log, "settings"),
        lambda: spies,
        Spy(r.scaler, log, "scaler"),
        clock=Spy(r.clock, log, "clock"),
        worker=Spy(r.worker, log, "worker"),
    )
    controller.start()
    r.worker.run_all()
    r.clock.advance(10)
    r.settings.set(KEY_SCALING, "fit")
    r.settings.set(KEY_SLIDE_INTERVAL_SECONDS, 5)
    r.worker.run_all()
    r.windows[0].fire_input(INPUT_MOTION)
    assert not controller.running
    assert log, "the spies saw nothing"
    assert log <= ALLOWED_CALLS, sorted(log - ALLOWED_CALLS)


FORBIDDEN_WORDS = {
    "lock",
    "locked",
    "locking",
    "unlock",
    "lockscreen",
    "login1",
    "logind",
    "screensaver",
    "dbus",
    "systemd",
    "sessionmanager",
    "inhibit",
    # ways to start any program, and the loaders that reach C code without an import of a bus
    "system",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "startfile",
    "ctypes",
    "cdll",
}

#: Fragments of the squashed (lower case, letters and digits only) text of every identifier and
#: string constant. A fragment, not a word: the point is to catch ``busctl``, ``DBusProxy``,
#: ``bus_get_sync`` or ``org.gnome.ScreenSaver`` whatever they are glued to. ("lock" stays a whole
#: word above, or ``clock`` and ``O_NONBLOCK`` would be hits.)
FORBIDDEN_FRAGMENTS = {
    "screensaver",
    "dbus",
    "setactive",
    "loginctl",
    "busctl",
    "qdbus",
    "gdbus",
    "subprocess",
    "spawn",
    "pydbus",
    "suspend",
    "logout",
    "systemctl",
    "session",
    "bus",
    "login1",
    "logind",
    "systemd",
    "inhibit",
    "popen",
    # other lockers, by their program names (the GNOME and RHEL way is the one above)
    "locker",
    "securelock",
    "swaylock",
    "i3lock",
    "xlock",
    "xtrlock",
    "slock",
}

#: The few names that do contain one of the words above and are not a lock facility. Each one is
#: an exact name (squashed), with the reason. A test checks that every entry is still in use.
ALLOWED_NAMES = {
    "sessionsettings": "preview_app.SessionSettings: the settings of this one run",
    "showtheslideshowpreviewanyinputendsititneverlocksthesession": (
        "the --help text of preview_app: prose, tells the user that it never locks"
    ),
    "lockgraceperiodseconds": "settings.py: the lock-grace-period-seconds key, a stored number",
    "keylockgraceperiodseconds": "settings.py: its key constant",
    "getlockgraceperiodseconds": "settings.py: its getter",
    "setlockgraceperiodseconds": "settings.py: its setter",
    "lockgraceperiod": "preferences.py: the label of the grace period field, prose",
    (
        "inputsoonerthanthisaftertheslideshowstartsdoesnotlockthesession"
        "strictlysooner0meanseveryinputlocks"
    ): "preferences.py: the hint under the grace period field, prose about what the number does",
}


#: The names of the idle request of a manual preview, allowed in ``preview_app.py`` only, and the
#: environment variable of the translation directory, allowed in ``i18n.py`` only: the key is
#: (module, squashed name), so the same name in any other module is still a finding.
MODULE_ALLOWED_NAMES = {
    ("i18n.py", "slideshowlocklocaledir"): (
        "i18n.LOCALEDIR_ENV: SLIDESHOW_LOCK_LOCALEDIR, the product's own name in the name of the "
        "variable that points at the catalogs (upper case, so the own-name rule does not see it)"
    ),
    ("preview_app.py", "inhibit"): (
        "preview_app.IdleHold: Gtk.Application.inhibit, the application's own request to the "
        "desktop not to blank the screen under a manual preview. GTK makes the request: no bus, "
        "no session, no lock call in the preview code"
    ),
    ("preview_app.py", "uninhibit"): (
        "preview_app.IdleHold: Gtk.Application.uninhibit, giving that request back"
    ),
    ("preview_app.py", "applicationinhibitflags"): (
        "preview_app.IdleHold: Gtk.ApplicationInhibitFlags.IDLE, the idle flag of that request "
        "(the only one used; the logout, switch and suspend flags are not)"
    ),
}


def _squash(text: str) -> str:
    for own_name in ("slideshow_lock", "SlideshowLock", "Slideshow Lock", "slideshow-lock"):
        text = text.replace(own_name, "slideshow")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _words(text: str):
    """Lower-case words of an identifier or string: split on camel case and punctuation, and
    the whole name with the punctuation removed ("ScreenSaver" -> "screensaver"). The product's
    own name (slideshow_lock, SlideshowLock, "Slideshow Lock") is not a lock reference."""
    for own_name in ("slideshow_lock", "SlideshowLock", "Slideshow Lock", "slideshow-lock"):
        text = text.replace(own_name, "slideshow")
    squashed = re.sub(r"[^a-z0-9]", "", text.lower())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w} | {squashed}


def lock_references(source: str, used_allowances=None, module=None):
    """Identifiers, imports and string constants (docstrings excluded) that name a lock,
    session or bus facility: a forbidden word, or a forbidden fragment, in the name. Names on
    ``ALLOWED_NAMES``, and those on ``MODULE_ALLOWED_NAMES`` for *module* (the file name), are
    skipped (and noted in *used_allowances* if a set is given)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names.append(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                names.append(node.value)
        for name in names:
            squashed = _squash(name)
            hits = (_words(name) & FORBIDDEN_WORDS) | {
                fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in squashed
            }
            if hits and squashed in ALLOWED_NAMES:
                if used_allowances is not None:
                    used_allowances.add(squashed)
                continue
            if hits and (module, squashed) in MODULE_ALLOWED_NAMES:
                if used_allowances is not None:
                    used_allowances.add((module, squashed))
                continue
            found |= hits
    return found


def _package_file(module: str) -> str:
    return os.path.join(os.path.dirname(preview_module.__file__), module)


#: The modules of the service (CORE-1), which lock, talk to the session and use the bus on
#: purpose. Every other module of the package, the preview's, is scanned below. A module goes on
#: this list only by a decision made here: a new module is in the scan from the start.
LOCK_SIDE_MODULES = {
    "dbus_adapters.py",
    "service.py",
    "session.py",
    "sleep_guard.py",
    "state_machine.py",
}

#: Every other module of the package, ``__init__.py`` included: a list of the preview's own
#: modules let a literal ``os.system("true")`` in ``__init__.py`` through both layers (measured).
PACKAGE_MODULES = sorted(
    name
    for name in os.listdir(os.path.dirname(preview_module.__file__))
    if name.endswith(".py") and name not in LOCK_SIDE_MODULES
)


def imported_module_names(source: str):
    """Every module name an ``import`` in *source* can refer to (``from slideshow_lock import
    state_machine`` and ``import slideshow_lock.state_machine`` both give ``state_machine``)."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
    return names


def test_ac6_d11_the_lock_side_list_names_modules_that_exist():
    package = os.path.dirname(preview_module.__file__)
    assert all(os.path.exists(os.path.join(package, name)) for name in LOCK_SIDE_MODULES)


@pytest.mark.parametrize("module", PACKAGE_MODULES)
def test_ac6_d11_no_preview_module_imports_a_lock_side_module(module):
    """The preview code never reaches the state machine, the session adapters or the sleep
    guard, not even by an import it does not use."""
    with open(_package_file(module), encoding="utf-8") as handle:
        imported = imported_module_names(handle.read())
    assert imported & {name[: -len(".py")] for name in LOCK_SIDE_MODULES} == set()


def test_ac6_d11_the_import_check_would_catch_an_import_of_a_lock_side_module():
    assert "state_machine" in imported_module_names("from slideshow_lock.state_machine import X")
    assert "dbus_adapters" in imported_module_names("from slideshow_lock import dbus_adapters")
    assert "sleep_guard" in imported_module_names("import slideshow_lock.sleep_guard as g")
    assert imported_module_names("from slideshow_lock import preview") == {
        "slideshow_lock",
        "preview",
    }


@pytest.mark.parametrize("module", PACKAGE_MODULES)
def test_ac6_d11_the_preview_code_has_no_lock_session_or_bus_reference(module):
    with open(_package_file(module), encoding="utf-8") as handle:
        assert lock_references(handle.read(), module=module) == set()


def test_ac6_d11_every_allowed_name_is_still_in_use():
    """An allowance for a name that no longer exists would silently allow its return."""
    used = set()
    for module in PACKAGE_MODULES:
        with open(_package_file(module), encoding="utf-8") as handle:
            lock_references(handle.read(), used, module)
    assert used == set(ALLOWED_NAMES) | set(MODULE_ALLOWED_NAMES)


def test_ac6_d11_the_scan_would_catch_a_lock_call(tmp_path):
    """Negative control: the scan is not blind."""
    assert lock_references("session.Lock()") >= {"lock", "session"}
    assert lock_references(
        "proxy = Gio.DBusProxy.new_for_bus_sync(name='org.freedesktop.login1')"
    ) >= {"login1", "dbus", "bus"}
    assert lock_references("from gi.repository import ScreenSaver") == {"screensaver"}
    assert lock_references("def lock_session(self): pass") >= {"lock"}
    assert lock_references("clock = block = GLibClock()") == set()  # whole words only
    assert lock_references('"""never locks"""\nx = 1') == set()  # docstrings are prose
    assert lock_references("from slideshow_lock import preview") == set()  # the product's name
    assert lock_references("x = 'slideshow-lock'") == set()


def test_ac6_d11_the_idle_request_names_are_allowed_in_preview_app_and_nowhere_else():
    request = 'self._application.inhibit(w, Gtk.ApplicationInhibitFlags.IDLE, "r")\nuninhibit(1)'
    assert lock_references(request, module="preview_app.py") == set()
    assert lock_references(request) >= {"inhibit"}  # no module: no allowance
    assert lock_references(request, module="preview.py") >= {"inhibit"}
    assert lock_references(request, module="preferences.py") >= {"inhibit"}


def idle_request_findings(source: str):
    """What is wrong with the idle request in *source*, as a list of reasons (empty: nothing).

    The request is the call ``<x>.inhibit(window, flags, reason)``; its flags must be exactly
    ``Gtk.ApplicationInhibitFlags.IDLE``: not a number, not another flag (logout, switch,
    suspend), not a combination. Any other mention of ``ApplicationInhibitFlags`` is a finding
    too, so the flag cannot be taken from somewhere else."""
    tree = ast.parse(source)
    problems = []
    flag_nodes = []
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "inhibit"
    ]
    if len(calls) != 1:
        problems.append(f"{len(calls)} calls of inhibit, expected 1")
    for call in calls:
        flags = call.args[1] if len(call.args) > 1 else None
        if not (
            isinstance(flags, ast.Attribute)
            and flags.attr == "IDLE"
            and isinstance(flags.value, ast.Attribute)
            and flags.value.attr == "ApplicationInhibitFlags"
            and isinstance(flags.value.value, ast.Name)
            and flags.value.value.id == "Gtk"
        ):
            problems.append(f"flags are not Gtk.ApplicationInhibitFlags.IDLE: {ast.dump(flags)}")
        else:
            flag_nodes.append(flags.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "ApplicationInhibitFlags":
            if node not in flag_nodes:
                problems.append("ApplicationInhibitFlags used outside the idle request")
    return problems


def test_ac6_d11_the_idle_request_in_preview_app_uses_the_idle_flag_and_nothing_else():
    with open(_package_file("preview_app.py"), encoding="utf-8") as handle:
        assert idle_request_findings(handle.read()) == []


def test_ac6_d11_the_idle_flag_check_is_not_blind():
    """Negative controls: another flag, a number, a combination, the flag taken from elsewhere."""
    request = "self._application.inhibit(window, {flags}, HOLD_REASON)"
    good = request.format(flags="Gtk.ApplicationInhibitFlags.IDLE")
    assert idle_request_findings(good) == []
    for flags in (
        "Gtk.ApplicationInhibitFlags.SWITCH",
        "Gtk.ApplicationInhibitFlags.LOGOUT",
        "Gtk.ApplicationInhibitFlags.SUSPEND",
        "8",
        "15",
        "Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SWITCH",
        "FLAGS",
    ):
        assert idle_request_findings(request.format(flags=flags)), flags
    assert idle_request_findings(good + "\nx = Gtk.ApplicationInhibitFlags.SWITCH")
    assert idle_request_findings("x = 1")  # no request at all
    assert idle_request_findings(good + "\n" + good)  # a second request


#: Calls that really lock, or reach the machinery that does, as a line of code each. They run
#: or fail at runtime wherever they are put; the scan must find every one of them by the names
#: and strings alone. (``Gio`` is the one GTK code already imports.)
LOCK_SNIPPETS = {
    "busctl": 'subprocess.run(["busctl", "--user", "call", "org.gnome.ScreenSaver"], check=False)',
    "gdbus": 'os.system("gdbus call --session --dest org.gnome.ScreenSaver --method Lock")',
    "qdbus": 'os.popen("qdbus org.gnome.ScreenSaver /org/gnome/ScreenSaver SetActive true")',
    "loginctl": 'GLib.spawn_command_line_async("loginctl lock-session")',
    "gio-call-sync": (
        'Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync("org.gnome.ScreenSaver", '
        '"/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", "SetActive", None, None, 0, -1, None)'
    ),
    "systemctl": 'os.system("systemctl suspend")',
    "pydbus": "import pydbus",
    "bus-socket": 'socket.socket(socket.AF_UNIX).connect("/run/user/1000/bus")',
    "logout": 'x.call("Logout")',
    # the names the preview's idle request is allowed to use, used for something else, in a
    # module that has no allowance
    "inhibit-call": 'x.call("Inhibit", args)',
    "inhibit-all-flags": 'x.inhibit(w, 15, "r")',
    # starting any program, with nothing in the line that names a lock facility
    "os.system": 'os.system("true")',
    "os.execv": 'os.execv("/bin/true", ["true"])',
    "os.execve": 'os.execve("/bin/true", ["true"], {})',
    "os.execvp": 'os.execvp("true", ["true"])',
    "os.execvpe": 'os.execvpe("true", ["true"], {})',
    "os.execl": 'os.execl("/bin/true", "true")',
    "os.execle": 'os.execle("/bin/true", "true", {})',
    "os.execlp": 'os.execlp("true", "true")',
    "os.execlpe": 'os.execlpe("true", "true", {})',
    "os.startfile": 'os.startfile("x")',
    "ctypes-import": "import ctypes",
    "cdll": 'cdll.LoadLibrary("libc.so.6")',
    # other lockers, by program name
    "swaylock": 'x = ["swaylock", "-f"]',
    "i3lock": 'x = ["i3lock"]',
    "xtrlock": 'x = ["xtrlock"]',
    "xlock": 'x = ["xlock"]',
    "slock": 'x = ["slock"]',
    "light-locker": 'x = ["light-locker-command", "--lock"]',
    "securelock": 'x = "securelock"',
}


@pytest.mark.parametrize("name", sorted(LOCK_SNIPPETS))
def test_ac6_d11_the_scan_finds_a_real_lock_call_hidden_in_stop(name):
    """The injection test of the review: the line goes into ``PreviewController.stop`` of the real
    source. The method spy test cannot see it (it only watches the objects handed in)."""
    with open(_package_file("preview.py"), encoding="utf-8") as handle:
        original = handle.read()
    anchor = "        self._running = False\n        self._cancel_timers()\n"
    assert original.count(anchor) == 1
    mutated = original.replace(anchor, anchor + "        " + LOCK_SNIPPETS[name] + "\n")
    assert lock_references(original, module="preview.py") == set()
    assert lock_references(mutated, module="preview.py"), f"{name}: the scan did not see it"


# -- AC7: decoding and scaling never run on the main loop ----------------------------------------


class InlineWorker:
    """What a controller without a worker thread would amount to: the work on the main loop."""

    def submit(self, fn, done):
        result, error = None, None
        try:
            result = fn()
        except Exception as exc:
            error = exc
        done(result, error)


def _pump(predicate, timeout=10.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            time.sleep(0.002)
    return predicate()


class SlowScaler(FakeScaler):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds

    def prepare(self, path, sizes, mode, pan=False):
        time.sleep(self.seconds)
        return super().prepare(path, sizes, mode, pan)


def _longest_main_loop_gap(tmp_path, backends, worker, work_seconds):
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1, scaler=SlowScaler(work_seconds))
    controller = PreviewController(
        r.source, r.settings, lambda: r.windows, r.scaler, clock=GLibClock(), worker=worker
    )
    gaps = []
    last = [time.monotonic()]

    def probe():
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now
        return GLib.SOURCE_CONTINUE

    source_id = GLib.timeout_add(10, probe)
    try:
        with hard_timeout(30):
            controller.start()
            assert _pump(lambda: len(r.windows[0].frames) >= 1)
            _pump(lambda: False, timeout=0.7)  # long enough for the prefetch work to run as well
    finally:
        GLib.source_remove(source_id)
        controller.stop()
    return max(gaps), r.scaler


def test_ac7_picture_work_runs_on_another_thread_and_the_main_loop_keeps_turning(
    tmp_path, backends
):
    worker = ThreadWorker()
    try:
        gap, scaler = _longest_main_loop_gap(tmp_path, backends, worker, work_seconds=0.4)
    finally:
        worker.close()
    assert set(scaler.threads) and threading.get_ident() not in scaler.threads
    assert gap < 0.15, f"the main loop stood still for {gap:.3f} s"


@pytest.mark.parametrize("change", ["scaling", "resize"])
def test_ac7_the_redo_after_a_settings_or_size_change_runs_on_the_worker_too(
    tmp_path, backends, change
):
    """The first picture and the prefetch are covered above; the redo of the shown picture (a
    different job, queued from the settings and size handlers) must not run on the main loop
    either. Every call of the scaler is on the worker thread, and the redo did happen."""
    r = Rig(tmp_path, backends, ["a.png", "b.png"], windows=1)
    worker = ThreadWorker()
    controller = PreviewController(
        r.source, r.settings, lambda: r.windows, r.scaler, clock=GLibClock(), worker=worker
    )
    window = r.windows[0]
    try:
        with hard_timeout(30):
            controller.start()
            assert _pump(lambda: len(window.frames) >= 1 and len(r.scaler.calls) >= 2)
            before = len(r.scaler.calls)
            if change == "scaling":
                r.settings.set(KEY_SCALING, "fit")
                assert _pump(lambda: window.frames[-1][0].method == "fake-fit-0")
            else:
                window.resize((2560, 1440))
                assert _pump(lambda: window.frames[-1][0].width == 2560)
    finally:
        controller.stop()
        worker.close()
    assert len(r.scaler.calls) > before  # the redo ran ...
    assert threading.get_ident() not in r.scaler.threads  # ... and no call was on this thread


def test_ac7_the_result_is_handed_over_on_the_main_loop_and_only_there(tmp_path, backends):
    """ "GTK from the main thread only": the worker thread must not call ``done`` itself.
    It queues the call on the GLib main loop, so nothing arrives while the loop is not run."""
    worker = ThreadWorker()
    seen = []
    ran_on = []

    def work():
        ran_on.append(threading.get_ident())
        return 42

    try:
        worker.submit(work, lambda result, error: seen.append((result, threading.get_ident())))
        deadline = time.monotonic() + 5
        while not ran_on and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.2)  # the work is long done; the main loop has not been run
        assert ran_on and ran_on[0] != threading.get_ident()
        assert seen == [], "the worker thread called done() itself"
        assert _pump(lambda: seen)
        assert seen == [(42, threading.get_ident())]  # delivered by the main loop, in this thread
    finally:
        worker.close()


def test_ac7_closing_the_worker_ends_its_thread(tmp_path, backends):
    worker = ThreadWorker()
    done = []
    worker.submit(lambda: 42, lambda result, error: done.append((result, error)))
    assert _pump(lambda: done)
    assert done == [(42, None)]
    thread = worker._thread
    assert thread.is_alive() and thread.daemon  # a stuck picture can never keep the process alive
    worker.close()
    thread.join(5)
    assert not thread.is_alive()


def test_ac7_negative_control_the_same_work_on_the_main_loop_is_caught(tmp_path, backends):
    gap, scaler = _longest_main_loop_gap(tmp_path, backends, InlineWorker(), work_seconds=0.4)
    assert threading.get_ident() in scaler.threads
    assert gap >= 0.35, f"the measurement did not see the block ({gap:.3f} s)"


# -- AC8: log convention -------------------------------------------------------------------------


def test_ac8_start_and_stop_are_logged_with_the_trigger_source(tmp_path, backends, caplog):
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        r = rig(tmp_path, backends)
        r.windows[0].fire_input(INPUT_KEY)
    lines = [m for m in caplog.messages if m.startswith("[slideshow]")]
    assert "[slideshow] started (source=preview, monitors=2)" in lines
    assert "[slideshow] stopped (source=preview, reason=input)" in lines
    assert all(rec.levelno == logging.INFO for rec in caplog.records if rec.message in lines)


def test_ac8_no_monitor_means_no_preview_and_a_warning(tmp_path, backends, caplog):
    r = Rig(tmp_path, backends, ["a.png"])
    controller = PreviewController(
        r.source, r.settings, lambda: [], r.scaler, clock=r.clock, worker=r.worker
    )
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        controller.start()
    assert not controller.running
    assert any("no monitor" in m for m in caplog.messages)


# -- helpers -------------------------------------------------------------------------------------


def _fs(name):
    from slideshow_lock.image_source import FsEvent

    return FsEvent(name.replace("_", "-"))
