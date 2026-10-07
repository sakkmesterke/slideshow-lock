"""Tests for the settings window's logic (UI-1), without GTK: the model between the fields and
``Settings``. The real window is checked with ``tools/wayland-smoke/smoke_preferences.py``.

The "invalid value is not saved" and "the window never shows a value that was not saved" rules
are the ones that matter: every rejected value is checked to leave the stored one untouched.
"""

from __future__ import annotations

import os

import pytest
from gi.repository import Gio

from slideshow_lock import APP_ID
from slideshow_lock.preferences_model import (
    CHOICES,
    DURATION_MAX_SECONDS,
    DURATION_MIN_SECONDS,
    DURATION_STEP_SECONDS,
    INT_RANGES,
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    INTERVAL_POSITIONS,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
    RANDOM_POOL,
    TRANSITION_CHOICES,
    TRANSITION_ORDER_CHOICES,
    PreferencesModel,
    describe_interval,
    format_hms,
    interval_index_for_seconds,
    interval_position_for_seconds,
    interval_seconds_for_position,
    snap_interval_position,
    split_hms,
    step_interval_position,
)
from slideshow_lock.settings import (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_DURATION,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITIONS,
    Settings,
    default_picture_folder,
)
from slideshow_lock.transitions import ALL_TRANSITIONS
from tests.conftest import _ALL_SETTINGS_KEYS


@pytest.fixture
def model():
    return PreferencesModel(Settings())


def _schema_key(name):
    return Gio.SettingsSchemaSource.get_default().lookup(APP_ID, True).get_key(name)


# -- the model is bound to the schema -------------------------------------------------------


def test_the_ranges_and_choices_are_the_ones_of_the_schema():
    for key, (low, high) in INT_RANGES.items():
        kind, value = _schema_key(key).get_range().unpack()
        assert (kind, tuple(value)) == ("range", (low, high)), key
    for key, values in CHOICES.items():
        kind, value = _schema_key(key).get_range().unpack()
        assert (kind, sorted(value)) == ("enum", sorted(values)), key


def test_the_transition_order_choices_are_the_ones_of_the_schema():
    kind, value = _schema_key(KEY_TRANSITION_ORDER).get_range().unpack()
    assert (kind, sorted(value)) == ("enum", sorted(TRANSITION_ORDER_CHOICES))


def test_every_key_of_the_schema_has_a_field(model):
    bound = (
        set(INT_RANGES)
        | set(CHOICES)
        | {
            KEY_PAN_PORTRAIT_IMAGES,
            KEY_PICTURE_FOLDER,
            KEY_TRANSITIONS,
            KEY_TRANSITION_ORDER,
            KEY_TRANSITION_DURATION,
        }
    )
    assert bound == set(_ALL_SETTINGS_KEYS)


def test_the_window_starts_from_the_stored_values(model):
    assert model.get(KEY_IDLE_TIMEOUT_SECONDS) == 120
    assert model.get(KEY_LOCK_GRACE_PERIOD_SECONDS) == 0
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 10
    assert model.get(KEY_ORDER) == "random"
    assert model.get(KEY_SCALING) == "fill"
    assert model.get(KEY_PAN_PORTRAIT_IMAGES) is False  # off unless switched on


def test_a_value_written_elsewhere_is_what_the_model_reads(model):
    other = Settings()  # another process or another window
    other.set_slide_interval_seconds(42)
    other.set_order("name")
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 42
    assert model.get(KEY_ORDER) == "name"


# -- whole numbers ------------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(INT_RANGES))
def test_the_limits_themselves_are_saved(model, key):
    low, high = INT_RANGES[key]
    for value in (low, high):
        result = model.set_int(key, value)
        assert result.ok, result
        assert model.get(key) == value


@pytest.mark.parametrize("key", sorted(INT_RANGES))
def test_a_value_outside_the_range_is_refused_and_the_stored_one_stays(model, key):
    low, high = INT_RANGES[key]
    before = model.get(key)
    for value in (low - 1, high + 1, -5, 10**12):
        result = model.set_int(key, value)
        assert not result.ok, value
        assert result.message != "Saved."
        assert model.get(key) == before


