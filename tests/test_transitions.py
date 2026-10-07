"""Tests for ``slideshow_lock.transitions``: the names and the lengths. Pure Python: no GTK, no
display. What a transition shows at a given moment is ``tests/test_transition_draw.py``."""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import transitions
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    CROSSFADE,
    DEFAULT_DURATION,
    DEFAULT_TRANSITIONS,
    DRAWABLE,
    FADE_BLACK,
    INTERVAL_SHARE,
    MAX_DURATION,
    MIN_DURATION,
    MIN_TRANSITION_SECONDS,
    ORDERS,
    RANDOM_POOL,
    choose,
    clamp_duration,
    clean,
    is_valid,
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
    transitions._reported_durations.clear()
    yield
    transitions._reported.clear()
    transitions._reported_durations.clear()


# -- the identifiers are a contract --------------------------------------------------------------


def test_the_ten_identifiers_are_these_and_in_this_order():
    """They are stored in the setting as written: renaming one orphans what users saved."""
    assert ALL_TRANSITIONS == NAMES
    assert len(set(ALL_TRANSITIONS)) == 10


def test_this_version_draws_the_cross_fade_and_the_fade_through_black_only():
    assert DRAWABLE == (CROSSFADE, FADE_BLACK) == ("crossfade", "fade-black")
    assert set(DRAWABLE) <= set(ALL_TRANSITIONS)


def test_random_is_the_eight_without_the_blur_and_ken_burns_in_the_canonical_order():
    assert RANDOM_POOL == (
        "crossfade",
        "fade-black",
        "slide-in",
        "push",
        "zoom",
        "wipe",
        "circle",
        "rotate",
    )


def test_the_default_is_ken_burns_alone():
    assert DEFAULT_TRANSITIONS == ("ken-burns",)
    assert ORDERS == ("random", "sequence")


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


def test_the_duration_is_one_value_for_all_with_these_limits():
    assert (DEFAULT_DURATION, MIN_DURATION, MAX_DURATION) == (1.0, 0.2, 5.0)
    assert INTERVAL_SHARE == 0.5
    assert MIN_TRANSITION_SECONDS == 0.2


@pytest.mark.parametrize("name", NAMES)
def test_every_transition_takes_the_same_stored_duration(name):
    assert transition_seconds(name, 10) == 1.0  # the default
    assert transition_seconds(name, 3600, 2.5) == 2.5
    assert transition_seconds(name, 3600, 5.0) == 5.0


@pytest.mark.parametrize("duration", [0.2, 1.0, 3.0, 5.0])
@pytest.mark.parametrize("interval", [1, 2, 3, 4, 5, 8, 10])
def test_a_transition_never_takes_more_than_half_of_the_interval(duration, interval):
    seconds = transition_seconds(CROSSFADE, interval, duration)
    assert seconds <= 0.5 * interval + 1e-9
    assert seconds == min(duration, 0.5 * interval)


def test_the_cap_is_half_of_the_interval():
    assert transition_seconds(CROSSFADE, 2) == 1.0  # exactly where the duration takes over
    assert transition_seconds(CROSSFADE, 1) == 0.5  # the shortest interval of the setting
    assert transition_seconds(CROSSFADE, 1.9) == 0.95
    assert transition_seconds(CROSSFADE, 10, 5.0) == 5.0  # the default interval allows the maximum
    assert transition_seconds(CROSSFADE, 9, 5.0) == 4.5


def test_a_transition_shorter_than_two_tenths_of_a_second_is_a_cut():
    assert transition_seconds(CROSSFADE, 0.4) == 0.2  # half is exactly the shortest
    assert transition_seconds(CROSSFADE, 0.39) == 0.0
    assert transition_seconds(CROSSFADE, 0.1) == 0.0
    assert transition_seconds(CROSSFADE, 10, 0.2) == 0.2  # the shortest the setting allows


@pytest.mark.parametrize("name", ["", "none", "sparkle", None, 3])
def test_a_name_that_is_not_a_transition_takes_no_time(name):
    assert transition_seconds(name, 10) == 0.0


@pytest.mark.parametrize(
    ("value", "kept"),
    [(0.2, 0.2), (1.0, 1.0), (5.0, 5.0), (2, 2.0), (0.1, 0.2), (0.0, 0.2), (-3, 0.2)]
    + [(5.01, 5.0), (60, 5.0), (float("inf"), 5.0), (float("-inf"), 0.2)],
)
def test_a_duration_is_brought_into_its_range(value, kept):
    assert clamp_duration(value) == kept


@pytest.mark.parametrize("value", [float("nan"), "1.0", None, True, [], "fast"])
def test_what_is_not_a_number_becomes_the_default(value):
    assert clamp_duration(value) == DEFAULT_DURATION


def test_transition_seconds_clamps_the_duration_it_is_given():
    assert transition_seconds(CROSSFADE, 3600, 99) == 5.0
    assert transition_seconds(CROSSFADE, 3600, 0.01) == 0.2
    assert transition_seconds(CROSSFADE, 3600, float("nan")) == 1.0


def test_a_duration_out_of_range_is_said_once_per_value(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            clamp_duration(60)
        clamp_duration(0.05)
        clamp_duration(1.0)  # in range: nothing to say
        clamp_duration("x")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 3
    assert all(m.startswith("[config]") for m in messages)
    assert sum("60" in m for m in messages) == 1


# -- which one -----------------------------------------------------------------------------------


def test_choose_takes_the_first_drawable_one_in_the_canonical_order():
    assert choose(["crossfade"]) == "crossfade"
    assert choose(["fade-black", "crossfade"]) == "crossfade"
    assert choose(["wipe", "fade-black"]) == "fade-black"


@pytest.mark.parametrize("chosen", [[], ["wipe"], ["wipe", "blur", "rotate"], ["sparkle"]])
def test_choose_has_nothing_when_no_chosen_name_can_be_drawn(chosen):
    assert choose(chosen) is None
