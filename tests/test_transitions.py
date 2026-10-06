"""Tests for ``slideshow_lock.transitions``: the names, the lengths and what a transition shows at
a given moment. Pure Python: no GTK, no display."""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import transitions
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BASE_SECONDS,
    BLACK,
    CROSSFADE,
    DEFAULT_TRANSITIONS,
    DRAWABLE,
    FADE_BLACK,
    MIN_TRANSITION_SECONDS,
    NEW,
    OLD,
    ORDERS,
    choose,
    clean,
    is_valid,
    layers,
    transition_seconds,
)

NAMES = (
    "crossfade",
    "fade-black",
    "slide-in",
    "push",
    "ken-burns",
    "zoom",
    "wipe",
    "circle",
    "blur",
    "rotate",
)


@pytest.fixture(autouse=True)
def _forget_reported_names():
    transitions._reported.clear()
    yield
    transitions._reported.clear()


# -- the identifiers are a contract --------------------------------------------------------------


def test_the_ten_identifiers_are_these_and_in_this_order():
    """They are stored in the setting as written: renaming one orphans what users saved."""
    assert ALL_TRANSITIONS == NAMES
    assert len(set(ALL_TRANSITIONS)) == 10


def test_this_version_draws_the_cross_fade_and_the_fade_through_black_only():
    assert DRAWABLE == (CROSSFADE, FADE_BLACK) == ("crossfade", "fade-black")
    assert set(DRAWABLE) <= set(ALL_TRANSITIONS)


def test_the_default_is_the_cross_fade_alone():
    assert DEFAULT_TRANSITIONS == ("crossfade",)
    assert ORDERS == ("random", "sequence")


def test_every_identifier_has_a_length_and_nothing_else_does():
    assert set(BASE_SECONDS) == set(ALL_TRANSITIONS)


@pytest.mark.parametrize("name", NAMES)
def test_every_identifier_is_valid(name):
    assert is_valid(name)


@pytest.mark.parametrize("name", ["", "Crossfade", "cross-fade", "none", " crossfade", None, 1, []])
def test_anything_else_is_not_valid(name):
    assert not is_valid(name)


def test_clean_keeps_the_valid_names_in_order_and_each_once():
    assert clean(["push", "crossfade", "push", "blur"]) == ["push", "crossfade", "blur"]
    assert clean([]) == []


def test_clean_drops_an_unknown_name_and_says_so_once_per_name(caplog):
    with caplog.at_level(logging.WARNING):
        assert clean(["crossfade", "sparkle", 7]) == ["crossfade"]
        assert clean(["sparkle", "crossfade"]) == ["crossfade"]  # the same name, read again
        assert clean(["sparkle", "glitter"]) == []  # a new one is reported
    messages = [r.getMessage() for r in caplog.records]
    assert sum("'sparkle'" in m for m in messages) == 1
    assert sum("'glitter'" in m for m in messages) == 1
    assert sum("7" in m for m in messages) == 1
    assert all(m.startswith("[config]") for m in messages)


# -- how long -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "seconds"), [(CROSSFADE, 1.0), (FADE_BLACK, 1.2)])
def test_on_a_long_interval_a_transition_takes_its_own_time(name, seconds):
    assert transition_seconds(name, 10) == seconds
    assert transition_seconds(name, 3600) == seconds


@pytest.mark.parametrize("name", [CROSSFADE, FADE_BLACK])
@pytest.mark.parametrize("interval", [1, 2, 3, 4, 5, 8])
def test_a_transition_never_takes_more_than_a_quarter_of_the_interval(name, interval):
    assert transition_seconds(name, interval) <= 0.25 * interval + 1e-9


def test_the_cap_is_a_quarter_of_the_interval():
    assert transition_seconds(CROSSFADE, 2) == 0.5
    assert transition_seconds(CROSSFADE, 1) == 0.25  # the shortest interval of the setting
    assert transition_seconds(FADE_BLACK, 4) == 1.0
    assert transition_seconds(FADE_BLACK, 4.8) == 1.2  # exactly where the own time takes over


