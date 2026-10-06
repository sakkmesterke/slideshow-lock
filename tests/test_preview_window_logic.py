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
from slideshow_lock.preview import (
    INPUT_BUTTON,
    INPUT_CLOSE,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
)
from slideshow_lock.preview_window import MOTION_THRESHOLD_PIXELS, PreviewWindow, _Canvas
from slideshow_lock.scaling import Frame
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
    window._old_texture = window._transition = None
    window._old_offset = (0, 0)
    window._transition_tick_id = window._transition_ticks = 0
    window._transition_t0 = None
    window._transition_seconds = window._transition_progress = 0.0
    window._scale = lambda: 1.0
    calls = []
    monkeypatch.setattr(_Canvas, "add_tick_callback", lambda self, fn: calls.append("tick") or 7)
    monkeypatch.setattr(_Canvas, "remove_tick_callback", lambda self, id_: calls.append("untick"))
    monkeypatch.setattr(_Canvas, "queue_draw", lambda self: None)
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


def test_a_frame_that_does_not_pan_starts_no_tick_whatever_the_animation_choice(monkeypatch):
    monkeypatch.setattr(preview_window, "animations_enabled", lambda: True)
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(tall_frame((0, 0)), 5.0)
    assert calls == [] and window._offset == (0, 0)


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
    assert window._transition == "crossfade" and window._transition_seconds == 1.0


def test_a_new_frame_without_a_transition_is_a_cut_and_forgets_the_old_texture(monkeypatch):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0)
    assert calls == [] and window._old_texture is None and window._transition is None


def test_the_first_picture_has_nothing_to_change_from(monkeypatch):
    window, calls = window_for_frames(monkeypatch)
    window.set_frame(flat_frame(), 0.0, ("crossfade", 1.0))
    assert calls == [] and window._old_texture is None and window._transition is None


def test_the_same_frame_again_is_not_a_change(monkeypatch):
    window, calls = window_for_frames(monkeypatch)
    frame = flat_frame()
    window.set_frame(frame, 0.0)
    calls.clear()
    window.set_frame(frame, 0.0, ("crossfade", 1.0))
    assert calls == [] and window._transition is None


@pytest.mark.parametrize("name,seconds", [("wipe", 0.8), ("sparkle", 1.0), ("crossfade", 0.0)])
def test_a_transition_that_cannot_be_drawn_is_a_cut(monkeypatch, name, seconds):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, (name, seconds))
    assert calls == [] and window._transition is None and window._old_texture is None


@pytest.mark.parametrize("transition", [None, ("crossfade", 1.0)], ids=["cut", "another"])
def test_any_call_of_set_frame_ends_a_running_transition_at_once(monkeypatch, transition):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("b.png"), 0.0, ("fade-black", 1.2))
    calls.clear()
    window.set_frame(flat_frame("c.png"), 0.0, transition)
    assert calls[0] == "untick"  # the running one is stopped first
    assert window._transition == (transition[0] if transition else None)


def test_a_message_or_a_close_ends_a_running_transition_and_drops_both_textures(monkeypatch):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("b.png"), 0.0, ("crossfade", 1.0))
    calls.clear()
    window.set_frame(None, 0.0)  # what show_message and close do
    assert calls == ["untick"]
    assert window._transition is None and window._old_texture is None and window._texture is None


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
    assert window._transition_t0 is None and window._transition_progress == 0.0
    run_ticks(window, [100.2])  # the second tick: this is where the time starts
    assert window._transition_t0 == int(100.2 * 1_000_000)
    assert window._transition_progress == 0.0


def test_the_progress_follows_the_frame_clock_not_the_number_of_frames(monkeypatch):
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 2.0))
    run_ticks(window, [10.0, 10.0])
    assert run_ticks(window, [10.5]) == [GLib.SOURCE_CONTINUE]
    assert window._transition_progress == pytest.approx(0.25)
    run_ticks(window, [11.0])
    assert window._transition_progress == pytest.approx(0.5)  # one frame later, a longer jump
    run_ticks(window, [11.9])
    assert window._transition_progress == pytest.approx(0.95)


@pytest.mark.parametrize("frames", [[10.0, 10.0, 11.0], [10.0, 10.0, 10.5, 11.4]])
def test_the_transition_ends_when_the_time_is_up_whatever_the_number_of_frames(monkeypatch, frames):
    window, calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    calls.clear()
    results = run_ticks(window, frames)
    assert results[-1] == GLib.SOURCE_REMOVE and GLib.SOURCE_REMOVE not in results[:-1]
    assert window._transition is None and window._old_texture is None
    assert calls == []  # the tick returned SOURCE_REMOVE: it is not removed a second time


