"""Tests for the CORE-3 settings layer.

Covers the four CORE-3 acceptance criteria from the card:
1. the service picks up a changed value without a restart (test_live_reload...)
2. an invalid value cannot be saved (test_*_rejects_out_of_range / _rejects_unknown_choice)
3. the identifier comes from a single constant (test_schema_id_matches_app_id)
4. the default picture folder is the XDG pictures dir itself, and a missing folder
   is a logged WARNING, not an error
   (test_default_picture_folder_* / test_get_picture_folder_warns_on_missing_folder)
"""

from __future__ import annotations

import logging
import time
import warnings

import pytest
from gi.repository import Gio, GLib

from slideshow_lock import APP_ID
from slideshow_lock.settings import (
    Settings,
    default_picture_folder,
)


def _pump_until(predicate, timeout_s: float = 2.0) -> bool:
    """Iterate the default GLib main context until *predicate()* is true.

    `Gio.Settings` "changed" notifications are delivered through the default
    main context, not synchronously inside `set_*`; tests that assert on a
    `connect_changed` callback need to pump that context themselves, since
    there is no running GLib/GTK main loop under pytest.
    """
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        if ctx.pending():
            ctx.iteration(False)
        else:
            time.sleep(0.01)
    return predicate()


# -- D17: single identifier constant -----------------------------------------


def test_schema_id_matches_app_id():
    # The schema id in data/io.github.trensoft.slideshowlock.gschema.xml
    # must be exactly APP_ID (D17) -- this fails loudly if either drifts.
    settings = Settings()
    assert settings._settings.props.schema_id == APP_ID


# -- defaults -----------------------------------------------------------------


def test_defaults_match_brief_section_5():
    settings = Settings()
    assert settings.get_idle_timeout_seconds() == 120
    assert settings.get_lock_grace_period_seconds() == 0
    assert settings.get_slide_interval_seconds() == 10
    assert settings.get_order() == "random"
    assert settings.get_scaling() == "fill"
    # Battery-sensitive animation: stays off until it is measured on the reference laptop.
    assert settings.get_pan_portrait_images() is False
    # Ken Burns is the picture change out of the box (an empty list would be the cut).
    assert settings.get_transitions() == ["ken-burns"]
    assert settings.get_transition_order() == "random"
    assert settings.get_transition_duration() == 1.0


# -- roundtrips -----------------------------------------------------------------


def test_idle_timeout_seconds_roundtrip():
    settings = Settings()
    assert settings.set_idle_timeout_seconds(300) is True
    assert settings.get_idle_timeout_seconds() == 300


def test_lock_grace_period_seconds_roundtrip():
    settings = Settings()
    assert settings.set_lock_grace_period_seconds(15) is True
    assert settings.get_lock_grace_period_seconds() == 15


def test_slide_interval_seconds_roundtrip():
    settings = Settings()
    assert settings.set_slide_interval_seconds(30) is True
    assert settings.get_slide_interval_seconds() == 30


def test_order_roundtrip_both_choices():
    settings = Settings()
    assert settings.set_order("name") is True
    assert settings.get_order() == "name"
    assert settings.set_order("random") is True
    assert settings.get_order() == "random"


def test_scaling_roundtrip_both_choices():
    settings = Settings()
    assert settings.set_scaling("fit") is True
    assert settings.get_scaling() == "fit"


def test_pan_portrait_images_roundtrip():
    settings = Settings()
    assert settings.set_pan_portrait_images(True) is True
    assert settings.get_pan_portrait_images() is True
    assert settings.set_pan_portrait_images(False) is True
    assert settings.get_pan_portrait_images() is False


def test_transitions_roundtrip_in_the_order_given():
    settings = Settings()
    assert settings.set_transitions(["fade-black", "crossfade"]) is True
    assert settings.get_transitions() == ["fade-black", "crossfade"]
    assert settings.set_transitions(("push",)) is True  # a tuple is a list too
    assert settings.get_transitions() == ["push"]


