"""The sample pictures: copied once, at the first login, into ``Pictures/sakkmesterke``.

The package installs a few pictures under ``<datadir>/slideshow-lock/pictures`` (the source). This
module puts a copy of each in the user's pictures folder, in a subfolder of its own, so that a new
user sees something in the slideshow without choosing a folder. The ``picture-folder`` setting is
never written: the folder walk is recursive, so an empty setting already shows the subfolder.

Plain standard library, no GTK: the caller (``control.py``) hands it three paths and a stop flag
and runs it on a thread of its own, so the login is not held up and the locking service is not
involved at all.

What the user keeps, whatever happens:

* A file the program has dealt with is written in a state file (``sample-pictures.json`` under
  ``$XDG_STATE_HOME/slideshow-lock``) and is never copied again. A picture or the whole subfolder
  the user deletes does not come back. A name that a later package adds is copied once.
* Nothing is overwritten: a file that is already there is left as it is (``os.link`` refuses to
  replace it) and counts as dealt with.
* Nothing is created when there is nothing to copy: no folder, no write, no cleanup.
* A copy that is cut short (logout, power) leaves only a hidden ``.<name>.part-<pid>`` file, which
  the folder walk does not show and the next start removes.

The order of ``install`` is part of the contract (``docs/logging-and-lifecycle.md``): source, lock,
state, what is still to do; then, only if there is something, the free space, a trial write of the
state, the folders, the orphan cleanup, the copies. Every folder step after the root goes through a
directory descriptor (``dir_fd``) with ``O_NOFOLLOW``, so a link planted in the folder is not
followed.

Not measured here: a real GNOME login, the behaviour on exFAT or a network home, the Gio monitor
on a ``link`` (``tests/test_sample_pictures_gio.py`` measures that one where Gio exists).
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import stat
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from slideshow_lock.image_source import IMAGE_EXTENSIONS
from slideshow_lock.scaling import MAX_FILE_BYTES

_LOG = logging.getLogger(__name__)

#: The folder in the user's pictures folder that the copies go into.
SUBDIR = "sakkmesterke"

#: Where the package installs the pictures: ``<one of DATA_DIRS>/slideshow-lock/pictures``. A fixed
#: list and not ``XDG_DATA_DIRS``: the pictures come from the package, and an environment variable
#: of the user's session must not be able to name another source. A test patches this constant.
DATA_DIRS = ("/usr/local/share", "/usr/share")
APP_DIR = "slideshow-lock"
DATA_SUBDIR = "pictures"

#: The credits file is copied with the pictures; the folder walk does not show it (not an image).
CREDITS_NAME = "CREDITS.txt"

#: Free space that has to be left after the copy. A rule of thumb, not measured: the copy needs the
#: size of the files plus this.
MARGIN_BYTES = 16 * 1024 * 1024

#: At most this many files are taken from the source folder (the package has a handful).
MAX_FILES = 64

STATE_VERSION = 1
#: The state file is read up to this size; a longer file is taken as damaged.
STATE_MAX_BYTES = 64 * 1024
#: ... and at most this many names; more is damaged too.
STATE_MAX_NAMES = 4096

CHUNK_BYTES = 1024 * 1024

#: The file mode of a copy, and of the folder (the umask still applies).
FILE_MODE = 0o644
DIR_MODE = 0o755

#: Statuses of ``Result``.
DONE = "done"
NOTHING_TO_DO = "nothing-to-do"
NO_SOURCE = "no-source"
NO_SPACE = "no-space"
BUSY = "busy"
STOPPED = "stopped"
ERROR = "error"

#: What a ``link`` that the file system does not offer answers with: then, and only then, the copy
#: is put in place with a ``rename`` after a check that nothing is there.
_NO_LINK_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


@dataclass
class Result:
    """What ``install`` did: how many were copied, found there already, or had been dealt with by an
    earlier run; ``status`` says how it ended and, for an ``error``, ``reason`` is a constant."""

    status: str
    copied: int = 0
    already_there: int = 0
    handled_before: int = 0
    reason: str = ""


class _Stopped(Exception):
    """The ``should_stop`` flag came up in the middle of a copy."""


class _Failed(Exception):
    """A step failed; ``status`` and ``reason`` end up in the ``Result``."""

    def __init__(self, status: str, reason: str, detail: str = ""):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


def _why(exc: OSError) -> str:
    """What an error says without a path in it: the name of the errno, or the class."""
    return errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__


# -- where things are ------------------------------------------------------------------------------


def find_source_dir(data_dirs: Optional[Tuple[str, ...]] = None) -> Optional[str]:
    """The folder the package put the pictures in, or ``None`` (the package is not installed, or
    the part that holds them is not). *data_dirs* defaults to ``DATA_DIRS``; the environment is
    never read."""
    for base in DATA_DIRS if data_dirs is None else data_dirs:
        if not os.path.isabs(base):
            continue
        path = os.path.join(base, APP_DIR, DATA_SUBDIR)
        if os.path.isdir(path):
            return path
    return None


def state_path(environ: Optional[Dict[str, str]] = None) -> str:
    """The state file: ``$XDG_STATE_HOME`` when that is an absolute path, else
    ``~/.local/state``."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(base):
        home = environ.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".local", "state")
    return os.path.join(base, APP_DIR, "sample-pictures.json")


