"""The copied sample pictures against the real ``Gio.FileMonitor`` and the real image source.

``tests/test_sample_pictures.py`` proves the copy with files only. This module proves what the
slideshow sees of it: ``source_from_settings(Settings(), probe=probe_loadable)`` (the source and
the probe the service builds, ``preview_app.build_source``) walks the default picture folder with
the ``picture-folder`` key never written, while the real ``install`` writes into it, the main
loop pumped by the test. No stand-in for the probe (a fake probe would not see the two second
freshness rule of ``scaling.read_image_file``) and no skip: where the monitor cannot be made the
test is red.

The default picture folder is ``tmp_path/Képek`` (``settings.default_picture_folder`` is replaced:
GLib reads the XDG directories once per process, ``test_sample_pictures_xdg.py`` measures the real
lookup in fresh interpreters).

Not measured here: a real GNOME login (the whole chain from ``slideshowlock autostart``), the
Wayland display, a slow disk. Those are the release gate.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from gi.repository import GLib

from slideshow_lock import sample_pictures as sp
from slideshow_lock import settings as settings_module
from slideshow_lock.image_source import ImageSource, source_from_settings
from slideshow_lock.scaling import ImageSkipped, probe_loadable, read_image_file
from slideshow_lock.settings import KEY_PICTURE_FOLDER, Settings
from tests.sample_fixtures import PICTURES, make_source
from tests.timeout_guard import per_test_deadline  # noqa: F401  (autouse fixture)


def wait_for(predicate, what: str, timeout_s: float = 10.0) -> None:
    """Iterate the default GLib main context until *predicate()* holds."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        if ctx.pending():
            ctx.iteration(False)
        else:
            time.sleep(0.005)
    assert predicate(), f"timed out after {timeout_s}s waiting for: {what}"


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path
        self.pictures = tmp_path / "home" / "Képek"
        self.source_dir = make_source(tmp_path)
        self.state = str(tmp_path / "state" / "sample-pictures.json")
        monkeypatch.setattr(settings_module, "default_picture_folder", lambda: str(self.pictures))
        self.settings = Settings()
        assert self.settings._settings.get_user_value(KEY_PICTURE_FOLDER) is None
        self.sources = []

    def install(self) -> sp.Result:
        return sp.install(self.source_dir, str(self.pictures), self.state)

    def start(self) -> ImageSource:
        source = source_from_settings(self.settings, probe=probe_loadable)
        self.sources.append(source)
        source.start()
        wait_for(lambda: source.scan_complete, "the first walk to finish")
        return source

    def listed(self, source) -> list:
        return sorted(os.path.relpath(p, str(self.pictures)) for p in source.images())

    def install_while_pumping(self, source) -> sp.Result:
        """``install`` on a thread (as the command runs it) while this thread pumps the main loop;
        every pass checks that no hidden half-copy is on the list."""
        done = []
        thread = threading.Thread(target=lambda: done.append(self.install()))
        thread.start()
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 20
        while thread.is_alive() and time.monotonic() < deadline:
            assert not [p for p in source.images() if ".part-" in p]
            if ctx.pending():
                ctx.iteration(False)
            else:
                time.sleep(0.002)
        thread.join(5)
        assert done, "the copy did not finish"
        return done[0]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    yield rig
    for source in rig.sources:
        source.stop()


EXPECTED = sorted(f"sakkmesterke/{name}" for name in PICTURES)


def test_pictures_that_are_there_at_the_start_are_found_with_the_key_empty(rig):
    assert rig.install().status == sp.DONE
    source = rig.start()
    assert rig.listed(source) == EXPECTED  # the credits file is not a picture


def test_pictures_copied_after_the_start_appear_and_a_half_copy_never_does(rig):
    rig.pictures.mkdir(parents=True)
    source = rig.start()
    assert rig.listed(source) == []
    assert rig.install_while_pumping(source).status == sp.DONE
    wait_for(lambda: len(source) == len(PICTURES), "the copies to appear")
    assert rig.listed(source) == EXPECTED
    assert not [p for p in source.images() if "/." in p]


def test_a_missing_pictures_folder_is_one_warning_at_the_start_and_none_after_the_copy(rig, caplog):
    assert not rig.pictures.exists()
    with caplog.at_level(logging.WARNING):
        source = rig.start()
    # at the start: one line from the setting and one from the image source, the two a missing
    # folder has always made (``[slideshow-dir]``); after the copy there is none
    lines = [
        (r.name, r.getMessage()) for r in caplog.records if "[slideshow-dir]" in r.getMessage()
    ]
    assert [name for name, _ in lines] == ["slideshow_lock.settings", "slideshow_lock.image_source"]
    assert "is missing" in lines[0][1]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert rig.install_while_pumping(source).status == sp.DONE
        wait_for(lambda: len(source) == len(PICTURES), "the copies to appear in the new folder")
    assert rig.listed(source) == EXPECTED
    assert not [r for r in caplog.records if "[slideshow-dir]" in r.getMessage()]


def test_two_copies_one_after_the_other_are_each_listed_exactly_once(rig):
    rig.pictures.mkdir(parents=True)
    source = rig.start()
    rig.install_while_pumping(source)
    wait_for(lambda: len(source) == len(PICTURES), "all copies to appear")
    wait_for(lambda: not GLib.MainContext.default().pending(), "the events to be delivered")
    listed = rig.listed(source)
    assert listed == EXPECTED and len(set(listed)) == len(listed)


def test_a_deleted_picture_goes_and_a_deleted_folder_empties_the_list_without_a_stop(rig, caplog):
    rig.install()
    source = rig.start()
    os.remove(rig.pictures / "sakkmesterke" / "fr02.jpg")
    wait_for(lambda: len(source) == len(PICTURES) - 1, "the deleted picture to go")
    assert "sakkmesterke/fr02.jpg" not in rig.listed(source)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        shutil.rmtree(rig.pictures / "sakkmesterke")
        wait_for(lambda: len(source) == 0, "the list to empty")
    assert source.advance() is None  # no picture, and no exception: the service goes on
    assert rig.install().status == sp.NOTHING_TO_DO  # and the folder does not come back
    assert not (rig.pictures / "sakkmesterke").exists()


def test_the_users_own_pictures_and_the_samples_are_listed_together(rig):
    rig.pictures.mkdir(parents=True)
    own = rig.pictures / "mine.png"
    own.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    rig.install()
    source = rig.start()
    assert rig.listed(source) == sorted(["mine.png"] + EXPECTED)


def test_a_copy_has_the_age_of_its_source_so_the_picture_reader_takes_it_at_once(rig, monkeypatch):
    rig.install()
    copied = str(rig.pictures / "sakkmesterke" / "fr01.jpg")
    assert read_image_file(copied)  # no ImageSkipped: it is an hour old, as its source


def test_without_the_time_of_the_source_the_copy_is_held_back_as_still_being_copied(
    rig, monkeypatch
):
    # the negative control of the case above: the same copy with no ``utime`` is two seconds fresh
    monkeypatch.setattr(os, "utime", lambda *args, **kwargs: None)
    rig.install()
    copied = str(rig.pictures / "sakkmesterke" / "fr01.jpg")
    with pytest.raises(ImageSkipped, match="still being copied"):
        read_image_file(copied)