def test_a_stored_cross_fade_stays_a_cross_fade_when_the_default_is_another():
    from slideshow_lock.transitions import DEFAULT_TRANSITIONS

    settings = Settings()
    assert settings.get_transitions() == ["ken-burns"]  # nothing stored: the default
    assert settings.get_transitions() == list(DEFAULT_TRANSITIONS)  # the schema and the code agree
    assert settings.set_transitions(["crossfade"]) is True
    assert settings.get_transitions() == ["crossfade"]  # a stored choice is not the default


def test_an_empty_list_of_transitions_is_a_real_choice_not_the_default():
    settings = Settings()
    assert settings.set_transitions([]) is True
    assert settings.get_transitions() == []  # no transition: not the schema's default again


def test_every_one_of_the_ten_names_can_be_stored():
    from slideshow_lock.transitions import ALL_TRANSITIONS

    settings = Settings()
    assert settings.set_transitions(list(ALL_TRANSITIONS)) is True
    assert settings.get_transitions() == list(ALL_TRANSITIONS)


def test_transition_order_roundtrip():
    settings = Settings()
    assert settings.set_transition_order("sequence") is True
    assert settings.get_transition_order() == "sequence"
    assert settings.set_transition_order("random") is True
    assert settings.get_transition_order() == "random"


def test_picture_folder_roundtrip(tmp_path):
    settings = Settings()
    target = str(tmp_path)
    assert settings.set_picture_folder(target) is True
    assert settings.get_picture_folder() == target


# -- acceptance criterion 2: invalid values cannot be saved -------------------


def test_idle_timeout_seconds_rejects_out_of_range(caplog):
    settings = Settings()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        ok = settings.set_idle_timeout_seconds(999999)  # schema max is 86400
    assert ok is False
    assert settings.get_idle_timeout_seconds() == 120  # unchanged default
    assert any("[config]" in record.message for record in caplog.records)
    assert any("idle-timeout-seconds" in record.message for record in caplog.records)


def test_idle_timeout_seconds_rejects_zero():
    # schema range min is 1: zero is not a valid idle timeout.
    settings = Settings()
    assert settings.set_idle_timeout_seconds(0) is False
    assert settings.get_idle_timeout_seconds() == 120


@pytest.mark.parametrize("value", [1, 59, 60, 3600, 3661, 86399])
def test_slide_interval_seconds_takes_00_00_01_up_to_23_59_59(value):
    settings = Settings()
    assert settings.set_slide_interval_seconds(value) is True
    assert settings.get_slide_interval_seconds() == value


@pytest.mark.parametrize("value", [0, 86400, 86401])
def test_slide_interval_seconds_takes_neither_00_00_00_nor_24_00_00(value):
    settings = Settings()
    assert settings.set_slide_interval_seconds(30) is True
    assert settings.set_slide_interval_seconds(value) is False
    assert settings.get_slide_interval_seconds() == 30  # unchanged


def test_order_rejects_unknown_choice(caplog):
    settings = Settings()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        ok = settings.set_order("diagonal")
    assert ok is False
    assert settings.get_order() == "random"  # unchanged default
    assert any("[config]" in record.message for record in caplog.records)


def test_scaling_rejects_unknown_choice():
    settings = Settings()
    assert settings.set_scaling("stretch") is False
    assert settings.get_scaling() == "fill"


def test_pan_portrait_images_rejects_values_that_are_not_a_real_bool(caplog):
    # Gio would happily store the truthiness of "no" or 1, so the wrapper must refuse them.
    settings = Settings()
    for bad in ("no", "false", 1, 0, None, [], "yes"):
        with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
            assert settings.set_pan_portrait_images(bad) is False
        assert settings.get_pan_portrait_images() is False  # unchanged default
    assert any("pan-portrait-images" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "bad",
    [
        ["sparkle"],
        ["crossfade", "sparkle"],
        ["crossfade", "crossfade"],  # one name twice
        ["Crossfade"],
        [""],
        [None],
        [1],
        "crossfade",  # a string is not a list of names
        None,
        7,
    ],
)
def test_transitions_rejects_what_is_not_a_list_of_known_names_once_each(bad, caplog):
    settings = Settings()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        assert settings.set_transitions(bad) is False
    assert settings.get_transitions() == ["ken-burns"]  # unchanged default
    assert any("transitions" in r.message and "[config]" in r.message for r in caplog.records)


