"""Tests for the CORE-4 image source: walk, symlink-loop guard, live changes.

These tests drive the real filesystem (under ``tmp_path`` only) but use a fake
monitor backend and a manual scheduler, so every step is deterministic and no
GLib main loop is needed. The real ``Gio.FileMonitor`` and GLib scheduler are
covered by ``tests/test_image_source_gio.py``.

Each test name carries the CORE-4 acceptance criterion it proves.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import time

import pytest

from slideshow_lock.image_source import (
    FsEvent,
    ImageSource,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def make_image(path, data: bytes = PNG) -> str:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class FakeWatcher:
    """Stands in for Gio.FileMonitor: tests emit the event a monitor would deliver."""

    def __init__(self):
        self.callbacks = {}

    def __call__(self, path, callback):
        self.callbacks[path] = callback
        return lambda: self.callbacks.pop(path, None)

    def emit(self, path, kind):
        """Deliver *kind* for *path* the way its parent directory's monitor would."""
        callback = self.callbacks.get(os.path.dirname(path))
        if callback is not None:
            callback(path, kind)

    def emit_self(self, path, kind):
        """Deliver an event about a watched directory itself, from its own monitor."""
        self.callbacks[path](path, kind)


class ManualScheduler:
    """Stands in for the GLib idle source: the test decides when a step runs."""

    def __init__(self):
        self.step = None
        self.steps_run = 0

    def __call__(self, step):
        self.step = step
        return self._cancel

    def _cancel(self):
        self.step = None

    def run_one(self) -> bool:
        step = self.step
        if step is None:
            return False
        more = step()
        self.steps_run += 1
        if not more and self.step is step:
            self.step = None
        return more

    def run_all(self, max_steps: int = 200_000) -> bool:
        """Run to the end of the walk. False if *max_steps* was hit first (no termination)."""
        for _ in range(max_steps):
            if not self.run_one():
                return True
        return False


@pytest.fixture
def backends():
    return FakeWatcher(), ManualScheduler()


def make_source(folder, backends, **kwargs) -> ImageSource:
    watcher, scheduler = backends
    kwargs.setdefault("order", "name")
    kwargs.setdefault("rng", random.Random(7))
    return ImageSource(folder, watcher=watcher, scheduler=scheduler, **kwargs)


def started(folder, backends, **kwargs) -> ImageSource:
    """A source whose initial walk has completed."""
    src = make_source(folder, backends, **kwargs)
    src.start()
    assert backends[1].run_all()
    return src


def names(src: ImageSource, root) -> list:
    return [os.path.relpath(p, str(root)) for p in src.images()]


# -- criterion 1: recursive walk, symlink-loop guard -------------------------------


def test_criterion1_walks_subfolders_recursively(tmp_path, backends):
    make_image(tmp_path / "a.png")
    make_image(tmp_path / "one" / "b.jpg")
    make_image(tmp_path / "one" / "two" / "three" / "c.PNG")
    src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["a.png", "one/b.jpg", "one/two/three/c.PNG"]
    assert src.scan_complete


def _build_symlink_loop(root):
    make_image(root / "a.png")
    make_image(root / "sub" / "b.png")
    make_image(root / "sub" / "deeper" / "c.png")
    os.symlink(str(root), str(root / "sub" / "loop"))  # sub/loop -> the root, a cycle
    os.symlink(str(root / "sub"), str(root / "sub" / "deeper" / "up"))  # deeper/up -> sub


def test_criterion1_symlink_loop_terminates_and_lists_every_image_exactly_once(tmp_path, backends):
    _build_symlink_loop(tmp_path)
    src = make_source(tmp_path, backends)
    src.start()
    assert backends[1].run_all(max_steps=10_000), "the walk did not terminate on a symlink loop"
    images = src.images()
    assert len(images) == 3
    assert len({os.path.realpath(p) for p in images}) == 3  # each real file exactly once
    assert sorted(os.path.basename(p) for p in images) == ["a.png", "b.png", "c.png"]


def test_criterion1_negative_control_without_the_guard_the_loop_test_fails(
    tmp_path, backends, monkeypatch
):
    """Control for the test above: with the (st_dev, st_ino) guard switched off, the
    same tree makes the walk revisit the loop, so the "exactly once" assertion of
    the real test could not hold. Proves the loop test can actually fail."""

    def claim_without_guard(self, path):
        key = self._stat_key(path)
        if key is None:
            return False
        self._dir_keys[path] = key  # but no "already visited" check
        return True

    monkeypatch.setattr(ImageSource, "_claim_dir", claim_without_guard)
    _build_symlink_loop(tmp_path)
    src = make_source(tmp_path, backends, step_budget_seconds=0)  # one unit of work per step
    src.start()
    terminated = backends[1].run_all(max_steps=5_000)
    images = src.images()
    real_files = {os.path.realpath(p) for p in images}
    assert len(real_files) == 3  # still only three real files...
    assert len(images) > 3 or not terminated  # ...but listed repeatedly, or never ends


