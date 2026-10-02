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
import subprocess
import time

import pytest
from gi.repository import GLib

from slideshow_lock.image_source import ImageSource, source_from_settings
from slideshow_lock.settings import Settings
from tests.timeout_guard import per_test_deadline  # noqa: F401  (autouse fixture)

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


def _counting_scheduler(stats):
    """The real GLib idle scheduler, wrapped to count and time every step."""
    from slideshow_lock.image_source import glib_idle_scheduler

    def scheduler(step):
        def counted():
            t0 = time.monotonic()
            more = step()
            stats["steps"] += 1
            stats["longest"] = max(stats["longest"], time.monotonic() - t0)
            return more

        return glib_idle_scheduler(counted)

    return scheduler


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
    stats = {"steps": 0, "longest": 0.0}
    src = real_source(tmp_path, scheduler=_counting_scheduler(stats), step_budget_seconds=0.005)
    found_at = []
    src.connect_current_changed(lambda _path: found_at.append(time.monotonic()))
    t0 = time.monotonic()
    src.start()
    wait_for(lambda: len(src) > 0, "the first image", timeout_s=30)
    steps_at_first = stats["steps"]
    complete_at_first = src.scan_complete
    wait_for(lambda: src.scan_complete and len(src) == total, "full walk", timeout_s=30)
    t_full = time.monotonic() - t0
    t_first = found_at[0] - t0
    with capsys.disabled():
        print(
            f"\n[CORE-4 measurement, real GLib main loop] {total} files in 420 folders, step "
            f"budget 2 ms: first image after {t_first * 1000:.2f} ms (step {steps_at_first}), "
            f"full walk {t_full * 1000:.1f} ms in {stats['steps']} steps, "
            f"longest single step {stats['longest'] * 1000:.1f} ms"
        )
    assert stats["steps"] > 1  # the walk really ran as separate main-loop steps
    assert stats["steps"] >= 8
    assert steps_at_first <= stats["steps"] / 3
    assert not complete_at_first  # the first image was usable while the walk was still going
    assert t_first < t_full / 4


def test_gio_criterion3_renamed_subfolder_keeps_its_images(tmp_path, real_source):
    make_image(tmp_path / "keep.png")
    make_image(tmp_path / "old" / "x.png")
    make_image(tmp_path / "old" / "deep" / "y.png")
    src = started(real_source(tmp_path))
    assert len(src) == 3

    os.rename(tmp_path / "old", tmp_path / "new")  # DELETED(old) + CREATED(new) from the monitor
    wait_for(
        lambda: names(src, tmp_path) == ["keep.png", "new/deep/y.png", "new/x.png"],
        "images to follow the renamed folder",
    )

    make_image(tmp_path / "new" / "z.png")  # and the renamed folder is monitored under its new name
    wait_for(lambda: len(src) == 4, "an image added under the new name")
    os.remove(tmp_path / "new" / "x.png")
    wait_for(lambda: len(src) == 3, "an image removed under the new name")


def test_gio_criterion4_deleting_the_displayed_image_while_the_walk_is_still_running(
    tmp_path, real_source
):
    total = 0
    for i in range(20):
        for j in range(20):
            for k in range(10):
                make_image(
                    tmp_path / f"d{i:02d}" / f"s{j:02d}" / f"img{k:03d}.jpg", b"\xff\xd8\xff\xe0"
                )
                total += 1
    src = real_source(tmp_path, step_budget_seconds=0.005)
    src.start()
    wait_for(lambda: len(src) >= 2, "two images while the walk runs")
    assert not src.scan_complete, "precondition: the walk must still be running"
    shown = src.current()
    changes = []
    src.connect_current_changed(changes.append)

    os.remove(shown)
    wait_for(lambda: shown not in src.images(), "the displayed image to be dropped")
    assert src.current() is not None and src.current() != shown
    assert changes and changes[0] != shown

    wait_for(lambda: src.scan_complete, "the walk to finish", timeout_s=30)
    assert shown not in src.images()  # the deleted file did not sneak back in
    assert len(src) == total - 1


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


# -- inotify watch exhaustion on the real kernel and the real Gio ------------------------------

WATCH_LIMIT_FILE = "/proc/sys/fs/inotify/max_user_watches"


def _set_watch_limit(value: int):
    result = subprocess.run(
        ["sudo", "-n", "sysctl", "-w", f"fs.inotify.max_user_watches={value}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def test_gio_watch_exhaustion_is_reported_once_and_the_walk_still_completes(
    tmp_path, real_source, caplog, capsys
):
    """What really happens when the kernel refuses inotify watches (ENOSPC).

    Lowers ``fs.inotify.max_user_watches`` with sudo, so it only runs on CI (a throwaway
    runner). On a developer machine see the manual trial in docs/image-source.md.
    """
    if not os.environ.get("CI"):
        pytest.skip(
            "lowers a kernel limit with sudo: CI only, manual trial is in docs/image-source.md"
        )
    folders = 60
    for n in range(folders):
        make_image(tmp_path / f"d{n:02d}" / "a.png")
    with open(WATCH_LIMIT_FILE) as fh:
        original = int(fh.read())
    ok, output = _set_watch_limit(25)
    assert ok, f"CI is expected to allow 'sudo -n sysctl': {output}"
    try:
        with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
            src = real_source(tmp_path)
            src.start()
            wait_for(lambda: src.scan_complete, "the walk to finish under exhausted watches")
        walked = len(src)
        watched = len(src._watches)
        unwatched_dir = next(
            str(tmp_path / f"d{n:02d}")
            for n in reversed(range(folders))
            if str(tmp_path / f"d{n:02d}") not in src._watches
        )
        make_image(os.path.join(unwatched_dir, "late.png"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(src) == walked:
            wait_for(lambda: True, "iteration")
            time.sleep(0.05)
        late_seen = len(src) > walked
        limit_lines = [r.message for r in caplog.records if "folder limits reached" in r.message]
        other = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING and "folder limits reached" not in r.message
        ]
    finally:
        _set_watch_limit(original)
    with capsys.disabled():
        print(
            f"\n[CORE-4 watch exhaustion, real kernel + Gio] limit 25, {folders} folders: "
            f"walked {walked} images, {watched} folders watched; "
            f"event from an unwatched folder delivered: {late_seen}; "
            f"summary lines: {limit_lines}; other warnings: {other}"
        )
    assert walked == folders  # exhausted watches never cost an image
    assert watched < folders  # the limit really bit
    assert len(limit_lines) == 1
