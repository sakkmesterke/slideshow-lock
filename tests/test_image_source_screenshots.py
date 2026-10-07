"""Tests for leaving screenshots out of the slideshow (the ``show-screenshots`` setting).

Same fake monitor and manual scheduler as ``tests/test_image_source.py``, real files under
``tmp_path``. What counts as a screenshot is read from the *names* of the walked entries, so the
tests build trees with such names. Nothing here needs GNOME: the translations GNOME Shell adds at
run time are tested with an injected translator, and a real GNOME screenshot folder is not measured
(``docs/image-source.md``).
"""

from __future__ import annotations

import os

import pytest

from slideshow_lock import image_source
from slideshow_lock.image_source import (
    SAMPLE_DIR_NAME,
    FsEvent,
    ImageSource,
    _gnome_shell_names,
    is_screenshot_dir_name,
    is_screenshot_file_name,
    source_from_settings,
)
from slideshow_lock.settings import Settings
from tests.test_image_source import (
    FakeWatcher,
    ManualScheduler,
    make_image,
    make_source,
    names,
    started,
)
from tests.timeout_guard import per_test_deadline  # noqa: F401  (autouse fixture)

#: Every kind of screenshot the filter knows by name: a folder (English, Hungarian, any case), and
#: files that start like GNOME's, KDE's and the Hungarian name.
SHOT_FILES = [
    "Screenshots/one.png",
    "Képernyőképek/two.png",
    "SCREENSHOTS/three.png",
    "Screenshot from 2026-10-07 11-00-00.png",
    "Screenshot_20261007_110000.png",
    "screenshot-4.jpg",
    "Képernyőkép 2026-10-07.png",
    "other/Screenshots/deep.png",
    "other/Screenshots/2026/deeper.png",
]

#: Pictures that are not screenshots, in places and with names that are close to the above.
PLAIN_FILES = [
    "holiday.png",
    "other/b.png",
    "Shots/c.png",  # not a screenshot name
    "my screenshot.png",  # "screenshot" is not at the start
    "Screenshotsarchive/d.png",  # the folder name is not exactly a screenshot folder's
]


@pytest.fixture
def backends():
    return FakeWatcher(), ManualScheduler()


@pytest.fixture(autouse=True)
def no_desktop_names(monkeypatch):
    """The names the tests expect are the fixed ones: not what the machine's gettext says."""
    monkeypatch.setattr(image_source, "_DESKTOP_NAMES", (set(), set()))


def build(root, files):
    for rel in files:
        make_image(root / rel)


def shown(src, root):
    return sorted(names(src, root))


# -- what is left out, by default ---------------------------------------------------------------


def test_by_default_screenshot_folders_and_files_are_left_out(tmp_path, backends):
    build(tmp_path, SHOT_FILES + PLAIN_FILES)
    src = started(tmp_path, backends)
    assert shown(src, tmp_path) == sorted(PLAIN_FILES)
    assert src.show_screenshots is False


def test_with_the_setting_on_nothing_is_left_out(tmp_path, backends):
    build(tmp_path, SHOT_FILES + PLAIN_FILES)
    src = started(tmp_path, backends, show_screenshots=True)
    assert shown(src, tmp_path) == sorted(SHOT_FILES + PLAIN_FILES)


@pytest.mark.parametrize("show", [False, True])
def test_a_picture_that_is_no_screenshot_stays_in_either_way(tmp_path, backends, show):
    build(tmp_path, PLAIN_FILES)
    src = started(tmp_path, backends, show_screenshots=show)
    assert shown(src, tmp_path) == sorted(PLAIN_FILES)


def test_the_folder_is_not_even_walked(tmp_path, backends):
    """A screenshot folder gets no monitor and is not listed: the filter works before the walk."""
    build(tmp_path, ["Screenshots/one.png", "keep.png"])
    watcher, _scheduler = backends
    started(tmp_path, backends)
    assert str(tmp_path / "Screenshots") not in watcher.callbacks
    assert str(tmp_path) in watcher.callbacks


# -- the sample pictures and a folder chosen on purpose are never emptied ----------------------


def test_the_sample_pictures_folder_has_the_name_of_the_sample_pictures_module():
    from slideshow_lock import sample_pictures

    assert SAMPLE_DIR_NAME == sample_pictures.SUBDIR


def test_nothing_in_the_sample_pictures_folder_is_left_out(tmp_path, backends):
    inside = [
        f"{SAMPLE_DIR_NAME}/Screenshot from the board.png",
        f"{SAMPLE_DIR_NAME}/Screenshots/y.png",
        f"{SAMPLE_DIR_NAME}/kept.png",
    ]
    build(tmp_path, inside + ["Screenshots/out.png", "plain.png"])
    src = started(tmp_path, backends)
    assert shown(src, tmp_path) == sorted(inside + ["plain.png"])


