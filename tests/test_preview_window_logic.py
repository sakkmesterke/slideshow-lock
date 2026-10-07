"""The input logic of ``PreviewWindow``, without a display.

``PreviewWindow.__init__`` builds a real GTK window, which needs a compositor. Its event handlers
are plain methods that only look at a few attributes, so they are run on an instance made
without ``__init__``. What is not covered here, because it is wired in the constructor, is
which GTK controller calls which handler (the click gesture, the cursor being hidden): that
is what ``tools/wayland-smoke`` checks on a real compositor.
"""

from __future__ import annotations

import pytest
from gi.repository import GLib

from slideshow_lock import preview_window
from slideshow_lock import transition_draw as td
from slideshow_lock.preview import (
    INPUT_BUTTON,
    INPUT_CLOSE,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
)
from slideshow_lock.preview_window import MOTION_THRESHOLD_PIXELS, PreviewWindow, _Canvas
from slideshow_lock.scaling import Frame
from slideshow_lock.transitions import ALL_TRANSITIONS
from tests.timeout_guard import (
    per_test_deadline,  # noqa: F401  (autouse fixture)
)


def make_window(listen=True):
    window = PreviewWindow.__new__(PreviewWindow)
    window._input_callbacks = []
    window._origin = None
    window._entered = False
    seen = []
    if listen:
        window.connect_input(seen.append)
    return window, seen


def test_the_first_enter_is_a_pointer_resting_under_the_window_and_is_not_input():
    window, seen = make_window()
    window._on_enter(None, 400.0, 300.0)
    assert seen == []
    assert window._origin == (400.0, 300.0)


def test_entering_again_later_is_the_pointer_arriving_and_is_input():
    window, seen = make_window()
    window._on_enter(None, 400.0, 300.0)
    window._on_enter(None, 10.0, 10.0)
    assert seen == [INPUT_MOTION]
    assert window._origin == (400.0, 300.0)  # the baseline stays where the window first saw it


def test_motion_is_measured_from_where_the_window_first_saw_the_pointer():
    assert MOTION_THRESHOLD_PIXELS == 2.0
    window, seen = make_window()
    window._on_enter(None, 400.0, 300.0)
    window._on_motion(None, 401.0, 300.0)  # 1 px
    window._on_motion(None, 401.4, 301.4)  # 1.98 px, diagonal
    window._on_motion(None, 400.0, 300.0)  # back on the baseline
    assert seen == []
    window._on_motion(None, 402.0, 300.0)  # exactly the threshold counts
    assert seen == [INPUT_MOTION]
    window, seen = make_window()
    window._on_enter(None, 400.0, 300.0)
    window._on_motion(None, 400.0, 297.0)  # up, not only to the right
    assert seen == [INPUT_MOTION]


def test_without_an_enter_the_first_motion_is_the_baseline_and_the_next_one_counts():
    window, seen = make_window()
    window._on_motion(None, 100.0, 100.0)
    assert seen == [] and window._origin == (100.0, 100.0)
    window._on_motion(None, 101.0, 100.0)
    assert seen == []
    window._on_motion(None, 105.0, 100.0)
    assert seen == [INPUT_MOTION]


def test_a_key_press_is_input_and_is_consumed():
    window, seen = make_window()
    assert window._on_key(None, 65, 38, 0) is True
    assert seen == [INPUT_KEY]


def test_a_scroll_is_input_and_is_consumed():
    window, seen = make_window()
    assert window._on_scroll(None, 0.0, 1.0) is True
    assert seen == [INPUT_SCROLL]


def test_a_window_closed_from_outside_reports_it_and_stays_for_close_to_destroy():
    window, seen = make_window()
    assert window._on_close_request(None) is True  # True: GTK must not destroy it itself
    assert seen == [INPUT_CLOSE]
    alone, nobody = make_window(listen=False)
    assert alone._on_close_request(None) is False  # nobody listens: close as GTK would
    assert nobody == []


def test_the_input_kinds_are_told_apart():
    assert len({INPUT_MOTION, INPUT_BUTTON, INPUT_KEY, INPUT_SCROLL, INPUT_CLOSE}) == 5
    assert INPUT_CLOSE == "close" and INPUT_KEY == "key"


