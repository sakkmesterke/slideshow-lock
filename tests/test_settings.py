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

from gi.repository import GLib

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