def test_the_sample_pictures_folder_as_the_root_is_shown_whole(tmp_path, backends):
    root = tmp_path / SAMPLE_DIR_NAME
    files = ["Screenshot from the board.png", "Screenshots/y.png", "kept.png"]
    build(root, files)
    src = started(root, backends)
    assert shown(src, root) == sorted(files)


@pytest.mark.parametrize("folder", ["Screenshots", "Képernyőképek", "screenshots"])
def test_a_screenshot_folder_chosen_as_the_root_is_shown_whole(tmp_path, backends, folder):
    """The explicit choice wins: the folder the user picked is not emptied by the filter."""
    root = tmp_path / folder
    files = ["Screenshot from 2026-10-07 11-00-00.png", "2026/Screenshot_1.png", "plain.png"]
    build(root, files)
    src = started(root, backends)
    assert shown(src, root) == sorted(files)


def test_a_folder_below_a_screenshot_folder_chosen_as_the_root_is_shown_whole(tmp_path, backends):
    root = tmp_path / "Screenshots" / "2026"
    files = ["Screenshot from a.png", "Screenshots/b.png", "plain.png"]
    build(root, files)
    src = started(root, backends)
    assert shown(src, root) == sorted(files)


def test_the_picture_folder_above_a_screenshot_folder_is_filtered(tmp_path, backends):
    """The default folder (the pictures folder) is filtered: the exception is for a folder that is
    itself a screenshot folder, not for one that has such a folder in it."""
    build(tmp_path / "Pictures", ["Screenshots/s.png", "plain.png"])
    src = started(tmp_path / "Pictures", backends)
    assert shown(src, tmp_path / "Pictures") == ["plain.png"]


# -- links: the filter reads names and changes nothing about how the walk follows links --------


def test_a_link_is_judged_by_its_own_name_not_by_where_it_points(tmp_path, backends):
    root = tmp_path / "root"
    build(tmp_path / "elsewhere", ["a.png"])
    build(tmp_path / "Screenshots", ["s.png"])  # the real folder, outside the root
    make_image(root / "plain.png")
    os.symlink(str(tmp_path / "elsewhere"), str(root / "Screenshots"))  # named like one
    os.symlink(str(tmp_path / "Screenshots"), str(root / "Holiday"))  # points to one
    src = started(root, backends)
    # the link named Screenshots is left out; the link named Holiday is followed as before
    assert shown(src, root) == ["Holiday/s.png", "plain.png"]


def test_a_link_to_a_file_is_judged_by_its_own_name(tmp_path, backends):
    root = tmp_path / "root"
    make_image(tmp_path / "real.png")
    os.makedirs(root)
    os.symlink(str(tmp_path / "real.png"), str(root / "Screenshot from link.png"))
    os.symlink(str(tmp_path / "real.png"), str(root / "link.png"))
    src = started(root, backends)
    assert shown(src, root) == ["link.png"]


