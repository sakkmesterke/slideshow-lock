"""The input logic of ``PreviewWindow``, without a display.

``PreviewWindow.__init__`` builds a real GTK window, which needs a compositor. Its event handlers
are plain methods that only look at a few attributes, so they are run on an instance made
without ``__init__``. What is not covered here, because it is wired in the constructor, is
which GTK controller calls which handler (the click gesture, the cursor being hidden): that
is what ``tools/wayland-smoke`` checks on a real compositor.
"""

from __future__ import annotations

import pytest

from slideshow_lock.preview import (
    INPUT_BUTTON,
    INPUT_CLOSE,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
)
from slideshow_lock.preview_window import MOTION_THRESHOLD_PIXELS, PreviewWindow
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
