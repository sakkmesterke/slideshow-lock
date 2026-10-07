"""Tests of the edits the settings window keeps until the user saves (``preferences_model.Draft``),
without GTK. The rule is the one of the Save button: nothing reaches the settings before ``save``,
``save`` writes every kept edit, and one that cannot be stored stays kept and is reported.

The tests read the settings through a second ``Settings`` object, as the service does: what they see
is what is stored, not what the draft holds.
"""

from __future__ import annotations

import pytest

from slideshow_lock.preferences_model import (
    CHOICES,
    INT_RANGES,
    INTERVAL_POSITIONS,
    INTERVAL_STOPS,
    RANDOM_POOL,
    SAVE_ORDER,
    Draft,
    PreferencesModel,
)
from slideshow_lock.settings import (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITIONS,
    Settings,
    default_picture_folder,
)


@pytest.fixture
def draft():
    return Draft(PreferencesModel(Settings()))


def stored():
    """The stored values, as a process other than the window sees them."""
    other = Settings()
    return {
        "idle": other.get_idle_timeout_seconds(),
        "grace": other.get_lock_grace_period_seconds(),
        "interval": other.get_slide_interval_seconds(),
        "order": other.get_order(),
        "scaling": other.get_scaling(),
        "pan": other.get_pan_portrait_images(),
        "transitions": other.get_transitions(),
        "transition_order": other.get_transition_order(),
        "folder": other.get_picture_folder(),
    }


def edit_everything(draft, folder):
    results = [
        draft.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 300),
        draft.edit_int(KEY_LOCK_GRACE_PERIOD_SECONDS, 7),
        draft.edit_interval_position(INTERVAL_POSITIONS[INTERVAL_STOPS.index(30)]),
        draft.edit_choice(KEY_ORDER, "name"),
        draft.edit_choice(KEY_SCALING, "fit"),
        draft.edit_pan_portrait_images(True),
        draft.edit_transition("zoom"),
        draft.edit_folder(str(folder)),
    ]
    assert all(r.ok for r in results), results


# -- nothing is written before save ----------------------------------------------------------------


def test_an_edit_is_kept_and_not_stored(draft, tmp_path):
    before = stored()
    edit_everything(draft, tmp_path)
    assert draft.dirty
    assert stored() == before


def test_the_draft_shows_its_own_values_over_the_stored_ones(draft, tmp_path):
    edit_everything(draft, tmp_path)
    assert draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 300
    assert draft.value(KEY_ORDER) == "name"
    assert draft.value(KEY_PAN_PORTRAIT_IMAGES) is True
    assert draft.value(KEY_TRANSITIONS) == "zoom"
    assert draft.folder_text() == str(tmp_path)
    assert draft.interval_view().seconds == 30
    assert draft.value(KEY_LOCK_GRACE_PERIOD_SECONDS) == 7


def test_what_is_not_edited_shows_the_stored_value(draft):
    assert draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 120
    assert draft.value(KEY_TRANSITIONS) == "crossfade"
    assert draft.interval_view().seconds == 5
    assert not draft.dirty


# -- save --------------------------------------------------------------------------------------


def test_save_writes_every_edit(draft, tmp_path):
    edit_everything(draft, tmp_path)
    result = draft.save()
    assert result.ok and result.message == "Saved."
    assert stored() == {
        "idle": 300,
        "grace": 7,
        "interval": 30,
        "order": "name",
        "scaling": "fit",
        "pan": True,
        "transitions": ["zoom"],
        "transition_order": "random",
        "folder": str(tmp_path),
    }
    assert not draft.dirty


@pytest.mark.parametrize(
    "edit, key",
    [
        (lambda d: d.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 300), KEY_IDLE_TIMEOUT_SECONDS),
        (lambda d: d.edit_int(KEY_LOCK_GRACE_PERIOD_SECONDS, 7), KEY_LOCK_GRACE_PERIOD_SECONDS),
        (lambda d: d.edit_interval_position(INTERVAL_POSITIONS[3]), KEY_SLIDE_INTERVAL_SECONDS),
        (lambda d: d.edit_choice(KEY_ORDER, "name"), KEY_ORDER),
        (lambda d: d.edit_choice(KEY_SCALING, "fit"), KEY_SCALING),
        (lambda d: d.edit_pan_portrait_images(True), KEY_PAN_PORTRAIT_IMAGES),
        (lambda d: d.edit_transition("wipe"), KEY_TRANSITIONS),
        (lambda d: d.edit_folder("/nonexistent/pictures"), KEY_PICTURE_FOLDER),
    ],
)
def test_save_writes_each_key_on_its_own(draft, edit, key):
    """One edit at a time, so that a key that save skipped is named by the test that fails."""
    before = stored()
    assert edit(draft).ok
    assert draft.pending.keys() == {key}
    assert draft.save().ok
    changed = {name for name, value in stored().items() if value != before[name]}
    assert len(changed) == 1, changed  # exactly the field that was edited
    assert not draft.dirty


def test_every_key_a_draft_can_hold_is_in_the_save_order():
    assert set(SAVE_ORDER) == (
        set(INT_RANGES)
        | set(CHOICES)
        | {KEY_PAN_PORTRAIT_IMAGES, KEY_PICTURE_FOLDER, KEY_TRANSITIONS}
    )
    assert len(SAVE_ORDER) == len(set(SAVE_ORDER))


