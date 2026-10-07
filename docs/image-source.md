# Image source (CORE-4)

Module: `slideshow_lock/image_source.py`. No GUI, no drawing, no scaling (CORE-2).
It turns the `picture-folder` setting into an ordered, always-current queue of
displayable images.

## API

```python
source = ImageSource(folder, order="random" | "name")  # or source_from_settings(settings)
source.connect_current_changed(callback)  # callback(path_or_None)
source.start()
source.current()  # path to show now, or None (empty state)
source.advance()  # next image, or None when empty; never raises
source.images()  # snapshot of the queue in play order
source.set_folder(path)
source.set_order(order)  # live settings changes
source.stop()
```

`connect_current_changed` fires when the first image shows up, when the image on
screen is deleted (the argument is the image that followed it) and when the queue
becomes empty (`None`). It does not fire for `advance()`.

## Behaviour

- **Walk.** Recursive, breadth first, in short time-boxed steps run from the GLib
  main loop (8 ms budget per step), so a huge tree never blocks startup or input:
  the first image is available within the first few steps. A step is cut off after
  its budget, but one unit of work is never interrupted: listing one directory
  (very large folders are the worst case) and reading one file header. Reading the
  header opens the file with `O_NONBLOCK` and checks `fstat`, so a file swapped for
  a FIFO cannot hang the walk; but a hung network mount can still block `open()`
  or `scandir()` in the kernel, and then the whole main loop stands still. Nothing
  in this module can prevent that (CORE-1 has its own condition for it). The tests of
  the step budget guard the cost budget only: they measure the CPU time of the walking
  thread and, on a fake clock, how much work a step does; they do not see a blocking
  call or a sleep (a step that waits is not busy, and a wall-clock threshold does not
  tell it from a loaded machine; QA measured that a step with a 150 to 500 ms sleep passes
  them). That is covered by a hand measurement on a real slow mount, not by a test.
- **Symlink loops.** Directories are identified by `(st_dev, st_ino)`. A loop, or a
  folder reachable through two links, is walked once. Symlinks are followed, so
  linking in a folder from elsewhere works. A symlinked *file* is its own entry.
  Two limits of that rule:
  - A folder reachable by two paths is shown under whichever path the walk meets
    first (the "winner"). If the winner link later disappears, the images that are
    only reachable by the other path stay missing until the source is restarted:
    the other path was skipped as a duplicate and is not rewalked.
  - A symlink inside the picture folder that leads out of it is followed, so
    content from outside the folder (any folder the user can read) can be shown,
    on the locked screen too. Only put links there that you are happy to have
    displayed. Nothing is filtered by where a link points.
- **Live changes.** One non-recursive `Gio.FileMonitor` per walked directory. The
  parent of the root folder is watched as well: a missing folder that appears later
  is picked up, a deleted root becomes the empty state, a replaced root is rewalked.
  A renamed subfolder keeps its images whichever of the delete and create events
  arrives first. A deleted root is always rewalked from scratch, even if a new
  folder got the old inode number back.
- **Limits.** At most `DEFAULT_MAX_DIRECTORIES` (10000) folders are walked and
  `DEFAULT_MAX_WATCHES` (2048) get a monitor (constructor parameters
  `max_directories` / `max_watches`; module constants, not settings). Folders past
  the walk limit are skipped; folders past the watch limit, or refused by the OS
  with an error, are walked once but unwatched: their later changes go unnoticed
  until restart. ONE summary WARNING (`folder limits or watch problems`) is logged
  when the walk completes, with the counts and the first error. Folders that hit a
  limit after that summary are logged one by one (first 10 per window, then
  counted), not only counted.