def test_criterion1_directory_reachable_through_two_links_is_walked_once(tmp_path, backends):
    outside = tmp_path / "outside"
    make_image(outside / "x.png")
    root = tmp_path / "root"
    make_image(root / "a.png")
    os.symlink(str(outside), str(root / "link1"))
    os.symlink(str(outside), str(root / "link2"))
    src = started(root, backends)
    assert len(src) == 2
    assert sum(1 for p in src.images() if os.path.basename(p) == "x.png") == 1


# -- criterion 2: large tree does not block the first image -------------------------


def _build_large_tree(root, top=20, mid=20, files=20):
    count = 0
    for i in range(top):
        for j in range(mid):
            for k in range(files):
                make_image(
                    root / f"d{i:02d}" / f"s{j:02d}" / f"img{k:03d}.jpg", b"\xff\xd8\xff\xe0"
                )
                count += 1
    return count


def test_criterion2_first_image_is_available_long_before_the_full_walk_completes(
    tmp_path, backends, capsys
):
    total = _build_large_tree(tmp_path)  # 8000 files, 420 folders, 3 levels
    scheduler = backends[1]
    src = make_source(tmp_path, backends)
    found_at = []
    src.connect_current_changed(lambda _path: found_at.append(time.monotonic()))
    src.start()

    steps_to_first = 0
    longest_step = 0.0
    t_walk_start = time.monotonic()
    while True:
        t0 = time.monotonic()
        more = scheduler.run_one()
        longest_step = max(longest_step, time.monotonic() - t0)
        if len(src) and not steps_to_first:
            steps_to_first = scheduler.steps_run
            at_first = (len(src), src.scan_complete)
        if not more:
            break
    t_full = time.monotonic() - t_walk_start
    t_first = found_at[0] - t_walk_start

    with capsys.disabled():
        print(
            f"\n[CORE-4 measurement] {total} files in 420 folders: first image found after "
            f"{t_first * 1000:.2f} ms (main loop regains control after step {steps_to_first}), "
            f"full walk {t_full * 1000:.1f} ms in {scheduler.steps_run} steps, "
            f"longest single step {longest_step * 1000:.1f} ms"
        )

    assert len(src) == total
    assert steps_to_first == 1
    assert at_first[0] > 0 and not at_first[1]  # first image while the walk is still going
    assert t_first < t_full / 4
    assert longest_step < 0.5  # no step monopolises the main loop


# -- criterion 3: live monitoring in both directions --------------------------------


def test_criterion3_copied_in_image_appears_and_deleted_image_disappears(tmp_path, backends):
    make_image(tmp_path / "a.png")
    src = started(tmp_path, backends)
    watcher = backends[0]
    assert names(src, tmp_path) == ["a.png"]

    added = make_image(tmp_path / "b.png")
    watcher.emit(added, FsEvent.CREATED)
    assert names(src, tmp_path) == ["a.png", "b.png"]

    os.remove(tmp_path / "a.png")
    watcher.emit(str(tmp_path / "a.png"), FsEvent.DELETED)
    assert names(src, tmp_path) == ["b.png"]


def test_criterion3_file_still_being_written_is_added_once_the_writer_is_done(
    tmp_path, backends, caplog
):
    src = started(tmp_path, backends)
    watcher = backends[0]
    path = tmp_path / "slow.png"
    path.write_bytes(b"")  # created, nothing written yet
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        watcher.emit(str(path), FsEvent.CREATED)
    assert len(src) == 0
    assert not [r for r in caplog.records if "skipping" in r.message]  # not an error yet
    path.write_bytes(PNG)
    watcher.emit(str(path), FsEvent.CHANGES_DONE)
    assert names(src, tmp_path) == ["slow.png"]


def test_criterion3_file_that_is_still_corrupt_when_the_writer_is_done_is_logged(
    tmp_path, backends, caplog
):
    src = started(tmp_path, backends)
    path = tmp_path / "junk.png"
    path.write_bytes(b"not an image")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        backends[0].emit(str(path), FsEvent.CHANGES_DONE)
    assert len(src) == 0
    assert any("junk.png" in r.message and "skipping" in r.message for r in caplog.records)


