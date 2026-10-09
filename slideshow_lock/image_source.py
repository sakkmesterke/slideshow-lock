# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""Image source for the slideshow: folder walk and live folder watching (CORE-4).

A GUI-independent module. It turns the configured picture folder (the CORE-3
``picture-folder`` setting) into an ordered, always-current queue of displayable
images. It does no drawing and no scaling (that is CORE-2).

Behaviour in short:

* The folder is walked recursively, breadth first. Directories are tracked by
  ``(st_dev, st_ino)``, so a symlink loop, or a directory reachable through two
  links, is walked once and every image is listed exactly once.
* The walk runs in small time-boxed steps driven by the GLib main loop, so a
  huge tree never freezes startup: the first image is available after the first
  step, long before the walk finishes.
* Every walked directory gets one directory monitor (``Gio.FileMonitor``, base
  repo only, D2). Copied-in or deleted images, and created or removed
  subfolders, change the queue with no restart. The parent of the root is
  watched too, so a missing folder that appears later is picked up, and a
  deleted root becomes the defined empty state.
* If the image on screen is deleted, the cursor moves on to the image that
  followed it. If the last image is deleted, ``current()`` is ``None``: a
  logged ``[slideshow-dir]`` WARNING (brief 3.7), not an error.
* In random order the same image comes back only after at least ``REPEAT_GAP`` (3) other images
  (fewer in a folder of fewer than four: the largest distance there is); see ``advance``.
* Hidden files and folders (leading ``.``) are skipped, which also keeps
  partial-copy temp files (rsync, browsers) out of the queue.
* Non-image files are filtered by extension. A file that has an image extension
  but cannot be read, or whose header is not a known image header, is skipped
  with a WARNING. Header sniffing only: a file that is damaged deeper in is
  caught by the display layer (CORE-2), which must skip it the same way.
* Screenshots are left out unless ``show_screenshots`` is true (the "show-screenshots"
  setting, off by default): a folder named like a screenshot folder (``Screenshots`` and its
  translations) is not walked, and a file named like a screenshot is not listed. The test is on
  the *name* of a walked entry, nothing else: a link is judged by its own name, not by where it
  points, so the filter only ever takes entries out and never changes how the walk follows links
  (see ``is_screenshot_dir_name``).
* Nothing assumes the folder exists, the default folder included.
* Resource limits: at most ``max_directories`` folders are walked and at most
  ``max_watches`` get a monitor (module constants, constructor parameters). Past
  a limit, or when the OS refuses a watch, the rest is skipped or left
  unwatched and ONE summary WARNING says so when the walk completes.
* Log volume: one problem is one line, and after the first few per window only a
  count is logged, so a mass failure cannot flood the journal.

Symlinks are followed (a link to a folder elsewhere is a legitimate way to
include it). A symlinked *file* that points at an image already in the tree is
a separate entry, because it is a separate path.

The core logic is plain stdlib. GLib/Gio are imported lazily by the two default
backends at the bottom, so the logic is testable with fake backends.
"""

from __future__ import annotations

import collections
import enum
import logging
import os
import random
import stat
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

_LOG = logging.getLogger(__name__)

ORDER_RANDOM = "random"
ORDER_NAME = "name"

#: In random order the same picture comes back only after at least this many other pictures (fewer
#: when the folder has fewer, then as many as it has: the largest distance there is).
REPEAT_GAP = 3

#: Extensions treated as candidate images. Kept in one place so it can be
#: trimmed once the display layer's (CORE-2) real loader support is known.
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"})

#: Folder names (compared case-insensitively) that hold screenshots: the one GNOME Shell makes in
#: the pictures folder (``Pictures/Screenshots``), and its Hungarian name. The translation of the
#: running desktop is added at run time (``_gnome_shell_names``), so other languages are covered
#: where GNOME's own catalog is installed; a name that is in neither is not recognised.
SCREENSHOT_DIR_NAMES = frozenset({"screenshots", "képernyőképek"})

#: File name starts (compared case-insensitively) of a screenshot: GNOME ("Screenshot from ..."),
#: KDE Spectacle and others ("Screenshot_2026...", "Screenshot-..."), and the Hungarian word.
SCREENSHOT_NAME_PREFIXES = ("screenshot", "képernyőkép")

#: The folder of the sample pictures (``sample_pictures.SUBDIR``, a test compares them). Nothing
#: under it is ever taken for a screenshot. Not imported from there: that module imports this one.
SAMPLE_DIR_NAME = "trensoft"

#: Work budget of one scan step. The scan hands control back to the main loop
#: after this long, so input and monitor events stay responsive during a walk.
DEFAULT_STEP_BUDGET_SECONDS = 0.008

#: Upper bound on walked folders. Past it the remaining folders are not walked.
DEFAULT_MAX_DIRECTORIES = 10_000

#: Upper bound on folders that get a directory monitor (one inotify watch each).
#: The per-user kernel limit is shared with the rest of the session, so this is
#: deliberately well below the common ``fs.inotify.max_user_watches`` values.
DEFAULT_MAX_WATCHES = 2_048

#: Per problem category, the first LOG_FIRST_N messages in a LOG_WINDOW_SECONDS
#: window are logged one by one; the rest are only counted, in one summary line.
LOG_FIRST_N = 10
LOG_WINDOW_SECONDS = 60.0


class FsEvent(enum.Enum):
    """The three kinds of filesystem change the source reacts to."""

    CREATED = "created"  # also: moved into the watched folder
    DELETED = "deleted"  # also: moved out of the watched folder
    CHANGES_DONE = "changes-done"  # a writer finished (file is complete)


EventCallback = Callable[[str, FsEvent], None]
#: ``watcher(path, callback) -> cancel``. *callback* receives
#: ``(changed_path, event)``. A folder that cannot be watched is reported by
#: raising ``OSError`` (or returning ``None``); the caller does the logging.
WatcherFactory = Callable[[str, EventCallback], Optional[Callable[[], None]]]
#: ``scheduler(step) -> cancel``. *step* is called repeatedly until it returns
#: False.
Scheduler = Callable[[Callable[[], bool]], Callable[[], None]]


def probe_image(path: str) -> str:
    """Raise if *path* is not a readable file with a known image header; else its format name.

    ``OSError`` means unreadable, ``ValueError`` means not a regular file, empty,
    or not a known image format. Opened ``O_NONBLOCK`` and checked with ``fstat``
    on the open descriptor, so a file swapped for a FIFO after the directory
    listing cannot hang the walk (TOCTOU). A stuck network mount can still block
    ``open()`` in the kernel, and the same goes for the directory listing (``os.scandir``)
    and the ``stat`` calls of the walk; that is not something this call can prevent, and the
    tests of the step budget do not see it (they measure CPU time and a fake clock).

    The name is the one gdk-pixbuf uses for the format (``"jpeg"``, ``"png"``, ``"gif"``,
    ``"bmp"``, ``"tiff"``, ``"webp"``), so a caller can tell what the header said without
    opening the file a second time.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        head = os.read(fd, 16)
    finally:
        os.close(fd)
    if not head:
        raise ValueError("empty file")
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    raise ValueError("not a recognised image header")


