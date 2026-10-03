"""Tests for ``slideshow_lock.preview_app``: the command line, the per-run settings, and the
worker thread that ``start_preview`` must not leave behind.

The module imports GTK 4 (the typelib is enough: nothing here opens a display). In CI the
GTK 4 typelib is installed and checked by the verify step, so this module is never skipped
there; on a machine without it the import error is the honest answer, not a silent skip.
"""

from __future__ import annotations

import pytest

from slideshow_lock import preview_app
from slideshow_lock.preview import INPUT_KEY, ThreadWorker
from slideshow_lock.preview_app import (
    SessionSettings,
    build_source,
    overrides_from_args,
    start_preview,
)
from slideshow_lock.settings import (
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    Settings,
)
from tests.test_image_source import FakeWatcher, ManualScheduler, make_image, started
from tests.test_preview import FakeScaler, FakeSettings, FakeWindow, _pump
from tests.timeout_guard import (
    per_test_deadline,  # noqa: F401  (autouse fixture)
)


def parsed(*argv):
    return preview_app._parse(list(argv))


# -- the command line ---------------------------------------------------------------------------


def test_no_option_replaces_no_setting():
    assert overrides_from_args(parsed()) == {}


def test_pan_is_replaced_only_when_asked_for():
    assert KEY_PAN_PORTRAIT_IMAGES not in overrides_from_args(parsed("--scaling", "fit"))
    assert overrides_from_args(parsed("--pan")) == {KEY_PAN_PORTRAIT_IMAGES: True}


def test_every_option_replaces_its_own_setting_and_nothing_else():
    got = overrides_from_args(
        parsed("--folder", "/pics", "--interval", "7", "--order", "name", "--scaling", "fit")
    )
    assert got == {
        KEY_PICTURE_FOLDER: "/pics",
        KEY_SLIDE_INTERVAL_SECONDS: 7,
        KEY_ORDER: "name",
        KEY_SCALING: "fit",
    }


@pytest.mark.parametrize("interval", ["0", "-5", "3601", "100000"])
def test_an_interval_outside_one_to_3600_seconds_is_refused(interval):
    with pytest.raises(ValueError, match="between 1 and 3600"):
        overrides_from_args(parsed("--interval", interval))


@pytest.mark.parametrize("interval", ["1", "3600"])
def test_the_ends_of_the_interval_range_are_accepted(interval):
    assert overrides_from_args(parsed("--interval", interval))[KEY_SLIDE_INTERVAL_SECONDS] == int(
        interval
    )


def test_main_prints_the_reason_and_returns_2_for_a_bad_interval(capsys):
    assert preview_app.main(["--interval", "0"]) == 2
    assert "between 1 and 3600" in capsys.readouterr().err


# -- the settings of one run ----------------------------------------------------------------------


class Stored:
    """The stored settings: every getter answers a value of its own, and a write is an error."""

    def get_picture_folder(self):
        return "stored-folder"

    def get_order(self):
        return "stored-order"

    def get_scaling(self):
        return "stored-scaling"

    def get_slide_interval_seconds(self):
        return 99

    def get_pan_portrait_images(self):
        return False

    def connect_changed(self, callback):
        self.callback = callback
        return 7


def test_a_replaced_setting_wins_and_every_other_one_is_the_stored_one():
    settings = SessionSettings(
        Stored(), {KEY_SCALING: "fit", KEY_PAN_PORTRAIT_IMAGES: True, KEY_SLIDE_INTERVAL_SECONDS: 3}
    )
    assert settings.get_scaling() == "fit"
    assert settings.get_pan_portrait_images() is True
    assert settings.get_slide_interval_seconds() == 3
    assert settings.get_picture_folder() == "stored-folder"
    assert settings.get_order() == "stored-order"


def test_without_replacements_every_setting_is_the_stored_one():
    settings = SessionSettings(Stored(), {})
    assert settings.get_scaling() == "stored-scaling"
    assert settings.get_slide_interval_seconds() == 99
    assert settings.get_pan_portrait_images() is False


def test_a_replacement_equal_to_false_or_zero_still_counts_as_a_replacement():
    settings = SessionSettings(Stored(), {KEY_PAN_PORTRAIT_IMAGES: False, KEY_ORDER: ""})
    assert settings.get_pan_portrait_images() is False
    assert settings.get_order() == ""


def test_changes_of_the_stored_settings_still_reach_the_listener():
    stored = Stored()
    seen = []
    assert SessionSettings(stored, {}).connect_changed(seen.append) == 7
    stored.callback("scaling")
    assert seen == ["scaling"]


# -- the worker thread --------------------------------------------------------------------------


def test_start_preview_closes_its_worker_thread_when_the_preview_stops(tmp_path, monkeypatch):
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    windows = [FakeWindow()]
    workers = []

    class Recording(ThreadWorker):
        def __init__(self):
            super().__init__()
            workers.append(self)

    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: windows)
    monkeypatch.setattr(preview_app, "ImageScaler", FakeScaler)
    monkeypatch.setattr(preview_app, "ThreadWorker", Recording)
    controller = start_preview(FakeSettings(), source)
    assert _pump(lambda: windows[0].frames)  # the worker thread ran and delivered a picture
    (worker,) = workers
    thread = worker._thread
    assert thread.is_alive()
    windows[0].fire_input(INPUT_KEY)
    assert not controller.running
    thread.join(5)
    assert not thread.is_alive()  # no thread left behind per preview


# -- the image source ------------------------------------------------------------------------------


def test_the_preview_source_leaves_out_pictures_no_loader_can_read(tmp_path, monkeypatch):
    """``build_source`` is what ``main`` and the smoke use: with the loader probe in place."""
    import os

    from gi.repository import GdkPixbuf

    from tests.test_scaling_gdk import RED, save, solid

    save(tmp_path, "a.png", solid(20, 10, RED), "png")
    (tmp_path / "b.webp").write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(40))
    real = GdkPixbuf.Pixbuf.get_file_info

    def no_webp_loader(path):
        with open(path, "rb") as handle:
            return (None, -1, -1) if b"WEBP" in handle.read(16) else real(path)

    monkeypatch.setattr(GdkPixbuf.Pixbuf, "get_file_info", staticmethod(no_webp_loader))
    settings = Settings()
    assert settings.set_picture_folder(str(tmp_path)) and settings.set_order("name")
    source = build_source(settings)
    source.start()
    try:
        assert _pump(lambda: source.scan_complete)  # the scan runs on the GLib main loop
        assert [os.path.basename(p) for p in source.images()] == ["a.png"]
    finally:
        source.stop()
