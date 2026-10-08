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
import signal
import time
from types import SimpleNamespace

import pytest

from slideshow_lock.image_source import (
    IMAGE_EXTENSIONS,
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


def _walk_tree(root, total):
    """Walk the tree at *root* step by step and record how the walk behaved."""
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

    # Wall-clock numbers are only reported. What the assertions use is the CPU time of this
    # thread (``cpu_*``): on a loaded machine a step can be descheduled for tens of milliseconds
    # between two clock reads (measured: the wall-clock assertions failed in most runs with eight
    # test runs on two cores), which says nothing about the walk. A walk that ignores its budget
    # is a step that is busy for the whole walk, and that shows in CPU time all the same.
    # What this guards is the cost budget. CPU time does not see a blocking call or a sleep
    # (a step that waits on a slow file system is not busy), and a wall-clock threshold does not
    # separate that from a loaded machine (measured: 54-489 ms for the longest step with eight
    # runs on two cores). A blocked step is covered by a hand measurement on a real slow mount,
    # not by this test; ``test_criterion2_a_step_does_the_work_of_its_budget_and_not_more`` below
    # checks the budget logic itself on a fake clock.
    walk = SimpleNamespace(
        total=total,
        budget=STEP_BUDGET,
        steps_to_first=0,
        longest_step=0.0,
        step_times=[],
        cpu_step_times=[],
    )
    t_start = time.monotonic()
    cpu_start = time.thread_time()
    while True:
        t0 = time.monotonic()
        cpu0 = time.thread_time()
        more = scheduler.run_one()
        now = time.monotonic()
        cpu_now = time.thread_time()
        walk.longest_step = max(walk.longest_step, now - t0)
        walk.step_times.append(now - t0)
        walk.cpu_step_times.append(cpu_now - cpu0)
        if len(src) and not walk.steps_to_first:
            walk.steps_to_first = scheduler.steps_run
            walk.queue_at_first = len(src)
            walk.complete_at_first = src.scan_complete
            walk.first_returned = now - t_start  # main loop has control again, image in queue
            walk.cpu_first_returned = cpu_now - cpu_start
            walk.first_found = found_at[0] - t_start
        if not more:
            break
    walk.full = time.monotonic() - t_start
    walk.cpu_full = time.thread_time() - cpu_start
    walk.steps = scheduler.steps_run
    walk.images = len(src)
    return walk


@pytest.fixture(scope="module")
def large_tree_walk(tmp_path_factory):
    """Walk a large synthetic tree step by step and record how the walk behaved."""
    root = tmp_path_factory.mktemp("large")
    total = _build_large_tree(root)  # 8000 files, 420 folders, 3 levels
    walk = _walk_tree(root, total)
    walk.root = root
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
    """Noise tolerant: one slow step is allowed, two are not, and the slowest step must stay
    well below the whole walk. A walk that ignores the budget is a single step as long as the
    whole walk, so it fails the second check (and the deterministic step-count tests above).
    Measured in CPU time of the walking thread (see the fixture): a busy machine delays a
    step without making it do more.

    Blind spot, measured by QA: CPU time does not see a wait. A step that sleeps 150 to 500 ms
    (mutants wb10, wb11, wb12) passes this test; a blocking call is not what it guards."""
    w = large_tree_walk
    times = sorted(w.cpu_step_times)
    second_slowest = times[-2] if len(times) >= 2 else times[-1]
    assert second_slowest < 10 * w.budget
    assert times[-1] < w.cpu_full / 2


def test_criterion2_main_loop_has_the_first_image_long_before_the_full_walk_ends(large_tree_walk):
    """CPU time again, so the same blind spot: a sleeping step is not seen (see the test of the
    step length above).

    The best of up to three walks counts. One stall inside a single unit of work is not a walk
    that ignores its budget (CI: first step 33.4 ms and 25.4 ms of CPU in two runs, against
    about 5 ms on a quiet machine; the cause on CI is not established, locally one gen-2 GC
    pause inside a single unit of work reproduces the pattern). A walk that does ignore the
    budget is busy for the whole walk on every attempt, so it still fails all three.

    An intermittent slowdown that spares one walk in three, and an overrun of the budget by a
    factor of about 4 to 10, is not reliably caught here; the budget logic is guarded
    deterministically by test_criterion2_a_step_does_the_work_of_its_budget_and_not_more."""

    def first_is_early(walk):
        return (
            walk.cpu_first_returned < 10 * walk.budget  # CPU time, see the fixture
            and walk.cpu_first_returned < walk.cpu_full / 4
        )

    walks = [large_tree_walk]
    while not first_is_early(walks[-1]) and len(walks) < 3:
        walks.append(_walk_tree(large_tree_walk.root, large_tree_walk.total))
    assert any(first_is_early(w) for w in walks), (
        "(cpu_first_returned, cpu_full) per walk: "
        f"{[(w.cpu_first_returned, w.cpu_full) for w in walks]}; limits: "
        f"cpu_first_returned < 10 * budget = {10 * large_tree_walk.budget} and "
        "cpu_first_returned < cpu_full / 4"
    )


class _TickingTime:
    """``time`` for the image source with a clock that moves one unit per reading and never by
    itself: how much work a step does within its budget no longer depends on the machine."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def test_criterion2_a_step_does_the_work_of_its_budget_and_not_more(
    tmp_path, backends, monkeypatch
):
    """Deterministic: the budget is eight clock units, and the clock moves one unit per reading,
    so a step may do at most eight units of work (one more for the reading that ends it). A walk
    that ignores the budget, one that checks the clock only every so many units, and one that
    widens the budget fail this on any machine, whatever one unit of work costs. Complements the
    CPU-time test above, which measures real time and does not replace this one."""
    for folder in range(10):
        for number in range(12):
            make_image(tmp_path / f"d{folder}" / f"img{number:02d}.png")
    monkeypatch.setattr("slideshow_lock.image_source.time", _TickingTime())
    src = make_source(tmp_path, backends, step_budget_seconds=8.0)
    done = []
    real_work = src._work

    def counting_work():
        did = real_work()
        if did:
            done.append(1)
        return did

    monkeypatch.setattr(src, "_work", counting_work)
    src.start()
    per_step = []
    while True:
        before = len(done)
        more = backends[1].run_one()
        per_step.append(len(done) - before)
        if not more:
            break
    assert len(src) == 120
    assert len(per_step) >= 15  # the walk really is cut into steps
    assert max(per_step) <= 8 + 1
    assert max(per_step) >= 8  # and a step does use its budget: no step of one unit each


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


def test_hard_timeout_is_not_an_exception_subclass():
    """The guard must not be catchable by ``except Exception``: the source has such
    handlers and would swallow the alarm, turning a hang into a "skipped file"."""
    assert not issubclass(HardTimeout, Exception)


def test_hard_timeout_gets_through_a_broad_except_exception(tmp_path):
    """Control for the BaseException choice: a swallowing handler inside the guarded
    block must not stop the alarm. If HardTimeout were an ``Exception`` the handler eats
    it, the block carries on, and this test goes red."""
    fifo = tmp_path / "swallowed.png"
    os.mkfifo(fifo)
    with pytest.raises(HardTimeout):
        with hard_timeout(0.3):
            try:
                open(fifo, "rb").close()  # hangs until the alarm
            except Exception:
                pass  # what the source's probe handler does
            pytest.fail("the alarm was swallowed by 'except Exception'")


def test_nested_hard_timeout_keeps_the_outer_timer_running():
    with hard_timeout(30):
        with hard_timeout(1):
            pass
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 25 < remaining <= 30  # the outer limit survived, minus the time spent


def test_nested_hard_timeout_does_not_outlive_a_shorter_outer_one():
    started_at = time.monotonic()
    with pytest.raises(HardTimeout):
        with hard_timeout(0.3):
            with hard_timeout(30):
                time.sleep(5)
    assert time.monotonic() - started_at < 3


def test_hard_timeout_that_fires_leaves_the_outer_timer_running():
    with hard_timeout(30):
        with pytest.raises(HardTimeout):
            with hard_timeout(0.2):
                time.sleep(5)
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 25 < remaining <= 30


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


def _showings(src, count):
    """The pictures in the order the screen shows them: the current one, then *count* advances."""
    return [src.current()] + [src.advance() for _ in range(count)]


def _gaps(showings):
    """For every picture that comes again: the number of OTHER pictures between the two showings."""
    last = {}
    gaps = []
    for index, path in enumerate(showings):
        if path in last:
            gaps.append(index - last[path] - 1)  # *path* is not among them: it is the last showing
        last[path] = index
    return gaps


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8, 25])
@pytest.mark.parametrize("seed", range(12))
def test_random_order_puts_at_least_three_other_pictures_between_two_showings(
    tmp_path, backends, count, seed
):
    for n in range(count):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="random", rng=random.Random(seed))
    showings = _showings(src, 6 * count + 20)
    wanted = min(3, count - 1)  # a folder of fewer than four: the largest distance there is
    gaps = _gaps(showings)
    assert gaps and min(gaps) >= wanted
    if count > 1:
        assert set(showings) == set(src.images())  # and every picture is still shown


def test_random_order_of_one_picture_shows_it_again_and_again_without_failing(tmp_path, backends):
    make_image(tmp_path / "only.png")
    src = started(tmp_path, backends, order="random")
    assert set(_showings(src, 30)) == {str(tmp_path / "only.png")}


def test_random_order_keeps_every_picture_once_per_cycle_with_the_gap(tmp_path, backends):
    for n in range(9):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="random", rng=random.Random(3))
    showings = _showings(src, 9 * 6 - 1)
    for cycle in range(6):
        assert len(set(showings[cycle * 9 : cycle * 9 + 9])) == 9


def test_name_order_of_four_or_more_pictures_keeps_the_gap_by_itself(tmp_path, backends):
    for n in range(5):
        make_image(tmp_path / f"{n}.png")
    src = started(tmp_path, backends, order="name")
    assert min(_gaps(_showings(src, 40))) >= 3


def test_random_order_changing_the_folder_forgets_the_history(tmp_path, backends):
    for n in range(5):
        make_image(tmp_path / "one" / f"{n}.png")
    for n in range(2):
        make_image(tmp_path / "two" / f"{n}.png")
    src = started(tmp_path / "one", backends, order="random", rng=random.Random(5))
    old = set(_showings(src, 12))
    assert len(old) == 5
    src.set_folder(str(tmp_path / "two"))
    backends[1].run_all()
    assert not src._recent  # the history names no picture of the old folder
    after = _showings(src, 10)
    assert set(after) == {str(tmp_path / "two" / f"{n}.png") for n in range(2)}
    assert not old & set(after)  # nothing of the old folder is named any more
    assert min(_gaps(after)) >= 1  # two pictures: they alternate
    # and back: the old history is not kept, the first picture is free to be any of the five
    src.set_folder(str(tmp_path / "one"))
    backends[1].run_all()
    again = _showings(src, 40)
    assert set(again) == old and min(_gaps(again)) >= 3


def test_random_order_deleting_pictures_while_showing_does_not_fail(tmp_path, backends):
    paths = [make_image(tmp_path / f"{n}.png") for n in range(6)]
    src = started(tmp_path, backends, order="random", rng=random.Random(2))
    _showings(src, 10)
    for path in paths[:4]:
        os.remove(path)
        backends[0].emit(path, FsEvent.DELETED)
    shown = _showings(src, 20)
    assert set(shown[1:]) <= {paths[4], paths[5]}
    assert min(_gaps(shown[1:])) >= 1


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
    limit_lines = [m for m in _warnings(caplog) if "folder limits or watch problems" in m]
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
    limit_lines = [m for m in _warnings(caplog) if "folder limits or watch problems" in m]
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


def _fake_kernel_view(monkeypatch, held_dirs):
    from slideshow_lock import image_source

    def kernel_view():
        held = set()
        for path in held_dirs:
            st = os.stat(path)
            held.add(((os.major(st.st_dev) << 20) | os.minor(st.st_dev), st.st_ino))
        return held

    monkeypatch.setattr(image_source, "_kernel_watch_inodes", kernel_view)


def test_watches_the_kernel_silently_did_not_install_are_reported_once_and_may_recover(
    tmp_path, backends, caplog, monkeypatch
):
    """Gio does not raise when inotify watches run out; /proc is the only witness."""
    for n in range(10):
        make_image(tmp_path / f"d{n:02d}" / "a.png")
    held = {str(tmp_path)} | {str(tmp_path / f"d{n:02d}") for n in range(4)}  # 5 of 11
    _fake_kernel_view(monkeypatch, held)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, verify_watches=True)
    assert len(src) == 10  # nothing is lost from the queue
    lines = [m for m in _warnings(caplog) if "folder limits or watch problems" in m]
    assert len(lines) == 1 and "6 folders have no confirmed kernel watch" in lines[0]
    assert "may recover" in lines[0] and "retries" in lines[0]
    assert "not watched" not in lines[0]  # not declared dead
    assert len(_warnings(caplog)) == 1
    # the monitors were NOT cancelled: GLib may still get them installed on its retry
    assert len(backends[0].callbacks) >= 11


def test_a_total_miss_is_reported_as_uncertainty_not_as_a_failure(
    tmp_path, backends, caplog, monkeypatch
):
    """Own watches exist but not one shows up in the kernel's list (the most common real case:
    the limit was used up by another application before we started)."""
    make_image(tmp_path / "a" / "a.png")
    _fake_kernel_view(monkeypatch, set())  # the kernel lists none of ours
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, verify_watches=True)
    assert len(src) == 1
    lines = [m for m in _warnings(caplog) if "folder limits or watch problems" in m]
    assert len(lines) == 1
    assert "could not confirm that the kernel installed the watches" in lines[0]
    assert "not watched" not in lines[0] and "no confirmed kernel watch" not in lines[0]
    assert len(_warnings(caplog)) == 1


def test_a_kernel_that_cannot_be_asked_makes_no_claim(tmp_path, backends, caplog, monkeypatch):
    from slideshow_lock import image_source

    make_image(tmp_path / "a" / "a.png")
    monkeypatch.setattr(image_source, "_kernel_watch_inodes", lambda: None)  # no /proc
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, verify_watches=True)
    assert len(src) == 1
    assert not _warnings(caplog)


def test_the_kernel_check_is_off_for_injected_watchers_unless_asked_for(
    tmp_path, backends, caplog, monkeypatch
):
    from slideshow_lock import image_source

    make_image(tmp_path / "a" / "a.png")
    monkeypatch.setattr(image_source, "_kernel_watch_inodes", lambda: set())
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        started(tmp_path, backends)  # a fake watcher installs no kernel watches
    assert not _warnings(caplog)


# -- follow-up items: bursts per folder, later waves, lost lines, flags ---------------------


def test_log_window_starts_afresh_when_the_folder_is_switched(tmp_path, backends, caplog):
    one, two = tmp_path / "one", tmp_path / "two"
    for n in range(LOG_FIRST_N + 3):
        make_image(one / f"bad{n:02d}.png", b"junk")
    make_image(two / "bad_a.png", b"junk")
    make_image(two / "bad_b.png", b"junk")
    make_image(two / "good.png")
    src = started(one, backends)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src.set_folder(str(two))
        backends[1].run_all()
    msgs = _warnings(caplog)
    assert any("bad_a.png" in m and "skipping" in m for m in msgs)  # logged one by one
    assert any("bad_b.png" in m and "skipping" in m for m in msgs)
    assert any(m.startswith("[slideshow-dir] 3 more") for m in msgs)  # the old window's count
    assert names(src, two) == ["good.png"]


def test_a_folder_created_after_the_summary_past_the_directory_cap_is_logged_per_folder(
    tmp_path, backends, caplog
):
    for n in range(3):
        make_image(tmp_path / f"d{n}" / "a.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, max_directories=2)  # the root and one subfolder
        assert len([m for m in _warnings(caplog) if "not walked (limit 2)" in m]) == 1
        os.makedirs(tmp_path / "later")
        backends[0].emit(str(tmp_path / "later"), FsEvent.CREATED)
    later = [m for m in _warnings(caplog) if "later" in m and "not walked" in m]
    assert len(later) == 1 and "folder limit 2 reached" in later[0]
    assert len(src) == 1


def test_a_folder_created_after_the_summary_past_the_watch_cap_is_logged_per_folder(
    tmp_path, backends, caplog
):
    for n in range(4):
        make_image(tmp_path / f"d{n}" / "a.png")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        src = started(tmp_path, backends, max_watches=2)
        make_image(tmp_path / "later" / "b.png")
        backends[0].emit(str(tmp_path / "later"), FsEvent.CREATED)
        backends[1].run_all()
    assert len(src) == 5  # walked, only not watched
    later = [m for m in _warnings(caplog) if "later" in m and "not watched" in m]
    assert len(later) == 1 and "watch limit 2 reached" in later[0]


def test_deleting_an_empty_root_still_logs_that_the_folder_does_not_exist(
    tmp_path, backends, caplog
):
    folder = tmp_path / "pics"
    os.makedirs(folder)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.image_source"):
        started(folder, backends)  # logs "no displayable images"
        assert any("no displayable images" in m for m in _warnings(caplog))
        shutil.rmtree(folder)
        backends[0].emit(str(folder), FsEvent.DELETED)
    assert any("does not exist" in m for m in _warnings(caplog))


def test_probe_opens_without_blocking_and_without_a_controlling_terminal(tmp_path, monkeypatch):
    seen = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("slideshow_lock.image_source.os.open", recording_open)
    probe_image(make_image(tmp_path / "a.png"))
    assert seen and seen[0] & os.O_NONBLOCK and seen[0] & os.O_NOCTTY


#: What each format's first bytes look like, and the name gdk-pixbuf gives the format. Checked on
#: the bytes alone (no loader), so the result is the same on a machine without a WebP or TIFF
#: loader: ``probe_loadable`` compares this name with the names of the loaders.
HEADERS = [
    ("jpeg", b"\xff\xd8\xff\xe0" + bytes(12)),
    ("png", PNG),
    ("gif", b"GIF87a" + bytes(10)),
    ("gif", b"GIF89a" + bytes(10)),
    ("bmp", b"BM" + bytes(14)),
    ("tiff", b"II*\x00" + bytes(12)),
    ("tiff", b"MM\x00*" + bytes(12)),
    ("webp", b"RIFF\x10\x00\x00\x00WEBPVP8 "),
]


@pytest.mark.parametrize("name,head", HEADERS, ids=[f"{n}-{h[:6]!r}" for n, h in HEADERS])
def test_probe_names_the_format_the_way_gdk_pixbuf_does(tmp_path, name, head):
    assert probe_image(make_image(tmp_path / "x.img", head)) == name


@pytest.mark.parametrize(
    "head",
    [
        b"RIFF\x10\x00\x00\x00WAVEfmt ",  # a RIFF file that is not WebP
        b"RIFF\x10\x00\x00\x00AVI LIST",
        b"FORM\x10\x00\x00\x00WEBPVP8 ",  # the WebP tag without the RIFF one
        b"GIF88a" + bytes(10),
        b"II*\x01" + bytes(12),
    ],
    ids=["wave", "avi", "webp-tag-only", "gif88a", "tiff-bad-magic"],
)
def test_probe_rejects_a_header_that_only_looks_like_a_known_one(tmp_path, head):
    with pytest.raises(ValueError, match="not a recognised image header"):
        probe_image(make_image(tmp_path / "x.img", head))


# -- JPEG 2000 is left out on purpose (docs/image-source.md, "Picture formats") --------------------

#: The two ways a JPEG 2000 file starts: the JP2 box signature, and a bare codestream.
JP2_HEADERS = {
    "jp2": b"\x00\x00\x00\x0cjP  \r\n\x87\n" + bytes(12),
    "codestream": b"\xff\x4f\xff\x51" + bytes(12),
}
JP2_EXTENSIONS = [".jp2", ".j2k", ".j2c", ".jpc", ".jpf", ".jpx", ".jpm", ".mj2"]


def test_the_extension_list_is_these_eight_formats_and_no_jpeg_2000():
    assert IMAGE_EXTENSIONS == frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
    )
    assert not IMAGE_EXTENSIONS & set(JP2_EXTENSIONS)


@pytest.mark.parametrize("ext", JP2_EXTENSIONS)
@pytest.mark.parametrize("kind", sorted(JP2_HEADERS))
def test_a_jpeg_2000_file_does_not_enter_the_picture_queue(tmp_path, backends, ext, kind):
    make_image(tmp_path / f"a{ext}", JP2_HEADERS[kind])
    make_image(tmp_path / f"png-inside{ext}", PNG)  # the extension alone keeps it out
    make_image(tmp_path / "b.png")  # negative control: a picture beside them is queued
    src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["b.png"]


@pytest.mark.parametrize("kind", sorted(JP2_HEADERS))
def test_probe_does_not_know_a_jpeg_2000_header_under_a_known_extension(tmp_path, backends, kind):
    path = make_image(tmp_path / "renamed.jpg", JP2_HEADERS[kind])
    with pytest.raises(ValueError, match="not a recognised image header"):
        probe_image(path)
    make_image(tmp_path / "ok.jpg", b"\xff\xd8\xff\xe0" + bytes(12))  # negative control
    src = started(tmp_path, backends)
    assert names(src, tmp_path) == ["ok.jpg"]
