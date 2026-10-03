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
    PreferencesModel,
    format_hms,
    join_hms,
    split_hms,
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
    # The three sliders are for the slide interval only: the idle time stays 1 to 86400 seconds.
    assert INT_RANGES[KEY_IDLE_TIMEOUT_SECONDS] == (1, 86400)
    assert INT_RANGES[KEY_LOCK_GRACE_PERIOD_SECONDS] == (0, 86400)
    assert model.set_int(KEY_IDLE_TIMEOUT_SECONDS, 86400).ok
    assert model.get(KEY_IDLE_TIMEOUT_SECONDS) == 86400


# -- the slide interval as hours, minutes and seconds ---------------------------------------------


def test_the_slide_interval_runs_from_00_00_01_to_23_59_59():
    assert INTERVAL_MIN_SECONDS == join_hms(0, 0, 1) == 1
    assert INTERVAL_MAX_SECONDS == join_hms(23, 59, 59) == 86399
    assert INT_RANGES[KEY_SLIDE_INTERVAL_SECONDS] == (1, 86399)


@pytest.mark.parametrize(
    ("seconds", "parts", "text"),
    [
        (1, (0, 0, 1), "00:00:01"),
        (59, (0, 0, 59), "00:00:59"),
        (60, (0, 1, 0), "00:01:00"),
        (120, (0, 2, 0), "00:02:00"),
        (300, (0, 5, 0), "00:05:00"),
        (3599, (0, 59, 59), "00:59:59"),
        (3600, (1, 0, 0), "01:00:00"),
        (3661, (1, 1, 1), "01:01:01"),
        (86399, (23, 59, 59), "23:59:59"),
    ],
)
def test_seconds_and_the_three_sliders_convert_both_ways(seconds, parts, text):
    assert split_hms(seconds) == parts
    assert join_hms(*parts) == seconds
    assert format_hms(seconds) == text


def test_every_slide_interval_survives_the_round_trip_through_the_sliders():
    for seconds in range(INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS + 1):
        hours, minutes, secs = split_hms(seconds)
        assert 0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= secs <= 59
        assert join_hms(hours, minutes, secs) == seconds


def test_every_position_of_the_sliders_is_a_distinct_number_of_seconds():
    seen = {join_hms(h, m, s) for h in range(24) for m in range(60) for s in range(60)}
    assert seen == set(range(0, 86400))  # 00:00:00 up to 23:59:59, none twice, none missing


@pytest.mark.parametrize(
    ("parts", "stored"),
    [
        ((0, 0, 1), 1),  # the shortest
        ((0, 0, 59), 59),
        ((0, 1, 0), 60),
        ((0, 59, 59), 3599),
        ((1, 0, 0), 3600),
        ((1, 1, 1), 3661),
        ((23, 59, 59), 86399),  # the longest
    ],
)
def test_the_sliders_are_saved_as_seconds(model, parts, stored):
    result = model.set_interval(*parts)
    assert result.ok and result.message == "Saved.", result
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == stored


def test_all_three_sliders_at_zero_become_the_shortest_slide_interval(model):
    model.set_interval(0, 5, 0)
    result = model.set_interval(0, 0, 0)
    assert result.ok
    assert "00:00:01" in result.message and result.message != "Saved."
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 1


@pytest.mark.parametrize("parts", [(24, 0, 0), (0, 60, 0), (0, 0, 60), (-1, 0, 5), (0, -1, 5)])
def test_a_slider_value_outside_its_range_is_refused_and_the_stored_time_stays(model, parts):
    model.set_interval(0, 5, 0)
    result = model.set_interval(*parts)
    assert not result.ok
    assert result.message.startswith(
        "Hours must be 0 to 23"
    )  # the model's own check, not the schema's
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 300


@pytest.mark.parametrize("parts", [(True, 0, 1), (0, 1.5, 0), (0, 0, "1"), (None, 0, 1)])
def test_slider_values_that_are_not_whole_numbers_are_refused(model, parts):
    assert not model.set_interval(*parts).ok
    assert model.get(KEY_SLIDE_INTERVAL_SECONDS) == 10


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