@pytest.mark.parametrize("value", ["12", 1.5, None, True, [3]])
def test_something_that_is_not_a_whole_number_is_refused(model, value):
    before = model.get(KEY_SLIDE_INTERVAL_SECONDS)
    assert not model.set_int(KEY_SLIDE_INTERVAL_SECONDS, value).ok
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == before


# -- the transition length --------------------------------------------------------------------


def test_the_duration_limits_are_the_ones_of_the_schema():
    kind, value = _schema_key(KEY_TRANSITION_DURATION).get_range().unpack()
    assert (kind, tuple(value)) == ("range", (DURATION_MIN_SECONDS, DURATION_MAX_SECONDS))
    assert (DURATION_MIN_SECONDS, DURATION_MAX_SECONDS, DURATION_STEP_SECONDS) == (0.2, 5.0, 0.1)
    assert _schema_key(KEY_TRANSITION_DURATION).get_default_value().unpack() == 1.0


def test_the_duration_limits_themselves_are_saved(model):
    for value in (DURATION_MIN_SECONDS, DURATION_MAX_SECONDS, 1.0):
        result = model.set_duration(value)
        assert result.ok, result
        assert model.get(KEY_TRANSITION_DURATION) == value


def test_a_fresh_window_has_one_second_and_looking_writes_nothing(model):
    assert model.get(KEY_TRANSITION_DURATION) == 1.0


@pytest.mark.parametrize("value", [0.19, 0.0, -1, 5.01, 6, 10**9, float("inf"), float("nan")])
def test_a_duration_outside_the_range_is_refused_and_the_stored_one_stays(model, value):
    assert model.set_duration(2.5).ok
    result = model.set_duration(value)
    assert not result.ok
    assert result.message not in ("Saved.", "")
    assert model.get(KEY_TRANSITION_DURATION) == 2.5


@pytest.mark.parametrize("value", ["2", None, True, False, [2.0], b"2"])
def test_something_that_is_not_a_number_is_no_duration(model, value):
    assert not model.set_duration(value).ok
    assert model.get(KEY_TRANSITION_DURATION) == 1.0


@pytest.mark.parametrize(
    ("value", "stored"), [(1.25, 1.2), (0.3000000001, 0.3), (2, 2.0), (4.96, 5.0)]
)
def test_a_duration_is_rounded_to_the_step_of_the_slider(model, value, stored):
    assert model.set_duration(value).ok
    assert model.get(KEY_TRANSITION_DURATION) == stored


def test_a_stored_duration_that_is_not_a_step_is_shown_and_left_alone(model):
    stub = Settings()
    stub._settings.set_double(KEY_TRANSITION_DURATION, 1.25)
    assert PreferencesModel(stub).get(KEY_TRANSITION_DURATION) == 1.25  # looking changes nothing
    assert Settings().get_transition_duration() == 1.25


def test_the_idle_timeout_cannot_be_zero_but_the_grace_period_can(model):
    assert not model.set_int(KEY_IDLE_TIMEOUT_SECONDS, 0).ok
    assert model.set_int(KEY_LOCK_GRACE_PERIOD_SECONDS, 0).ok  # 0: every input counts (D16)


def test_the_idle_timeout_and_the_grace_period_keep_their_plain_ranges(model):
    # The slider is for the slide interval only: the idle time stays 1 to 86400 seconds.
    assert INT_RANGES[KEY_IDLE_TIMEOUT_SECONDS] == (1, 86400)
    assert INT_RANGES[KEY_LOCK_GRACE_PERIOD_SECONDS] == (0, 86400)
    assert model.set_int(KEY_IDLE_TIMEOUT_SECONDS, 86400).ok
    assert model.get(KEY_IDLE_TIMEOUT_SECONDS) == 86400


# -- the slide interval on one slider ------------------------------------------------------

