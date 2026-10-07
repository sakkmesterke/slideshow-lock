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
    KEY_HARDWARE_ACCELERATION,
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SHOW_SCREENSHOTS,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_DURATION,
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
        "screenshots": other.get_show_screenshots(),
        "acceleration": other.get_hardware_acceleration(),
        "transitions": other.get_transitions(),
        "transition_order": other.get_transition_order(),
        "duration": other.get_transition_duration(),
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
        draft.edit_show_screenshots(True),
        draft.edit_hardware_acceleration(False),
        draft.edit_transition("zoom"),
        draft.edit_duration(2.5),
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
    assert draft.value(KEY_SHOW_SCREENSHOTS) is True
    assert draft.value(KEY_HARDWARE_ACCELERATION) is False
    assert draft.value(KEY_TRANSITIONS) == "zoom"
    assert draft.folder_text() == str(tmp_path)
    assert draft.interval_view().seconds == 30
    assert draft.value(KEY_TRANSITION_DURATION) == 2.5
    assert draft.value(KEY_LOCK_GRACE_PERIOD_SECONDS) == 7


def test_what_is_not_edited_shows_the_stored_value(draft):
    assert draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 120
    assert draft.value(KEY_TRANSITIONS) == "ken-burns"
    assert draft.interval_view().seconds == 10
    assert draft.value(KEY_TRANSITION_DURATION) == 1.0
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
        "screenshots": True,
        "acceleration": False,
        "transitions": ["zoom"],
        "transition_order": "random",
        "duration": 2.5,
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
        (lambda d: d.edit_show_screenshots(True), KEY_SHOW_SCREENSHOTS),
        (lambda d: d.edit_hardware_acceleration(False), KEY_HARDWARE_ACCELERATION),
        (lambda d: d.edit_transition("wipe"), KEY_TRANSITIONS),
        (lambda d: d.edit_duration(3.3), KEY_TRANSITION_DURATION),
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
        | {
            KEY_HARDWARE_ACCELERATION,
            KEY_PAN_PORTRAIT_IMAGES,
            KEY_SHOW_SCREENSHOTS,
            KEY_PICTURE_FOLDER,
            KEY_TRANSITIONS,
            KEY_TRANSITION_DURATION,
        }
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


def test_the_screenshots_switch_is_a_draft_edit_like_the_others(draft):
    assert draft.value(KEY_SHOW_SCREENSHOTS) is False
    assert draft.edit_show_screenshots(True).ok
    assert draft.pending == {KEY_SHOW_SCREENSHOTS: True}
    assert stored()["screenshots"] is False  # not stored before save
    assert draft.edit_show_screenshots(False).ok  # back to the stored value: not an edit any more
    assert not draft.dirty
    assert not draft.edit_show_screenshots("yes").ok
    assert not draft.dirty


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
        KEY_SHOW_SCREENSHOTS: True,
        KEY_HARDWARE_ACCELERATION: False,
        KEY_TRANSITIONS: ["zoom"],
        KEY_TRANSITION_DURATION: 2.5,
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


def test_a_duration_back_at_the_stored_value_is_no_edit(draft):
    assert draft.edit_duration(2.0).ok and draft.dirty
    assert draft.edit_duration(1.0).ok  # the stored default
    assert not draft.dirty


def test_a_refused_duration_keeps_nothing(draft):
    assert not draft.edit_duration(9.0).ok
    assert not draft.edit_duration(float("nan")).ok
    assert not draft.dirty


def test_the_duration_edit_is_rounded_to_a_tenth_before_it_is_kept(draft):
    assert draft.edit_duration(1.2345).ok
    assert draft.value(KEY_TRANSITION_DURATION) == 1.2
    assert draft.preview_values()[KEY_TRANSITION_DURATION] == 1.2


# -- when there is something to save (the state of the Save button is ``dirty``) ------------------

