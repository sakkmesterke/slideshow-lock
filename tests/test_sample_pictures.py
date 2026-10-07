"""``slideshow_lock.sample_pictures``: the copy of the packaged pictures into ``sakkmesterke``.

Standard library only, real files under ``tmp_path``, no GTK (it also runs with ``--noconftest``).
Nothing here is skipped: a case that needs a privilege the test does not have is made with an
injected error (``os.link`` that answers ``EPERM``, a ``geteuid`` that answers another user), not
with ``chmod`` and a check for root.

Every gate of the order of ``install`` has a case that goes red without it:

* nothing to do: no folder, no write, no state rewrite (the user's deleted folder stays deleted);
* space before the first folder, a trial write of the state before the first folder;
* the lock, the damaged state, the unreadable state, the foreign subfolder;
* the copy: write, time, flush, link; no overwrite; the stop flag; the cleanup of orphans.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from slideshow_lock import sample_pictures as sp
from tests.sample_fixtures import PICTURES, make_source

pytestmark = pytest.mark.usefixtures("umask_022")

PACKAGE = Path(sp.__file__).resolve().parent


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


class Rig:
    """A package folder, a pictures folder that does not exist yet, a state file."""

    def __init__(self, root: Path):
        self.root = root
        self.source = make_source(root)
        self.pictures = str(root / "home" / "Pictures")
        self.state = str(root / "state" / "slideshow-lock" / "sample-pictures.json")

    @property
    def sub(self) -> Path:
        return Path(self.pictures) / sp.SUBDIR

    def run(self, **kwargs) -> sp.Result:
        return sp.install(self.source, self.pictures, self.state, **kwargs)

    def handled(self):
        return json.loads(Path(self.state).read_text())["handled"]

    def names(self):
        return sorted(entry.name for entry in self.sub.iterdir())

    def tree(self, *folders):
        """Every entry under *folders* with what changes when something is written."""
        seen = {}
        for folder in folders or (str(self.root),):
            for base, dirs, files in os.walk(folder):
                for name in dirs + files:
                    path = os.path.join(base, name)
                    info = os.lstat(path)
                    seen[path] = (info.st_mode, info.st_size, info.st_mtime_ns)
        return seen


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


class Spy:
    """Every call of some ``os`` functions, kept while the real ones still run."""

    def __init__(self, monkeypatch, names=("mkdir", "replace", "rename", "link", "utime", "fsync")):
        self.calls = []
        for name in names:
            self._wrap(monkeypatch, name)
        original = os.open

        def spy_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                self.calls.append(("open-create", os.fspath(path)))
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy_open)

    def _wrap(self, monkeypatch, name):
        original = getattr(os, name)

        def spy(*args, **kwargs):
            self.calls.append((name, args[:2]))
            return original(*args, **kwargs)

        monkeypatch.setattr(os, name, spy)

    def of(self, name):
        return [call for call in self.calls if call[0] == name]


def fd_name(fd) -> str:
    return os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# -- where the package and the state are ----------------------------------------------------------


def test_the_package_folder_is_the_first_data_dir_that_has_it(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    make_source(second)  # makes second/share/slideshow-lock/pictures
    want = second / "share" / "slideshow-lock" / "pictures"
    assert sp.find_source_dir((str(first), str(second / "share"))) == str(want)
    make_source(first)
    assert sp.find_source_dir((str(first / "share"), str(second / "share"))) == str(
        first / "share" / "slideshow-lock" / "pictures"
    )


def test_no_package_folder_is_none_and_a_relative_data_dir_is_not_looked_at(tmp_path, monkeypatch):
    assert sp.find_source_dir((str(tmp_path),)) is None
    make_source(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert sp.find_source_dir(("share",)) is None


def test_the_fixed_data_dirs_are_the_default_and_the_environment_is_not_read(tmp_path, monkeypatch):
    assert sp.DATA_DIRS == ("/usr/local/share", "/usr/share")
    make_source(tmp_path)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))
    monkeypatch.setattr(sp, "DATA_DIRS", (str(tmp_path / "empty"),))
    assert sp.find_source_dir() is None  # red if the environment variable were read
    monkeypatch.setattr(sp, "DATA_DIRS", (str(tmp_path / "share"),))
    assert sp.find_source_dir() is not None  # the constant is what decides, at call time


def test_the_state_file_follows_xdg_state_home_when_it_is_absolute(tmp_path):
    base = str(tmp_path / "st")
    assert sp.state_path({"XDG_STATE_HOME": base, "HOME": "/h"}) == os.path.join(
        base, "slideshow-lock", "sample-pictures.json"
    )
    for bad in ("", "relative/state"):
        assert sp.state_path({"XDG_STATE_HOME": bad, "HOME": "/h"}) == (
            "/h/.local/state/slideshow-lock/sample-pictures.json"
        )
    assert sp.state_path({"HOME": "/h"}).startswith("/h/.local/state/")


@pytest.mark.parametrize(
    "name",
    ["fr01.jpg", "a b.png", "CREDITS.txt", "Képek.jpg", "x" * 255],
)
def test_a_plain_name_is_valid(name):
    assert sp._valid_name(name)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", ".hidden.jpg", "a/b.jpg", "../x.jpg", "a\x00b.jpg", "a\nb.jpg", "a\x7f.jpg"]
    + ["\udcff.jpg", "x" * 256, None, 7, b"x.jpg"],
)
def test_anything_else_is_not_a_name(name):
    assert not sp._valid_name(name)


def test_a_picture_is_told_by_its_extension_like_the_folder_walk_does():
    assert sp.is_image_name("a.JPG") and sp.is_image_name("a.png")
    assert not sp.is_image_name("CREDITS.txt") and not sp.is_image_name("a.xcf")


# -- the first start ---------------------------------------------------------------------------


def test_the_first_start_copies_the_pictures_and_the_credits(rig):
    result = rig.run()
    assert (result.status, result.copied, result.already_there, result.handled_before) == (
        sp.DONE,
        4,
        0,
        0,
    )
    assert rig.names() == sorted(PICTURES + (sp.CREDITS_NAME,))
    for name in rig.names():
        assert (rig.sub / name).read_bytes() == (Path(rig.source) / name).read_bytes()
    assert sorted(rig.handled()) == rig.names()


def test_the_copy_has_the_time_of_the_source_and_not_the_time_of_the_copy(rig):
    rig.run()
    for name in PICTURES:
        copied, original = os.stat(rig.sub / name), os.stat(Path(rig.source) / name)
        assert copied.st_mtime_ns == original.st_mtime_ns
    assert os.stat(rig.sub / PICTURES[0]).st_mtime < os.stat(rig.state).st_mtime - 1000


def test_the_modes_follow_the_umask_and_the_state_is_private(rig):
    rig.run()
    assert stat.S_IMODE(os.stat(rig.sub).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(rig.sub / PICTURES[0]).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(rig.state).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(rig.state)).st_mode) == 0o700


def test_the_pictures_folder_is_made_when_it_is_missing_and_nothing_hidden_is_left(rig):
    assert not os.path.exists(rig.pictures)
    rig.run()
    assert not [n for n in rig.names() if n.startswith(".")]
    assert sorted(os.listdir(os.path.dirname(rig.state))) == [
        "sample-pictures.json",
        "sample-pictures.json.lock",
    ]


def test_the_picture_folder_setting_is_not_in_the_way_only_the_folder_is_used(rig):
    os.makedirs(rig.pictures)
    (Path(rig.pictures) / "own.png").write_bytes(b"mine")
    rig.run()
    assert (Path(rig.pictures) / "own.png").read_bytes() == b"mine"
    assert sorted(os.listdir(rig.pictures)) == ["own.png", sp.SUBDIR]


def test_a_pictures_folder_that_is_a_link_is_followed(rig, tmp_path):
    elsewhere = tmp_path / "other-disk"
    elsewhere.mkdir()
    os.makedirs(os.path.dirname(rig.pictures))
    os.symlink(elsewhere, rig.pictures)
    assert rig.run().status == sp.DONE
    assert sorted(os.listdir(elsewhere / sp.SUBDIR)) == sorted(PICTURES + (sp.CREDITS_NAME,))


def test_the_copy_is_one_summary_line_without_a_path_or_a_name(rig, caplog):
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.sample_pictures"):
        rig.run()
    [line] = [r.getMessage() for r in caplog.records]
    assert line == "[samples] copied 4, already there 0, dealt with before 0"
    assert not warnings_of(caplog)


# -- what is copied ----------------------------------------------------------------------------


def test_only_pictures_and_the_credits_are_taken_and_only_valid_files(rig):
    src = Path(rig.source)
    (src / "notes.txt").write_text("x")
    (src / "design.xcf").write_bytes(b"x")
    (src / ".hidden.jpg").write_bytes(b"x")
    (src / "dir.jpg").mkdir()
    os.symlink("/etc/hostname", src / "link.jpg")
    os.mkfifo(src / "pipe.jpg")
    rig.run()
    assert rig.names() == sorted(PICTURES + (sp.CREDITS_NAME,))


def test_a_file_larger_than_the_picture_reader_takes_is_left_out(rig, monkeypatch):
    monkeypatch.setattr(sp, "MAX_FILE_BYTES", os.stat(Path(rig.source) / "fr02.jpg").st_size - 1)
    rig.run()
    assert "fr02.jpg" not in rig.names() and "fr03.jpg" not in rig.names()
    assert "fr01.jpg" in rig.names()


def test_the_source_is_cut_at_the_most_files(rig, monkeypatch):
    monkeypatch.setattr(sp, "MAX_FILES", 2)
    assert rig.run().copied == 2


def test_no_package_is_no_source_and_leaves_no_trace(rig):
    for source in (None, str(Path(rig.source) / "missing")):
        assert sp.install(source, rig.pictures, rig.state).status == sp.NO_SOURCE
    assert not os.path.exists(rig.pictures) and not os.path.exists(os.path.dirname(rig.state))


def test_a_package_without_pictures_is_nothing_to_do_and_creates_no_state_folder(rig):
    for entry in Path(rig.source).iterdir():
        entry.unlink()
    (Path(rig.source) / "notes.txt").write_text("x")
    assert rig.run().status == sp.NOTHING_TO_DO
    assert not os.path.exists(rig.pictures) and not os.path.exists(os.path.dirname(rig.state))


# -- the second start, and the user's choices ----------------------------------------------------


def test_the_second_start_has_nothing_to_do_and_writes_nothing(rig, monkeypatch):
    rig.run()
    before = rig.tree()
    spy = Spy(monkeypatch)
    result = rig.run()
    assert (result.status, result.copied, result.handled_before) == (sp.NOTHING_TO_DO, 0, 4)
    assert rig.tree() == before
    assert spy.calls == []  # no mkdir, no replace, no open for writing, no link, no utime


def test_a_folder_the_user_deleted_does_not_come_back_and_nothing_is_written(rig, monkeypatch):
    rig.run()
    import shutil

    shutil.rmtree(rig.sub)
    state_before = Path(rig.state).read_bytes()
    state_mtime = os.stat(rig.state).st_mtime_ns
    spy = Spy(monkeypatch)
    assert rig.run().status == sp.NOTHING_TO_DO
    assert not rig.sub.exists()
    assert Path(rig.state).read_bytes() == state_before
    assert os.stat(rig.state).st_mtime_ns == state_mtime
    assert spy.calls == []


def test_a_picture_the_user_deleted_does_not_come_back_but_the_rest_stays(rig):
    rig.run()
    (rig.sub / "fr02.jpg").unlink()
    assert rig.run().status == sp.NOTHING_TO_DO
    assert "fr02.jpg" not in rig.names() and "fr01.jpg" in rig.names()


def test_a_name_a_later_package_adds_is_copied_once_into_a_recreated_folder(rig):
    rig.run()
    import shutil

    shutil.rmtree(rig.sub)
    make_source(rig.root, PICTURES + ("fr04.jpg",))
    result = rig.run()
    assert (result.status, result.copied, result.handled_before) == (sp.DONE, 1, 4)
    assert rig.names() == ["fr04.jpg"]
    assert rig.run().status == sp.NOTHING_TO_DO


def test_nothing_is_cleaned_up_when_there_is_nothing_to_copy(rig):
    rig.run()
    orphan = rig.sub / f".fr01.jpg.part-{os.getpid()}"
    orphan.write_bytes(b"half")
    rig.run()
    assert orphan.exists()


def test_a_file_that_is_there_is_never_overwritten_and_counts_as_dealt_with(rig):
    rig.sub.mkdir(parents=True)
    (rig.sub / "fr01.jpg").write_bytes(b"the user's own")
    os.utime(rig.sub / "fr01.jpg", (1, 1))
    result = rig.run()
    assert (result.copied, result.already_there) == (3, 1)
    assert (rig.sub / "fr01.jpg").read_bytes() == b"the user's own"
    assert os.stat(rig.sub / "fr01.jpg").st_mtime == 1
    assert "fr01.jpg" in rig.handled()


# -- the order of the copy ---------------------------------------------------------------------


def test_a_picture_is_written_then_timed_then_flushed_and_only_then_given_its_name(
    rig, monkeypatch
):
    events = []
    real = {name: getattr(os, name) for name in ("write", "utime", "fsync", "link")}

    def wrap(name, label):
        def spy(*args, **kwargs):
            if name in ("write", "fsync"):
                if fd_name(args[0]).startswith(".fr01.jpg.part-"):
                    events.append(label)
            else:
                events.append(label)
            return real[name](*args, **kwargs)

        monkeypatch.setattr(os, name, spy)

    for name in real:
        wrap(name, name)
    rig.run()
    first = [e for e in events if e in ("write", "utime", "fsync", "link")]
    assert first.index("write") < first.index("utime") < first.index("fsync") < first.index("link")
    assert events.count("link") == 4


def test_the_hidden_name_of_a_half_copy_is_what_the_folder_walk_skips(rig, monkeypatch):
    seen = []
    real = os.link

    def link(src, dst, **kwargs):
        seen.append((src, dst))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", link)
    rig.run()
    assert seen[0] == (f".fr01.jpg.part-{os.getpid()}", "fr01.jpg")
    assert all(src.startswith(".") for src, _ in seen)


def test_a_link_that_finds_the_name_taken_does_not_fall_back_to_a_replace(rig, monkeypatch):
    rig.sub.mkdir(parents=True)
    (rig.sub / "fr01.jpg").write_bytes(b"mine")

    def rename(*args, **kwargs):
        raise AssertionError("a rename would replace the user's file")

    monkeypatch.setattr(os, "rename", rename)
    assert rig.run().already_there == 1
    assert (rig.sub / "fr01.jpg").read_bytes() == b"mine"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS])
def test_a_file_system_without_links_gets_a_checked_rename(rig, monkeypatch, code):
    def no_link(*args, **kwargs):
        raise OSError(code, "no hard links")

    monkeypatch.setattr(os, "link", no_link)
    result = rig.run()
    assert (result.status, result.copied) == (sp.DONE, 4)
    assert (rig.sub / "fr01.jpg").read_bytes() == (Path(rig.source) / "fr01.jpg").read_bytes()
    assert not [n for n in rig.names() if n.startswith(".")]


def test_without_links_a_name_that_is_taken_is_left_alone(rig, monkeypatch):
    rig.sub.mkdir(parents=True)
    (rig.sub / "fr01.jpg").write_bytes(b"mine")
    monkeypatch.setattr(
        os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "no hard links"))
    )
    assert rig.run().already_there == 1
    assert (rig.sub / "fr01.jpg").read_bytes() == b"mine"


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EACCES, errno.EIO])
def test_any_other_error_of_the_link_is_an_error_not_a_rename(rig, monkeypatch, code):
    def no_link(*args, **kwargs):
        raise OSError(code, "refused")

    def rename(*args, **kwargs):
        raise AssertionError("the fallback is for file systems without links only")

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "rename", rename)
    result = rig.run()
    assert (result.status, result.reason, result.copied) == (sp.ERROR, "copy-failed", 0)
    assert not [n for n in rig.names() if n.startswith(".")]
    assert rig.handled() == []  # nothing is marked: the next start tries again


# -- stopping and orphans -----------------------------------------------------------------------


def test_the_stop_flag_between_two_files_keeps_the_first_and_the_next_start_goes_on(rig):
    result = rig.run(should_stop=lambda: (rig.sub / "fr01.jpg").exists())
    assert (result.status, result.copied) == (sp.STOPPED, 1)
    assert rig.names() == ["fr01.jpg"]
    assert rig.handled() == ["fr01.jpg"]
    resumed = rig.run()
    assert (resumed.status, resumed.copied, resumed.handled_before) == (sp.DONE, 3, 1)


def test_a_stop_flag_that_is_up_from_the_start_makes_no_file_at_all(rig, monkeypatch):
    spy = Spy(monkeypatch, names=("link",))
    result = rig.run(should_stop=lambda: True)
    assert (result.status, result.copied) == (sp.STOPPED, 0)
    assert [c for c in spy.of("open-create") if ".part-" in c[1]] == []  # not even a hidden file
    assert rig.names() == [] and rig.handled() == []


def test_the_stop_flag_in_the_middle_of_a_file_removes_the_hidden_file(rig, monkeypatch):
    monkeypatch.setattr(sp, "CHUNK_BYTES", 100)
    count = [0]

    def stop_in_the_third_chunk():
        count[0] += 1
        return count[0] == 4  # one check per file start, then one per chunk

    result = rig.run(should_stop=stop_in_the_third_chunk)
    assert result.status == sp.STOPPED
    assert not [n for n in rig.names() if n.startswith(".")]
    assert rig.handled() == []


def test_the_orphans_of_an_earlier_run_are_removed_and_nothing_else(rig):
    rig.sub.mkdir(parents=True)
    old = ".fr01.jpg.part-123"
    (rig.sub / old).write_bytes(b"half")
    (rig.sub / ".x.part-1").write_bytes(b"the user's")  # not one of our names
    (rig.sub / ".frXjpg.part-1").write_bytes(b"dot is not a wildcard")
    (rig.sub / ".fr02.jpg.part-x").write_bytes(b"no pid")
    (rig.sub / ".fr02.jpg.part-1²").write_bytes(b"not a digit of ours")
    os.symlink("/etc/hostname", rig.sub / ".fr01.jpg.part-9")
    rig.run()
    left = [n for n in rig.names() if n.startswith(".")]
    assert sorted(left) == sorted(
        [".x.part-1", ".frXjpg.part-1", ".fr02.jpg.part-x", ".fr02.jpg.part-1²", ".fr01.jpg.part-9"]
    )


def test_a_second_start_at_the_same_time_is_busy_and_writes_nothing(rig):
    os.makedirs(os.path.dirname(rig.state))
    lock = os.open(rig.state + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        before = rig.tree()
        assert rig.run().status == sp.BUSY
        assert rig.tree() == before
    finally:
        os.close(lock)
    assert rig.run().status == sp.DONE  # the lock was let go: the next one copies


def test_the_lock_file_is_private(rig):
    rig.run()
    assert stat.S_IMODE(os.stat(rig.state + ".lock").st_mode) == 0o600


def test_a_link_in_the_place_of_the_lock_file_is_not_followed(rig, tmp_path):
    target = tmp_path / "somebody-elses"
    target.write_bytes(b"")
    os.makedirs(os.path.dirname(rig.state))
    os.symlink(target, rig.state + ".lock")
    held = os.open(target, os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)  # a followed link would meet this lock and be busy
    try:
        assert rig.run().status == sp.DONE
    finally:
        os.close(held)
    assert target.read_bytes() == b""
    assert os.path.islink(rig.state + ".lock")


def test_a_file_system_without_locks_copies_without_cleaning_up(rig, monkeypatch):
    rig.sub.mkdir(parents=True)
    orphan = rig.sub / ".fr01.jpg.part-123"
    orphan.write_bytes(b"half")

    def no_lock(fd, op):
        raise OSError(errno.ENOLCK, "no locks")

    monkeypatch.setattr(fcntl, "flock", no_lock)
    assert rig.run().status == sp.DONE
    assert orphan.exists()  # the cleanup needs the lock


# -- space ------------------------------------------------------------------------------------


class FakeVfs:
    f_frsize = 4096

    def __init__(self, free_bytes):
        self.f_bavail = free_bytes // 4096


def test_too_little_room_is_no_space_and_nothing_is_made(rig, monkeypatch, caplog):
    need = sum(os.stat(Path(rig.source) / n).st_size for n in os.listdir(rig.source))
    monkeypatch.setattr(os, "fstatvfs", lambda fd: FakeVfs(need + sp.MARGIN_BYTES - 4096))
    spy = Spy(monkeypatch, names=("mkdir", "replace"))
    before = rig.tree(str(rig.root / "home"))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        result = rig.run()
    assert result.status == sp.NO_SPACE
    assert rig.tree(str(rig.root / "home")) == before and not os.path.exists(rig.pictures)
    assert spy.of("replace") == []
    assert not [c for c in spy.of("mkdir") if str(rig.root / "home") in str(c[1][0])]
    [line] = warnings_of(caplog)
    assert "need" in line and "free" in line and str(rig.root) not in line


def test_room_for_the_files_and_the_margin_is_enough(rig, monkeypatch):
    need = sum(os.stat(Path(rig.source) / n).st_size for n in os.listdir(rig.source))
    monkeypatch.setattr(os, "fstatvfs", lambda fd: FakeVfs(need + sp.MARGIN_BYTES + 4096))
    assert rig.run().status == sp.DONE


def test_the_margin_is_a_parameter_and_the_default_is_16_mib(rig):
    assert sp.MARGIN_BYTES == 16 * 1024 * 1024
    assert rig.run(margin_bytes=10**18).status == sp.NO_SPACE
    assert rig.run(margin_bytes=0).status == sp.DONE


def test_the_room_is_measured_on_the_nearest_folder_that_exists(rig, monkeypatch):
    measured = []

    def fstatvfs(fd):
        measured.append(os.readlink(f"/proc/self/fd/{fd}"))
        return FakeVfs(10**12)

    monkeypatch.setattr(os, "fstatvfs", fstatvfs)
    (rig.root / "home").mkdir()
    rig.run()
    assert measured == [str(rig.root / "home")]


def test_the_room_that_cannot_be_measured_is_an_error_and_nothing_is_made(rig, monkeypatch):
    def broken(fd):
        raise PermissionError(errno.EACCES, "no")

    monkeypatch.setattr(os, "fstatvfs", broken)
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "space-unknown")
    assert not os.path.exists(rig.pictures)


# -- the state ------------------------------------------------------------------------------------


def test_a_state_that_cannot_be_written_is_an_error_before_any_folder_is_made(rig, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError(errno.EROFS, "read-only")

    monkeypatch.setattr(os, "replace", refused)
    before = rig.tree(str(rig.root / "home"))
    result = rig.run()
    assert (result.status, result.reason, result.copied) == (sp.ERROR, "state-unwritable", 0)
    assert not os.path.exists(rig.pictures)  # no folder to come back at every login
    assert rig.tree(str(rig.root / "home")) == before
    assert not Path(rig.state).exists()


def test_a_state_that_cannot_be_read_is_an_error_and_nothing_is_copied(rig, monkeypatch, caplog):
    rig.run()
    import shutil

    shutil.rmtree(rig.sub)
    real = os.open

    def denied(path, flags, *args, **kwargs):
        if os.fspath(path) == rig.state:
            raise PermissionError(errno.EACCES, "denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    Path(rig.state).write_text(json.dumps({"version": 1, "handled": ["fr01.jpg"]}))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        result = rig.run()
    assert (result.status, result.reason, result.copied) == (sp.ERROR, "state-unreadable", 0)
    assert not rig.sub.exists()  # an empty list would bring back what the user deleted
    assert len(warnings_of(caplog)) == 1


DAMAGED = {
    "not json": b"not json at all",
    "empty": b"",
    "a list": b'["fr01.jpg"]',
    "no version": b'{"handled": []}',
    "other version": b'{"version": 2, "handled": []}',
    "a bool version": b'{"version": true, "handled": []}',
    "handled not a list": b'{"version": 1, "handled": "fr01.jpg"}',
    "a path": b'{"version": 1, "handled": ["../x.jpg"]}',
    "a number": b'{"version": 1, "handled": [7]}',
    "a hidden name": b'{"version": 1, "handled": [".x.jpg"]}',
    "too many": json.dumps({"version": 1, "handled": [f"{i}.jpg" for i in range(5000)]}).encode(),
    "too long": b'{"version": 1, "handled": [], "pad": "' + b"x" * (1 << 20) + b'"}',
    "not utf-8": b'{"version": 1, "handled": ["\xff"]}',
    "too long, still valid json": b'{"version": 1, "handled": []}' + b" " * 70000,
    "lists nested too deep": b"[" * 60000,  # under the size limit, over the recursion limit
    "objects nested too deep": b'{"a":' * 13000,
}


@pytest.mark.parametrize("content", DAMAGED.values(), ids=DAMAGED.keys())
def test_a_damaged_state_is_an_empty_list_and_one_warning_and_the_next_write_mends_it(
    rig, caplog, content
):
    os.makedirs(os.path.dirname(rig.state))
    Path(rig.state).write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        result = rig.run()
    assert (result.status, result.copied) == (sp.DONE, 4)
    [line] = warnings_of(caplog)
    assert "damaged" in line and str(rig.root) not in line
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        assert rig.run().status == sp.NOTHING_TO_DO
    assert not warnings_of(caplog)


def test_a_link_in_the_place_of_the_state_is_damaged_and_replaced_not_followed(rig, tmp_path):
    target = tmp_path / "somebody-elses"
    target.write_text("keep me")
    os.makedirs(os.path.dirname(rig.state))
    os.symlink(target, rig.state)
    assert rig.run().status == sp.DONE
    assert target.read_text() == "keep me"
    assert not os.path.islink(rig.state) and stat.S_ISREG(os.lstat(rig.state).st_mode)
    assert rig.run().status == sp.NOTHING_TO_DO


def test_a_pipe_or_a_device_in_the_place_of_the_state_does_not_hang(rig, caplog):
    os.makedirs(os.path.dirname(rig.state))
    os.mkfifo(rig.state)
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        assert rig.run().status == sp.DONE
    assert stat.S_ISREG(os.lstat(rig.state).st_mode)
    assert len(warnings_of(caplog)) == 1


def test_a_link_to_a_device_in_the_place_of_the_state_is_damaged(rig):
    os.makedirs(os.path.dirname(rig.state))
    os.symlink("/dev/zero", rig.state)
    assert rig.run().status == sp.DONE
    assert stat.S_ISREG(os.lstat(rig.state).st_mode)


def test_a_state_folder_that_cannot_be_made_is_an_error_not_an_exception(rig, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "mkdir", refused)
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "state-unwritable")
    assert not os.path.exists(rig.pictures)


def test_a_state_folder_that_is_a_file_is_an_error_not_an_exception(rig):
    os.makedirs(os.path.dirname(os.path.dirname(rig.state)))
    Path(os.path.dirname(rig.state)).write_text("a file where the folder should be")
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "state-unreadable")
    assert not os.path.exists(rig.pictures)


# -- the target ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["link", "file", "dangling"])
def test_a_subfolder_that_is_not_ours_is_left_alone(rig, tmp_path, caplog, kind):
    os.makedirs(rig.pictures)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if kind == "link":
        os.symlink(elsewhere, rig.sub)
    elif kind == "dangling":
        os.symlink(tmp_path / "nowhere", rig.sub)
    else:
        rig.sub.write_text("a file")
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        result = rig.run()
    assert (result.status, result.reason, result.copied) == (sp.ERROR, "foreign-dir", 0)
    assert os.listdir(elsewhere) == []
    assert rig.handled() == []  # no mark: the user may put it right
    assert len(warnings_of(caplog)) == 1


def test_a_subfolder_of_another_user_is_left_alone(rig, monkeypatch):
    rig.sub.mkdir(parents=True)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(rig.sub).st_uid + 1)
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "foreign-dir")
    assert os.listdir(rig.sub) == []


def test_a_pictures_folder_that_is_a_file_is_an_error_not_an_exception(rig):
    os.makedirs(os.path.dirname(rig.pictures))
    Path(rig.pictures).write_text("a file")
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "pictures-unwritable")


def test_a_real_subfolder_without_a_state_gets_only_what_is_missing(rig):
    rig.sub.mkdir(parents=True)
    (rig.sub / "fr02.jpg").write_bytes(b"x")
    (rig.sub / "mine.png").write_bytes(b"y")
    result = rig.run()
    assert (result.copied, result.already_there) == (3, 1)
    assert (rig.sub / "mine.png").read_bytes() == b"y"


# -- what goes wrong while writing -------------------------------------------------------------


def failing_write(monkeypatch, error):
    real = os.write

    def write(fd, data):
        if fd_name(fd).startswith(".fr01.jpg.part-"):
            raise error
        return real(fd, data)

    monkeypatch.setattr(os, "write", write)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_a_disk_that_fills_up_stops_at_once_and_marks_nothing(rig, monkeypatch, caplog, code):
    failing_write(monkeypatch, OSError(code, "full"))
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.sample_pictures"):
        result = rig.run()
    assert (result.status, result.copied) == (sp.NO_SPACE, 0)
    assert not [n for n in rig.names() if n.startswith(".")]
    assert rig.handled() == []
    [line] = warnings_of(caplog)
    assert "fr01" not in line and str(rig.root) not in line


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EIO])
def test_any_other_write_error_is_an_error_with_the_hidden_file_removed(rig, monkeypatch, code):
    failing_write(monkeypatch, OSError(code, "no"))
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "copy-failed")
    assert not [n for n in rig.names() if n.startswith(".")]
    assert rig.handled() == []


def test_a_hidden_file_that_cannot_be_made_is_an_error(rig, monkeypatch):
    real = os.open

    def deny(path, flags, *args, **kwargs):
        if str(path).startswith(".fr01.jpg.part-"):
            raise PermissionError(errno.EACCES, "denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    assert rig.run().status == sp.ERROR
    assert rig.handled() == []


def test_a_source_that_changes_while_it_is_copied_is_an_error(rig, monkeypatch):
    real = os.read
    state = {"done": False}

    def read(fd, count):
        data = real(fd, count)
        if data and not state["done"] and fd_name(fd) == "fr01.jpg":
            state["done"] = True
            with open(Path(rig.source) / "fr01.jpg", "ab") as handle:
                handle.write(b"more")
        return data

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(sp, "CHUNK_BYTES", 100)
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "copy-failed")
    assert "fr01.jpg" not in rig.names()


def test_a_stale_hidden_file_with_our_own_process_id_is_replaced(rig):
    rig.sub.mkdir(parents=True)
    (rig.sub / f".fr01.jpg.part-{os.getpid()}").write_bytes(b"left by a dead run")
    assert rig.run().status == sp.DONE
    assert (rig.sub / "fr01.jpg").read_bytes() == (Path(rig.source) / "fr01.jpg").read_bytes()


def test_a_state_write_that_fails_after_a_copy_is_an_error_and_the_copy_stays(rig, monkeypatch):
    calls = [0]
    real = os.replace

    def replace(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:  # the trial write was the first
            raise OSError(errno.EIO, "disk")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    result = rig.run()
    assert (result.status, result.reason) == (sp.ERROR, "state-unwritable")
    assert rig.names() == ["fr01.jpg"]
    resumed = rig.run()  # the file is there and is marked the next time
    assert (resumed.copied, resumed.already_there) == (3, 1)


# -- the module itself ---------------------------------------------------------------------------


def _imports(path: Path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
            names.update(alias.name for alias in node.names)
    return names


def test_the_module_needs_no_gtk_and_no_network():
    imported = _imports(PACKAGE / "sample_pictures.py")
    forbidden = {
        "gi",
        "socket",
        "urllib",
        "http",
        "ftplib",
        "ssl",
        "requests",
        "smtplib",
        "subprocess",
        "ctypes",
    }
    assert not imported & forbidden


@pytest.mark.spawns_processes
def test_importing_it_does_not_load_gtk():
    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, slideshow_lock.sample_pictures as m; print('gi' in sys.modules)",
        ],
        cwd=PACKAGE.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False"


def test_nothing_but_the_control_command_imports_it():
    importers = sorted(
        path.name
        for path in PACKAGE.glob("*.py")
        if path.name != "sample_pictures.py" and "sample_pictures" in _imports(path)
    )
    assert importers == ["control.py"]