# Written out, not computed the way the module does it: a dropped, added or nudged step must
# not pass.
EXPECTED_STOPS = (
    *(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),  # 1st quarter: every second
    *(15, 20, 25, 30, 35, 40, 45, 50, 55, 60),  # 2nd quarter: every 5 seconds
    *(120, 180, 300, 600, 900, 1200, 1800, 2700, 3600),  # 3rd quarter: 2 min ... 60 min
    *(7200, 10800, 14400, 21600, 28800, 43200, 86399),  # 4th quarter: 2 h ... 12 h, the end
)
EXPECTED_POSITIONS = (
    *(0, 70, 140, 210, 280, 350, 420, 490, 560, 630),  # 1 s ... 10 s, 70 apart
    *(693, 756, 819, 882, 945, 1008, 1071, 1134, 1197, 1260),  # 15 s ... 1 min, 63 apart
    *(1330, 1400, 1470, 1540, 1610, 1680, 1750, 1820, 1890),  # 2 min ... 1 h, 70 apart
    *(1980, 2070, 2160, 2250, 2340, 2430, 2520),  # 2 h ... the end, 90 apart
)
STEPS = len(EXPECTED_STOPS)


def test_the_slide_interval_runs_from_00_00_01_to_23_59_59():
    assert INTERVAL_MIN_SECONDS == 1
    assert INTERVAL_MAX_SECONDS == 23 * 3600 + 59 * 60 + 59 == 86399
    assert INT_RANGES[KEY_SLIDE_INTERVAL_SECONDS] == (1, 86399)
    assert INTERVAL_STOPS[0] == 1 and INTERVAL_STOPS[-1] == 86399


def test_the_steps_are_exactly_the_four_quarters_and_there_are_36_of_them():
    assert INTERVAL_STOPS == EXPECTED_STOPS
    assert STEPS == len(INTERVAL_STOPS) == len(INTERVAL_POSITIONS) == 36


def test_the_first_quarter_is_every_second_and_the_second_every_5_seconds():
    assert INTERVAL_STOPS[:10] == tuple(range(1, 11))
    assert INTERVAL_STOPS[10:20] == tuple(range(15, 61, 5))