def _now() -> float:
    return time.monotonic()


class _BurstLog:
    """WARNING lines for one problem category, with a cap per time window.

    The first ``first`` messages of a window are logged normally; later ones are
    counted and reported as a single summary line (``flush``), so the journald
    rate limit is not spent on one mass failure.
    """

    def __init__(self, what: str) -> None:
        self._what = what
        self._window_start: Optional[float] = None
        self._emitted = 0
        self._suppressed = 0

    def warn(self, message: str, *args) -> None:
        self.log(logging.WARNING, message, *args)

    def log(self, level: int, message: str, *args) -> None:
        now = _now()
        if self._window_start is None or now - self._window_start >= LOG_WINDOW_SECONDS:
            self.flush()
            self._window_start = now
            self._emitted = 0
        if self._emitted < LOG_FIRST_N:
            self._emitted += 1
            _LOG.log(level, message, *args)
        else:
            self._suppressed += 1

    def reset(self) -> None:
        """Report what was counted and start a fresh window (new folder, new start)."""
        self.flush()
        self._window_start = None
        self._emitted = 0

    def flush(self) -> None:
        if self._suppressed:
            _LOG.warning(
                "[slideshow-dir] %d more %s not logged one by one (only the first %d per %d s are)",
                self._suppressed,
                self._what,
                LOG_FIRST_N,
                int(LOG_WINDOW_SECONDS),
            )
            self._suppressed = 0


def _kernel_watch_inodes() -> Optional[Set[Tuple[int, int]]]:
    """``(kernel_dev, ino)`` of every inotify watch this process holds, or None.

    Read from ``/proc/self/fdinfo``. Needed because Gio does not report a refused
    watch (``ENOSPC``, per-user ``fs.inotify.max_user_watches`` exhausted):
    ``monitor_directory()`` returns a monitor that never fires and no error is raised.
    None means the kernel could not be asked (not Linux, no /proc).
    """
    held: Set[Tuple[int, int]] = set()
    try:
        fds = os.listdir("/proc/self/fd")
    except OSError:
        return None
    for fd in fds:
        try:
            if os.readlink(f"/proc/self/fd/{fd}") != "anon_inode:inotify":
                continue
            with open(f"/proc/self/fdinfo/{fd}") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith("inotify wd:"):
                continue
            fields = dict(tok.split(":", 1) for tok in line.split()[1:] if ":" in tok)
            try:
                held.add((int(fields["sdev"], 16), int(fields["ino"], 16)))
            except (KeyError, ValueError):
                continue
    return held


#: Message ids of the start of a screenshot's file name in GNOME Shell's catalog: the one in its
#: source (js/ui/screenshot.js) and the lower case spelling the name has on disk.
_SHELL_FILE_MSGIDS = ("Screenshot From %s", "Screenshot from %s")


