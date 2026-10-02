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
* Hidden files and folders (leading ``.``) are skipped, which also keeps
  partial-copy temp files (rsync, browsers) out of the queue.
* Non-image files are filtered by extension. A file that has an image extension
  but cannot be read, or whose header is not a known image header, is skipped
  with a WARNING. Header sniffing only: a file that is damaged deeper in is
  caught by the display layer (CORE-2), which must skip it the same way.
* Nothing assumes the folder exists, the default folder included.

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
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

_LOG = logging.getLogger(__name__)

ORDER_RANDOM = "random"
ORDER_NAME = "name"

#: Extensions treated as candidate images. Kept in one place so it can be
#: trimmed once the display layer's (CORE-2) real loader support is known.
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"})

#: Work budget of one scan step. The scan hands control back to the main loop
#: after this long, so input and monitor events stay responsive during a walk.
DEFAULT_STEP_BUDGET_SECONDS = 0.008


class FsEvent(enum.Enum):
    """The three kinds of filesystem change the source reacts to."""

    CREATED = "created"  # also: moved into the watched folder
    DELETED = "deleted"  # also: moved out of the watched folder
    CHANGES_DONE = "changes-done"  # a writer finished (file is complete)


EventCallback = Callable[[str, FsEvent], None]
#: ``watcher(path, callback) -> cancel`` or ``None`` if the folder cannot be
#: watched. *callback* receives ``(changed_path, event)``.
WatcherFactory = Callable[[str, EventCallback], Optional[Callable[[], None]]]
#: ``scheduler(step) -> cancel``. *step* is called repeatedly until it returns
#: False.
Scheduler = Callable[[Callable[[], bool]], Callable[[], None]]


def probe_image(path: str) -> None:
    """Raise if *path* is not a readable file with a known image header.

    ``OSError`` means unreadable, ``ValueError`` means the header is empty or not a
    known image format.
    """
    with open(path, "rb") as fh:
        head = fh.read(16)
    if not head:
        raise ValueError("empty file")
    if not (
        head.startswith(b"\xff\xd8\xff")  # JPEG
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or head.startswith(b"BM")
        or head.startswith((b"II*\x00", b"MM\x00*"))  # TIFF
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    ):
        raise ValueError("not a recognised image header")