def test_the_third_quarter_is_round_minutes_and_the_fourth_round_hours_up_to_the_end():
    minutes = [s // 60 for s in INTERVAL_STOPS[20:29]]
    assert minutes == [2, 3, 5, 10, 15, 20, 30, 45, 60]  # 1 min is the last step of the 2nd
    hours = [s // 3600 for s in INTERVAL_STOPS[29:35]]
    assert hours == [2, 3, 4, 6, 8, 12]  # 1 h is the last step of the 3rd
    assert INTERVAL_STOPS[35] == 86399  # "24 h" under the slider, 23:59:59 stored


@pytest.mark.parametrize("shared", [10, 60, 3600])
def test_a_step_where_two_quarters_meet_is_on_the_scale_once(shared):
    assert INTERVAL_STOPS.count(shared) == 1
    assert INTERVAL_STOPS.index(shared) == EXPECTED_STOPS.index(shared)


def test_the_steps_rise_and_so_do_their_positions():
    assert list(INTERVAL_STOPS) == sorted(set(INTERVAL_STOPS))
    assert list(INTERVAL_POSITIONS) == sorted(set(INTERVAL_POSITIONS))
    assert INTERVAL_POSITIONS[0] == 0 and INTERVAL_POSITIONS[-1] == INTERVAL_SLIDER_MAX


def test_the_positions_are_written_out_and_the_slider_is_2520_long():
    assert INTERVAL_POSITIONS == EXPECTED_POSITIONS
    assert INTERVAL_SLIDER_MAX == 2520


@pytest.mark.parametrize(
    ("seconds", "share"), [(1, 0), (10, 25), (60, 50), (3600, 75), (86399, 100)]
)
def test_the_quarters_end_at_25_50_75_and_100_percent_of_the_slider(seconds, share):
    assert interval_position_for_seconds(seconds) * 100 == share * INTERVAL_SLIDER_MAX


@pytest.mark.parametrize(
    ("first", "last", "gap"), [(0, 9, 70), (9, 19, 63), (19, 28, 70), (28, 35, 90)]
)
def test_the_steps_of_a_quarter_are_spread_evenly_inside_it(first, last, gap):
    """The indexes are the steps at the ends of a quarter: 1 s, 10 s, 1 min, 1 h, the end."""
    positions = INTERVAL_POSITIONS[first : last + 1]
    assert [b - a for a, b in zip(positions, positions[1:])] == [gap] * (last - first)
    assert positions[-1] - positions[0] == INTERVAL_SLIDER_MAX // 4  # a quarter of the length


@pytest.mark.parametrize("index", range(STEPS))
def test_every_step_maps_to_its_position_and_back(index):
    seconds, position = INTERVAL_STOPS[index], INTERVAL_POSITIONS[index]
    assert interval_position_for_seconds(seconds) == position
    assert interval_seconds_for_position(position) == seconds
    assert snap_interval_position(position) == position


def test_the_ends_of_the_slider():
    assert interval_seconds_for_position(0) == 1  # 00:00:01
    assert interval_seconds_for_position(INTERVAL_SLIDER_MAX) == 86399  # "24 h", 23:59:59
    assert format_hms(interval_seconds_for_position(INTERVAL_SLIDER_MAX)) == "23:59:59"
    assert interval_position_for_seconds(0) == 0
    assert interval_position_for_seconds(86400) == INTERVAL_SLIDER_MAX


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (9, 10),
        (10, 15),  # the 1st quarter into the 2nd, and on into 5-second steps
        (55, 60),
        (60, 120),  # 1 min into the 3rd quarter
        (45 * 60, 3600),
        (3600, 7200),  # 1 h into the 4th quarter
        (43200, 86399),
        (1, 2),
    ],
)
def test_the_neighbouring_steps_at_the_seams_in_both_directions(before, after):
    i = interval_index_for_seconds(before)
    assert INTERVAL_STOPS[i : i + 2] == (before, after)
    pos = INTERVAL_POSITIONS[i]
    assert step_interval_position(pos, 1) == INTERVAL_POSITIONS[i + 1]
    assert step_interval_position(INTERVAL_POSITIONS[i + 1], -1) == pos
    assert interval_seconds_for_position(step_interval_position(pos, 1)) == after


def test_a_step_stops_at_the_ends():
    assert step_interval_position(0, -1) == 0
    assert step_interval_position(0, 1) == INTERVAL_POSITIONS[1]
    assert step_interval_position(INTERVAL_SLIDER_MAX, 1) == INTERVAL_SLIDER_MAX
    assert step_interval_position(INTERVAL_SLIDER_MAX, -1) == INTERVAL_POSITIONS[-2]
    assert step_interval_position(0, -1000) == 0
    assert step_interval_position(0, 1000) == INTERVAL_SLIDER_MAX


def test_a_raw_position_snaps_to_the_nearest_step_the_lower_one_halfway():
    assert snap_interval_position(1) == 0 and snap_interval_position(34) == 0
    assert snap_interval_position(35) == 0  # halfway between 0 and 70: the lower one
    assert snap_interval_position(36) == 70
    assert snap_interval_position(661) == 630  # 31 from 630, 32 from 693
    assert snap_interval_position(662) == 693  # 32 from 630, 31 from 693
    assert snap_interval_position(1295) == 1260  # 35 from 1260, 35 from 1330: the lower one
    assert snap_interval_position(1296) == 1330
    assert snap_interval_position(2475) == 2430  # 45 from 2430, 45 from 2520: the lower one
    assert snap_interval_position(2476) == 2520
    assert interval_seconds_for_position(300) == interval_seconds_for_position(300 + 15)


