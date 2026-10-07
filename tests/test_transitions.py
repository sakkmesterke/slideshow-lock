"""Tests for ``slideshow_lock.transitions``: the names and the lengths. Pure Python: no GTK, no
display. What a transition shows at a given moment is ``tests/test_transition_draw.py``."""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import transitions
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BASE_SECONDS,
    CROSSFADE,
    DEFAULT_TRANSITIONS,
    DRAWABLE,
    FADE_BLACK,
    MIN_TRANSITION_SECONDS,
    ORDERS,
    RANDOM_POOL,
    choose,
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
