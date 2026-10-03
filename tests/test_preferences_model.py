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
from slideshow_lock.preferences_model import CHOICES, INT_RANGES, PreferencesModel
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