@pytest.mark.parametrize(
    ("stored", "step"),
    [
        (0, 1),
        (2, 2),  # an earlier step of the old scale that is still a step
        (11, 10),
        (12, 10),
        (13, 15),
        (20, 20),
        (59, 60),
        (90, 60),  # halfway between 60 and 120: the lower one
        (100, 120),
        (150, 120),
        (151, 180),
        (240, 180),  # 4 min: halfway between 3 and 5 min, the lower one
        (4 * 3600, 4 * 3600),
        (7200, 7200),
        (50000, 43200),
        (86399, 86399),
        (86400, 86399),
        (10**9, 86399),
    ],
)
def test_a_value_between_the_steps_goes_to_the_nearest_one_in_seconds(stored, step):
    assert INTERVAL_STOPS[interval_index_for_seconds(stored)] == step


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(1, "1 s"), (45, "45 s"), (60, "1 min"), (2700, "45 min"), (3600, "1 h"), (43200, "12 h")],
)
def test_the_human_text_of_a_step(seconds, text):
    assert describe_interval(seconds) == text


def test_the_longest_step_reads_23_59_59_and_not_24_hours():
    assert describe_interval(86399) == "the longest"
    assert format_hms(86399) == "23:59:59"
    assert "24" not in describe_interval(86399)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(1, "00:00:01"), (3661, "01:01:01"), (3599, "00:59:59"), (86399, "23:59:59")],
)
def test_hours_minutes_seconds_text(seconds, text):
    assert format_hms(seconds) == text
    assert (
        split_hms(seconds)[0] * 3600 + split_hms(seconds)[1] * 60 + split_hms(seconds)[2] == seconds
    )


@pytest.mark.parametrize("index", range(STEPS))
def test_every_step_is_saved_as_its_seconds(model, index):
    result = model.set_interval_position(INTERVAL_POSITIONS[index])
    assert result.ok and result.message == "Saved.", result
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == INTERVAL_STOPS[index]
    view = model.interval_view()
    assert (view.seconds, view.position, view.on_scale) == (
        INTERVAL_STOPS[index],
        INTERVAL_POSITIONS[index],
        True,
    )
    assert view.text == format_hms(INTERVAL_STOPS[index])


def test_a_position_between_two_steps_saves_the_nearest_step(model):
    assert model.set_interval_position(1290).ok  # 30 from 1260 (1 min), 40 from 1330 (2 min)
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 60


@pytest.mark.parametrize("position", [-1, 2521, 10**6, True, 1.5, "3", None])
def test_a_position_off_the_slider_is_refused_and_the_stored_value_stays(model, position):
    model.set_interval_position(INTERVAL_POSITIONS[0])
    assert not model.set_interval_position(position).ok
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 1


def test_a_stored_value_that_is_not_a_step_is_shown_at_the_nearest_and_not_changed(model):
    assert model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 100).ok
    for _ in range(3):  # looking, again and again, writes nothing
        view = model.interval_view()
        assert (view.seconds, view.text, view.on_scale) == (100, "00:01:40", False)
        assert view.position == INTERVAL_POSITIONS[interval_index_for_seconds(120)]
        assert "nearest" in view.caption
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 100


def test_the_default_interval_is_10_seconds_and_a_step():
    assert Settings().get_slide_interval_seconds() == 10  # the schema's default
    assert 10 in INTERVAL_STOPS


def test_a_fresh_window_shows_the_default_10_seconds_on_the_scale(model):
    view = model.interval_view()
    assert (view.seconds, view.text, view.caption, view.on_scale) == (10, "00:00:10", "10 s", True)
    assert view.position == INTERVAL_POSITIONS[9] == 630
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 10  # looking wrote nothing


def test_a_stored_zero_or_a_full_day_cannot_exist_so_the_slider_never_sees_them(model):
    assert not model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 0).ok
    assert not model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 86400).ok
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 10  # the stored default stays


def test_the_slide_interval_in_seconds_takes_one_second_and_a_day_less_one_second_only(model):
    assert model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 1).ok
    assert model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 86399).ok
    assert not model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 0).ok  # 00:00:00
    assert not model.set_int(KEY_SLIDE_INTERVAL_SECONDS, 86400).ok  # 24:00:00
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 86399