@pytest.mark.parametrize("callbacks", [0, 2])
def test_every_listener_hears_every_input(callbacks):
    window, _seen = make_window(listen=False)
    heard = [[] for _ in range(callbacks)]
    for sink in heard:
        window.connect_input(sink.append)
    window._on_key(None)
    assert all(sink == [INPUT_KEY] for sink in heard)


# -- a panning frame, and the desktop's animation choice at the moment it is shown -----------


def window_for_frames(monkeypatch):
    """A canvas that records what it is asked to do instead of drawing."""
    window = _Canvas.__new__(_Canvas)
    window._frame = window._texture = None
    window._tick_id = 0
    window._pan_t0 = None
    window._offset = (0, 0)
    window._old_texture = window._old_frame = window._run = None
    window._old_offset = (0, 0)
    window._reduced = {}
    window._run_tick_id = 0
    window._move = window._old_move = None
    window._move_tick_id = 0
    window._now = None
    window._drawn_at = 0
    window._scale = lambda: 1.0
    calls = []
    monkeypatch.setattr(_Canvas, "add_tick_callback", lambda self, fn: calls.append("tick") or 7)
    monkeypatch.setattr(_Canvas, "remove_tick_callback", lambda self, id_: calls.append("untick"))
    monkeypatch.setattr(_Canvas, "queue_draw", lambda self: None)
    monkeypatch.setattr(_Canvas, "get_width", lambda self: 4)  # the window is the frames' size
    monkeypatch.setattr(_Canvas, "get_height", lambda self: 3)
    monkeypatch.setattr(preview_window, "software_gl", lambda widget: False)
    return window, calls


def tall_frame(pan_range=(0, 600)):
    return Frame("p.png", 4, 12, 12, bytes(12 * 12), "fake", pan_range)


@pytest.mark.parametrize("enabled", [True, False], ids=["animations-on", "animations-off"])
def test_a_panning_frame_scrolls_only_while_the_desktop_animates(monkeypatch, enabled):
    """The controller no longer asks for a tall frame when animations are off, but a frame made
    just before the choice changed can still arrive: the window then shows its middle (the
    controller's centre crop is the same part) and starts no tick."""
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: enabled)
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(tall_frame((10, 600)), 5.0)
    if enabled:
        assert calls == ["tick"] and window._offset == (0, 0)
    else:
        assert calls == [] and window._offset == (5, 300)


def test_a_frame_that_does_not_pan_starts_no_pan_tick_whatever_the_animation_choice(monkeypatch):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: True)
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(tall_frame((0, 0)), 5.0)
    assert window._tick_id == 0 and window._offset == (0, 0)
    assert calls == ["tick"] and window._move is not None  # the tick of its slow move


class _FakeGtkSettings:
    """What ``Gtk.Settings.get_default()`` returns, with the one property the code may read."""

    def __init__(self, enable_animations):
        self._properties = {"gtk-enable-animations": enable_animations}
        self.asked = []

    def get_property(self, name):
        self.asked.append(name)
        return self._properties[name]  # any other name is a KeyError


def _desktop_says(monkeypatch, value):
    fake = _FakeGtkSettings(value)
    monkeypatch.setattr(preview_window.Gtk.Settings, "get_default", staticmethod(lambda: fake))
    return fake


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (1, True), (0, False)])
def test_animations_follow_the_desktops_gtk_enable_animations_property(
    monkeypatch, value, expected
):
    fake = _desktop_says(monkeypatch, value)
    assert preview_window.animations_enabled() is expected
    assert fake.asked == ["gtk-enable-animations"]


def test_animations_are_read_again_for_every_call_so_a_change_takes_effect_with_the_next_picture(
    monkeypatch,
):
    fake = _desktop_says(monkeypatch, True)
    assert preview_window.animations_enabled() is True
    fake._properties["gtk-enable-animations"] = False
    assert preview_window.animations_enabled() is False


def test_animations_stay_on_when_there_is_no_default_gtk_settings_object(monkeypatch):
    monkeypatch.setattr(preview_window.Gtk.Settings, "get_default", staticmethod(lambda: None))
    assert preview_window.animations_enabled() is True


# -- a transition: when it starts, when it ends, what is drawn -----------------------------------


