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
    INT_RANGES,
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    INTERVAL_POSITIONS,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
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
    Settings,
    default_picture_folder,
)
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


def test_every_key_of_the_schema_has_a_field(model):
    bound = set(INT_RANGES) | set(CHOICES) | {KEY_PAN_PORTRAIT_IMAGES, KEY_PICTURE_FOLDER}
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

ROUND_STEPS = [
    *(m * 60 for m in (2, 3, 5, 10, 15, 20, 30, 45)),
    *(h * 3600 for h in (1, 2, 3, 4, 6, 8, 12)),
    86399,
]


def test_the_slide_interval_runs_from_00_00_01_to_23_59_59():
    assert INTERVAL_MIN_SECONDS == 1
    assert INTERVAL_MAX_SECONDS == 23 * 3600 + 59 * 60 + 59 == 86399
    assert INT_RANGES[KEY_SLIDE_INTERVAL_SECONDS] == (1, 86399)
    assert INTERVAL_STOPS[0] == 1 and INTERVAL_STOPS[-1] == 86399


def test_the_left_half_is_every_second_up_to_a_minute_and_the_right_half_the_round_values():
    assert INTERVAL_STOPS[:60] == tuple(range(1, 61))
    assert list(INTERVAL_STOPS[60:]) == ROUND_STEPS
    assert len(INTERVAL_STOPS) == len(INTERVAL_POSITIONS) == 76
    assert INTERVAL_STOPS.count(60) == 1  # one minute is on the scale once


def test_the_steps_rise_and_so_do_their_positions():
    assert list(INTERVAL_STOPS) == sorted(set(INTERVAL_STOPS))
    assert list(INTERVAL_POSITIONS) == sorted(set(INTERVAL_POSITIONS))
    assert INTERVAL_POSITIONS[0] == 0 and INTERVAL_POSITIONS[-1] == INTERVAL_SLIDER_MAX


def test_the_positions_are_4_apart_on_the_left_and_15_apart_on_the_right():
    # Written out, not computed the way the module does it: a nudged position must not pass.
    assert INTERVAL_POSITIONS[:60] == tuple(range(0, 240, 4))
    assert INTERVAL_POSITIONS[60:] == tuple(range(255, 481, 15))
    assert len(INTERVAL_POSITIONS[60:]) == 16
    assert INTERVAL_SLIDER_MAX == 480


def test_the_slider_is_half_seconds_and_half_round_values():
    half = INTERVAL_SLIDER_MAX // 2
    assert all(p < half for p in INTERVAL_POSITIONS[:60])  # 1 s to 1 min: the left half
    assert all(p > half for p in INTERVAL_POSITIONS[60:])  # 2 min to the end: the right half
    assert INTERVAL_POSITIONS[59] == 236 and INTERVAL_POSITIONS[60] == 255


@pytest.mark.parametrize("index", range(76))
def test_every_step_maps_to_its_position_and_back(index):
    seconds, position = INTERVAL_STOPS[index], INTERVAL_POSITIONS[index]
    assert interval_position_for_seconds(seconds) == position
    assert interval_seconds_for_position(position) == seconds
    assert snap_interval_position(position) == position


def test_the_ends_of_the_slider():
    assert interval_seconds_for_position(0) == 1  # 00:00:01
    assert interval_seconds_for_position(INTERVAL_SLIDER_MAX) == 86399  # "24 h", 23:59:59
    assert format_hms(interval_seconds_for_position(INTERVAL_SLIDER_MAX)) == "23:59:59"
    assert interval_position_for_seconds(0) == 0 and interval_position_for_seconds(86400) == 480


@pytest.mark.parametrize(
    ("before", "after"),
    [(60, 120), (2700, 3600), (43200, 86399), (59, 60), (1, 2)],
)
def test_the_neighbouring_steps_at_the_seams(before, after):
    i = interval_index_for_seconds(before)
    assert INTERVAL_STOPS[i : i + 2] == (before, after)
    pos = INTERVAL_POSITIONS[i]
    assert step_interval_position(pos, 1) == INTERVAL_POSITIONS[i + 1]
    assert step_interval_position(INTERVAL_POSITIONS[i + 1], -1) == pos


def test_a_step_stops_at_the_ends():
    assert step_interval_position(0, -1) == 0
    assert step_interval_position(0, 1) == INTERVAL_POSITIONS[1]
    assert step_interval_position(INTERVAL_SLIDER_MAX, 1) == INTERVAL_SLIDER_MAX
    assert step_interval_position(INTERVAL_SLIDER_MAX, -1) == INTERVAL_POSITIONS[-2]
    assert step_interval_position(0, -1000) == 0 and step_interval_position(0, 1000) == 480


def test_a_raw_position_snaps_to_the_nearest_step_the_lower_one_halfway():
    assert snap_interval_position(1) == 0 and snap_interval_position(2) == 0
    assert snap_interval_position(3) == 4
    assert snap_interval_position(245) == 236  # 9 from 236, 10 from 255
    assert snap_interval_position(246) == 255  # 10 from 236, 9 from 255
    assert snap_interval_position(6) == 4 and snap_interval_position(7) == 8
    assert snap_interval_position(2) == 0  # halfway between 0 and 4: the lower one
    assert interval_seconds_for_position(300) == interval_seconds_for_position(300 + 7)


@pytest.mark.parametrize(
    ("stored", "step"),
    [
        (0, 1),
        (100, 120),
        (70, 60),
        (90, 60),  # halfway between 60 and 120: the lower one
        (150, 120),
        (151, 180),
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


@pytest.mark.parametrize("index", range(76))
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
    assert model.set_interval_position(240).ok  # 4 from 236 (1 min), 15 from 255
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 60


@pytest.mark.parametrize("position", [-1, 481, 10**6, True, 1.5, "3", None])
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


def test_the_default_interval_is_a_step():
    assert Settings().get_slide_interval_seconds() == 10
    assert 10 in INTERVAL_STOPS


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