def test_the_filter_never_resolves_a_link(tmp_path, backends, monkeypatch):
    """The filter must not look where a link goes: no ``realpath``, no ``readlink``, no ``lstat``
    from the filter's code, so it cannot widen what the walk reaches."""
    build(tmp_path, ["Screenshots/s.png", "plain.png"])
    os.symlink(str(tmp_path / "Screenshots"), str(tmp_path / "Holiday"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the filter resolved a link")

    src = make_source(tmp_path, backends)
    monkeypatch.setattr(os.path, "realpath", forbidden)
    monkeypatch.setattr(os, "readlink", forbidden)
    monkeypatch.setattr(os, "lstat", forbidden)
    assert src._left_out(str(tmp_path / "Holiday"), is_dir=True) is False
    assert src._left_out(str(tmp_path / "Screenshots"), is_dir=True) is True
    assert src._left_out(str(tmp_path / "Screenshot a.png"), is_dir=False) is True


def test_a_symlink_loop_still_ends_with_the_filter_on(tmp_path, backends):
    build(tmp_path, ["a.png", "sub/b.png", "sub/Screenshots/s.png"])
    os.symlink(str(tmp_path), str(tmp_path / "sub" / "loop"))
    src = make_source(tmp_path, backends)
    src.start()
    assert backends[1].run_all(max_steps=10_000), "the walk did not end on a symlink loop"
    assert shown(src, tmp_path) == ["a.png", "sub/b.png"]


# -- live changes -----------------------------------------------------------------------------


def test_a_screenshot_that_appears_later_is_left_out(tmp_path, backends):
    watcher, scheduler = backends
    build(tmp_path, ["plain.png"])
    src = started(tmp_path, backends)
    shot = make_image(tmp_path / "Screenshot from 2026-10-07 12-00-00.png")
    watcher.emit(shot, FsEvent.CREATED)
    watcher.emit(shot, FsEvent.CHANGES_DONE)
    other = make_image(tmp_path / "new.png")
    watcher.emit(other, FsEvent.CREATED)
    watcher.emit(other, FsEvent.CHANGES_DONE)
    scheduler.run_all()
    assert shown(src, tmp_path) == ["new.png", "plain.png"]


def test_a_screenshot_folder_that_appears_later_is_not_walked(tmp_path, backends):
    watcher, scheduler = backends
    build(tmp_path, ["plain.png"])
    src = started(tmp_path, backends)
    shots = tmp_path / "Screenshots"
    make_image(shots / "late.png")
    watcher.emit(str(shots), FsEvent.CREATED)
    scheduler.run_all()
    assert shown(src, tmp_path) == ["plain.png"]
    assert str(shots) not in watcher.callbacks


def test_a_screenshot_that_appears_later_is_shown_when_the_setting_is_on(tmp_path, backends):
    watcher, scheduler = backends
    build(tmp_path, ["plain.png"])
    src = started(tmp_path, backends, show_screenshots=True)
    shot = make_image(tmp_path / "Screenshot from 2026-10-07 12-00-00.png")
    watcher.emit(shot, FsEvent.CREATED)
    watcher.emit(shot, FsEvent.CHANGES_DONE)
    scheduler.run_all()
    assert shown(src, tmp_path) == ["Screenshot from 2026-10-07 12-00-00.png", "plain.png"]


# -- switching the setting while the slideshow runs -----------------------------------------------


def test_switching_the_setting_walks_the_folder_again(tmp_path, backends):
    build(tmp_path, SHOT_FILES + PLAIN_FILES)
    src = started(tmp_path, backends)
    assert shown(src, tmp_path) == sorted(PLAIN_FILES)
    src.set_show_screenshots(True)
    assert backends[1].run_all()
    assert shown(src, tmp_path) == sorted(SHOT_FILES + PLAIN_FILES)
    src.set_show_screenshots(False)
    assert backends[1].run_all()
    assert shown(src, tmp_path) == sorted(PLAIN_FILES)


def test_switching_to_the_same_value_does_nothing(tmp_path, backends):
    build(tmp_path, SHOT_FILES + PLAIN_FILES)
    src = started(tmp_path, backends)
    seen = []
    src.connect_current_changed(seen.append)
    src.set_show_screenshots(False)
    assert seen == []
    assert shown(src, tmp_path) == sorted(PLAIN_FILES)


def test_switching_tells_the_listeners_the_picture_on_screen_is_gone(tmp_path, backends):
    build(tmp_path, ["a.png"])
    src = started(tmp_path, backends)
    seen = []
    src.connect_current_changed(seen.append)
    src.set_show_screenshots(True)
    assert seen == [None]  # what a new folder does too: the queue starts again
    backends[1].run_all()
    assert seen[-1] == str(tmp_path / "a.png")


def test_a_stopped_source_stays_stopped_when_the_setting_changes(tmp_path, backends):
    build(tmp_path, SHOT_FILES)
    src = make_source(tmp_path, backends)
    src.set_show_screenshots(True)
    assert len(src) == 0
    src.start()
    assert backends[1].run_all()
    assert len(src) == len(SHOT_FILES)


# -- the setting reaches the source ---------------------------------------------------------------


def test_the_source_from_the_settings_follows_the_key(tmp_path, backends):
    watcher, scheduler = backends
    build(tmp_path, ["Screenshots/s.png", "plain.png"])
    settings = Settings()
    assert settings.set_picture_folder(str(tmp_path))
    source = source_from_settings(settings, watcher=watcher, scheduler=scheduler)
    source.start()
    assert scheduler.run_all()
    assert shown(source, tmp_path) == ["plain.png"]  # off is the default
    assert settings.set_show_screenshots(True)
    assert scheduler.run_all()
    assert shown(source, tmp_path) == ["Screenshots/s.png", "plain.png"]
    assert settings.set_show_screenshots(False)
    assert scheduler.run_all()
    assert shown(source, tmp_path) == ["plain.png"]
    settings.disconnect_changed()


def test_the_source_from_the_settings_starts_from_the_stored_value(tmp_path, backends):
    watcher, scheduler = backends
    build(tmp_path, ["Screenshots/s.png", "plain.png"])
    settings = Settings()
    assert settings.set_picture_folder(str(tmp_path))
    assert settings.set_show_screenshots(True)
    source = source_from_settings(settings, watcher=watcher, scheduler=scheduler)
    assert source.show_screenshots is True
    source.start()
    assert scheduler.run_all()
    assert shown(source, tmp_path) == ["Screenshots/s.png", "plain.png"]
    settings.disconnect_changed()


# -- the names ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Screenshots", "screenshots", "SCREENSHOTS", "Képernyőképek", "KÉPERNYŐKÉPEK"],
)
def test_screenshot_folder_names(name):
    assert is_screenshot_dir_name(name)


@pytest.mark.parametrize(
    "name",
    ["Screenshot", "Screenshots2", "My Screenshots", "Shots", "trensoft", "Pictures", ""],
)
def test_names_that_are_not_screenshot_folders(name):
    assert not is_screenshot_dir_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "Screenshot from 2026-10-07 11-00-00.png",
        "screenshot from x.png",
        "Screenshot_20261007.png",
        "Screenshot-1.jpg",
        "Képernyőkép 2026.png",
        "KÉPERNYŐKÉP.png",
    ],
)
def test_screenshot_file_names(name):
    assert is_screenshot_file_name(name)