- **Kernel watch exhaustion.** The per-user kernel limit
  `fs.inotify.max_user_watches` is shared with the whole session, hence the low
  default. **Gio does not report a refused watch**: `monitor_directory()` succeeds,
  there is no `GLib.Error`, and the monitor does not fire (measured on a real
  kernel, see the manual trial below). The source therefore asks the kernel which
  watches it holds (`/proc/self/fdinfo`, Linux only) when a walk completes:
  - Folders with no matching kernel watch are reported as "no confirmed kernel
    watch", and their monitors are **kept**. After a failed `inotify_add_watch`
    GLib puts the subscription on a "missing" list and retries it every few seconds
    (`inotify-helper.c`, `inotify-missing.c`, GLib 2.80), so such a folder **may
    recover by itself**; changes made before that are missed, and the source does
    not look again until the next walk.
  - If we hold watches but none of them can be matched (the common case when another
    application used up the limit before we started), the single WARNING says
    "could not confirm that the kernel installed the watches". That is an
    uncertainty, not a failure report: it also happens on a filesystem whose inode
    numbers differ from what inotify lists.
  - No claim at all if `/proc` cannot be read.
  - Known gaps: the watch on the root's parent folder (the one that notices a
    missing root appearing) is not part of the check. On a tree that crosses
    filesystems with different device numbering (for example a btrfs subvolume) a
    valid watch can be counted as missing; the target (RHEL 10, XFS) is not
    affected. A second exhaustion wave after the walk (more applications taking
    watches later) is not noticed.
- **Log volume.** One problem is one line. For unreadable or corrupt images and
  unreadable folders the first 10 lines per 60 s window are logged one by one; the
  rest are counted and reported in one "N more ... not logged one by one" line
  (at the end of the walk, and when the next window opens), so a mass failure does
  not eat the journald rate limit.
- **Deleting what is on screen.** The cursor moves to the image that followed it
  (wrapping to the start). Deleting the last image gives `current() is None` and a
  `[slideshow-dir]` WARNING, per brief 3.7; it is not an error, and the source
  recovers when an image returns.
- **Which files count.** Extension in `IMAGE_EXTENSIONS` (case-insensitive), not
  hidden (no leading `.`), a regular file or a link to one, and a readable file
  whose first bytes are a known image header (JPEG, PNG, GIF, BMP, TIFF, WebP).
  Anything else with an image extension is skipped with a WARNING naming the file.
  A file still being copied is re-checked when the writer finishes, and only
  logged then. The check reads the header only: a file damaged deeper in is for
  the display layer to skip the same way (it does: see [`preview.md`](preview.md), section 3).