# -- where the folder chooser opens ----------------------------------------------------------


def _with_default(monkeypatch, path):
    monkeypatch.setattr("slideshow_lock.preferences_model.default_picture_folder", lambda: path)


def test_the_chooser_opens_in_the_folder_in_use_when_it_exists(model, tmp_path, monkeypatch):
    chosen = tmp_path / "chosen"
    system = tmp_path / "Képek"
    chosen.mkdir()
    system.mkdir()
    _with_default(monkeypatch, str(system))
    model.set_folder(str(chosen))
    assert model.chooser_start_folder() == str(chosen)


def test_the_chooser_opens_in_the_system_pictures_folder_when_the_default_is_in_use(
    model, tmp_path, monkeypatch
):
    system = tmp_path / "Képek"
    system.mkdir()
    _with_default(monkeypatch, str(system))
    assert model.chooser_start_folder() == str(system)  # nothing chosen: the default is in use


def test_the_chooser_opens_in_the_system_pictures_folder_when_the_chosen_one_is_gone(
    model, tmp_path, monkeypatch
):
    system = tmp_path / "Képek"
    system.mkdir()
    _with_default(monkeypatch, str(system))
    model.set_folder(str(tmp_path / "gone"))  # a missing folder can be stored (brief 3.7)
    assert model.chooser_start_folder() == str(system)


def test_the_chooser_opens_in_the_home_directory_only_when_no_pictures_folder_exists(
    model, tmp_path, monkeypatch
):
    _with_default(monkeypatch, str(tmp_path / "Képek"))  # does not exist
    model.set_folder(str(tmp_path / "gone"))
    assert model.chooser_start_folder() == os.path.expanduser("~")


def test_the_chooser_opens_in_the_folder_of_the_field_not_in_the_stored_one(
    model, tmp_path, monkeypatch
):
    """The field holds an edit that is not saved yet: that is where the user is looking."""
    stored = tmp_path / "stored"
    typed = tmp_path / "typed"
    system = tmp_path / "Képek"
    for folder in (stored, typed, system):
        folder.mkdir()
    _with_default(monkeypatch, str(system))
    model.set_folder(str(stored))
    assert model.chooser_start_folder(str(typed)) == str(typed)
    assert model.chooser_start_folder(str(tmp_path / "gone")) == str(system)
    assert model.chooser_start_folder("") == str(system)  # the field empty: the default folder


# -- choices and the pan switch ----------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(CHOICES))
def test_every_listed_choice_is_saved(model, key):
    for value in CHOICES[key]:
        assert model.set_choice(key, value).ok
        assert model.get(key) == value


@pytest.mark.parametrize("key", sorted(CHOICES))
@pytest.mark.parametrize("value", ["shuffle", "", "Fill", None, 1])
def test_a_choice_that_is_not_listed_is_refused_and_the_stored_one_stays(model, key, value):
    before = model.get(key)
    assert not model.set_choice(key, value).ok
    assert model.get(key) == before


def test_pan_is_saved_on_and_off(model):
    assert model.set_pan_portrait_images(True).ok
    assert model.get(KEY_PAN_PORTRAIT_IMAGES) is True
    assert model.set_pan_portrait_images(False).ok
    assert model.get(KEY_PAN_PORTRAIT_IMAGES) is False


@pytest.mark.parametrize("value", ["yes", 1, 0, None, []])
def test_pan_takes_only_a_real_on_or_off(model, value):
    assert not model.set_pan_portrait_images(value).ok
    assert model.get(KEY_PAN_PORTRAIT_IMAGES) is False


# -- transitions -----------------------------------------------------------------------------------


def test_the_transition_drop_down_offers_none_the_ten_transitions_and_the_random_mix():
    assert TRANSITION_CHOICES == (
        "none",
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
        "random",
    )