def _gnome_shell_names(
    translate: Optional[Callable[[str], str]] = None,
) -> Tuple[Set[str], Set[str]]:
    """``(folder names, file name prefixes)`` that GNOME Shell uses for screenshots in the
    language of this session, found in its own catalog (text domain ``gnome-shell``), lower case.

    GNOME Shell translates the name of its screenshot folder and the start of a file name
    ("Screenshot From %s"). Where the catalog is not installed, or has no such string, nothing is
    added: the English text comes back, which is already known. Never raises. *translate* is for
    the tests; by default it is the ``gnome-shell`` catalog of the session's language.
    """
    if translate is None:
        import gettext

        translate = gettext.translation("gnome-shell", fallback=True).gettext
    folders: Set[str] = set()
    prefixes: Set[str] = set()
    try:
        folder = translate("Screenshots").strip().casefold()
        if folder and os.sep not in folder:
            folders.add(folder)
        # gettext matches the message id exactly, so both spellings are asked (see above).
        for msgid in _SHELL_FILE_MSGIDS:
            head = translate(msgid).split("%s", 1)[0].strip().casefold()
            if len(head) >= 4:  # "%s" first, or a stub, would hide every picture
                prefixes.add(head)
    except Exception as exc:  # a broken catalog must not stop the slideshow
        _LOG.debug("[slideshow-dir] no GNOME screenshot names (%s)", exc)
    return folders, prefixes


_DESKTOP_NAMES: Optional[Tuple[Set[str], Set[str]]] = None


def _desktop_names() -> Tuple[Set[str], Set[str]]:
    """``_gnome_shell_names()`` for this process, read once (the language does not change)."""
    global _DESKTOP_NAMES
    if _DESKTOP_NAMES is None:
        _DESKTOP_NAMES = _gnome_shell_names()
    return _DESKTOP_NAMES


def is_screenshot_dir_name(name: str) -> bool:
    """True if a folder called *name* is a screenshot folder. The name only, never the target of a
    link of that name and never the path."""
    key = name.casefold()
    return key in SCREENSHOT_DIR_NAMES or key in _desktop_names()[0]


def is_screenshot_file_name(name: str) -> bool:
    """True if a file called *name* starts like a screenshot's name (see
    ``SCREENSHOT_NAME_PREFIXES``). The name only."""
    key = name.casefold()
    return key.startswith(SCREENSHOT_NAME_PREFIXES + tuple(sorted(_desktop_names()[1])))