def test_after_the_end_the_canvas_draws_the_one_picture_plainly(monkeypatch):
    monkeypatch.setattr(_Canvas, "get_width", lambda self: 4)
    monkeypatch.setattr(_Canvas, "get_height", lambda self: 3)
    window, _calls = canvas_with_a_picture(monkeypatch)
    window.set_frame(flat_frame("new.png"), 0.0, ("crossfade", 1.0))
    during = _Snapshot()
    window.do_snapshot(during)
    assert during.calls[0][0] == "push_cross_fade"  # while it runs: the cross fade
    run_ticks(window, [5.0, 5.0, 6.5])
    after = _Snapshot()
    window.do_snapshot(after)
    assert [c[0] for c in after.calls] == ["append_color", "append_texture"]
    assert after.calls[1][1] is window._texture  # 1:1, not a filtered last frame


class _Snapshot:
    """Records the drawing calls ``_Canvas`` makes."""

    def __init__(self):
        self.calls = []

    def append_color(self, *_args):
        self.calls.append(("append_color",))

    def append_texture(self, texture, _rect):
        self.calls.append(("append_texture", texture))

    def push_cross_fade(self, progress):
        self.calls.append(("push_cross_fade", progress))

    def pop(self):
        self.calls.append(("pop",))


class _Texture:
    def get_width(self):
        return 4

    def get_height(self):
        return 3


def running_canvas(monkeypatch, name, ticks, progress):
    window, _calls = window_for_frames(monkeypatch)
    window._old_texture, window._texture = _Texture(), _Texture()
    window._transition, window._transition_ticks = name, ticks
    window._transition_progress = progress
    return window


def layers_drawn(snapshot):
    """What each of the two layers of a cross fade holds: the texture, or None for plain black."""
    calls = snapshot.calls
    first_pop = calls.index(("pop",))
    start, end = calls[1:first_pop], calls[first_pop + 1 : -1]
    return [next((c[1] for c in part if c[0] == "append_texture"), None) for part in (start, end)]


def test_the_cross_fade_is_one_push_with_the_old_picture_first_and_the_new_one_second(
    monkeypatch,
):
    window = running_canvas(monkeypatch, "crossfade", 3, 0.4)
    snapshot = _Snapshot()
    window._snapshot_transition(snapshot, 4, 3)
    assert snapshot.calls[0] == ("push_cross_fade", 0.4)
    assert [c[0] for c in snapshot.calls].count("pop") == 2  # a cross fade is closed by two pops
    assert snapshot.calls[-1] == ("pop",)
    assert layers_drawn(snapshot) == [window._old_texture, window._texture]


def test_every_layer_of_a_transition_starts_with_a_full_window_of_black(monkeypatch):
    window = running_canvas(monkeypatch, "crossfade", 3, 0.4)
    snapshot = _Snapshot()
    window._snapshot_transition(snapshot, 4, 3)
    names = [c[0] for c in snapshot.calls]
    assert names == [
        "push_cross_fade",
        "append_color",
        "append_texture",
        "pop",
        "append_color",
        "append_texture",
        "pop",
    ]


def test_the_fade_through_black_goes_old_to_black_and_then_black_to_new(monkeypatch):
    window = running_canvas(monkeypatch, "fade-black", 3, 0.25)
    snapshot = _Snapshot()
    window._snapshot_transition(snapshot, 4, 3)
    assert snapshot.calls[0] == ("push_cross_fade", pytest.approx(0.5))
    assert layers_drawn(snapshot) == [window._old_texture, None]
    window = running_canvas(monkeypatch, "fade-black", 3, 0.75)
    snapshot = _Snapshot()
    window._snapshot_transition(snapshot, 4, 3)
    assert snapshot.calls[0] == ("push_cross_fade", pytest.approx(0.5))
    assert layers_drawn(snapshot) == [None, window._texture]


@pytest.mark.parametrize("name", ["crossfade", "fade-black"])
@pytest.mark.parametrize("ticks", [0, 1])
def test_the_first_frame_draws_the_old_picture_at_progress_zero_with_both_textures(
    monkeypatch, name, ticks
):
    """Where the new texture is uploaded, out of sight: even the fade through black, which has
    no new picture in its first half, draws it then."""
    window = running_canvas(monkeypatch, name, ticks, 0.0)
    snapshot = _Snapshot()
    window._snapshot_transition(snapshot, 4, 3)
    assert snapshot.calls[0] == ("push_cross_fade", 0.0)
    assert layers_drawn(snapshot) == [window._old_texture, window._texture]