def test_transition_order_rejects_unknown_choice():
    settings = Settings()
    assert settings.set_transition_order("shuffle") is False
    assert settings.get_transition_order() == "random"


def test_a_stored_name_the_program_does_not_know_is_left_out_and_logged_once(caplog):
    """Written by another version, or by hand: the setting still works, and the log says why a
    choice did not count. The getter is read for every picture, so it must not log every time."""
    from slideshow_lock import transitions

    transitions._reported.clear()
    settings = Settings()
    settings._settings.set_strv("transitions", ["fade-black", "sparkle", "crossfade", "fade-black"])
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.transitions"):
        for _ in range(3):
            assert settings.get_transitions() == ["fade-black", "crossfade"]
    assert [r.getMessage() for r in caplog.records].count(
        "[config] ignoring the unknown transition name 'sparkle' in the setting"
    ) == 1


# -- acceptance criterion 1: no restart needed ---------------------------------


def test_live_reload_notifies_without_restart():
    # Two independent Settings/Gio.Settings instances, standing in for "the
    # preferences window writes a value" and "the running service is
    # watching for it" -- no object is recreated between the write and the
    # callback firing, which is the behaviour acceptance criterion 1 requires.
    writer = Settings()
    reader = Settings()

    seen_keys = []
    reader.connect_changed(seen_keys.append)

    writer.set_idle_timeout_seconds(240)

    assert _pump_until(lambda: "idle-timeout-seconds" in seen_keys)
    assert reader.get_idle_timeout_seconds() == 240


def test_disconnect_changed_stops_every_callback_of_that_object_and_no_other():
    writer, reader, other = Settings(), Settings(), Settings()
    first, second, kept = [], [], []
    reader.connect_changed(first.append)
    reader.connect_changed(second.append)
    other.connect_changed(kept.append)

    writer.set_idle_timeout_seconds(241)
    assert _pump_until(lambda: first and second and kept)  # all three are listening

    reader.disconnect_changed()
    first.clear()
    second.clear()
    kept.clear()
    writer.set_idle_timeout_seconds(242)
    assert _pump_until(lambda: kept)  # the change was delivered where a listener is left
    assert first == [] and second == []

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # GLib reports a handler id that is gone as a Warning
        reader.disconnect_changed()  # a second call changes nothing
    reader.connect_changed(first.append)  # and the object can listen again
    writer.set_idle_timeout_seconds(243)
    assert _pump_until(lambda: first)


# -- acceptance criterion 4: XDG-derived default picture folder (D25) ---------


def test_default_picture_folder_uses_the_xdg_pictures_dir_itself(monkeypatch):
    monkeypatch.setattr(GLib, "get_user_special_dir", lambda _kind: "/home/example-user/Pictures")
    folder = default_picture_folder()
    assert folder == "/home/example-user/Pictures"


def test_default_picture_folder_falls_back_to_home_when_xdg_unset(monkeypatch):
    monkeypatch.setattr(GLib, "get_user_special_dir", lambda _kind: None)
    monkeypatch.setattr(GLib, "get_home_dir", lambda: "/home/example-user")
    folder = default_picture_folder()
    assert folder == "/home/example-user/Pictures"


def test_get_picture_folder_resolves_default_when_key_is_empty(monkeypatch):
    monkeypatch.setattr(GLib, "get_user_special_dir", lambda _kind: "/home/example-user/Pictures")
    settings = Settings()
    assert settings.get_picture_folder() == "/home/example-user/Pictures"