def test_a_transition_shorter_than_two_tenths_of_a_second_is_a_cut():
    assert MIN_TRANSITION_SECONDS == 0.2
    assert transition_seconds(CROSSFADE, 0.8) == 0.2  # a quarter is exactly the shortest
    assert transition_seconds(CROSSFADE, 0.79) == 0.0
    assert transition_seconds(CROSSFADE, 0.1) == 0.0


@pytest.mark.parametrize("name", ["", "none", "sparkle", None, 3])
def test_a_name_that_is_not_a_transition_takes_no_time(name):
    assert transition_seconds(name, 10) == 0.0


# -- which one -----------------------------------------------------------------------------------


def test_choose_takes_the_first_drawable_one_in_the_canonical_order():
    assert choose(["crossfade"]) == "crossfade"
    assert choose(["fade-black", "crossfade"]) == "crossfade"
    assert choose(["wipe", "fade-black"]) == "fade-black"


@pytest.mark.parametrize("chosen", [[], ["wipe"], ["wipe", "blur", "rotate"], ["sparkle"]])
def test_choose_has_nothing_when_no_chosen_name_can_be_drawn(chosen):
    assert choose(chosen) is None


# -- what is on screen ----------------------------------------------------------------------------


#: How much of each picture is visible at a cross fade between two layers.
def visible(parts):
    weights = {OLD: 0.0, NEW: 0.0, BLACK: 0.0}
    weights[parts.start] += 1.0 - parts.progress
    weights[parts.end] += parts.progress
    return weights


@pytest.mark.parametrize("name", DRAWABLE)
def test_at_the_start_only_the_old_picture_is_visible(name):
    assert visible(layers(name, 0.0)) == {OLD: 1.0, NEW: 0.0, BLACK: 0.0}


@pytest.mark.parametrize("name", DRAWABLE)
def test_at_the_end_only_the_new_picture_is_visible(name):
    assert visible(layers(name, 1.0)) == {OLD: 0.0, NEW: 1.0, BLACK: 0.0}


@pytest.mark.parametrize("name", DRAWABLE)
def test_the_new_picture_only_gains_and_the_old_one_only_loses(name):
    steps = [i / 200 for i in range(201)]
    seen = [visible(layers(name, p)) for p in steps]
    for before, after in zip(seen, seen[1:]):
        assert after[NEW] >= before[NEW] - 1e-12
        assert after[OLD] <= before[OLD] + 1e-12
    for weights in seen:
        assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_the_cross_fade_mixes_the_two_pictures_and_never_shows_black():
    for p in (0.1, 0.5, 0.9):
        parts = layers(CROSSFADE, p)
        assert (parts.start, parts.end) == (OLD, NEW)
        assert parts.progress == pytest.approx(p)
        assert visible(parts)[BLACK] == 0.0


def test_the_fade_through_black_is_all_black_in_the_middle_and_both_pictures_are_never_together():
    middle = visible(layers(FADE_BLACK, 0.5))
    assert middle == {OLD: 0.0, NEW: 0.0, BLACK: 1.0}
    for p in [i / 100 for i in range(101)]:
        weights = visible(layers(FADE_BLACK, p))
        assert weights[OLD] == 0.0 or weights[NEW] == 0.0


def test_the_fade_through_black_has_no_jump_at_the_middle():
    before = visible(layers(FADE_BLACK, 0.5 - 1e-9))
    after = visible(layers(FADE_BLACK, 0.5 + 1e-9))
    for kind in (OLD, NEW, BLACK):
        assert abs(before[kind] - after[kind]) < 1e-6


@pytest.mark.parametrize("name", DRAWABLE)
def test_progress_outside_zero_to_one_is_held_at_the_ends(name):
    assert layers(name, -3) == layers(name, 0.0)
    assert layers(name, 7) == layers(name, 1.0)


@pytest.mark.parametrize("name", [n for n in ALL_TRANSITIONS if n not in DRAWABLE] + ["sparkle"])
def test_a_name_this_version_cannot_draw_has_no_picture(name):
    assert layers(name, 0.5) is None