def flat_frame(name="f.png"):
    return Frame(name, 4, 3, 12, bytes(12 * 3), "fake", (0, 0))


class _Clock:
    def __init__(self, seconds):
        self.us = int(seconds * 1_000_000)

    def get_frame_time(self):
        return self.us


def canvas_with_a_picture(monkeypatch):
    """A canvas that shows one frame already (the old one), and records its tick calls."""
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(flat_frame("old.png"), 0.0)
    calls.clear()
    return window, calls


def test_a_new_frame_with_a_transition_keeps_the_old_texture_and_starts_a_tick(monkeypatch):
    window, calls = canvas_with_a_picture(monkeypatch)
    old = window._texture
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    assert calls == ["tick"]
    assert window._old_texture is old and window._texture is not old
    assert window._run.name == "crossfade" and window._run.seconds == 1.0


def test_a_new_frame_without_a_transition_is_a_cut_and_forgets_the_old_texture(monkeypatch):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0)
    assert calls == [] and window._old_texture is None and window._run is None


def test_the_first_picture_has_nothing_to_change_from(monkeypatch):
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(flat_frame(), 0.0, ("crossfade", 1.0))
    assert calls == [] and window._old_texture is None and window._run is None


def test_the_same_frame_again_is_not_a_change(monkeypatch):
    window, calls = window_for_frames(monkeypatch)
    frame = flat_frame()
    window.set_frame(frame, 0.0)
    calls.clear()
    window.set_frame(frame, 0.0, ("crossfade", 1.0))
    assert calls == [] and window._run is None


@pytest.mark.parametrize("name,seconds", [("sparkle", 1.0), ("crossfade", 0.0)])
def test_a_transition_that_cannot_be_drawn_is_a_cut(monkeypatch, name, seconds):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, (name, seconds))
    assert calls == [] and window._run is None and window._old_texture is None


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_every_one_of_the_ten_starts_a_run(monkeypatch, name):
    window, calls = canvas_with_a_picture(monkeypatch)
    monkeypatch.setattr(preview_window, "_reduced_texture", lambda frame, factor: object())
    window.set_frame(flat_frame("new.png"), 4.0, (name, 0.8))
    assert calls == ["tick", "tick"] and window._run is not None  # the slow move's and the run's
    assert window._run.name == name  # the frames fill the window, so Ken Burns stays Ken Burns


def test_ken_burns_runs_as_long_as_its_fade_and_the_slow_move_is_the_pictures_own(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 9.0, ("ken-burns", 0.8))
    assert window._run.name == "ken-burns" and window._run.seconds == 0.8