def test_the_random_mix_is_the_eight_without_blur_and_ken_burns():
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


def test_the_window_starts_at_the_stored_transition(model):
    assert model.transition_choice() == "ken-burns"  # the default: the random mix is not
    assert model.get(KEY_TRANSITIONS) == ["ken-burns"]
    assert model.get(KEY_TRANSITION_ORDER) == "random"  # the default of the order key


@pytest.mark.parametrize("value", TRANSITION_CHOICES)
def test_every_listed_transition_is_saved_and_read_back(model, value):
    assert model.set_transition(value).ok
    assert model.transition_choice() == value


def test_a_stored_cross_fade_still_reads_as_the_cross_fade(model):
    model._settings.set_transitions(["crossfade"])
    assert model.get(KEY_TRANSITIONS) == ["crossfade"]
    assert model.transition_choice() == "crossfade"  # not the default (Ken Burns)


def test_none_is_saved_as_the_empty_list_and_is_not_the_default_again(model):
    assert model.set_transition("none").ok
    assert model.get(KEY_TRANSITIONS) == []
    assert model.transition_choice() == "none"


@pytest.mark.parametrize("value", [t for t in TRANSITION_CHOICES if t not in ("none", "random")])
def test_a_transition_choice_is_saved_as_a_list_of_one_name(model, value):
    assert model.set_transition(value).ok
    assert model.get(KEY_TRANSITIONS) == [value]


def test_a_single_transition_leaves_the_order_key_as_it_was(model):
    assert model.set_transition_order("sequence").ok
    assert model.set_transition("zoom").ok
    assert model.get(KEY_TRANSITION_ORDER) == "sequence"


def test_the_random_mix_stores_the_pool_and_the_order_random(model):
    assert model.set_transition_order("sequence").ok
    assert model.set_transition("random").ok
    assert model.get(KEY_TRANSITIONS) == list(RANDOM_POOL)
    assert model.get(KEY_TRANSITION_ORDER) == "random"
    assert model.transition_choice() == "random"


@pytest.mark.parametrize("value", ["Crossfade", "", None, 1, ["crossfade"], "circle-reveal"])
def test_a_transition_that_is_not_offered_is_refused_and_the_stored_one_stays(model, value):
    before = model.get(KEY_TRANSITIONS)
    assert not model.set_transition(value).ok
    assert model.get(KEY_TRANSITIONS) == before


def test_a_stored_list_of_two_or_more_names_shows_as_the_random_mix(model):
    """Reading never writes: the stored list stays as it is, whatever it holds."""
    model._settings._settings.set_strv("transitions", ["wipe", "fade-black"])
    assert model.transition_choice() == "random"
    assert model.get(KEY_TRANSITIONS) == ["wipe", "fade-black"]
    model._settings._settings.set_strv("transitions", list(ALL_TRANSITIONS))
    assert model.transition_choice() == "random"


def test_a_stored_list_of_one_name_shows_that_name_also_when_it_cannot_be_drawn_yet(model):
    model._settings._settings.set_strv("transitions", ["blur"])
    assert model.transition_choice() == "blur"


def test_a_stored_list_with_only_unknown_names_shows_none(model):
    model._settings._settings.set_strv("transitions", ["no-such-one"])
    assert model.transition_choice() == "none"


@pytest.mark.parametrize("value", TRANSITION_ORDER_CHOICES)
def test_every_listed_transition_order_is_saved(model, value):
    assert model.set_transition_order(value).ok
    assert model.get(KEY_TRANSITION_ORDER) == value


@pytest.mark.parametrize("value", ["shuffle", "", "Random", None, 1])
def test_a_transition_order_that_is_not_listed_is_refused(model, value):
    before = model.get(KEY_TRANSITION_ORDER)
    assert not model.set_transition_order(value).ok
    assert model.get(KEY_TRANSITION_ORDER) == before


# -- the picture folder (D25) ---------------------------------------------------------------