def test_criterion3_new_subfolder_with_images_appears_and_deleted_subfolder_disappears(
    tmp_path, backends
):
    make_image(tmp_path / "a.png")
    src = started(tmp_path, backends)
    watcher, scheduler = backends

    os.makedirs(tmp_path / "new")
    watcher.emit(str(tmp_path / "new"), FsEvent.CREATED)
    scheduler.run_all()
    assert str(tmp_path / "new") in watcher.callbacks  # the new folder is monitored too

    made = make_image(tmp_path / "new" / "n1.png")
    watcher.emit(made, FsEvent.CREATED)  # created after the folder was already watched
    assert names(src, tmp_path) == ["a.png", "new/n1.png"]

    shutil.rmtree(tmp_path / "new")
    watcher.emit(str(tmp_path / "new"), FsEvent.DELETED)
    assert names(src, tmp_path) == ["a.png"]
    assert str(tmp_path / "new") not in watcher.callbacks  # its monitor was released


def test_criterion3_subfolder_moved_in_with_images_already_inside_appears(tmp_path, backends):
    root = tmp_path / "root"
    make_image(root / "a.png")
    make_image(tmp_path / "staging" / "deep" / "m1.png")
    make_image(tmp_path / "staging" / "m2.png")
    src = started(root, backends)
    watcher, scheduler = backends

    os.rename(tmp_path / "staging", root / "moved")  # one event for the folder, files already in
    watcher.emit(str(root / "moved"), FsEvent.CREATED)
    scheduler.run_all()
    assert names(src, root) == ["a.png", "moved/deep/m1.png", "moved/m2.png"]


def test_criterion3_new_images_join_a_random_queue_too(tmp_path, backends):
    for n in range(4):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="random")
    added = make_image(tmp_path / "late.png")
    backends[0].emit(added, FsEvent.CREATED)
    assert len(src) == 5
    seen = {src.current()} | {src.advance() for _ in range(4)}
    assert str(tmp_path / "late.png") in seen  # shown within one full cycle


# -- criterion 4: deleting the image on screen --------------------------------------


@pytest.mark.parametrize("order", ["name", "random"])
def test_criterion4_deleting_displayed_image_advances_to_next_without_stalling_or_raising(
    tmp_path, backends, order
):
    for n in "abcd":
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order=order)
    watcher = backends[0]
    src.advance()  # something other than the very first image is on screen
    before = src.images()
    shown = src.current()
    expected_next = before[(before.index(shown) + 1) % len(before)]
    changes = []
    src.connect_current_changed(changes.append)

    os.remove(shown)
    watcher.emit(shown, FsEvent.DELETED)  # must not raise

    assert src.current() == expected_next  # moved on to the image that followed
    assert changes == [expected_next]  # the display layer was told
    assert shown not in src.images()
    seen = {src.current()} | {src.advance() for _ in range(len(src))}
    assert seen == set(before) - {shown}  # and the slideshow keeps cycling the rest


def test_criterion4_deleting_the_displayed_image_at_the_end_of_the_queue_wraps_to_the_start(
    tmp_path, backends
):
    for n in "abc":
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends)
    src.advance()
    src.advance()
    last = src.current()
    assert last.endswith("c.png")
    os.remove(last)
    backends[0].emit(last, FsEvent.DELETED)
    assert src.current().endswith("a.png")


def test_criterion4_deleting_an_image_other_than_the_displayed_one_leaves_the_display_alone(
    tmp_path, backends
):
    for n in "abc":
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends)
    src.advance()  # b
    changes = []
    src.connect_current_changed(changes.append)
    gone = str(tmp_path / "a.png")  # before the cursor
    os.remove(gone)
    backends[0].emit(gone, FsEvent.DELETED)
    assert src.current().endswith("b.png")
    assert changes == []


def test_criterion4_deleting_the_last_remaining_image_gives_a_defined_empty_state_and_logs(
    tmp_path, backends, caplog
):
    only = make_image(tmp_path / "only.png")
    src = started(tmp_path, backends)
    changes = []
    src.connect_current_changed(changes.append)

    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        os.remove(only)
        backends[0].emit(only, FsEvent.DELETED)

    assert src.current() is None
    assert src.advance() is None  # no exception on an empty queue
    assert len(src) == 0
    assert changes == [None]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "[slideshow-dir]" in warnings[0].message

    revived = make_image(tmp_path / "again.png")  # and it recovers when an image returns
    backends[0].emit(revived, FsEvent.CREATED)
    assert src.current() == revived
    assert changes == [None, revived]


