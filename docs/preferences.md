# UI-1: the settings window

A plain GTK 4 window for the stored settings. Run it from a source checkout:

```
glib-compile-schemas data/
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences
```

## What is in it

| Field | Key | Accepts |
|---|---|---|
| Picture folder (text, "Browse...") | `picture-folder` | an absolute path, `~/...`, or empty for the default |
| Start the slideshow after | `idle-timeout-seconds` | 1 to 86400 |
| Lock grace period | `lock-grace-period-seconds` | 0 to 86400 (input sooner than this does not lock, strictly sooner: D16) |
| Show each picture for | `slide-interval-seconds` | 1 to 3600 |
| Picture order | `order` | random, name |
| Scaling | `scaling` | fill, fit |
| Scroll tall pictures | `pan-portrait-images` | on, off (off by default) |

"Preview" runs the CORE-2 preview (`preview_app.start_preview`) on the stored settings, in the
same process. It never locks (D11); any key, click, scroll or mouse movement ends it, and
closing the window ends it too. There is no on/off switch: that goes through the systemd user unit
(D4) and is not part of this window. No key of its own was added to the schema.

## How it behaves

- A value is saved the moment it is changed. There is no "Apply".
- The window says "Saved." only for a value that is stored and reads back as written. A refused
  value is not stored, the field goes back to the stored one, and the status line says why.
- A number outside its range, or text that is no number, is thrown away (the old value stays);
  GTK's default would have clamped it to the nearest limit. One corner: an emptied field reads as
  0, so it is thrown away everywhere but in the grace period, whose minimum is 0.
- The folder field is empty while the default folder is in use (D25), and shows the default as its
  hint. The default is the system's pictures folder itself, read recursively (the
  `XDG_PICTURES_DIR` of `~/.config/user-dirs.dirs`, `~/Képek` on a Hungarian system; `~/Pictures`
  if none is configured or if it is the home directory itself). A line under it names the folder in
  use and says if it does not exist; a missing folder is not an error. A relative path and a path
  that is a file are refused. Clearing the field and saving stores the empty value again, which
  means "the default" (the system's pictures folder, not a folder that was typed earlier).
- A value changed by another process shows up in the window.

## Structure

- `slideshow_lock/preferences_model.py` has no GTK in it: which key a field is bound to, what is
  accepted, what is shown, and whether a save really happened. `tests/test_preferences_model.py`
  tests it in the CI, including that its ranges and choices are the schema's.
- `slideshow_lock/preferences.py` puts it on the screen with plain Gtk widgets (no libadwaita:
  the CI has no libadwaita typelib). The folder chooser is `Gtk.FileChooserNative`; `Gtk.FileDialog`
  needs GTK 4.10 and does not exist on the GTK 4.8 this was built on.
- `tools/wayland-smoke/smoke_preferences.py` drives the real window on a headless compositor:
  `SMOKE_SCRIPT=smoke_preferences.py tools/wayland-smoke/run.sh [--screenshot DIR]`. Not run by
  the CI (it needs a compositor).

## Not covered

- How the window looks on the real desktop and theme. A screenshot from the headless run is a
  picture of GTK's default theme, not of GNOME on the reference machine.
- The folder chooser dialog itself (no portal on a headless compositor): only its "cancel" and
  "folder chosen" paths are exercised.
- Real keyboard and mouse use of the widgets: the smoke sets the widgets by calling their setters,
  which fires the same handlers.
- This module is not under the D11 scan of `tests/test_preview.py`: its labels say "lock" and
  "session" on purpose. The preview it starts is that scanned code, and the window itself has no
  lock call.
- Each "Preview" click gives the image source one more change listener on its own `Settings`
  object (`source_from_settings` has no disconnect); the listeners of an ended preview do nothing.
