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
from types import SimpleNamespace

import pytest

from slideshow_lock.image_source import (
    LOG_FIRST_N,
    FsEvent,
    ImageSource,
    probe_image,
)
from tests.timeout_guard import (
    HardTimeout,
    hard_timeout,
    per_test_deadline,  # noqa: F401  (autouse fixture)
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

STEP_BUDGET = 0.005  # small enough that a blocking walk (80+ ms) is far outside 10x the budget


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


@pytest.fixture(scope="module")
def large_tree_walk(tmp_path_factory):
    """Walk a large synthetic tree step by step and record how the walk behaved."""
    root = tmp_path_factory.mktemp("large")
    total = _build_large_tree(root)  # 8000 files, 420 folders, 3 levels
    scheduler = ManualScheduler()
    src = ImageSource(
        str(root),
        order="name",
        watcher=FakeWatcher(),
        scheduler=scheduler,
        step_budget_seconds=STEP_BUDGET,
    )
    found_at = []
    src.connect_current_changed(lambda _path: found_at.append(time.monotonic()))
    src.start()

    walk = SimpleNamespace(total=total, budget=STEP_BUDGET, steps_to_first=0, longest_step=0.0)
    t_start = time.monotonic()
    while True:
        t0 = time.monotonic()
        more = scheduler.run_one()
        now = time.monotonic()
        walk.longest_step = max(walk.longest_step, now - t0)
        if len(src) and not walk.steps_to_first:
            walk.steps_to_first = scheduler.steps_run
            walk.queue_at_first = len(src)
            walk.complete_at_first = src.scan_complete
            walk.first_returned = now - t_start  # main loop has control again, image in queue
            walk.first_found = found_at[0] - t_start
        if not more:
            break
    walk.full = time.monotonic() - t_start
    walk.steps = scheduler.steps_run
    walk.images = len(src)
    return walk


def test_criterion2_measurement_report(large_tree_walk, capsys):
    w = large_tree_walk
    with capsys.disabled():
        print(
            f"\n[CORE-4 measurement] {w.total} files in 420 folders, step budget "
            f"{w.budget * 1000:.0f} ms: first image found after {w.first_found * 1000:.2f} ms, "
            f"main loop regains control with it after {w.first_returned * 1000:.2f} ms "
            f"(step {w.steps_to_first}), full walk {w.full * 1000:.1f} ms in {w.steps} steps, "
            f"longest single step {w.longest_step * 1000:.1f} ms"
        )
    assert w.images == w.total


def test_criterion2_first_image_is_in_the_queue_within_the_first_few_steps(large_tree_walk):
    w = large_tree_walk
    assert w.queue_at_first > 0
    assert w.steps_to_first <= w.steps / 3  # images only sit three folders deep in this tree


def test_criterion2_the_walk_is_still_running_when_the_first_image_is_available(large_tree_walk):
    assert not large_tree_walk.complete_at_first


def test_criterion2_the_walk_is_split_into_many_steps(large_tree_walk):
    assert large_tree_walk.steps >= 8


def test_criterion2_no_single_step_runs_much_longer_than_the_step_budget(large_tree_walk):
    assert large_tree_walk.longest_step < 10 * large_tree_walk.budget


def test_criterion2_main_loop_has_the_first_image_long_before_the_full_walk_ends(large_tree_walk):
    w = large_tree_walk
    assert w.first_returned < 10 * w.budget
    assert w.first_returned < w.full / 4


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
    with hard_timeout(10):  # a hang must fail this test, not stall the run
        src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["pic.png"]


def test_criterion5_probe_does_not_block_on_a_fifo_swapped_in_after_the_check(tmp_path):
    fifo = tmp_path / "swapped.png"
    os.mkfifo(fifo)
    with hard_timeout(10):
        with pytest.raises(ValueError, match="not a regular file"):
            probe_image(str(fifo))


def test_criterion5_a_file_swapped_for_a_fifo_after_the_listing_does_not_hang_the_walk(
    tmp_path, backends, caplog
):
    make_image(tmp_path / "pic.png")
    victim = make_image(tmp_path / "victim.png")
    src = make_source(tmp_path, backends, step_budget_seconds=0)
    src.start()
    backends[1].run_one()  # the folder is listed; the entries were classified as regular files
    os.remove(victim)
    os.mkfifo(victim)  # now the race: a FIFO under the name that was a regular file
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        with hard_timeout(10):
            assert backends[1].run_all()
    assert names(src, tmp_path) == ["pic.png"]
    assert any("victim.png" in r.message and "skipping" in r.message for r in caplog.records)


def test_hard_timeout_guard_turns_a_hang_into_a_failure(tmp_path):
    """Control for the FIFO tests: a plain blocking open() really does hang, and the guard
    really does interrupt it."""
    fifo = tmp_path / "blocks.png"
    os.mkfifo(fifo)
    with pytest.raises(HardTimeout):
        with hard_timeout(0.3):
            open(fifo, "rb").close()  # no writer: blocks forever without the guard


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


# -- resource limits (follow-up: caps on folders and watches) -----------------------------


def _warnings(caplog):
    return [r.message for r in caplog.records if r.levelno >= logging.WARNING]


def test_directory_cap_stops_the_walk_and_logs_exactly_one_summary_warning(
    tmp_path, backends, caplog
):
    for n in range(30):
        make_image(tmp_path / f"d{n:02d}" / "a.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, max_directories=10)
    assert 0 < len(src) < 30
    limit_lines = [m for m in _warnings(caplog) if "folder limits reached" in m]
    assert len(limit_lines) == 1
    assert "not walked (limit 10)" in limit_lines[0] and "[slideshow-dir]" in limit_lines[0]
    assert len(_warnings(caplog)) == 1  # one summary, nothing per folder


def test_watch_cap_leaves_the_extra_folders_unwatched_but_still_walked(tmp_path, backends, caplog):
    for n in range(20):
        make_image(tmp_path / f"d{n:02d}" / "a.png")
    watcher = backends[0]
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, max_watches=5)
    assert len(src) == 20  # every folder was still walked
    watched_dirs = [p for p in watcher.callbacks if p != str(tmp_path.parent)]
    assert len(watched_dirs) == 5
    limit_lines = [m for m in _warnings(caplog) if "folder limits reached" in m]
    assert len(limit_lines) == 1 and "16 folders are not watched (limit 5" in limit_lines[0]
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("failure", ["raises", "returns none"])
def test_os_refusing_watches_is_one_summary_line_with_the_reason(
    tmp_path, backends, caplog, failure
):
    for n in range(12):
        make_image(tmp_path / f"d{n:02d}" / "a.png")
    real = backends[0]

    def refusing(path, callback):
        if path in (str(tmp_path), str(tmp_path.parent)):
            return real(path, callback)  # root and its ancestor keep working
        if failure == "raises":
            raise OSError(28, "No space left on device")
        return None

    scheduler = backends[1]
    src = ImageSource(
        str(tmp_path),
        order="name",
        watcher=refusing,
        scheduler=scheduler,
        rng=random.Random(7),
    )
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src.start()
        assert scheduler.run_all()
    assert len(src) == 12
    unwatched = [m for m in _warnings(caplog) if "not watched" in m]
    assert len(unwatched) == 1 and "12 folders" in unwatched[0]
    if failure == "raises":
        assert "No space left on device" in unwatched[0]
    assert len(_warnings(caplog)) == 1


def test_a_folder_created_past_the_directory_cap_is_counted_not_walked(tmp_path, backends, caplog):
    make_image(tmp_path / "a.png")
    src = started(tmp_path, backends, max_directories=1)  # only the root fits
    os.makedirs(tmp_path / "extra")
    make_image(tmp_path / "extra" / "b.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        backends[0].emit(str(tmp_path / "extra"), FsEvent.CREATED)
        backends[1].run_all()
    assert names(src, tmp_path) == ["a.png"]


# -- log volume (follow-up: one failure is one line, bursts are counted) -------------------


def test_mass_failures_log_the_first_n_lines_then_one_count_line(tmp_path, backends, caplog):
    for n in range(LOG_FIRST_N + 25):
        make_image(tmp_path / f"bad{n:02d}.png", b"junk")
    make_image(tmp_path / "good.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["good.png"]
    skipping = [m for m in _warnings(caplog) if "skipping" in m]
    assert len(skipping) == LOG_FIRST_N
    summary = [m for m in _warnings(caplog) if "more unreadable or corrupt images" in m]
    assert len(summary) == 1 and summary[0].startswith("[slideshow-dir] 25 more")


def test_one_failed_folder_listing_is_one_log_line(tmp_path, backends, caplog, monkeypatch):
    make_image(tmp_path / "ok" / "a.png")
    make_image(tmp_path / "locked" / "b.png")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr("slideshow_lock.image_source.os.scandir", scandir)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["ok/a.png"]
    lines = [m for m in _warnings(caplog) if "cannot read folder" in m]
    assert len(lines) == 1


def test_log_window_reopens_and_flushes_the_count_of_the_previous_window(
    tmp_path, backends, caplog, monkeypatch
):
    from slideshow_lock import image_source

    clock = {"now": 1000.0}
    monkeypatch.setattr(image_source, "_now", lambda: clock["now"])
    src = started(tmp_path, backends)
    watcher = backends[0]

    def corrupt(name):
        path = tmp_path / name
        path.write_bytes(b"junk")
        watcher.emit(str(path), FsEvent.CHANGES_DONE)

    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        for n in range(LOG_FIRST_N + 3):
            corrupt(f"a{n}.png")
        assert len([m for m in _warnings(caplog) if "skipping" in m]) == LOG_FIRST_N
        clock["now"] += image_source.LOG_WINDOW_SECONDS + 1
        corrupt("later.png")
    assert len([m for m in _warnings(caplog) if "skipping" in m]) == LOG_FIRST_N + 1
    assert any(m.startswith("[slideshow-dir] 3 more") for m in _warnings(caplog))
    assert len(src) == 0


# -- root deleted and re-created (follow-up) -------------------------------------------------


def test_root_deleted_event_rewalks_even_when_the_folder_has_the_same_inode(tmp_path, backends):
    folder = tmp_path / "pics"
    make_image(folder / "old.png")
    src = started(folder, backends)
    watcher, scheduler = backends
    key_before = os.stat(folder).st_ino

    os.remove(folder / "old.png")  # same directory object (same inode) with new content
    make_image(folder / "new.png")
    assert os.stat(folder).st_ino == key_before
    watcher.emit(str(folder), FsEvent.DELETED)  # what the parent's monitor reports
    scheduler.run_all()
    assert names(src, folder) == ["new.png"]  # an inode comparison would have kept old.png


def test_root_deleted_and_created_again_on_the_real_filesystem_is_rewalked(tmp_path, backends):
    folder = tmp_path / "pics"
    make_image(folder / "old.png")
    src = started(folder, backends)
    watcher, scheduler = backends

    shutil.rmtree(folder)
    watcher.emit(str(folder), FsEvent.DELETED)
    assert len(src) == 0
    make_image(folder / "new.png")  # may or may not reuse the old inode number
    watcher.emit(str(folder), FsEvent.CREATED)
    scheduler.run_all()
    assert names(src, folder) == ["new.png"]


def test_root_own_monitor_reporting_its_deletion_also_rewalks(tmp_path, backends):
    folder = tmp_path / "pics"
    make_image(folder / "old.png")
    src = started(folder, backends)
    watcher, scheduler = backends
    os.remove(folder / "old.png")
    make_image(folder / "new.png")
    watcher.emit_self(str(folder), FsEvent.DELETED)
    scheduler.run_all()
    assert names(src, folder) == ["new.png"]


# -- renamed subfolder, both event orders (follow-up) ------------------------------------------


@pytest.mark.parametrize("order", ["deleted first", "created first"])
def test_renamed_subfolder_keeps_its_images_whatever_the_event_order(tmp_path, backends, order):
    make_image(tmp_path / "old" / "x.png")
    make_image(tmp_path / "old" / "deep" / "y.png")
    make_image(tmp_path / "keep.png")
    src = started(tmp_path, backends)
    watcher, scheduler = backends

    os.rename(tmp_path / "old", tmp_path / "new")  # same inode under a new name
    old, new = str(tmp_path / "old"), str(tmp_path / "new")
    events = [(old, FsEvent.DELETED), (new, FsEvent.CREATED)]
    if order == "created first":
        events.reverse()
    for path, kind in events:
        watcher.emit(path, kind)
    scheduler.run_all()

    assert names(src, tmp_path) == ["keep.png", "new/deep/y.png", "new/x.png"]
    assert old not in watcher.callbacks and new in watcher.callbacks  # monitors followed the rename


# -- deleting the displayed image while the walk is still running (follow-up) -------------------


def test_criterion4_deleting_the_displayed_image_while_the_walk_is_still_running(
    tmp_path, backends, caplog
):
    for n in range(6):
        make_image(tmp_path / f"{n}.png")
    make_image(tmp_path / "sub" / "s.png")
    src = make_source(tmp_path, backends, step_budget_seconds=0)  # one unit of work per step
    src.start()
    scheduler = backends[1]
    while len(src) < 3:
        assert scheduler.run_one()
    assert not src.scan_complete  # the walk is genuinely still going
    before = src.images()
    shown = src.current()
    expected_next = before[(before.index(shown) + 1) % len(before)]

    os.remove(shown)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        backends[0].emit(shown, FsEvent.DELETED)  # must not raise
        assert src.current() == expected_next
        assert scheduler.run_all()  # and the walk finishes
    assert shown not in src.images()  # the deleted file does not sneak back in
    assert names(src, tmp_path) == ["1.png", "2.png", "3.png", "4.png", "5.png", "sub/s.png"]
    assert not [m for m in _warnings(caplog) if "no displayable images" in m]


def test_criterion4_deleting_the_only_known_image_mid_walk_is_not_yet_an_empty_state_warning(
    tmp_path, backends, caplog
):
    for n in range(4):
        make_image(tmp_path / f"{n}.png")
    src = make_source(tmp_path, backends, step_budget_seconds=0)
    src.start()
    scheduler = backends[1]
    while len(src) < 1:
        scheduler.run_one()
    shown = src.current()
    os.remove(shown)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        backends[0].emit(shown, FsEvent.DELETED)
        assert src.current() is None  # nothing else known yet
        assert not [m for m in _warnings(caplog) if "no displayable images" in m]  # walk not done
        scheduler.run_all()
    assert src.current() is not None and shown not in src.images()