def test_get_picture_folder_warns_on_missing_folder(tmp_path, caplog):
    settings = Settings()
    missing = str(tmp_path / "does-not-exist")
    settings.set_picture_folder(missing)

    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        returned = settings.get_picture_folder()

    # brief 3.7: a missing folder is a logged event, not an exception, and
    # the service (here: the caller) keeps running -- so the path is still
    # returned, not swallowed.
    assert returned == missing
    assert any("[slideshow-dir]" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_get_picture_folder_no_warning_when_folder_exists(tmp_path, caplog):
    settings = Settings()
    settings.set_picture_folder(str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        settings.get_picture_folder()

    assert not any("[slideshow-dir]" in record.message for record in caplog.records)


# -- first-run-done and the user's own picture folder (the login start of ``control``) -------------


@pytest.fixture
def first_run_keys():
    settings = Settings()
    for key in ("first-run-done", "picture-folder"):
        settings._settings.reset(key)
    yield settings
    for key in ("first-run-done", "picture-folder"):
        settings._settings.reset(key)


def test_first_run_done_is_false_until_it_is_set(first_run_keys):
    assert first_run_keys.get_first_run_done() is False
    assert first_run_keys.set_first_run_done(True) is True
    assert Settings().get_first_run_done() is True


def test_first_run_done_takes_nothing_but_a_boolean(first_run_keys, caplog):
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        assert first_run_keys.set_first_run_done("yes") is False
    assert first_run_keys.get_first_run_done() is False


def test_the_default_picture_folder_is_not_a_chosen_one(first_run_keys):
    assert first_run_keys.has_chosen_picture_folder() is False


@pytest.mark.parametrize("value", ["/home/user/Pictures/Holiday", ""])
def test_a_stored_picture_folder_is_a_chosen_one_even_an_empty_one(first_run_keys, value):
    first_run_keys.set_picture_folder(value)
    assert first_run_keys.has_chosen_picture_folder() is True


def test_an_empty_key_means_the_default_picture_folder(first_run_keys):
    assert first_run_keys.uses_default_picture_folder() is True
    first_run_keys.set_picture_folder("")
    assert first_run_keys.uses_default_picture_folder() is True


def test_the_default_written_out_is_still_the_default_with_or_without_a_slash(first_run_keys):
    default = default_picture_folder()
    for text in (default, default + "/", default + "//", default + "/x/.."):
        first_run_keys.set_picture_folder(text)
        assert first_run_keys.uses_default_picture_folder() is True, text


@pytest.mark.parametrize("text", ["/home/user/Pictures/Holiday", "/", "relative", "/tmp"])
def test_any_other_folder_is_not_the_default(first_run_keys, text):
    first_run_keys.set_picture_folder(text)
    assert first_run_keys.uses_default_picture_folder() is False


def test_asking_about_the_default_is_no_warning_when_the_folder_is_missing(first_run_keys, caplog):
    first_run_keys.set_picture_folder("/nonexistent/chosen")
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.settings"):
        assert first_run_keys.uses_default_picture_folder() is False
        assert Settings().uses_default_picture_folder() is False
    assert not [r for r in caplog.records if "[slideshow-dir]" in r.getMessage()]


# -- transition-duration ----------------------------------------------------------------------


@pytest.mark.parametrize("value", [0.2, 0.5, 1.0, 2.75, 5.0, 3])
def test_transition_duration_roundtrip(value):
    settings = Settings()
    assert settings.set_transition_duration(value) is True
    assert settings.get_transition_duration() == float(value)


@pytest.mark.parametrize(
    "value", [0.19, 0.0, -1.0, 5.01, 60, float("nan"), float("inf"), "1.0", None, True, [1.0]]
)
def test_transition_duration_rejects_what_is_outside_its_range_or_not_a_number(value, caplog):
    settings = Settings()
    assert settings.set_transition_duration(2.0) is True
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.settings"):
        assert settings.set_transition_duration(value) is False
    assert settings.get_transition_duration() == 2.0  # unchanged
    assert any(
        "transition-duration" in r.message and "[config]" in r.message for r in caplog.records
    )


def test_the_schema_holds_the_duration_to_its_range():
    schema = Gio.SettingsSchemaSource.get_default().lookup(APP_ID, True)
    assert schema.get_key("transition-duration").get_range().unpack() == ("range", (0.2, 5.0))
    assert schema.get_key("transition-duration").get_default_value().unpack() == 1.0
    assert schema.get_key("slide-interval-seconds").get_default_value().unpack() == 10