@pytest.mark.parametrize(
    "name", ["holiday.png", "my screenshot.png", "shot.png", "a Screenshot.png"]
)
def test_names_that_are_not_screenshot_files(name):
    assert not is_screenshot_file_name(name)


def test_the_translations_of_gnome_shell_are_added_when_its_catalog_is_there():
    texts = {"Screenshots": "Bildschirmfotos", "Screenshot from %s": "Bildschirmfoto vom %s"}
    folders, prefixes = _gnome_shell_names(lambda msgid: texts.get(msgid, msgid))
    assert folders == {"bildschirmfotos"}
    assert prefixes == {"bildschirmfoto vom"}


def test_without_a_gnome_catalog_nothing_new_is_added():
    """No catalog: the translator hands the English text back, which is already known."""
    folders, prefixes = _gnome_shell_names(lambda msgid: msgid)
    assert folders == {"screenshots"}
    assert prefixes == {"screenshot from"}
    assert all(is_screenshot_dir_name(name) for name in folders)


@pytest.mark.parametrize(
    "texts",
    [
        {"Screenshot from %s": "%s"},  # nothing before the date: it would match every picture
        {"Screenshot from %s": "ab %s"},  # too short to be a name start
        {"Screenshots": "a/b"},  # a path, not a folder name
        {"Screenshots": "  "},
    ],
)
def test_a_translation_that_would_match_too_much_is_not_taken(texts):
    folders, prefixes = _gnome_shell_names(lambda msgid: texts.get(msgid, msgid))
    assert "" not in folders and "" not in prefixes
    assert "a/b" not in folders
    assert all(len(prefix) >= 4 for prefix in prefixes)


def test_a_broken_catalog_changes_nothing():
    def broken(_msgid):
        raise OSError("bad catalog")

    assert _gnome_shell_names(broken) == (set(), set())


def test_the_names_of_the_session_are_used_by_the_filter(tmp_path, backends, monkeypatch):
    monkeypatch.setattr(
        image_source, "_DESKTOP_NAMES", ({"bildschirmfotos"}, {"bildschirmfoto vom"})
    )
    build(tmp_path, ["Bildschirmfotos/a.png", "Bildschirmfoto vom 2026-10-07.png", "plain.png"])
    assert shown(started(tmp_path, backends), tmp_path) == ["plain.png"]
    assert is_screenshot_dir_name("Bildschirmfotos")
    assert is_screenshot_file_name("Bildschirmfoto vom x.png")


def test_the_default_source_of_names_does_not_raise_here():
    """The real lookup (the ``gnome-shell`` text domain of this machine, if any) on its own."""
    folders, prefixes = _gnome_shell_names()
    assert all(isinstance(name, str) for name in folders | prefixes)


def test_a_source_made_without_the_argument_leaves_screenshots_out(tmp_path, backends):
    build(tmp_path, ["Screenshots/s.png", "plain.png"])
    watcher, scheduler = backends
    src = ImageSource(str(tmp_path), watcher=watcher, scheduler=scheduler, order="name")
    src.start()
    assert scheduler.run_all()
    assert shown(src, tmp_path) == ["plain.png"]