def test_with_nothing_chosen_the_default_folder_is_in_use_and_the_field_stays_empty(model):
    view = model.folder_view()
    assert view.text == ""
    assert view.default == default_picture_folder()
    assert os.path.basename(view.default) != "slideshow-lock"  # the pictures folder itself
    assert view.default in view.note


def test_a_chosen_folder_is_saved_and_shown(model, tmp_path):
    result = model.set_folder(str(tmp_path))
    assert result.ok, result
    view = model.folder_view()
    assert view.text == str(tmp_path)
    assert view.note == f"In use: {tmp_path}"
    assert Settings().get_picture_folder() == str(tmp_path)


def test_an_empty_folder_goes_back_to_the_default(model, tmp_path):
    assert model.set_folder(str(tmp_path)).ok
    assert model.set_folder("").ok
    assert model.folder_view().text == ""
    assert Settings().get_picture_folder() == default_picture_folder()


def test_a_folder_that_does_not_exist_is_saved_and_the_note_says_so(model, tmp_path):
    missing = tmp_path / "later"
    assert model.set_folder(str(missing)).ok  # brief 3.7: not an error
    assert "does not exist yet" in model.folder_view().note
    missing.mkdir()
    assert "does not exist" not in model.folder_view().note


def test_whitespace_around_the_path_is_dropped_and_the_path_is_tidied(model, tmp_path):
    assert model.set_folder(f"  {tmp_path}/sub/../  ").ok
    assert model.folder_view().text == str(tmp_path)


def test_a_tilde_means_the_home_folder(model, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert model.set_folder("~/pics").ok
    assert model.folder_view().text == str(tmp_path / "pics")


@pytest.mark.parametrize("text", ["pictures", "./pictures", "../up", "~other/x"])
def test_a_path_that_is_not_absolute_is_refused_and_the_stored_one_stays(model, tmp_path, text):
    assert model.set_folder(str(tmp_path)).ok
    assert not model.set_folder(text).ok
    assert model.folder_view().text == str(tmp_path)


def test_a_file_is_refused_as_a_folder(model, tmp_path):
    a_file = tmp_path / "x.png"
    a_file.write_bytes(b"x")
    assert not model.set_folder(str(a_file)).ok
    assert model.folder_view().text == ""


@pytest.mark.parametrize("value", ["/tmp/a\0b", None, 5, b"/tmp"])
def test_a_folder_value_that_is_not_a_clean_string_is_refused(model, value):
    assert not model.set_folder(value).ok
    assert model.folder_view().text == ""


# -- "saved" is said only when it is saved ---------------------------------------------------


class _Settings(Settings):
    """A Settings that misbehaves on purpose, to see what the model says about it."""

    mode = "reject"

    def set_slide_interval_seconds(self, value):
        if self.mode == "reject":
            return False
        if self.mode == "lie":  # says yes, stores nothing
            return True
        return super().set_slide_interval_seconds(value)

    def set_picture_folder(self, value):
        return self.mode != "reject"


def test_a_setter_that_says_no_is_not_reported_as_saved():
    stub = _Settings()
    result = PreferencesModel(stub).set_int(KEY_SLIDE_INTERVAL_SECONDS, 20)
    assert not result.ok
    assert result.message != "Saved."
    assert stub.get_slide_interval_seconds() == 10


def test_a_setter_that_says_yes_but_stores_nothing_is_not_reported_as_saved():
    stub = _Settings()
    stub.mode = "lie"
    result = PreferencesModel(stub).set_int(KEY_SLIDE_INTERVAL_SECONDS, 20)
    assert not result.ok
    assert result.message != "Saved."


def test_a_folder_the_settings_refuse_is_not_reported_as_saved(tmp_path):
    stub = _Settings()
    assert not PreferencesModel(stub).set_folder(str(tmp_path)).ok


def test_a_folder_the_settings_claim_to_take_but_do_not_store_is_not_reported_as_saved(tmp_path):
    stub = _Settings()
    stub.mode = "lie"
    assert not PreferencesModel(stub).set_folder(str(tmp_path)).ok