# -- names -----------------------------------------------------------------------------------------


def _valid_name(name: object) -> bool:
    """A plain file name: not empty, no path separator, not ``.``/``..``, not hidden, no NUL or
    control character, valid UTF-8, at most 255 bytes. Whether it is a picture is
    ``is_image_name``."""
    if not isinstance(name, str) or not name or name in (".", "..") or name.startswith("."):
        return False
    if "/" in name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    try:
        return len(name.encode("utf-8")) <= 255
    except UnicodeEncodeError:  # a lone surrogate: a name that is not UTF-8 on disk
        return False


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _tmp_name(name: str) -> str:
    return f".{name}.part-{os.getpid()}"


def _is_orphan_of(entry: str, names: Set[str]) -> bool:
    """``.<one of names>.part-<digits>``: the hidden file of a copy that did not finish. Compared as
    strings, with no pattern: a dot in a name is a dot and not a wildcard."""
    if not entry.startswith(".") or ".part-" not in entry:
        return False
    base, _, suffix = entry[1:].rpartition(".part-")
    return base in names and suffix.isascii() and suffix.isdigit()


# -- the source ------------------------------------------------------------------------------------


def _open_source(source_dir: str, name: str) -> Tuple[int, os.stat_result]:
    """The file *name* of the source folder, opened and checked on the descriptor: a regular file
    (not a link, nothing else), not larger than the picture reader takes."""
    fd = os.open(
        os.path.join(source_dir, name),
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise OSError(errno.EINVAL, "not a usable file")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _packaged(source_dir: str) -> List[Tuple[str, int]]:
    """The files to copy, ``(name, size)``: the pictures by name, then the credits file, of the
    source folder; those that pass the name and file checks. A file that does not is left out."""
    try:
        entries = sorted(os.listdir(source_dir), key=lambda name: (name == CREDITS_NAME, name))
    except OSError:
        return []
    found: List[Tuple[str, int]] = []
    for name in entries:
        if not _valid_name(name) or not (is_image_name(name) or name == CREDITS_NAME):
            continue
        try:
            fd, info = _open_source(source_dir, name)
        except OSError:
            continue
        os.close(fd)
        found.append((name, info.st_size))
    if len(found) > MAX_FILES:
        _LOG.warning(
            "[samples] the source holds more than %d files, the rest are left out", MAX_FILES
        )
        found = found[:MAX_FILES]
    return found


# -- the lock and the state ------------------------------------------------------------------------


def _open_lock(state_file: str) -> Tuple[Optional[int], bool]:
    """Take the lock next to the state file. ``(fd, True)`` when held; ``(None, False)`` when the
    file system gives no lock (the copy goes on without it: ``link`` refuses to overwrite either
    way, the lock only saves two runs from doing the same work). Raises ``BlockingIOError`` when
    another run holds it. An existing lock file is opened as it is; a missing one is created, with
    its folder."""
    path = state_file + ".lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, flags | os.O_CREAT, 0o600)
    except OSError as exc:
        _LOG.debug("[samples] no lock file (%s), going on without the lock", _why(exc))
        return None, False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        _LOG.debug("[samples] no lock (%s), going on without it", _why(exc))
        return None, False
    return fd, True


def _parse_state(raw: bytes) -> Optional[Set[str]]:
    """The names in a state file, or ``None`` when it is not the file this program writes."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: nested too deep
        return None
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        return None
    if data["version"] != STATE_VERSION:
        return None
    names = data.get("handled")
    if not isinstance(names, list) or len(names) > STATE_MAX_NAMES:
        return None
    if not all(isinstance(name, str) and _valid_name(name) for name in names):
        return None
    return set(names)


def _load_state(state_file: str) -> Set[str]:
    """The names dealt with before. No file is a first start (an empty set). A file that is not the
    one this program writes (a link, a pipe, too long, not JSON, the wrong shape) is damaged: an
    empty set and one WARNING; the trial write that follows replaces it. Only a real read error
    (``EACCES``, ``EIO``, ...) is a ``_Failed``, because an empty set would bring back what the
    user deleted."""
    try:
        fd = os.open(state_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return set()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _LOG.warning(
                "[samples] the state file is damaged (a link), starting from an empty list"
            )
            return set()
        raise _Failed(ERROR, "state-unreadable", _why(exc)) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            damaged = "not a regular file"
            raw = b""
        else:
            raw = os.read(fd, STATE_MAX_BYTES + 1)
            damaged = "too long" if len(raw) > STATE_MAX_BYTES else ""
    except OSError as exc:
        raise _Failed(ERROR, "state-unreadable", _why(exc)) from exc
    finally:
        os.close(fd)
    handled = None if damaged else _parse_state(raw)
    if handled is None:
        _LOG.warning(
            "[samples] the state file is damaged (%s), starting from an empty list",
            damaged or "not the expected content",
        )
        return set()
    return handled


def _save_state(state_file: str, handled: Set[str]) -> None:
    """Write the state: a new file next to it, flushed, then renamed over it (a link in its place
    is replaced, not followed). Raises ``OSError``."""
    folder = os.path.dirname(state_file)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = f"{state_file}.tmp-{os.getpid()}"
    payload = json.dumps({"version": STATE_VERSION, "handled": sorted(handled)}).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:  # left by a run that died with the same process id
        os.unlink(tmp)
        fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, state_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- space and folders -----------------------------------------------------------------------------


def _free_bytes(pictures_dir: str) -> int:
    """The free space where the pictures will go: measured on the nearest folder that exists (the
    pictures folder, or above it), so nothing has to be created to find out."""
    path = pictures_dir
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        info = os.fstatvfs(fd)
    finally:
        os.close(fd)
    return info.f_bavail * info.f_frsize


def _open_target(pictures_dir: str) -> int:
    """The descriptor of the subfolder, made when it is missing. The pictures folder itself may be a
    link (it can be on another disk); the subfolder may not, and must be ours."""
    os.makedirs(pictures_dir, mode=DIR_MODE, exist_ok=True)
    root = os.open(pictures_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(SUBDIR, DIR_MODE, dir_fd=root)
        except FileExistsError:
            pass
        try:
            sub = os.open(
                SUBDIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root
            )
        except OSError as exc:  # a link, or a file: the user's business
            raise _Failed(ERROR, "foreign-dir", _why(exc)) from exc
    finally:
        os.close(root)
    try:
        if os.fstat(sub).st_uid != os.geteuid():
            raise _Failed(ERROR, "foreign-dir", "owner")
    except BaseException:
        os.close(sub)
        raise
    return sub


def _clean_orphans(sub: int, names: Set[str]) -> None:
    try:
        entries = os.listdir(sub)
    except OSError:
        return
    for entry in entries:
        if not _is_orphan_of(entry, names):
            continue
        try:
            if stat.S_ISREG(os.stat(entry, dir_fd=sub, follow_symlinks=False).st_mode):
                os.unlink(entry, dir_fd=sub)
        except OSError:
            pass


# -- the copy --------------------------------------------------------------------------------------


def _copy_one(source_dir: str, name: str, sub: int, should_stop: Callable[[], bool]) -> bool:
    """Copy *name* into the subfolder: ``True`` when it is there now by this copy, ``False`` when a
    file of that name was there already (it is left as it is). The data goes to a hidden file first;
    it gets the time of the source (a picture younger than two seconds is held back by the reader),
    is flushed, and only then given its name, by a ``link`` that does not replace anything."""
    tmp = _tmp_name(name)
    src, info = _open_source(source_dir, name)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            dst = os.open(tmp, flags, FILE_MODE, dir_fd=sub)
        except FileExistsError:  # left by a run that died with the same process id
            os.unlink(tmp, dir_fd=sub)
            dst = os.open(tmp, flags, FILE_MODE, dir_fd=sub)
        try:
            try:
                written = 0
                while True:
                    if should_stop():
                        raise _Stopped()
                    chunk = os.read(src, CHUNK_BYTES)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view) :]
                    written += len(chunk)
                if written != info.st_size or os.fstat(src).st_size != info.st_size:
                    raise OSError(errno.EIO, "the source changed while it was copied")
                os.utime(dst, ns=(info.st_mtime_ns, info.st_mtime_ns))
                os.fsync(dst)
            finally:
                os.close(dst)
            try:
                os.link(tmp, name, src_dir_fd=sub, dst_dir_fd=sub, follow_symlinks=False)
                return True
            except FileExistsError:
                return False
            except OSError as exc:
                if exc.errno not in _NO_LINK_ERRNOS:
                    raise
            # no hard links here (exFAT, some network disks): check, then rename
            try:
                os.stat(name, dir_fd=sub, follow_symlinks=False)
                return False
            except FileNotFoundError:
                pass
            os.rename(tmp, name, src_dir_fd=sub, dst_dir_fd=sub)
            return True
        finally:
            try:
                os.unlink(tmp, dir_fd=sub)
            except OSError:
                pass
    finally:
        os.close(src)


def install(
    source_dir: Optional[str],
    pictures_dir: str,
    state_file: str,
    *,
    margin_bytes: int = MARGIN_BYTES,
    should_stop: Callable[[], bool] = lambda: False,
) -> Result:
    """Copy the pictures of *source_dir* that have not been dealt with into
    ``pictures_dir/sakkmesterke``, and write down which are. Never raises for what the file system
    does: the outcome is the ``Result``. See the module doc for the order."""
    packaged = _packaged(source_dir) if source_dir and os.path.isdir(source_dir) else None
    if packaged is None:
        _LOG.debug("[samples] no pictures in the package")
        return Result(NO_SOURCE)
    if not packaged:
        return Result(NOTHING_TO_DO)
    lock_fd: Optional[int] = None
    try:
        try:
            lock_fd, lock_held = _open_lock(state_file)
        except BlockingIOError:
            _LOG.debug("[samples] another start is copying")
            return Result(BUSY)
        return _install_locked(
            source_dir, pictures_dir, state_file, packaged, lock_held, margin_bytes, should_stop
        )
    except _Failed as failure:
        _LOG.warning("[samples] not copied: %s (%s)", failure.reason, failure.detail)
        return Result(failure.status, reason=failure.reason)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _install_locked(
    source_dir: str,
    pictures_dir: str,
    state_file: str,
    packaged: List[Tuple[str, int]],
    lock_held: bool,
    margin_bytes: int,
    should_stop: Callable[[], bool],
) -> Result:
    handled = _load_state(state_file)
    names = {name for name, _ in packaged}
    pending = [(name, size) for name, size in packaged if name not in handled]
    result = Result(DONE, handled_before=len(names) - len(pending))
    if not pending:
        result.status = NOTHING_TO_DO
        return result

    need = sum(size for _, size in pending) + margin_bytes
    try:
        free = _free_bytes(pictures_dir)
    except OSError as exc:
        raise _Failed(ERROR, "space-unknown", _why(exc)) from exc
    if free < need:
        _LOG.warning(
            "[samples] not copied: not enough free space (need %d bytes, free %d)", need, free
        )
        result.status = NO_SPACE
        return result

    try:
        _save_state(state_file, handled)  # a state that cannot be written would copy at every login
    except OSError as exc:
        raise _Failed(ERROR, "state-unwritable", _why(exc)) from exc

    try:
        sub = _open_target(pictures_dir)
    except OSError as exc:
        raise _Failed(ERROR, "pictures-unwritable", _why(exc)) from exc
    try:
        if lock_held:
            _clean_orphans(sub, names)
        for name, _size in pending:
            if should_stop():
                result.status = STOPPED
                break
            try:
                if _copy_one(source_dir, name, sub, should_stop):
                    result.copied += 1
                else:
                    result.already_there += 1
            except _Stopped:
                result.status = STOPPED
                break
            except OSError as exc:
                status = NO_SPACE if exc.errno in _NO_SPACE_ERRNOS else ERROR
                raise _Failed(status, "copy-failed", _why(exc)) from exc
            handled.add(name)
            try:
                _save_state(state_file, handled)
            except OSError as exc:
                raise _Failed(ERROR, "state-unwritable", _why(exc)) from exc
    finally:
        os.close(sub)
    _LOG.info(
        "[samples] copied %d, already there %d, dealt with before %d",
        result.copied,
        result.already_there,
        result.handled_before,
    )
    return result
