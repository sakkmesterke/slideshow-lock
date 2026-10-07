"""The edge where the new picture meets the old one is a ramp, not a line: no display.

The pictures a transition paints are ``Draw`` values. These tests read them the way the window does
(cut, move, turn, scale, then the soft edge's gradient) and look at the profile of the new
picture's opacity along lines across the window: a hard edge is one step from nothing to whole
between two neighbouring pixels, a soft one is a ramp. What GTK makes of the same values, pixel
by pixel, is ``tests/test_transition_edges_gdk.py``.

Only ``compose`` and the fields of ``Draw`` are used to read the pictures, so that the same
profile can be read from a ``Draw`` that has no soft edge at all (it is one step then).
"""

from __future__ import annotations

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BLUR,
    CIRCLE,
    CROSSFADE,
    FADE_BLACK,
    KEN_BURNS,
    NEW,
    PUSH,
    ROTATE,
    SLIDE_IN,
    WIPE,
    ZOOM,
)
from tests import soft_edge_model as model

W, H = 1280.0, 720.0

#: The transitions whose new picture has an edge inside the window while it comes in, and the ones
#: that have none: the cross fades and the blur have no edge, and the Ken Burns picture is larger
#: than the window the whole time (``test_ken_burns_covers_the_window_the_whole_run``).
SOFT = (SLIDE_IN, PUSH, WIPE, CIRCLE, ZOOM, ROTATE)
NO_EDGE = (CROSSFADE, FADE_BLACK, KEN_BURNS, BLUR)

#: Moments with room for the band to show (the first and the last stretch of a run are the ones
#: where the edge is at the window's border and the band has narrowed to nothing).
MIDDLE = (0.15, 0.3, 0.45, 0.6, 0.75)

#: The most the opacity of the new picture may rise between two neighbouring pixels, as a share of
#: its whole opacity there. A line is 1.0; the band is 12 % of the shorter side wide (86 pixels
#: here: 0.012 a pixel), and it narrows towards the end of a run, to about 10 pixels (0.1) at the
#: latest moment looked at.
STEP_LIMIT = 0.2


def _lines():
    """Pixel lines across the window: rows and columns, near the middle and near the borders."""
    for fraction in (0.05, 0.25, 0.5, 0.75, 0.95):
        y = int(H * fraction)
        yield [(x + 0.5, y + 0.5) for x in range(int(W))]
        x = int(W * fraction)
        yield [(x + 0.5, y + 0.5) for y in range(int(H))]


def _steepest_edge(name, progress):
    """The largest rise of the new picture's opacity between neighbouring pixels on the lines
    across the window, as a share of the opacity it has there; and the biggest difference between
    the ends of one line (is there an edge at all)."""
    draws = td.compose(name, progress, W, H)
    opacity = max(d.opacity for d in draws if d.layer == NEW)
    steepest, spread = 0.0, 0.0
    for line in _lines():
        values = [model.new_alpha(draws, x, y, W, H) for x, y in line]
        steepest = max([steepest] + [abs(b - a) for a, b in zip(values, values[1:])])
        spread = max(spread, max(values) - min(values))
    return steepest / opacity if opacity > 0 else 0.0, spread


@pytest.mark.parametrize("name", SOFT)
def test_the_edge_of_the_new_picture_is_a_ramp_not_a_step(name):
    for p in MIDDLE:
        steepest, spread = _steepest_edge(name, p)
        assert spread > 0.05, (name, p)  # there is an edge to look at
        assert steepest <= STEP_LIMIT, (name, p, steepest)


@pytest.mark.parametrize("name", SOFT)
def test_the_profile_measure_sees_a_hard_edge_as_a_step(name, monkeypatch):
    """The control of the test above: with the soft edge taken away the same measure reads a step,
    so a pass is the ramp's and not the measure being blind."""
    real = td.compose

    def hard(*args, **kwargs):
        return [d._replace(soft=None) for d in real(*args, **kwargs)]

    monkeypatch.setattr(td, "compose", hard)
    steepest, _spread = _steepest_edge(name, 0.45)
    assert steepest > 0.9, (name, steepest)