def test_ken_burns_on_a_picture_that_does_not_fill_the_window_is_a_cross_fade(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    small = Frame("s.png", 2, 3, 6, bytes(6 * 3), "fake", (0, 0))
    window.set_frame(small, 9.0, ("ken-burns", 0.8))
    assert window._run.name == "crossfade" and window._run.seconds == 0.8


def test_the_blur_is_a_cross_fade_on_software_rendering_and_nothing_is_reduced(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    monkeypatch.setattr(preview_window, "software_gl", lambda widget: True)

    def never(frame, factor):
        raise AssertionError("no reduced picture is needed")

    monkeypatch.setattr(preview_window, "_reduced_texture", never)
    window.set_frame(flat_frame("new.png"), 0.0, ("blur", 1.2))
    assert window._run.name == "crossfade" and window._reduced == {}


def test_the_blur_reduces_both_pictures_and_forgets_them_at_the_end(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    made = []
    monkeypatch.setattr(
        preview_window,
        "_reduced_texture",
        lambda frame, factor: made.append((frame.path, factor)) or object(),
    )
    window.set_frame(flat_frame("new.png"), 0.0, ("blur", 1.2))
    assert window._run.name == "blur"
    assert sorted(made) == [("new.png", 4), ("old.png", 4)]
    assert set(window._reduced) == {"old", "new"}
    window._end_transition()
    assert window._reduced == {}


def test_a_blur_that_cannot_make_its_pictures_is_a_cross_fade_and_says_so(monkeypatch, caplog):
    window, _calls = canvas_with_a_picture(monkeypatch)

    def fails(frame, factor):
        raise ValueError("no pixbuf")

    monkeypatch.setattr(preview_window, "_reduced_texture", fails)
    with caplog.at_level("WARNING"):
        window.set_frame(flat_frame("new.png"), 0.0, ("blur", 1.2))
    assert window._run.name == "crossfade"
    assert "blur transition not possible" in caplog.text


@pytest.mark.parametrize("transition", [None, ("crossfade", 1.0)], ids=["cut", "another"])
def test_any_call_of_set_frame_ends_a_running_transition_at_once(monkeypatch, transition):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("b.png"), 0.0, ("fade-black", 1.2))
    calls.clear()
    window.set_frame(flat_frame("c.png"), 0.0, transition)
    assert calls[0] == "untick"  # the running one is stopped first
    assert (window._run.name if window._run else None) == (transition[0] if transition else None)


def test_a_message_or_a_close_ends_a_running_transition_and_drops_both_textures(monkeypatch):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("b.png"), 0.0, ("crossfade", 1.0))
    calls.clear()
    window.set_frame(None, 0.0)  # what show_message and close do
    assert calls == ["untick"]
    assert window._run is None and window._old_texture is None and window._texture is None


def test_the_old_picture_stands_where_it_stopped_and_the_new_one_starts_at_the_top(monkeypatch):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: True)
    window, _calls = window_for_frames(monkeypatch)
    window.set_frame(tall_frame((0, 600)), 5.0)
    window._offset = (0, 600)  # the pan of the old picture has run to its end
    window.set_frame(tall_frame((0, 600)), 5.0, ("crossfade", 1.0))
    assert window._old_offset == (0, 600) and window._offset == (0, 0)


def run_ticks(window, times):
    """Deliver a tick at each of *times* (seconds); the list of what each returned."""
    return [window._on_transition_tick(None, _Clock(t)) for t in times]


def test_the_clock_starts_at_the_second_tick_and_the_first_frame_is_at_progress_zero(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    run_ticks(window, [100.0])  # the first tick: the new texture is uploaded
    assert window._run.first and window._run.progress == 0.0
    run_ticks(window, [100.2])  # the second tick: this is where the time starts
    assert not window._run.first and window._run.progress == 0.0


def test_the_progress_follows_the_frame_clock_not_the_number_of_frames(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 2.0))
    run_ticks(window, [10.0, 10.0])
    assert run_ticks(window, [10.5]) == [GLib.SOURCE_CONTINUE]
    assert window._run.progress == pytest.approx(0.25)
    run_ticks(window, [11.0])
    assert window._run.progress == pytest.approx(0.5)  # one frame later, a longer jump
    run_ticks(window, [11.9])
    assert window._run.progress == pytest.approx(0.95)


@pytest.mark.parametrize("frames", [[10.0, 10.0, 11.0], [10.0, 10.0, 10.5, 11.4]])
def test_the_transition_ends_when_the_time_is_up_whatever_the_number_of_frames(monkeypatch, frames):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    calls.clear()
    results = run_ticks(window, frames)
    assert results[-1] == GLib.SOURCE_REMOVE and GLib.SOURCE_REMOVE not in results[:-1]
    assert window._run is None and window._old_texture is None
    assert calls == []  # the tick returned SOURCE_REMOVE: it is not removed a second time


