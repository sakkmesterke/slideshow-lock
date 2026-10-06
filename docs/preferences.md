# UI-1: the settings window

A GTK 4 window for the stored settings, in titled groups of rows (Pictures, Start the slideshow, Timing). Run it from a source checkout:

```
glib-compile-schemas data/
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences
```

`slideshow_lock.settings_app` (what `./run.sh settings` and `slideshow-lock settings` start) is the same window and also closes the shell's overview when the Preview button is pressed; see `docs/architecture/dbus-state-machine.md`, section 3.7a.

## What is in it

| Field | Key | Accepts |
|---|---|---|
| Picture folder (text, "Browse...") | `picture-folder` | an absolute path, `~/...`, or empty for the default |
| Start the slideshow after | `idle-timeout-seconds` | 1 to 86400 |
| Lock grace period | `lock-grace-period-seconds` | 0 to 86400 (input sooner than this does not lock, strictly sooner: D16) |
| Show each picture for (one slider, big HH:MM:SS above it) | `slide-interval-seconds` | 1 to 86399 seconds, shown as 00:00:01 to 23:59:59 |
| Picture order | `order` | random, name |
| Scaling | `scaling` | fill, fit |
| Scroll tall pictures | `pan-portrait-images` | on, off (off by default) |

"Preview" runs the CORE-2 preview (`preview_app.start_preview`) on the stored settings, in the
same process. It never locks (D11); any key, click, scroll or mouse movement ends it, and
closing the window ends it too. While it shows, the window's `Gtk.Application` holds an idle
request for it, so the desktop's own idle delay does not blank the screen under the preview; it is
given back whichever way the preview ends (`docs/preview.md`, section 2.1). While it holds the
request the desktop's idle-based blanking and automatic lock do not run, for two minutes at most:
the preview ends by itself then (`PREVIEW_LIMIT_SECONDS`; not measured on a real GNOME session).
The exception is a session manager that never answers the request: the call has no time limit,
the settings window freezes with it, and the limit cannot end the preview then
(`docs/preview.md`, section 2.1).
Each "Preview" click makes its own `Settings` object. The image source and the preview controller
each keep a change listener on it, and both are dropped (`Settings.disconnect_changed`) when the
preview ends, whichever way: input, the time limit, the window closing, no monitor, or a start
that failed. The window's own change listener is not part of that: it stays on the window's own
settings after the window closes, and does nothing from then on (`_closed`).
There is no on/off switch: that goes through the systemd user unit
(D4) and is not part of this window. No key of its own was added to the schema.

## How it behaves

- A value is saved the moment it is changed. There is no "Apply".
- The window says "Saved." only for a value that is stored and reads back as written. A refused
  value is not stored, the field goes back to the stored one, and the status line says why.
- The slide interval is one slider with a big HH:MM:SS (`00:00:05`) and a short text (`5 s`) above
  it, updated while the slider moves. The default is 5 seconds. The slider is cut into four equal
  quarters of its length, 36 steps in all, and each quarter has its steps spread evenly:
  1. 1 to 10 s, every second (1, 2, ... 10);
  2. 10 to 60 s, every 5 seconds (15, 20, ... 60);
  3. 1 to 60 minutes, round values: 1, 2, 3, 5, 10, 15, 20, 30, 45, 60;
  4. 1 to 24 hours, round values: 1, 2, 3, 4, 6, 8, 12, 24. The end reads "24 h" under the slider
     but is stored and shown as 23:59:59 (86399 s).

  A step where two quarters meet (10 s, 1 minute, 1 hour) is one step, not two. The quarters end at
  25 %, 50 %, 75 % and 100 % of the slider, and the marks under it are 10 s, 1 min, 1 h and 24 h.
  Because the quarters have 9, 10, 9 and 7 gaps between steps, the distance between two steps on
  the slider differs from quarter to quarter. The arrow keys (left/down: shorter, right/up:
  longer), Page Up/Down (5 steps), Home, End and the mouse wheel move by steps of that scale.
- The slider is stored in seconds in the same key as before: 1 to 86399. 00:00:00 and 24:00:00
  cannot be set, in the window or through `Settings` (the schema refuses 0 and 86400), and the
  slider has no position for them. The schema range was 1 to 3600 before: the minimum is the same
  and the maximum grew, so every value stored earlier is still valid and reads as it did (nothing is
  migrated). A one-second interval is allowed on purpose: no limit of its own was added.
- A stored value that is not a step (100 s, say) is shown at the nearest step, measured in
  seconds (100 s sits at 2 min, 11 s at 10 s; halfway between two steps, the lower one: 90 s sits at
  1 min), and the big text and a note say it is not a step. Opening the window never writes: the
  stored value stays until the user moves the slider. A stored 0 or 86400 cannot exist (GSettings
  gives the default, 5 s, for a value outside the range). The idle time (1 to 86400 seconds) and
  the grace period are still plain number fields.
- The folder chooser (Browse) opens in the folder in use when it exists. If that folder is missing,
  or the default is in use, it opens in the system's pictures folder (`~/Pictures` when none is
  configured or it is the home directory itself), and in the home directory only when even that
  folder does not exist; never where the chooser was last.
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
- "Start the slideshow after" and "Lock grace period" are number fields with the word "seconds"
  next to them (`_with_unit` in `preferences.py`; read from the source, not looked at on a real
  screen).

## Structure

- `slideshow_lock/preferences_model.py` has no GTK in it: which key a field is bound to, what is
  accepted, what is shown, and whether a save really happened. `tests/test_preferences_model.py`
  tests it in the CI, including that its ranges and choices are the schema's.
- `slideshow_lock/preferences.py` puts it on the screen with plain Gtk widgets and a few CSS rules
  of its own for the rounded groups (no libadwaita: the CI has no libadwaita typelib, and the
  brief's `Adw.PreferencesWindow` would be a new dependency, so it waits for a decision; the
  container this was built in has no `Adw` typelib either). The folder chooser is `Gtk.FileChooserNative`; `Gtk.FileDialog`
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