def test_a_zero_width_soft_edge_is_the_old_hard_one(monkeypatch):
    monkeypatch.setattr(td, "SOFT_EDGE_SHARE", 0.0)
    for name in SOFT:
        steepest, _spread = _steepest_edge(name, 0.45)
        assert steepest > 0.9, (name, steepest)


def _share_not_whole(name, progress):
    """The share of a grid of window pixels where the picture's cut/edge still lets the old one
    through (the opacity of the picture itself is the transition's own matter and not counted)."""
    count, total = 0, 0
    draws = [d for d in td.compose(name, progress, W, H) if d.layer == NEW]
    for gy in range(36):
        for gx in range(64):
            total += 1
            whole = max(
                model.geometry(d, (gx + 0.5) * W / 64, (gy + 0.5) * H / 36, W, H) for d in draws
            )
            if whole < 1.0 - 1e-6:
                count += 1
    return count / total


@pytest.mark.parametrize("name", SOFT)
def test_the_last_frame_leaves_no_band_and_the_run_towards_it_never_adds_one(name):
    shares = [_share_not_whole(name, p) for p in (0.9, 0.95, 0.98, 0.99, 0.999, 1.0)]
    assert shares[-1] == 0.0, (name, shares)
    assert shares[-2] <= 0.02, (name, shares)  # a moment before the end: no band to vanish at once
    assert all(b <= a + 1e-12 for a, b in zip(shares, shares[1:])), (name, shares)


@pytest.mark.parametrize("name", SOFT)
def test_the_first_frame_shows_nothing_of_the_new_picture(name):
    for p in (0.0, 0.0001):
        draws = td.compose(name, p, W, H)
        for line in _lines():
            assert max(model.new_alpha(draws, x, y, W, H) for x, y in line) <= 1e-3, (name, p)


@pytest.mark.parametrize("name", (SLIDE_IN, PUSH, WIPE, CIRCLE, ZOOM, ROTATE))
def test_the_ends_are_the_plain_pictures_exactly(name):
    assert all(d.soft is None for d in td.compose(name, 1.0, W, H))
    assert all(d.soft is None or d.soft.width > 0 for d in td.compose(name, 0.5, W, H))
    first = td.compose(name, 0.0, W, H)[0]
    assert first.layer == "old" and first.dx == 0.0 and first.soft is None


def test_push_has_no_dark_seam_the_new_picture_comes_in_over_the_old_one():
    """The two pictures of a push end where they meet: a soft edge on one of them alone would let
    the window's black show through. The new one comes in over the last pixels of the old one."""
    for p in (0.02, 0.1, 0.3, 0.5, 0.7, 0.9, 0.98):
        draws = td.compose(PUSH, p, W, H)
        old = draws[0]
        for y in (H / 2, H / 4):
            for x in range(0, int(W), 3):
                covered = model.geometry(old, x + 0.5, y, W, H)
                union = 1.0 - (1.0 - covered) * (1.0 - model.new_alpha(draws, x + 0.5, y, W, H))
                assert union >= 1.0 - 1e-6, (p, x, union)


@pytest.mark.parametrize("name", NO_EDGE)
def test_the_transitions_without_an_edge_keep_none(name):
    for step in range(101):
        assert all(d.soft is None for d in td.compose(name, step / 100.0, W, H)), name


def test_ken_burns_covers_the_window_the_whole_run():
    """Why it has no soft edge: its picture is enlarged and shifted by less than the enlargement
    leaves over, so the window never sees its border."""
    for step in range(101):
        draw = td.compose(KEN_BURNS, step / 100.0, W, H)[-1]
        assert abs(draw.dx) <= (draw.scale - 1.0) * W / 2 + 1e-9, step