- **Screenshots.** Left out unless the `show-screenshots` setting is on (it is off by default; the
  settings window has a "Show screenshots" switch). `ImageSource(..., show_screenshots=False)`,
  `set_show_screenshots(bool)` (a live change walks the folder again, like `set_folder`).
  What counts, judged by the **name** of a walked entry only:
  - a **folder** called `Screenshots` or `Képernyőképek` (any case) is not walked and gets no
    monitor, with everything under it; and the same for the name GNOME Shell gives the folder in
    the language of the session, read from GNOME Shell's own catalog (text domain `gnome-shell`,
    message "Screenshots") when that catalog is installed;
  - a **file** whose name starts (any case) with `Screenshot` (GNOME: `Screenshot from 2026-10-07
    11-00-00.png`; KDE Spectacle: `Screenshot_20261007_110000.png`), with `Képernyőkép`, or with
    the text GNOME Shell's catalog gives before the date of "Screenshot from %s".
  - **Not touched:** a picture whose name merely contains the word (`my screenshot.png`); the
    folder of the sample pictures (`sakkmesterke`) and everything under it; and a picture folder
    chosen on purpose: when the root itself, or a folder above it, is a screenshot folder (or
    `sakkmesterke`), nothing in it is left out (the explicit choice wins).
  - **Links.** The filter reads names and never looks where a link goes (a test makes
    `realpath`, `readlink` and `lstat` fail). It only takes entries out of the walk: how the walk
    follows links, the `(st_dev, st_ino)` loop guard and the limits are exactly as with the
    setting on. So a link *named* `Screenshots` is left out, and a link named something else that
    points to a screenshot folder is followed like any link ("Nothing is filtered by where a link
    points").
  - **Not measured:** a real GNOME (this was written without one). That GNOME Shell 42 and later
    saves to `Pictures/Screenshots/Screenshot from <date>.png`, that the folder name is translated,
    and what the translations are, is from memory of GNOME's behaviour, not from its source: the
    source and the catalogs could not be fetched here. The Hungarian folder name `Képernyőképek` is
    from the project brief. A folder or a file name that is in neither list, and a screenshot tool
    that names them differently, is shown.
- **Order.** `name`: case-insensitive by full path. `random`: every image once per
  cycle, no image twice in a row across a cycle boundary; a new image joins the
  current cycle.
- **The folder may not exist.** Nothing assumes it does, the XDG default included.
  A missing folder is the empty state plus one `[slideshow-dir]` WARNING, and the
  source keeps watching for it to appear.

## Picture formats

The extensions that count are `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tif`, `.tiff` and
`.webp`. Whether a file of one of them can be shown depends on the gdk-pixbuf loaders the machine
has; the program has no decoder of its own. The table is read from the package file lists of the
repositories (see the spec comment); no picture of these formats was loaded on those systems for it.

| Format (has a loader from) | RHEL 10, Rocky, AlmaLinux, base repositories only | The same with EPEL 10 | Fedora 43 and later |
|---|---|---|---|
| JPEG, PNG | built into gdk-pixbuf | built into gdk-pixbuf | not measured |
| TIFF, GIF | `gdk-pixbuf2-modules`, which the package requires | the same | not measured |
| BMP | **no loader** | `gdk-pixbuf2-modules-extra` | not measured |
| WebP | **no loader** | `webp-pixbuf-loader` | not measured |

- **EPEL is not needed, and not installed for you.** The package requires `gdk-pixbuf2-modules`
  on RHEL and its rebuilds, and only *recommends* the two EPEL packages. Without EPEL enabled a
  BMP or WebP file is not shown.
- **What a file without a loader costs.** One WARNING in the journal (`no installed gdk-pixbuf
  loader`; the lines are rate-limited, with a summary after a burst), and the file stays out of
  the picture queue. The other pictures play on.
- **JPEG 2000 (`.jp2`) is left out on purpose.** No repository that was looked at (Fedora 43, 44
  and rawhide, EPEL 10, Rocky 10, CentOS Stream 10) has a gdk-pixbuf loader for it, and gdk-pixbuf
  itself has none, so a `.jp2` extension would only produce files that never load. A `.jp2` file is
  not in the queue, and `probe_image` does not know its header.
- **Fedora 43 and later: not measured.** There gdk-pixbuf hands the formats to glycin; what that
  does with a cut-off file or a very large picture was not measured for this program, and the
  package asks for nothing extra there.
- **Not measured at all:** `rpmbuild` and `dnf install` of the package on EL10 with and without
  EPEL, and whether a RHEL 10 Workstation has `gdk-pixbuf2-modules` installed from the start.

## Manual trial: inotify watch exhaustion

Automated on GitHub Actions only (`test_gio_watch_exhaustion_is_reported_once_and_the_walk_still_completes`
lowers the kernel limit with `sudo`, which a developer machine should not do
unasked). **Remove or re-gate that test before CI moves to a self-hosted runner:**
it changes a kernel setting that other jobs on a shared machine would see. The
healthy-tree counterpart (`test_gio_healthy_tree_gets_every_watch_confirmed_and_no_warning`)
changes nothing and stays. To try it by hand, on a test machine:

1. `sudo sysctl -w fs.inotify.max_user_watches=25` (note the old value first).
2. Point the source at a folder with 60 subfolders, each holding an image, and
   start it.
3. Expect: all 60 images listed, one `[slideshow-dir] folder limits or watch
   problems ...` WARNING saying how many folders have no confirmed kernel watch
   (and that they may recover). A file added to such a folder is noticed only if
   GLib's retry (every few seconds) got the watch installed.
4. Restore the old value with `sysctl -w`.

## Tests

`tests/test_image_source.py` drives the real filesystem with a fake monitor and a
manual scheduler (deterministic, no main loop). `tests/test_image_source_gio.py`
runs the same behaviours through the real `Gio.FileMonitor` and a pumped GLib main
context. Test names carry the acceptance criterion they prove.
The format name `probe_image` returns for each of the six headers (`jpeg`, `png`, `gif` for both
signatures, `bmp`, `tiff` for both byte orders, `webp`) is pinned on the bytes alone, without a
loader, together with headers that only look like a known one (a RIFF file that is not WebP, the
WebP tag without RIFF, `GIF88a`, a TIFF with a wrong magic number).

`tests/test_image_source.py` also pins that a `.jp2` file is not in the queue, even with a JPEG 2000
header, and that `probe_image` rejects that header. `tests/test_packaging.py` pins the conditional
`Requires` and `Recommends` lines of the spec for the formats above.