def test_criterion4_deleting_a_subfolder_that_holds_the_displayed_image_moves_on(
    tmp_path, backends
):
    make_image(tmp_path / "a.png")
    make_image(tmp_path / "sub" / "s1.png")
    make_image(tmp_path / "sub" / "s2.png")
    make_image(tmp_path / "z.png")
    src = started(tmp_path, backends)
    src.advance()
    assert src.current().endswith("sub/s1.png")
    shutil.rmtree(tmp_path / "sub")
    backends[0].emit(str(tmp_path / "sub"), FsEvent.DELETED)
    assert src.current().endswith("z.png")
    assert names(src, tmp_path) == ["a.png", "z.png"]


# -- criterion 5: unreadable / corrupt / non-image files ------------------------------


def test_criterion5_corrupt_image_is_skipped_with_a_log_entry_not_a_crash(
    tmp_path, backends, caplog
):
    make_image(tmp_path / "good.png")
    make_image(tmp_path / "broken.jpg", b"this is not a jpeg at all")
    make_image(tmp_path / "empty.png", b"")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["good.png"]
    skipped = [r.message for r in caplog.records if "skipping" in r.message]
    assert len(skipped) == 2
    assert all("[slideshow-dir]" in m for m in skipped)
    assert any("broken.jpg" in m for m in skipped) and any("empty.png" in m for m in skipped)


def test_criterion5_unreadable_image_is_skipped_with_a_log_entry_not_a_crash(
    tmp_path, backends, caplog
):
    make_image(tmp_path / "good.png")
    locked = make_image(tmp_path / "locked.png")
    os.chmod(locked, 0)
    if os.access(locked, os.R_OK):  # root ignores file modes; fall back to a failing probe
        os.chmod(locked, 0o644)
        pytest.skip("running as a user that can read mode 000 files (root)")
    try:
        with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
            src = started(tmp_path, backends)
    finally:
        os.chmod(locked, 0o644)
    assert names(src, tmp_path) == ["good.png"]
    assert any("locked.png" in r.message and "skipping" in r.message for r in caplog.records)


def test_criterion5_a_failing_probe_never_crashes_the_source(tmp_path, backends, caplog):
    make_image(tmp_path / "good.png")
    make_image(tmp_path / "bad.png")

    def probe(path):
        if path.endswith("bad.png"):
            raise PermissionError(13, "Permission denied")
        if path.endswith("worse.png"):
            raise RuntimeError("probe bug")

    make_image(tmp_path / "worse.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, probe=probe)
    assert names(src, tmp_path) == ["good.png"]
    assert len([r for r in caplog.records if "skipping" in r.message]) == 2


def test_criterion5_non_image_files_are_filtered_out_silently(tmp_path, backends, caplog):
    make_image(tmp_path / "pic.png")
    make_image(tmp_path / "notes.txt", b"hello")
    make_image(tmp_path / "movie.mp4", b"\x00\x00\x00\x18ftypmp42")
    make_image(tmp_path / "noextension", PNG)
    make_image(tmp_path / ".hidden.png")
    make_image(tmp_path / ".cache" / "thumb.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["pic.png"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_criterion5_a_fifo_with_an_image_name_does_not_hang_the_walk(tmp_path, backends):
    make_image(tmp_path / "pic.png")
    os.mkfifo(tmp_path / "pipe.png")
    src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["pic.png"]


# -- criterion 6: the folder may not exist ------------------------------------------------


def test_criterion6_missing_folder_is_a_logged_empty_state_not_an_error(tmp_path, backends, caplog):
    missing = tmp_path / "Pictures" / "slideshow-lock"  # neither level exists
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = make_source(missing, backends)
        src.start()
        backends[1].run_all()
    assert src.current() is None
    assert src.advance() is None
    assert len(src) == 0
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        len(warnings) == 1 and "[slideshow-dir]" in warnings[0] and "does not exist" in warnings[0]
    )


def test_criterion6_missing_folder_is_picked_up_when_it_is_created_later(tmp_path, backends):
    folder = tmp_path / "Pictures" / "slideshow-lock"
    src = make_source(folder, backends)
    src.start()
    watcher, scheduler = backends
    assert str(tmp_path) in watcher.callbacks  # watching the nearest existing ancestor

    os.makedirs(tmp_path / "Pictures")
    watcher.emit(str(tmp_path / "Pictures"), FsEvent.CREATED)
    assert str(tmp_path / "Pictures") in watcher.callbacks  # moved down to the new ancestor
    assert str(tmp_path) not in watcher.callbacks

    make_image(folder / "late.png")
    watcher.emit(str(folder), FsEvent.CREATED)
    scheduler.run_all()
    assert names(src, folder) == ["late.png"]


def test_criterion6_ancestor_events_about_other_names_are_ignored(tmp_path, backends):
    folder = tmp_path / "pics"
    make_image(folder / "a.png")
    src = started(folder, backends)
    other = make_image(tmp_path / "unrelated.png")
    backends[0].emit(other, FsEvent.CREATED)
    assert names(src, folder) == ["a.png"]


def test_criterion6_folder_deleted_while_running_is_the_empty_state_and_it_recovers(
    tmp_path, backends, caplog
):
    folder = tmp_path / "pics"
    make_image(folder / "a.png")
    src = started(folder, backends)
    watcher, scheduler = backends
    changes = []
    src.connect_current_changed(changes.append)

    shutil.rmtree(folder)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        watcher.emit(str(folder), FsEvent.DELETED)  # the parent's monitor reports it
    assert src.current() is None and changes == [None]
    assert any("does not exist" in r.message for r in caplog.records)

    revived = make_image(folder / "b.png")
    watcher.emit(str(folder), FsEvent.CREATED)
    scheduler.run_all()
    assert src.current() == revived


# -- ordering and runtime changes -----------------------------------------------------------


def test_name_order_is_sorted_case_insensitively(tmp_path, backends):
    for n in ["b.png", "A.png", "c.png", "B2.png"]:
        make_image(tmp_path / n)
    src = started(tmp_path, backends, order="name")
    assert names(src, tmp_path) == ["A.png", "b.png", "B2.png", "c.png"]


def test_random_order_shows_every_image_once_per_cycle(tmp_path, backends):
    for n in range(6):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="random")
    for _ in range(3):
        cycle = [src.current()] + [src.advance() for _ in range(5)]
        assert len(set(cycle)) == 6
        src.advance()  # step into the next cycle