def test_saving_with_nothing_edited_writes_nothing_and_says_nothing(draft):
    seen = []
    listener = Settings()
    listener.connect_changed(seen.append)
    result = draft.save()
    assert result.ok and result.message == ""
    assert seen == []


def test_the_random_mix_saves_the_pool_and_the_order_random(draft):
    Settings().set_transition_order("sequence")
    assert draft.edit_transition("random").ok
    assert draft.save().ok
    assert stored()["transitions"] == list(RANDOM_POOL)
    assert stored()["transition_order"] == "random"


# -- what is kept and what is dropped ------------------------------------------------------------


def test_an_edit_back_to_the_stored_value_is_dropped(draft):
    assert draft.edit_choice(KEY_ORDER, "name").ok and draft.dirty
    assert draft.edit_choice(KEY_ORDER, "random").ok
    assert not draft.dirty


def test_a_refused_edit_changes_nothing(draft):
    assert draft.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 300).ok
    refused = draft.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 0)
    assert not refused.ok and refused.message
    assert draft.pending == {KEY_IDLE_TIMEOUT_SECONDS: 300}  # the earlier edit is still there
    assert not draft.edit_choice(KEY_ORDER, "shuffle").ok
    assert not draft.edit_transition("sparkle").ok
    assert not draft.edit_folder("relative/path").ok
    assert not draft.edit_interval_position(INTERVAL_POSITIONS[-1] + 1).ok
    assert draft.pending == {KEY_IDLE_TIMEOUT_SECONDS: 300}


def test_the_folder_is_kept_as_it_would_be_stored(draft, tmp_path):
    assert draft.edit_folder(f"  {tmp_path}/sub/../  ").ok
    assert draft.folder_text() == str(tmp_path)
    assert draft.edit_folder("").ok  # the default folder: the stored text is "" too
    assert not draft.dirty


def test_a_stored_value_that_is_not_a_step_is_not_rewritten_by_a_save_of_something_else(draft):
    Settings().set_slide_interval_seconds(100)
    assert draft.interval_view().on_scale is False
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.save().ok
    assert stored()["interval"] == 100


def test_a_stored_list_of_several_names_is_not_rewritten_unless_the_choice_is_changed(draft):
    Settings()._settings.set_strv("transitions", ["wipe", "push"])
    assert draft.value(KEY_TRANSITIONS) == "random"
    assert draft.edit_transition("random").ok  # the same entry: nothing to keep
    assert not draft.dirty
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.save().ok
    assert stored()["transitions"] == ["wipe", "push"]


# -- a value that cannot be stored ----------------------------------------------------------


def test_a_key_that_cannot_be_stored_stays_kept_and_the_others_are_saved(draft, monkeypatch):
    monkeypatch.setattr(Settings, "set_scaling", lambda self, value: False)
    assert draft.edit_choice(KEY_SCALING, "fit").ok
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 300).ok
    result = draft.save()
    assert not result.ok and result.message != "Saved."
    assert draft.pending == {KEY_SCALING: "fit"}
    assert stored()["order"] == "name" and stored()["idle"] == 300
    assert stored()["scaling"] == "fill"
    monkeypatch.undo()
    assert draft.save().ok  # the next save tries it again
    assert stored()["scaling"] == "fit"


def test_a_value_that_does_not_read_back_is_not_called_saved(draft, monkeypatch):
    monkeypatch.setattr(Settings, "get_order", lambda self: "random")
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.save().ok is False
    assert draft.dirty


def test_the_first_failure_is_the_one_reported(draft, monkeypatch):
    monkeypatch.setattr(Settings, "set_order", lambda self, value: False)
    monkeypatch.setattr(Settings, "set_scaling", lambda self, value: False)
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.edit_choice(KEY_SCALING, "fit").ok
    result = draft.save()
    assert not result.ok
    assert set(draft.pending) == {KEY_ORDER, KEY_SCALING}


# -- what the preview reads ------------------------------------------------------------------


def test_the_preview_values_are_the_edits_as_the_settings_getters_return_them(draft, tmp_path):
    edit_everything(draft, tmp_path)
    assert draft.preview_values() == {
        KEY_IDLE_TIMEOUT_SECONDS: 300,
        KEY_LOCK_GRACE_PERIOD_SECONDS: 7,
        KEY_SLIDE_INTERVAL_SECONDS: 30,
        KEY_ORDER: "name",
        KEY_SCALING: "fit",
        KEY_PAN_PORTRAIT_IMAGES: True,
        KEY_TRANSITIONS: ["zoom"],
        KEY_PICTURE_FOLDER: str(tmp_path),
    }


def test_the_preview_values_resolve_the_default_folder_and_the_random_mix(draft, tmp_path):
    assert draft.edit_folder(str(tmp_path)).ok
    assert draft.edit_transition("random").ok
    Settings().set_picture_folder(str(tmp_path))  # now the stored folder is not the default one
    assert draft.edit_folder("").ok  # back to the default: an edit now
    values = draft.preview_values()
    assert values[KEY_PICTURE_FOLDER] == default_picture_folder()
    assert values[KEY_TRANSITIONS] == list(RANDOM_POOL)
    assert values[KEY_TRANSITION_ORDER] == "random"


def test_there_are_no_preview_values_without_edits(draft):
    assert draft.preview_values() == {}


def test_reading_the_preview_values_stores_nothing(draft, tmp_path):
    before = stored()
    edit_everything(draft, tmp_path)
    draft.preview_values()
    assert stored() == before
