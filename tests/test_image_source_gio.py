"""CORE-4 image source against the real ``Gio.FileMonitor`` and the GLib main loop.

``tests/test_image_source.py`` proves the logic with fake backends. This module
proves the same behaviour end to end: real files created, moved and deleted
under ``tmp_path``, real monitor events, delivered through the GLib main
context, which the tests pump themselves (there is no running main loop under
pytest).
"""

from __future__ import annotations

import logging
import os
import shutil
import time

import pytest
from gi.repository import GLib

from slideshow_lock.image_source import ImageSource, source_from_settings
from slideshow_lock.settings import Settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def make_image(path, data: bytes = PNG) -> str:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def wait_for(predicate, what: str, timeout_s: float = 5.0) -> None:
    """Iterate the default GLib main context until *predicate()* holds."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        if ctx.pending():
            ctx.iteration(False)
        else:
            time.sleep(0.005)
    assert predicate(), f"timed out after {timeout_s}s waiting for: {what}"


@pytest.fixture
def real_source():
    """Factory for sources with the default (GLib + Gio) backends, stopped at teardown."""
    made = []

    def factory(folder, **kwargs) -> ImageSource:
        kwargs.setdefault("order", "name")
        src = ImageSource(str(folder), **kwargs)
        made.append(src)
        return src

    yield factory
    for src in made:
        src.stop()


def started(src: ImageSource) -> ImageSource:
    src.start()
    wait_for(lambda: src.scan_complete, "initial walk to finish")
    return src


def names(src: ImageSource, root) -> list:
    return [os.path.relpath(p, str(root)) for p in src.images()]


def test_gio_criterion3_copied_in_image_appears_and_deleted_image_disappears(tmp_path, real_source):
    make_image(tmp_path / "a.png")
    src = started(real_source(tmp_path))
    assert names(src, tmp_path) == ["a.png"]

    make_image(tmp_path / "b.png")
    wait_for(lambda: len(src) == 2, "copied-in image to appear")
    assert names(src, tmp_path) == ["a.png", "b.png"]

    os.remove(tmp_path / "a.png")
    wait_for(lambda: len(src) == 1, "deleted image to disappear")
    assert names(src, tmp_path) == ["b.png"]


def test_gio_criterion3_new_subfolder_with_images_appears_and_deleted_subfolder_disappears(
    tmp_path, real_source
):
    make_image(tmp_path / "a.png")
    src = started(real_source(tmp_path))

    make_image(tmp_path / "new" / "n1.png")  # creates the folder and the image
    wait_for(lambda: len(src) == 2, "image in the new subfolder to appear")

    make_image(tmp_path / "new" / "n2.png")  # the new folder is monitored from now on
    wait_for(lambda: len(src) == 3, "second image in the new subfolder to appear")
    assert names(src, tmp_path) == ["a.png", "new/n1.png", "new/n2.png"]

    shutil.rmtree(tmp_path / "new")
    wait_for(lambda: len(src) == 1, "images of the deleted subfolder to disappear")
    assert names(src, tmp_path) == ["a.png"]


def test_gio_criterion3_subfolder_moved_in_with_images_already_inside_appears(
    tmp_path, real_source
):
    root = tmp_path / "root"
    make_image(root / "a.png")
    make_image(tmp_path / "staging" / "deep" / "m1.png")
    make_image(tmp_path / "staging" / "m2.png")
    src = started(real_source(root))

    os.rename(tmp_path / "staging", root / "moved")
    wait_for(lambda: len(src) == 3, "moved-in subfolder images to appear")
    assert names(src, root) == ["a.png", "moved/deep/m1.png", "moved/m2.png"]


def test_gio_criterion4_deleting_the_displayed_image_advances_to_the_next(tmp_path, real_source):
    for n in "abcd":
        make_image(tmp_path / f"{n}.png")
    src = started(real_source(tmp_path))
    src.advance()  # b.png is on screen
    shown = src.current()
    assert shown.endswith("b.png")
    changes = []
    src.connect_current_changed(changes.append)

    os.remove(shown)
    wait_for(lambda: shown not in src.images(), "displayed image to be dropped")

    assert src.current().endswith("c.png")  # the image that followed it
    assert changes == [src.current()]
    assert [os.path.basename(src.advance()) for _ in range(3)] == ["d.png", "a.png", "c.png"]


def test_gio_criterion4_deleting_the_last_image_gives_the_empty_state_and_logs(
    tmp_path, real_source, caplog
):
    only = make_image(tmp_path / "only.png")
    src = started(real_source(tmp_path))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        os.remove(only)
        wait_for(lambda: src.current() is None, "queue to become empty")
    assert src.advance() is None
    assert any(
        "[slideshow-dir]" in r.message and r.levelno == logging.WARNING for r in caplog.records
    )

    revived = make_image(tmp_path / "again.png")
    wait_for(lambda: src.current() == revived, "an image to come back")


def test_gio_criterion5_corrupt_image_in_a_watched_folder_is_skipped_with_a_log(
    tmp_path, real_source, caplog
):
    make_image(tmp_path / "good.png")
    src = started(real_source(tmp_path))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        make_image(tmp_path / "broken.jpg", b"definitely not a jpeg")
        wait_for(
            lambda: any("broken.jpg" in r.message for r in caplog.records),
            "the corrupt file to be reported",
        )
    assert names(src, tmp_path) == ["good.png"]


def test_gio_criterion6_missing_folder_is_picked_up_when_it_is_created_later(
    tmp_path, real_source, caplog
):
    folder = tmp_path / "Pictures" / "slideshow-lock"  # neither level exists yet
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(real_source(folder))
    assert src.current() is None
    assert any("does not exist" in r.message for r in caplog.records)

    make_image(folder / "late.png")
    wait_for(lambda: len(src) == 1, "images of the newly created folder to appear")
    assert names(src, folder) == ["late.png"]


def test_gio_criterion2_first_image_arrives_long_before_the_full_walk_on_a_large_tree(
    tmp_path, real_source, capsys
):
    total = 0
    for i in range(20):
        for j in range(20):
            for k in range(20):
                make_image(
                    tmp_path / f"d{i:02d}" / f"s{j:02d}" / f"img{k:03d}.jpg", b"\xff\xd8\xff\xe0"
                )
                total += 1
    src = real_source(tmp_path)
    found_at = []
    src.connect_current_changed(lambda _path: found_at.append(time.monotonic()))
    t0 = time.monotonic()
    src.start()
    wait_for(lambda: src.scan_complete and len(src) == total, "full walk", timeout_s=30)
    t_full = time.monotonic() - t0
    t_first = found_at[0] - t0
    with capsys.disabled():
        print(
            f"\n[CORE-4 measurement, real GLib main loop] {total} files in 420 folders: "
            f"first image after {t_first * 1000:.2f} ms, full walk {t_full * 1000:.1f} ms"
        )
    assert t_first < t_full / 4


def test_gio_settings_picture_folder_and_order_changes_apply_without_restart(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    make_image(one / "a.png")
    make_image(two / "b.png")
    make_image(two / "c.png")
    settings = Settings()
    assert settings.set_picture_folder(str(one))
    assert settings.set_order("name")
    src = source_from_settings(settings)
    try:
        started(src)
        assert names(src, one) == ["a.png"]

        assert settings.set_picture_folder(str(two))
        wait_for(lambda: names(src, two) == ["b.png", "c.png"], "source to follow the folder key")

        assert settings.set_order("random")
        wait_for(lambda: src.order == "random", "source to follow the order key")
        assert sorted(names(src, two)) == ["b.png", "c.png"]
    finally:
        src.stop()