class _Snapshot:
    """Records the drawing calls ``_Canvas`` makes."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))

        return record


def test_after_the_end_the_canvas_draws_the_one_picture_plainly(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    run_ticks(window, [5.0, 5.0, 5.5])
    during = _Snapshot()
    window.do_snapshot(during)
    assert "push_opacity" in [c[0] for c in during.calls]  # while it runs: the new one fades in
    run_ticks(window, [6.5])
    after = _Snapshot()
    window.do_snapshot(after)
    assert [c[0] for c in after.calls] == ["append_color", "append_texture"]
    assert after.calls[1][1] is window._texture  # 1:1, not a filtered last frame


def test_the_first_frame_draws_both_pictures_and_the_second_draws_them_at_progress_zero(
    monkeypatch,
):
    """Where the new texture is uploaded, out of sight: even the fade through black, which has
    no new picture in its first half, draws it then (at one pixel, with almost no opacity)."""
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("fade-black", 1.2))
    snapshot = _Snapshot()
    window.do_snapshot(snapshot)
    textures = [c[1] for c in snapshot.calls if c[0] == "append_texture"]
    assert window._old_texture in textures and window._texture in textures
    assert snapshot.calls[0][0] == "append_color"  # black comes first


# -- the soft edges: the calls the canvas makes for them -----------------------------------------


class _Texture:
    def __init__(self, width, height):
        self._size = (width, height)

    def get_width(self):
        return self._size[0]

    def get_height(self):
        return self._size[1]


class _SnapshotWithoutMasks(_Snapshot):
    """GTK before 4.10: no ``push_mask``."""

    def __getattr__(self, name):
        if name == "push_mask":
            raise AttributeError(name)
        return super().__getattr__(name)


@pytest.fixture(autouse=False)
def mask_mode(monkeypatch):
    """``Gsk.MaskMode`` does not exist before GTK 4.10 (the tests of the calls run on any GTK)."""
    import types

    monkeypatch.setattr(
        preview_window.Gsk, "MaskMode", types.SimpleNamespace(ALPHA="alpha"), raising=False
    )


def painting_canvas(width=64, height=36):
    canvas = _Canvas.__new__(_Canvas)
    canvas._scale = lambda: 1.0
    canvas._old_texture, canvas._texture = _Texture(width, height), _Texture(width, height)
    canvas._old_offset = canvas._offset = (0, 0)
    canvas._reduced = {}
    return canvas


def paint_calls(name, progress, snapshot=None):
    """The names of the calls the canvas makes to paint the new picture of *name* at *progress*."""
    from slideshow_lock import transition_draw as td

    snapshot = snapshot if snapshot is not None else _Snapshot()
    draw = td.compose(name, progress, 64.0, 36.0)[-1]
    painting_canvas()._paint(snapshot, draw, 64.0, 36.0)
    return [call[0] for call in snapshot.calls]


def test_a_soft_edge_is_a_mask_whose_gradient_comes_first_and_the_picture_after_it(mask_mode):
    """GTK takes what is recorded before the first ``pop`` as the mask, the rest as the picture it
    cuts: the other way round, the gradient would be painted, masked by the picture."""
    names = paint_calls("slide-in", 0.5)
    start = names.index("push_mask")
    assert names[start : start + 5] == [
        "push_mask",
        "append_linear_gradient",
        "pop",
        "append_texture",
        "pop",
    ]
    names = paint_calls("circle", 0.5)
    start = names.index("push_mask")
    assert names[start : start + 3] == ["push_mask", "append_radial_gradient", "pop"]
    assert names.index("append_texture") > start + 2


def test_an_edge_on_two_axes_is_two_masks_one_inside_the_other(mask_mode):
    names = paint_calls("zoom", 0.25)
    start = names.index("push_mask")
    assert names[start : start + 9] == [
        "push_mask",
        "append_linear_gradient",
        "pop",
        "push_mask",
        "append_linear_gradient",
        "pop",
        "append_texture",
        "pop",
        "pop",
    ]


@pytest.mark.parametrize("name", ("slide-in", "push", "wipe", "circle", "zoom", "rotate"))
@pytest.mark.parametrize("progress", (0.0, 0.1, 0.5, 0.9, 1.0))
def test_every_push_is_popped_with_or_without_masks(mask_mode, name, progress):
    for snapshot in (_Snapshot(), _SnapshotWithoutMasks()):
        names = paint_calls(name, progress, snapshot)
        pushes = [n for n in names if n.startswith("push_")]
        assert names.count("pop") == len(pushes) + names.count("push_mask")  # a mask ends twice
        assert names.count("save") == names.count("restore")
        for position, called in enumerate(names):
            if called.endswith("_gradient"):
                assert names[position - 1] == "push_mask"


@pytest.mark.parametrize("name", ("slide-in", "push", "wipe", "circle", "zoom", "rotate"))
def test_before_gtk_4_10_the_edges_stay_as_they_were_cut(name):
    names = paint_calls(name, 0.5, _SnapshotWithoutMasks())
    assert "push_mask" not in names and not any(n.endswith("_gradient") for n in names)
    assert "append_texture" in names


# -- the slow move of a picture: always on, through the transitions ------------------------------


def moving_canvas(monkeypatch, seconds=20.0):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: True)
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(flat_frame("old.png"), seconds)
    calls.clear()
    return window, calls


def test_a_picture_that_does_not_scroll_has_a_slow_move_as_long_as_it_and_its_transition_live(
    monkeypatch,
):
    window, _calls = moving_canvas(monkeypatch)
    assert window._move.span == td.picture_seconds(20.0)


@pytest.mark.parametrize("pan_range,enabled", [((0, 600), True), ((0, 0), False)])
def test_a_scrolling_picture_or_no_animations_means_no_slow_move(monkeypatch, pan_range, enabled):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: enabled)
    window, _calls = window_for_frames(monkeypatch)
    window.set_frame(tall_frame(pan_range), 20.0)
    assert window._move is None


def test_the_clock_of_the_move_starts_at_the_first_tick_and_the_poses_follow_it(monkeypatch):
    window, _calls = moving_canvas(monkeypatch)
    assert window._pose(window._move, 4.0) == td.base_pose(0.0, window._move.span, 4.0, True)
    window._on_move_tick(None, _Clock(100.0))
    window._on_move_tick(None, _Clock(110.0))
    span = window._move.span
    assert window._pose(window._move, 4.0) == td.base_pose(10.0, span, 4.0, True)


def test_the_slow_move_is_redrawn_at_thirty_frames_a_second_not_at_every_frame(
    monkeypatch,
):
    window, _calls = moving_canvas(monkeypatch)
    draws = []
    monkeypatch.setattr(_Canvas, "queue_draw", lambda self: draws.append(1))
    for frame in range(60):  # one second of a 60 Hz frame clock
        window._on_move_tick(None, _Clock(frame / 60.0))
    assert 28 <= len(draws) <= 32


def test_the_outgoing_picture_keeps_its_move_and_the_incoming_one_starts_its_own(monkeypatch):
    window, calls = moving_canvas(monkeypatch)
    window._on_move_tick(None, _Clock(5.0))
    window._on_move_tick(None, _Clock(25.0))
    before = window._move
    window.set_frame(flat_frame("new.png"), 20.0, ("crossfade", 1.0))
    assert window._old_move is before and window._move is not before
    assert window._move.born is None  # it starts with the first tick of the transition
    window._on_transition_tick(None, _Clock(25.5))
    assert window._move.born == 25_500_000
    old_pose = window._pose(window._old_move, 4.0)
    assert old_pose == td.base_pose(20.5, before.span, 4.0, True)  # it goes on, no jump


def test_the_same_frame_again_keeps_its_move_running(monkeypatch):
    window, _calls = moving_canvas(monkeypatch)
    move = window._move
    window.set_frame(window._frame, 20.0)
    assert window._move is move


def test_the_move_goes_on_from_the_same_value_when_the_transition_ends(monkeypatch):
    window, _calls = moving_canvas(monkeypatch)
    window.set_frame(flat_frame("new.png"), 20.0, ("crossfade", 1.0))
    window._on_transition_tick(None, _Clock(30.0))  # the first frame
    window._on_transition_tick(None, _Clock(30.0))  # the clock of the run starts
    born = window._move.born
    assert born == 30_000_000 and window._run is not None
    window._on_transition_tick(None, _Clock(31.5))  # over
    assert window._run is None and window._move.born == born  # not started again
    window._on_move_tick(None, _Clock(31.6))
    assert window._pose(window._move, 4.0) == td.base_pose(1.6, window._move.span, 4.0, True)


def test_a_picture_smaller_than_the_window_only_grows_and_is_not_shifted(monkeypatch):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: True)
    window, _calls = window_for_frames(monkeypatch)
    small = Frame("s.png", 2, 3, 6, bytes(6 * 3), "fake", (0, 0))  # the window is 4 x 3
    window.set_frame(small, 20.0)
    window._on_move_tick(None, _Clock(0.0))
    window._on_move_tick(None, _Clock(10.0))
    pose = window._pose(window._move, 4.0)
    assert pose.dx == 0.0 and 1.0 < pose.scale <= 1.0 + td.SMALL_ZOOM
