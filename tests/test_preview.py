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
        self.calls = []
        self.threads = []

    def prepare(self, path, sizes, mode, pan=False):
        self.calls.append((os.path.basename(path), list(sizes), mode, pan))
        self.threads.append(threading.get_ident())
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
        **source_kwargs,
    ):
        self.root = tmp_path
        for name in files:
            make_image(tmp_path / name)
        self.backends = backends
        self.source = started(tmp_path, backends, order=order, **source_kwargs)
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
}


def _words(text: str):
    """Lower-case words of an identifier or string: split on camel case and punctuation, and
    the whole name with the punctuation removed ("ScreenSaver" -> "screensaver"). The product's
    own name (slideshow_lock, SlideshowLock, "Slideshow Lock") is not a lock reference."""
    for own_name in ("slideshow_lock", "SlideshowLock", "Slideshow Lock"):
        text = text.replace(own_name, "slideshow")
    squashed = re.sub(r"[^a-z0-9]", "", text.lower())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w} | {squashed}


def lock_references(source: str):
    """Identifiers, imports and string constants (docstrings excluded) that name a lock,
    session or bus facility."""
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
            found |= _words(name) & FORBIDDEN_WORDS
    return found


PREVIEW_MODULES = ["preview.py", "preview_window.py", "preview_app.py", "scaling.py"]


@pytest.mark.parametrize("module", PREVIEW_MODULES)
def test_ac6_d11_the_preview_code_has_no_lock_session_or_bus_reference(module):
    path = os.path.join(os.path.dirname(preview_module.__file__), module)
    with open(path, encoding="utf-8") as handle:
        assert lock_references(handle.read()) == set()


def test_ac6_d11_the_scan_would_catch_a_lock_call(tmp_path):
    """Negative control: the scan is not blind."""
    assert lock_references("session.Lock()") == {"lock"}
    assert lock_references(
        "proxy = Gio.DBusProxy.new_for_bus_sync(name='org.freedesktop.login1')"
    ) >= {"login1"}
    assert lock_references("from gi.repository import ScreenSaver") == {"screensaver"}
    assert lock_references("def lock_session(self): pass") == {"lock"}
    assert lock_references("clock = block = GLibClock()") == set()  # whole words only
    assert lock_references('"""never locks"""\nx = 1') == set()  # docstrings are prose
    assert lock_references("from slideshow_lock import preview") == set()  # the product's name


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