def _has_image_extension(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _root_is_exempt(root: str) -> bool:
    """True if the filter must not touch this root: the root or one of its parents is a screenshot
    folder or the sample pictures' folder. Whoever picked such a folder wants what is in it."""
    return any(
        part == SAMPLE_DIR_NAME or is_screenshot_dir_name(part)
        for part in root.split(os.sep)
        if part
    )


def _name_key(path: str) -> Tuple[str, str]:
    return (path.casefold(), path)


class ImageSource:
    """Ordered, live queue of the displayable images under one folder.

    Single-threaded: all methods and all callbacks run on the GLib main loop.
    """

    def __init__(
        self,
        folder: str,
        *,
        order: str = ORDER_RANDOM,
        show_screenshots: bool = False,
        probe: Callable[[str], Any] = probe_image,
        watcher: Optional[WatcherFactory] = None,
        scheduler: Optional[Scheduler] = None,
        rng: Optional[random.Random] = None,
        step_budget_seconds: float = DEFAULT_STEP_BUDGET_SECONDS,
        max_directories: int = DEFAULT_MAX_DIRECTORIES,
        max_watches: int = DEFAULT_MAX_WATCHES,
        verify_watches: Optional[bool] = None,
    ) -> None:
        self._check_order(order)
        self._root = os.path.abspath(folder)
        self._order = order
        self._show_screenshots = bool(show_screenshots)
        self._root_exempt = _root_is_exempt(self._root)
        self._probe = probe
        self._watcher = watcher if watcher is not None else gio_directory_watcher
        self._scheduler = scheduler if scheduler is not None else glib_idle_scheduler
        self._rng = rng if rng is not None else random.Random()
        self._budget = step_budget_seconds
        self._max_dirs = max_directories
        self._max_watches = max_watches
        # Asking the kernel which watches exist only makes sense for the real Gio watcher.
        self._verify_watches = (watcher is None) if verify_watches is None else verify_watches

        self._running = False
        self._tree_active = False
        self._gen = 0  # bumped on every (re)start; late callbacks from old watches are dropped

        self._files: Set[str] = set()
        self._play: List[str] = []  # play order
        self._pos = 0  # index of the current image in _play
        self._recent: Deque[str] = collections.deque(maxlen=REPEAT_GAP)  # the last pictures shown
        self._empty_logged = False
        self._listeners: List[Callable[[Optional[str]], None]] = []

        self._pending: Deque[str] = collections.deque()  # directories still to list
        self._buffer: Deque[os.DirEntry] = collections.deque()  # entries of the dir being walked
        self._visited: Dict[Tuple[int, int], str] = {}  # (st_dev, st_ino) -> path that owns it
        self._dir_keys: Dict[str, Tuple[int, int]] = {}
        self._watches: Dict[str, Callable[[], None]] = {}
        self._sched_cancel: Optional[Callable[[], None]] = None
        self._ancestor: Optional[str] = None
        self._ancestor_cancel: Optional[Callable[[], None]] = None
        self._root_key: Optional[Tuple[int, int]] = None
        self._ancestor_failed: Optional[str] = None

        self._dirs_skipped = 0  # folders not walked because of max_directories
        self._unwatched = 0  # walked folders without a monitor (limit or OS refusal)
        self._watch_error: Optional[str] = None  # first reason the OS refused a watch
        self._unconfirmed: Set[str] = set()  # watched folders the kernel does not list
        self._kernel_unconfirmed_all = False  # not one of our watches could be matched
        self._limits_reported = False
        self._skipped_images_log = _BurstLog("unreadable or corrupt images")
        self._unreadable_dirs_log = _BurstLog("unreadable folders")
        self._limits_log = _BurstLog("folders past the limits")  # after the summary was logged

    # -- public API ----------------------------------------------------------

    @property
    def folder(self) -> str:
        return self._root

    @property
    def order(self) -> str:
        return self._order

    @property
    def show_screenshots(self) -> bool:
        return self._show_screenshots

    @property
    def scan_complete(self) -> bool:
        """True when no directory is left to walk (live watching continues)."""
        return not self._pending and not self._buffer

    def start(self) -> None:
        """Begin walking and watching. Safe to call when the folder is missing."""
        if self._running:
            return
        self._running = True
        self._gen += 1
        self._reconcile_root()

    def stop(self) -> None:
        """Tear everything down: cancel the walk and every monitor. No callbacks fire."""
        self._running = False
        self._gen += 1
        self._stop_tree(notify=False)
        self._unwatch_ancestor()
        for burst in (self._skipped_images_log, self._unreadable_dirs_log, self._limits_log):
            burst.reset()  # a new folder or start gets a fresh window

    def set_folder(self, folder: str) -> None:
        """Switch to another folder at runtime (live settings reload)."""
        root = os.path.abspath(folder)
        if root == self._root:
            return

        def change() -> None:
            self._root = root
            self._root_exempt = _root_is_exempt(root)

        self._rewalk(change)

    def set_show_screenshots(self, show: bool) -> None:
        """Show or leave out the screenshots at runtime (live settings reload): the folder is
        walked again, as for a new folder, because what is in the queue changes."""
        show = bool(show)
        if show == self._show_screenshots:
            return

        def change() -> None:
            self._show_screenshots = show

        self._rewalk(change)

    def _rewalk(self, change: Callable[[], None]) -> None:
        """Stop, apply *change*, and start again if it was running."""
        was_running = self._running
        old = self.current()
        self.stop()
        change()
        self._empty_logged = False
        if old is not None:
            self._notify(None)
        if was_running:
            self.start()

    def set_order(self, order: str) -> None:
        """Switch between ``"random"`` and ``"name"`` at runtime."""
        self._check_order(order)
        if order == self._order:
            return
        self._order = order
        old = self.current()
        self._play = list(self._files)
        if order == ORDER_NAME:
            self._play.sort(key=_name_key)
            self._pos = self._play.index(old) if old is not None else 0
        else:
            self._rng.shuffle(self._play)
            if old is not None:
                # keep what is on screen first, the rest in fresh random order
                self._play.remove(old)
                self._play.insert(0, old)
            self._pos = 0

    def current(self) -> Optional[str]:
        """Path of the image that should be on screen, or ``None`` if there is none."""
        return self._play[self._pos] if self._play else None

    def advance(self) -> Optional[str]:
        """Step to the next image and return it (``None`` if the queue is empty).

        Never raises on an empty or changing queue. Not reported to
        ``connect_current_changed`` listeners: the caller asked for the move.
        """
        if not self._play:
            return None
        self._remember(self._play[self._pos])
        self._pos += 1
        if self._pos >= len(self._play):
            self._pos = 0
            if self._order == ORDER_RANDOM and len(self._play) > 1:
                self._rng.shuffle(self._play)
        if self._order == ORDER_RANDOM:
            self._keep_apart()
        return self._play[self._pos]

    def _remember(self, path: str) -> None:
        """Note *path* as the latest picture shown (the one the cursor leaves)."""
        if not self._recent or self._recent[-1] != path:
            self._recent.append(path)

    def _keep_apart(self) -> None:
        """In random order, do not let the picture at the cursor be one of the last ``REPEAT_GAP``
        shown: swap it with another picture that is not (one later in the cycle if there is one,
        else an earlier one), so at least ``REPEAT_GAP`` other pictures come between two showings
        of the same one. A folder with ``n`` pictures keeps ``n - 1`` of them apart when ``n`` is
        smaller (the largest distance there is; one picture is shown again and again). There is
        always a picture to swap with: the window is smaller than the folder."""
        gap = min(REPEAT_GAP, len(self._play) - 1)
        window = set(list(self._recent)[-gap:]) if gap > 0 else set()
        if self._play[self._pos] not in window:
            return
        later = [i for i in range(self._pos + 1, len(self._play)) if self._play[i] not in window]
        earlier = [i for i in range(self._pos) if self._play[i] not in window]
        choices = later or earlier
        if not choices:  # cannot happen (see above); the picture stays
            return
        swap = self._rng.choice(choices)
        self._play[self._pos], self._play[swap] = self._play[swap], self._play[self._pos]

    def images(self) -> List[str]:
        """Snapshot of the queue in play order."""
        return list(self._play)

    def __len__(self) -> int:
        return len(self._play)

    def connect_current_changed(self, callback: Callable[[Optional[str]], None]) -> None:
        """Call *callback(path_or_None)* when the current image changes on its own.

        That happens when the first image shows up, when the image on screen is
        deleted (callback gets the image that follows it), and when the queue
        becomes empty (``None``). Not called for ``advance()``.
        """
        self._listeners.append(callback)

    # -- root / ancestor handling --------------------------------------------

    def _reconcile_root(self) -> None:
        """Bring the walk in line with whether the root folder exists right now.

        Idempotent; called on start and on every event about the root or one of
        its ancestors.
        """
        self._rewatch_ancestor()
        # checked after re-watching so a folder created in between is not missed
        key = self._stat_key(self._root)
        if key is not None and self._tree_active and key != self._root_key:
            self._stop_tree()  # the folder was replaced by a new one
        if key is not None and not self._tree_active:
            self._start_tree()
        elif key is None:
            was_active = self._tree_active
            self._stop_tree()
            if was_active or not self._empty_logged:
                self._empty_logged = True
                _LOG.warning(
                    "[slideshow-dir] picture folder %r does not exist, slideshow has nothing "
                    "to show, service keeps running and watches for the folder to appear",
                    self._root,
                )

    def _rewatch_ancestor(self) -> None:
        """Watch the nearest existing ancestor of the root (its parent, normally)."""
        nearest = os.path.dirname(self._root)
        while nearest and not os.path.isdir(nearest):
            parent = os.path.dirname(nearest)
            if parent == nearest:
                break
            nearest = parent
        if nearest == self._ancestor and self._ancestor_cancel is not None:
            return
        self._unwatch_ancestor()
        if not nearest or not os.path.isdir(nearest):
            return
        gen = self._gen
        try:
            cancel = self._watcher(nearest, lambda path, ev: self._on_ancestor_event(gen, path, ev))
        except OSError as exc:
            cancel = None
            if self._ancestor_failed != nearest:  # once per folder, not on every retry
                self._ancestor_failed = nearest
                _LOG.warning(
                    "[slideshow-dir] cannot watch %r (%s), a missing picture folder will not "
                    "be noticed when it appears",
                    nearest,
                    exc,
                )
        if cancel is not None:
            self._ancestor = nearest
            self._ancestor_cancel = cancel

    def _unwatch_ancestor(self) -> None:
        if self._ancestor_cancel is not None:
            self._ancestor_cancel()
        self._ancestor = None
        self._ancestor_cancel = None

    def _on_ancestor_event(self, gen: int, path: str, event: FsEvent) -> None:
        if gen != self._gen:
            return
        if path == self._root and event is FsEvent.DELETED:
            self._root_deleted()
        elif path == self._root or self._root.startswith(path + os.sep):
            self._reconcile_root()

    def _root_deleted(self) -> None:
        """The root's own path was deleted: forget the whole tree, then look again.

        Unconditional on purpose. Comparing inodes is not enough: a deleted and
        re-created folder can get the same inode number back, and the stale tree
        would then be kept.
        """
        self._stop_tree()
        self._empty_logged = False  # so the "does not exist" line is not lost
        self._reconcile_root()

    def _start_tree(self) -> None:
        self._tree_active = True
        self._empty_logged = False
        self._dirs_skipped = 0
        self._unwatched = 0
        self._watch_error = None
        self._unconfirmed = set()
        self._kernel_unconfirmed_all = False
        self._limits_reported = False
        if self._claim_dir(self._root):
            self._root_key = self._dir_keys[self._root]
            self._pending.append(self._root)
        self._ensure_scanning()

    def _stop_tree(self, *, notify: bool = True) -> None:
        """Forget everything about the walked tree (monitors, walk state, images)."""
        if self._sched_cancel is not None:
            self._sched_cancel()
            self._sched_cancel = None
        for cancel in self._watches.values():
            cancel()
        self._watches.clear()
        self._pending.clear()
        self._buffer.clear()
        self._visited.clear()
        self._dir_keys.clear()
        self._root_key = None
        self._tree_active = False
        if notify:
            self._remove_where(lambda _path: True)
        else:
            self._play = []
            self._files = set()
            self._pos = 0
        self._recent.clear()  # a new folder: its history names no picture of the old one

    # -- scan ------------------------------------------------------------------

    def _ensure_scanning(self) -> None:
        if self._sched_cancel is None and not self.scan_complete:
            self._sched_cancel = self._scheduler(self._run_step)

    def _run_step(self) -> bool:
        deadline = time.monotonic() + self._budget
        while self._work():
            if time.monotonic() >= deadline:
                return True
        self._sched_cancel = None
        _LOG.info(
            "[slideshow-dir] scan of %r complete: %d images in %d folders",
            self._root,
            len(self._files),
            len(self._dir_keys),
        )
        self._verify_kernel_watches()
        self._report_limits()
        self._skipped_images_log.flush()
        self._unreadable_dirs_log.flush()
        self._check_empty()
        return False

    def _verify_kernel_watches(self) -> None:
        """Compare our folders with the inotify watches the kernel really holds.

        Gio does not report a refused watch, so this is the only way to notice one.
        The monitors are NOT cancelled when a watch is missing: after a failed
        ``inotify_add_watch`` GLib keeps the subscription on a "missing" list and retries
        it every few seconds, so the folder may recover by itself.

        Best effort and conservative: no claim if the kernel cannot be asked. If none of
        our watches can be matched at all, that is reported as "could not confirm", not as
        a failure (it also happens on a filesystem whose inode numbers differ from what
        inotify reports).
        """
        if not self._verify_watches or not self._watches:
            return
        held = _kernel_watch_inodes()
        if held is None:
            return
        missing = set()
        matched = 0
        for path in self._watches:
            key = self._dir_keys.get(path)
            if key is None:
                continue
            kernel_dev = (os.major(key[0]) << 20) | os.minor(key[0])
            if (kernel_dev, key[1]) in held:
                matched += 1
            else:
                missing.add(path)
        self._kernel_unconfirmed_all = matched == 0
        if self._kernel_unconfirmed_all:
            return
        if self._limits_reported:  # a later wave: logged per folder, with the burst cap
            for path in sorted(missing - self._unconfirmed):
                self._limits_log.warn(
                    "[slideshow-dir] folder %r has no confirmed kernel watch (inotify limit?)",
                    path,
                )
        self._unconfirmed = missing

    def _report_limits(self) -> None:
        """ONE summary WARNING when a limit was hit or a watch could not be confirmed."""
        problem = (
            self._dirs_skipped
            or self._unwatched
            or self._unconfirmed
            or self._kernel_unconfirmed_all
        )
        if self._limits_reported or not problem:
            return
        self._limits_reported = True
        parts = []
        if self._dirs_skipped:
            parts.append(f"{self._dirs_skipped} folders were not walked (limit {self._max_dirs})")
        if self._unwatched:
            reason = f", first refusal: {self._watch_error}" if self._watch_error else ""
            parts.append(
                f"{self._unwatched} folders are not watched (limit {self._max_watches}{reason}), "
                "changes in them will not be noticed until restart"
            )
        if self._unconfirmed:
            parts.append(
                f"{len(self._unconfirmed)} folders have no confirmed kernel watch (the inotify "
                "limit was probably reached; GLib retries a refused watch every few seconds, so "
                "they may recover, but changes made in the meantime are missed)"
            )
        if self._kernel_unconfirmed_all:
            parts.append(
                "could not confirm that the kernel installed the watches (none of them showed "
                "up in /proc/self/fdinfo; if the inotify limit is used up, changes will not be "
                "noticed)"
            )
        _LOG.warning(
            "[slideshow-dir] folder limits or watch problems under %r: %s; service keeps running",
            self._root,
            "; ".join(parts),
        )

    def _work(self) -> bool:
        """Do one small unit of walk work (one entry or one directory listing)."""
        if self._buffer:
            self._handle_entry(self._buffer.popleft())
            return True
        if self._pending:
            self._list_dir(self._pending.popleft())
            return True
        return False

    def _list_dir(self, path: str) -> None:
        # monitor first, list second: nothing created in between is missed
        self._watch_dir(path)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except (FileNotFoundError, NotADirectoryError):
            _LOG.debug("[slideshow-dir] folder %r vanished before it was listed", path)
            return
        except OSError as exc:
            self._unreadable_dirs_log.warn(
                "[slideshow-dir] cannot read folder %r (%s), skipping it", path, exc
            )
            return
        entries.sort(key=lambda entry: entry.name)
        self._buffer = collections.deque(entries)

    def _handle_entry(self, entry: os.DirEntry) -> None:
        if entry.name.startswith("."):
            return
        try:
            if entry.is_dir():
                if not self._left_out(entry.path, is_dir=True) and self._claim_dir(entry.path):
                    self._pending.append(entry.path)
                return
            if not entry.is_file() or not _has_image_extension(entry.name):
                return
        except OSError:
            return
        if self._left_out(entry.path, is_dir=False):
            return
        self._consider_file(entry.path, final=True)

    def _left_out(self, path: str, *, is_dir: bool) -> bool:
        """True if *path* is a screenshot folder or file that the setting leaves out.

        Judged by the entry's own name only (a link by the name of the link), so the walk and the
        link rules are the same with the filter on or off: it can only take entries out. Never
        for the sample pictures' folder (``SAMPLE_DIR_NAME`` under the root), and never when the
        root is itself, or lies under, a screenshot folder: a folder chosen on purpose is shown.
        """
        if self._show_screenshots or self._root_exempt:
            return False
        if path.startswith(os.path.join(self._root, SAMPLE_DIR_NAME) + os.sep):
            return False
        name = os.path.basename(path)
        return is_screenshot_dir_name(name) if is_dir else is_screenshot_file_name(name)

    def _claim_dir(self, path: str) -> bool:
        """Register *path* as a walked directory; False if it must not be walked.

        This is the symlink-loop guard: a directory is identified by
        ``(st_dev, st_ino)``, not by its path. Also False past ``max_directories``.
        """
        key = self._stat_key(path)
        if key is None:
            return False
        owner = self._visited.get(key)
        if owner is not None and owner != path:
            if self._stat_key(owner) == key:
                _LOG.debug("[slideshow-dir] folder %r already walked as %r", path, owner)
                return False
            # The earlier path no longer leads to this directory: it was renamed and the
            # event for its new name arrived before the one for the old name.
            self._drop_subtree(owner, check_empty=False)
        if len(self._dir_keys) >= self._max_dirs:
            self._dirs_skipped += 1
            if self._limits_reported:  # the summary is out already: log this one separately
                self._limits_log.warn(
                    "[slideshow-dir] folder %r not walked: folder limit %d reached",
                    path,
                    self._max_dirs,
                )
            return False
        self._visited[key] = path
        self._dir_keys[path] = key
        return True

    @staticmethod
    def _stat_key(path: str) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None

    def _consider_file(self, path: str, *, final: bool) -> None:
        """Add *path* to the queue if it is a displayable image.

        *final* False means the file may still be being written: a failed check
        is only a DEBUG note, and the CHANGES_DONE event checks again.
        """
        if path in self._files:
            return
        try:
            self._probe(path)
        except FileNotFoundError:
            _LOG.debug("[slideshow-dir] image %r vanished before it was read", path)
            return
        except Exception as exc:  # unreadable, corrupt, or a probe bug: never crash the source
            if final:
                self._skipped_images_log.warn(
                    "[slideshow-dir] skipping unreadable or corrupt image %r (%s)", path, exc
                )
            else:
                _LOG.debug("[slideshow-dir] image %r not usable yet (%s)", path, exc)
            return
        self._insert(path)

    def _watch_dir(self, path: str) -> None:
        if path in self._watches:
            return
        if len(self._watches) >= self._max_watches:
            self._unwatched += 1
            self._later_unwatched(path, f"watch limit {self._max_watches} reached")
            return
        gen = self._gen
        try:
            cancel = self._watcher(path, lambda changed, ev: self._on_event(gen, changed, ev))
        except OSError as exc:
            cancel = None
            if self._watch_error is None:
                self._watch_error = str(exc)
            reason = str(exc)
        else:
            reason = "no monitor"
        if cancel is not None:
            self._watches[path] = cancel
        else:
            self._unwatched += 1  # reported once, in the summary
            self._later_unwatched(path, reason)

    def _later_unwatched(self, path: str, reason: str) -> None:
        if self._limits_reported:  # the summary is out already: log this one separately
            self._limits_log.warn("[slideshow-dir] folder %r is not watched (%s)", path, reason)

    # -- live changes -----------------------------------------------------------

    def _on_event(self, gen: int, path: str, event: FsEvent) -> None:
        if gen != self._gen or not self._tree_active:
            return
        if os.path.basename(path).startswith("."):
            return
        if event is FsEvent.DELETED:
            self._on_deleted(path)
        else:
            self._on_created(path, final=event is FsEvent.CHANGES_DONE)

    def _on_created(self, path: str, *, final: bool) -> None:
        if path == self._root:
            return
        if os.path.isdir(path):
            if (
                path not in self._dir_keys
                and not self._left_out(path, is_dir=True)
                and self._claim_dir(path)
            ):
                self._pending.append(path)
                self._ensure_scanning()
            return
        if (
            _has_image_extension(path)
            and os.path.isfile(path)
            and not self._left_out(path, is_dir=False)
        ):
            self._consider_file(path, final=final)

    def _on_deleted(self, path: str) -> None:
        if path == self._root:
            self._root_deleted()
        elif path in self._dir_keys:
            self._drop_subtree(path)
        elif path in self._files:
            self._remove_where(lambda p: p == path)
            self._check_empty()

    def _drop_subtree(self, path: str, *, check_empty: bool = True) -> None:
        prefix = path + os.sep
        for directory in [d for d in self._dir_keys if d == path or d.startswith(prefix)]:
            cancel = self._watches.pop(directory, None)
            if cancel is not None:
                cancel()
            key = self._dir_keys.pop(directory)
            if self._visited.get(key) == directory:
                del self._visited[key]
        self._remove_where(lambda p: p.startswith(prefix))
        if check_empty:
            self._check_empty()

    # -- queue maintenance --------------------------------------------------------

    def _insert(self, path: str) -> None:
        old = self.current()
        self._files.add(path)
        if not self._play:
            self._play.append(path)
            self._pos = 0
        elif self._order == ORDER_NAME:
            idx = self._name_index(path)
            self._play.insert(idx, path)
            if idx <= self._pos:
                self._pos += 1  # the image on screen moved one slot right
        else:
            # random: land somewhere after the cursor, so it is shown this cycle
            idx = self._rng.randint(self._pos + 1, len(self._play))
            self._play.insert(idx, path)
        self._empty_logged = False
        if old is None:
            self._notify(self.current())

    def _name_index(self, path: str) -> int:
        key = _name_key(path)
        lo, hi = 0, len(self._play)
        while lo < hi:
            mid = (lo + hi) // 2
            if _name_key(self._play[mid]) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _remove_where(self, predicate: Callable[[str], bool]) -> None:
        """Drop every image matching *predicate*; if the current one goes, move on."""
        old = self.current()
        survivors: List[str] = []
        new_pos = 0  # survivors before the cursor = index of the current image, or of its successor
        for i, path in enumerate(self._play):
            if predicate(path):
                self._files.discard(path)
                continue
            survivors.append(path)
            if i < self._pos:
                new_pos += 1
        if len(survivors) == len(self._play):
            return
        self._play = survivors
        self._pos = new_pos if new_pos < len(survivors) else 0
        if self._order == ORDER_RANDOM and self._play and self.current() != old:
            self._keep_apart()  # the successor takes the cursor's place: same rule as in advance
        new = self.current()
        if new != old:
            self._notify(new)

    def _check_empty(self) -> None:
        """Log the defined empty state once, when the queue is empty and the walk is done."""
        if self._play or not self._tree_active or not self.scan_complete or self._empty_logged:
            return
        self._empty_logged = True
        _LOG.warning(
            "[slideshow-dir] no displayable images in %r, slideshow has nothing to show, "
            "service keeps running",
            self._root,
        )

    def _notify(self, path: Optional[str]) -> None:
        for callback in list(self._listeners):
            try:
                callback(path)
            except Exception:
                _LOG.exception("[slideshow-dir] current-image listener failed")

    @staticmethod
    def _check_order(order: str) -> None:
        if order not in (ORDER_RANDOM, ORDER_NAME):
            raise ValueError(f"order must be {ORDER_RANDOM!r} or {ORDER_NAME!r}, got {order!r}")


# -- settings wiring (CORE-3) ---------------------------------------------------


def source_from_settings(settings, **kwargs) -> ImageSource:
    """Build an ``ImageSource`` from a ``Settings`` and keep it in step with it.

    The returned source is not started. Changing the ``picture-folder`` or
    ``order`` key later moves the source over without a restart.
    """
    from slideshow_lock.settings import KEY_ORDER, KEY_PICTURE_FOLDER, KEY_SHOW_SCREENSHOTS

    source = ImageSource(
        settings.get_picture_folder(),
        order=settings.get_order(),
        show_screenshots=settings.get_show_screenshots(),
        **kwargs,
    )

    def on_changed(key: str) -> None:
        if key == KEY_PICTURE_FOLDER:
            source.set_folder(settings.get_picture_folder())
        elif key == KEY_ORDER:
            source.set_order(settings.get_order())
        elif key == KEY_SHOW_SCREENSHOTS:
            source.set_show_screenshots(settings.get_show_screenshots())

    settings.connect_changed(on_changed)
    return source


# -- default backends (GLib / Gio, imported lazily) ------------------------------


def glib_idle_scheduler(step: Callable[[], bool]) -> Callable[[], None]:
    """Run *step* from the GLib main loop whenever it is idle, until it returns False."""
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    state = {"id": 0}

    def run() -> bool:
        if step():
            return GLib.SOURCE_CONTINUE
        state["id"] = 0
        return GLib.SOURCE_REMOVE

    state["id"] = GLib.idle_add(run)

    def cancel() -> None:
        if state["id"]:
            GLib.source_remove(state["id"])
            state["id"] = 0

    return cancel


def gio_directory_watcher(path: str, callback: EventCallback) -> Optional[Callable[[], None]]:
    """Watch one directory (not recursive) with ``Gio.FileMonitor``.

    Raises ``OSError`` if the monitor cannot be created; the caller logs it.
    """
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib

    event = Gio.FileMonitorEvent
    created = {event.CREATED, event.MOVED_IN}
    deleted = {event.DELETED, event.MOVED_OUT}
    try:
        monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.NONE, None)
    except GLib.Error as exc:
        raise OSError(f"cannot create a monitor for {path!r}: {exc.message}") from exc

    def on_changed(_monitor, file, other_file, event_type) -> None:
        changed = file.get_path() if file is not None else None
        if changed is None:
            return
        if event_type in created:
            callback(changed, FsEvent.CREATED)
        elif event_type in deleted:
            callback(changed, FsEvent.DELETED)
        elif event_type == event.CHANGES_DONE_HINT:
            callback(changed, FsEvent.CHANGES_DONE)

    monitor.connect("changed", on_changed)
    return monitor.cancel