def test_the_width_of_the_band_is_one_named_share_of_the_shorter_side(monkeypatch):
    assert td.soft_width(W, H) == pytest.approx(td.SOFT_EDGE_SHARE * H)
    assert td.soft_width(H, W) == pytest.approx(td.SOFT_EDGE_SHARE * H)
    monkeypatch.setattr(td, "SOFT_EDGE_SHARE", 0.3)
    expected = 0.3 * H
    assert td.compose(WIPE, 0.5, W, H)[-1].soft.width == pytest.approx(expected)
    assert td.compose(CIRCLE, 0.5, W, H)[-1].soft.width == pytest.approx(expected)
    assert td.compose(SLIDE_IN, 0.5, W, H)[-1].soft.width == pytest.approx(expected)
    assert td.compose(ZOOM, 0.25, W, H)[-1].soft.width <= expected
    assert td.compose(ROTATE, 0.25, W, H)[-1].soft.width <= expected
    assert td.compose(PUSH, 0.5, W, H)[-1].soft.width == pytest.approx(expected)


def test_every_transition_is_either_soft_or_has_no_edge():
    assert set(SOFT) | set(NO_EDGE) == set(ALL_TRANSITIONS)
    assert not set(SOFT) & set(NO_EDGE)


# -- the stops of the gradients ---------------------------------------------------------------


def test_one_soft_end_is_a_ramp_from_nothing_to_whole_over_the_width():
    stops = td.edge_stops(100.0, 20.0, True, False)
    assert stops == [(0.0, 0.0), (0.2, 1.0), (1.0, 1.0)]
    assert td.stops_alpha(stops, 0.1) == pytest.approx(0.5)
    stops = td.edge_stops(100.0, 20.0, False, True)
    assert stops == [(0.0, 1.0), (0.8, 1.0), (1.0, 0.0)]


def test_a_ramp_is_measured_from_its_end_also_when_the_rectangle_is_shorter_than_it():
    """The front of a wipe that has not gone one band's width yet: whole at no point of it."""
    stops = td.edge_stops(10.0, 40.0, False, True)
    assert td.stops_alpha(stops, 0.0) == pytest.approx(10.0 / 40.0)
    assert td.stops_alpha(stops, 1.0) == 0.0


def test_two_soft_ends_that_meet_peak_at_their_crossing():
    stops = td.edge_stops(30.0, 20.0, True, True)
    peak = max(alpha for _offset, alpha in stops)
    assert peak == pytest.approx(0.75)
    assert td.stops_alpha(stops, 0.0) == 0.0 and td.stops_alpha(stops, 1.0) == 0.0
    both = td.edge_stops(100.0, 10.0, True, True)
    assert both == [(0.0, 0.0), (0.1, 1.0), (0.9, 1.0), (1.0, 0.0)]


def test_a_soft_circle_is_whole_inside_and_nothing_at_the_rim():
    stops = td.rim_stops(100.0, 25.0)
    assert stops == [(0.0, 1.0), (0.75, 1.0), (1.0, 0.0)]
    small = td.rim_stops(10.0, 40.0)  # not yet a band's width across: not whole even in the middle
    assert small == [(0.0, 0.25), (1.0, 0.0)]


def test_degenerate_sizes_do_not_divide_by_zero():
    assert td.edge_stops(0.0, 10.0, True, True)[0][1] == 0.0
    assert td.edge_stops(10.0, 0.0, True, True) == [(0.0, 1.0), (1.0, 1.0)]
    assert td.rim_stops(0.0, 10.0)[0][1] == 0.0
    assert td.rim_stops(10.0, 0.0) == [(0.0, 1.0), (1.0, 1.0)]
    assert td.stops_alpha([(0.0, 0.3), (1.0, 0.7)], -1.0) == 0.3
    assert td.stops_alpha([(0.0, 0.3), (1.0, 0.7)], 2.0) == 0.7