#: Per key: how to change the field, and how to bring it back to what is stored (the defaults of
#: the schema; the first test below checks that the stored values are those).
SAVE_STATE_CASES = {
    KEY_IDLE_TIMEOUT_SECONDS: (
        lambda d, tmp: d.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 300),
        lambda d, tmp: d.edit_int(KEY_IDLE_TIMEOUT_SECONDS, 120),
    ),
    KEY_LOCK_GRACE_PERIOD_SECONDS: (
        lambda d, tmp: d.edit_int(KEY_LOCK_GRACE_PERIOD_SECONDS, 7),
        lambda d, tmp: d.edit_int(KEY_LOCK_GRACE_PERIOD_SECONDS, 0),
    ),
    KEY_SLIDE_INTERVAL_SECONDS: (
        lambda d, tmp: d.edit_interval_position(INTERVAL_POSITIONS[INTERVAL_STOPS.index(30)]),
        lambda d, tmp: d.edit_interval_position(INTERVAL_POSITIONS[INTERVAL_STOPS.index(10)]),
    ),
    KEY_ORDER: (
        lambda d, tmp: d.edit_choice(KEY_ORDER, "name"),
        lambda d, tmp: d.edit_choice(KEY_ORDER, "random"),
    ),
    KEY_SCALING: (
        lambda d, tmp: d.edit_choice(KEY_SCALING, "fit"),
        lambda d, tmp: d.edit_choice(KEY_SCALING, "fill"),
    ),
    KEY_PAN_PORTRAIT_IMAGES: (
        lambda d, tmp: d.edit_pan_portrait_images(True),
        lambda d, tmp: d.edit_pan_portrait_images(False),
    ),
    KEY_SHOW_SCREENSHOTS: (
        lambda d, tmp: d.edit_show_screenshots(True),
        lambda d, tmp: d.edit_show_screenshots(False),
    ),
    KEY_HARDWARE_ACCELERATION: (
        lambda d, tmp: d.edit_hardware_acceleration(False),
        lambda d, tmp: d.edit_hardware_acceleration(True),
    ),
    KEY_TRANSITIONS: (
        lambda d, tmp: d.edit_transition("zoom"),
        lambda d, tmp: d.edit_transition("ken-burns"),
    ),
    KEY_TRANSITION_DURATION: (
        lambda d, tmp: d.edit_duration(2.5),
        lambda d, tmp: d.edit_duration(1.0),
    ),
    KEY_PICTURE_FOLDER: (
        lambda d, tmp: d.edit_folder(str(tmp)),
        lambda d, tmp: d.edit_folder(""),
    ),
}


def test_the_save_state_cases_cover_every_key_a_draft_can_hold():
    assert set(SAVE_STATE_CASES) == set(SAVE_ORDER)


def test_the_save_state_cases_start_from_the_stored_defaults(draft):
    """The way back in the cases is the stored value: if a default of the schema moved, the cases
    would test an edit, not a way back."""
    for key, (_change, restore) in SAVE_STATE_CASES.items():
        assert restore(draft, None).ok, key
        assert not draft.dirty, key


def test_there_is_nothing_to_save_when_the_window_opens(draft):
    assert not draft.dirty
    assert draft.pending == {}


@pytest.mark.parametrize("key", SAVE_ORDER)
def test_one_changed_field_is_something_to_save(draft, key, tmp_path):
    change, _restore = SAVE_STATE_CASES[key]
    assert change(draft, tmp_path).ok
    assert draft.dirty
    assert set(draft.pending) == {key}


@pytest.mark.parametrize("key", SAVE_ORDER)
def test_there_is_nothing_to_save_after_a_save(draft, key, tmp_path):
    change, _restore = SAVE_STATE_CASES[key]
    assert change(draft, tmp_path).ok
    assert draft.save().ok
    assert not draft.dirty
    assert draft.pending == {}


@pytest.mark.parametrize("key", SAVE_ORDER)
def test_a_field_put_back_to_what_is_stored_is_nothing_to_save(draft, key, tmp_path):
    change, restore = SAVE_STATE_CASES[key]
    assert change(draft, tmp_path).ok
    assert draft.dirty
    assert restore(draft, tmp_path).ok
    assert not draft.dirty
    assert draft.pending == {}


def test_putting_one_field_back_leaves_the_other_edits_to_save(draft, tmp_path):
    """Only a draft with no edit left is clean: one field put back does not clear the rest."""
    edit_everything(draft, tmp_path)
    _change, restore = SAVE_STATE_CASES[KEY_ORDER]
    assert restore(draft, tmp_path).ok
    assert draft.dirty
    assert KEY_ORDER not in draft.pending
    assert len(draft.pending) == len(SAVE_ORDER) - 1


def test_a_field_changed_after_a_save_is_measured_against_the_new_stored_value(draft):
    assert draft.edit_choice(KEY_ORDER, "name").ok
    assert draft.save().ok
    assert not draft.dirty
    assert draft.edit_choice(KEY_ORDER, "random").ok  # no longer the stored value
    assert draft.dirty
    assert draft.edit_choice(KEY_ORDER, "name").ok  # the stored one now
    assert not draft.dirty
