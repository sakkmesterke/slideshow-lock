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
  the first image is available after the first step. The longest stall is one
  directory listing.
- **Symlink loops.** Directories are identified by `(st_dev, st_ino)`. A loop, or a
  folder reachable through two links, is walked once. Symlinks are followed, so
  linking in a folder from elsewhere works. A symlinked *file* is its own entry.
- **Live changes.** One non-recursive `Gio.FileMonitor` per walked directory. The
  parent of the root folder is watched as well: a missing folder that appears later
  is picked up, a deleted root becomes the empty state, a replaced root is rewalked.
  A directory that cannot be watched (for example the inotify watch limit) is
  logged and still walked once; only its later changes go unnoticed.
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
  the display layer to skip the same way.
- **Order.** `name`: case-insensitive by full path. `random`: every image once per
  cycle, no image twice in a row across a cycle boundary; a new image joins the
  current cycle.
- **The folder may not exist.** Nothing assumes it does, the XDG default included.
  A missing folder is the empty state plus one `[slideshow-dir]` WARNING, and the
  source keeps watching for it to appear.

## Tests

`tests/test_image_source.py` drives the real filesystem with a fake monitor and a
manual scheduler (deterministic, no main loop). `tests/test_image_source_gio.py`
runs the same behaviours through the real `Gio.FileMonitor` and a pumped GLib main
context. Test names carry the acceptance criterion they prove.