def test_random_order_does_not_repeat_an_image_across_the_cycle_boundary(tmp_path, backends):
    for n in range(3):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="random")
    previous = src.current()
    for _ in range(60):
        nxt = src.advance()
        assert nxt != previous
        previous = nxt


def test_name_order_inserting_before_the_displayed_image_keeps_the_display_stable(
    tmp_path, backends
):
    make_image(tmp_path / "a.png")
    make_image(tmp_path / "c.png")
    src = started(tmp_path, backends, order="name")
    src.advance()  # c is on screen
    changes = []
    src.connect_current_changed(changes.append)
    added = make_image(tmp_path / "b.png")  # sorts right in front of c
    backends[0].emit(added, FsEvent.CREATED)
    assert src.current().endswith("c.png") and changes == []
    assert src.advance().endswith("a.png")  # wrapped: c was still the last one


def test_set_order_at_runtime_keeps_the_image_on_screen(tmp_path, backends):
    for n in "abcd":
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="name")
    src.advance()
    shown = src.current()
    src.set_order("random")
    assert src.current() == shown and sorted(src.images()) == sorted(
        str(tmp_path / f"{n}.png") for n in "abcd"
    )
    src.set_order("name")
    assert src.current() == shown
    assert names(src, tmp_path) == ["a.png", "b.png", "c.png", "d.png"]


def test_set_order_rejects_unknown_values(tmp_path, backends):
    src = make_source(tmp_path, backends)
    with pytest.raises(ValueError):
        src.set_order("diagonal")


def test_set_folder_at_runtime_switches_the_source(tmp_path, backends):
    make_image(tmp_path / "one" / "a.png")
    make_image(tmp_path / "two" / "b.png")
    src = started(tmp_path / "one", backends)
    watcher, scheduler = backends
    changes = []
    src.connect_current_changed(changes.append)
    src.set_folder(str(tmp_path / "two"))
    scheduler.run_all()
    assert names(src, tmp_path / "two") == ["b.png"]
    assert str(tmp_path / "one") not in watcher.callbacks  # old monitors are gone
    assert changes == [None, str(tmp_path / "two" / "b.png")]


def test_stop_releases_every_monitor_and_ignores_late_events(tmp_path, backends):
    make_image(tmp_path / "a.png")
    make_image(tmp_path / "sub" / "b.png")
    src = started(tmp_path, backends)
    watcher = backends[0]
    late = dict(watcher.callbacks)
    src.stop()
    assert watcher.callbacks == {}
    for path, callback in late.items():  # events already in flight must be harmless
        callback(os.path.join(path, "a.png"), FsEvent.DELETED)
    assert len(src) == 0