def _has_image_extension(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


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
        probe: Callable[[str], None] = probe_image,
        watcher: Optional[WatcherFactory] = None,
        scheduler: Optional[Scheduler] = None,
        rng: Optional[random.Random] = None,
        step_budget_seconds: float = DEFAULT_STEP_BUDGET_SECONDS,
    ) -> None:
        self._check_order(order)
        self._root = os.path.abspath(folder)
        self._order = order
        self._probe = probe
        self._watcher = watcher if watcher is not None else gio_directory_watcher
        self._scheduler = scheduler if scheduler is not None else glib_idle_scheduler
        self._rng = rng if rng is not None else random.Random()
        self._budget = step_budget_seconds

        self._running = False
        self._tree_active = False
        self._gen = 0  # bumped on every (re)start; late callbacks from old watches are dropped

        self._files: Set[str] = set()
        self._play: List[str] = []  # play order
        self._pos = 0  # index of the current image in _play
        self._empty_logged = False
        self._listeners: List[Callable[[Optional[str]], None]] = []

        self._pending: Deque[str] = collections.deque()  # directories still to list
        self._buffer: Deque[os.DirEntry] = collections.deque()  # entries of the dir being walked
        self._visited: Set[Tuple[int, int]] = set()  # (st_dev, st_ino) of claimed directories
        self._dir_keys: Dict[str, Tuple[int, int]] = {}
        self._watches: Dict[str, Callable[[], None]] = {}
        self._sched_cancel: Optional[Callable[[], None]] = None
        self._ancestor: Optional[str] = None
        self._ancestor_cancel: Optional[Callable[[], None]] = None
        self._root_key: Optional[Tuple[int, int]] = None

    # -- public API ----------------------------------------------------------

    @property
    def folder(self) -> str:
        return self._root

    @property
    def order(self) -> str:
        return self._order

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

    def set_folder(self, folder: str) -> None:
        """Switch to another folder at runtime (live settings reload)."""
        root = os.path.abspath(folder)
        if root == self._root:
            return
        was_running = self._running
        old = self.current()
        self.stop()
        self._root = root
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
        self._pos += 1
        if self._pos >= len(self._play):
            self._pos = 0
            if self._order == ORDER_RANDOM and len(self._play) > 1:
                last = self._play[-1]
                self._rng.shuffle(self._play)
                if self._play[0] == last:  # do not show the same image twice in a row
                    swap = self._rng.randrange(1, len(self._play))
                    self._play[0], self._play[swap] = self._play[swap], self._play[0]
        return self._play[self._pos]

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
        cancel = self._watcher(nearest, lambda path, ev: self._on_ancestor_event(gen, path))
        if cancel is not None:
            self._ancestor = nearest
            self._ancestor_cancel = cancel

    def _unwatch_ancestor(self) -> None:
        if self._ancestor_cancel is not None:
            self._ancestor_cancel()
        self._ancestor = None
        self._ancestor_cancel = None

    def _on_ancestor_event(self, gen: int, path: str) -> None:
        if gen != self._gen:
            return
        if path == self._root or self._root.startswith(path + os.sep):
            self._reconcile_root()

    def _start_tree(self) -> None:
        self._tree_active = True
        self._empty_logged = False
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
        self._check_empty()
        return False

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
            _LOG.warning("[slideshow-dir] cannot read folder %r (%s), skipping it", path, exc)
            return
        entries.sort(key=lambda entry: entry.name)
        self._buffer = collections.deque(entries)

    def _handle_entry(self, entry: os.DirEntry) -> None:
        if entry.name.startswith("."):
            return
        try:
            if entry.is_dir():
                if self._claim_dir(entry.path):
                    self._pending.append(entry.path)
                return
            if not entry.is_file() or not _has_image_extension(entry.name):
                return
        except OSError:
            return
        self._consider_file(entry.path, final=True)

    def _claim_dir(self, path: str) -> bool:
        """Register *path* as a walked directory; False if it was already walked.

        This is the symlink-loop guard: a directory is identified by
        ``(st_dev, st_ino)``, not by its path.
        """
        key = self._stat_key(path)
        if key is None:
            return False
        if key in self._visited:
            _LOG.debug("[slideshow-dir] folder %r already walked (link loop or duplicate)", path)
            return False
        self._visited.add(key)
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
            _LOG.log(
                logging.WARNING if final else logging.DEBUG,
                "[slideshow-dir] skipping unreadable or corrupt image %r (%s)",
                path,
                exc,
            )
            return
        self._insert(path)

    def _watch_dir(self, path: str) -> None:
        if path in self._watches:
            return
        gen = self._gen
        cancel = self._watcher(path, lambda changed, ev: self._on_event(gen, changed, ev))
        if cancel is not None:
            self._watches[path] = cancel
        else:
            _LOG.warning(
                "[slideshow-dir] cannot watch folder %r, changes in it will not be noticed", path
            )

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
            if path not in self._dir_keys and self._claim_dir(path):
                self._pending.append(path)
                self._ensure_scanning()
            return
        if _has_image_extension(path) and os.path.isfile(path):
            self._consider_file(path, final=final)

    def _on_deleted(self, path: str) -> None:
        if path == self._root:
            self._reconcile_root()
        elif path in self._dir_keys:
            self._drop_subtree(path)
        elif path in self._files:
            self._remove_where(lambda p: p == path)
            self._check_empty()

    def _drop_subtree(self, path: str) -> None:
        prefix = path + os.sep
        for directory in [d for d in self._dir_keys if d == path or d.startswith(prefix)]:
            cancel = self._watches.pop(directory, None)
            if cancel is not None:
                cancel()
            self._visited.discard(self._dir_keys.pop(directory))
        self._remove_where(lambda p: p.startswith(prefix))
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
    from slideshow_lock.settings import KEY_ORDER, KEY_PICTURE_FOLDER

    source = ImageSource(settings.get_picture_folder(), order=settings.get_order(), **kwargs)

    def on_changed(key: str) -> None:
        if key == KEY_PICTURE_FOLDER:
            source.set_folder(settings.get_picture_folder())
        elif key == KEY_ORDER:
            source.set_order(settings.get_order())

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
    """Watch one directory (not recursive) with ``Gio.FileMonitor``."""
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
        _LOG.warning("[slideshow-dir] cannot create a monitor for %r (%s)", path, exc)
        return None

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
