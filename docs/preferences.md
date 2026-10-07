# UI-1: the settings window

A GTK 4 and libadwaita window for the settings, in titled groups of rows (Pictures, Transitions, Start the slideshow, Timing), with a Save button in the header bar. Run it from a source checkout:

```
glib-compile-schemas data/
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences
```

`slideshow_lock.settings_app` (what `./run.sh settings` and `slideshow-lock settings` start) is the same window and also closes the shell's overview when the Preview button is pressed; see `docs/architecture/dbus-state-machine.md`, section 3.7a. The menu entry and the login start run `slideshowlock` (`slideshow_lock.control`), which starts the service unit first and then opens this window; it does not read or write any setting except `first-run-done` (section 3.7b). The `first-run-done` key is not shown in the window.

## What is in it

| Field | Key | Accepts |
|---|---|---|
| Picture folder (text, "Browse...") | `picture-folder` | an absolute path, `~/...`, or empty for the default |
| Start the slideshow after | `idle-timeout-seconds` | 1 to 86400 |
| Lock grace period | `lock-grace-period-seconds` | 0 to 86400 (input sooner than this does not lock, strictly sooner: D16) |
| Show each picture for (one slider, big HH:MM:SS above it) | `slide-interval-seconds` | 1 to 86399 seconds, shown as 00:00:01 to 23:59:59 (10 s by default) |
| Picture order | `order` | random, name |
| Scaling | `scaling` | fill, fit |
| Scroll tall pictures | `pan-portrait-images` | on, off (off by default) |
| Show screenshots | `show-screenshots` | on, off (off by default): off leaves screenshots out of the slideshow, see `docs/image-source.md`, "Screenshots" |
| Between pictures (drop-down, in the Transitions group) | `transitions`, `transition-order` | none (an empty list); one of the ten: cross-fade (`crossfade`), fade through black (`fade-black`), slide in (`slide-in`), push (`push`), Ken Burns (`ken-burns`, the default), zoom (`zoom`), wipe (`wipe`), circle reveal (`circle`), blur (`blur`), rotate (`rotate`); or the random mix (see below) |
| Transition length (slider with the value on its left, in the Transitions group) | `transition-duration` | 0.2 to 5.0 seconds, in steps of 0.1 (1.0 by default); the same for every transition |

"Preview" runs the CORE-2 preview (`preview_app.start_preview`) on the values in the window, saved
or not (see "Save" below), in the same process. It never locks (D11); any key, click, scroll or mouse movement ends it, and
closing the window ends it too. While it shows, the window's `Gtk.Application` holds an idle
request for it, so the desktop's own idle delay does not blank the screen under the preview; it is
given back whichever way the preview ends (`docs/preview.md`, section 2.1). While it holds the
request the desktop's idle-based blanking and automatic lock do not run, for two minutes at most:
the preview ends by itself then (`PREVIEW_LIMIT_SECONDS`; not measured on a real GNOME session).
The exception is a session manager that never answers the request: the call has no time limit,
the settings window freezes with it, and the limit cannot end the preview then
(`docs/preview.md`, section 2.1).
Each "Preview" click makes its own `Settings` object and wraps it in
`preview_app.SessionSettings` with the edits that are not saved yet (`Draft.preview_values`): the
image source and the preview read those values, and nothing is written. The image source and the
preview controller each keep a change listener on it, and both are dropped (`Settings.disconnect_changed`) when the
preview ends, whichever way: input, the time limit, the window closing, no monitor, or a start
that failed. The window's own change listener is not part of that: it stays on the window's own
settings after the window closes, and does nothing from then on (`_closed`).
There is no on/off switch: that goes through the systemd user unit
(D4) and is not part of this window. No key of its own was added to the schema.

## How it behaves

- **Save.** An edit is checked when it is made and kept in a draft (`preferences_model.Draft`); the
  service keeps running on what is stored. It reaches the settings when the user presses "Save" in
  the header bar and when the window is closed, without a question (the close writes what Save
  would). The Save button is on only while something would change: an edit that brings a field back
  to the stored value is dropped. Only the edited keys are written, so a stored value the window
  cannot show as it is (a slide interval that is not a step, a list of several transitions) is not
  rewritten by saving something else. If a value cannot be stored (the settings refuse it, or it does
  not read back), "Save" says so in the status line and keeps the edit; the close logs it and closes
  all the same (it cannot ask, and a window that will not close is worse). Before this change every
  field was written the moment it changed; that is gone, so a change made in the window no longer
  reaches the running service until Save or the close.
- **Preview** runs on the values in the window, saved or not, and stores none of them.
- The window says "Saved." only for a value that is stored and reads back as written. A refused
  value is not kept, the field goes back to the value in effect (the draft's, otherwise the stored
  one), and the status line says why.
- The slide interval is one slider with a big HH:MM:SS (`00:00:10`) and a short text (`10 s`) above
  it, updated while the slider moves. The default is 10 seconds (it was 5 before 1.0.1). The slider is cut into four equal
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
  gives the default, 10 s, for a value outside the range). The idle time (1 to 86400 seconds) and
  the grace period are still plain number fields.
- The "Between pictures" drop-down (group Transitions) offers None, the ten transitions in the
  order of `transitions.ALL_TRANSITIONS`, and "Random mix". `transitions` is a list of names: a
  transition is stored as a list of that one name (`transition-order` is left as it was), None is
  the empty list (a real choice, not the default), and the random mix is the list of eight, every
  transition but Blur and Ken Burns (`preferences_model.RANDOM_POOL`), with `transition-order`
  `random`. "Random mix" is only an entry of the window: a stored list of two or more names reads
  back as "Random mix" (the window cannot show more), and a list of one name as that transition.
  Reading never writes: a list written by hand or by another version stays until the user picks
  another entry. The default is Ken Burns, not the random mix. All ten are drawn (`docs/preview.md`,
  section 2); where Ken Burns cannot be drawn it is a cross-fade. The row says that without desktop animations the pictures change at once.
  What the transitions do and when there is none: `docs/preview.md`, section 2.
- The "Transition length" slider (group Transitions, 0.2 to 5.0 s in steps of 0.1, 1.0 by default)
  writes `transition-duration`, one value for every transition. A new edit is rounded to the step
  (1.25 becomes 1.2); a stored value that is not a step is shown as it is and left alone until the
  slider is moved, like the slide interval. The slider always shows the stored length, even when the
  engine cuts it short: a transition is never longer than half of the slide interval, so 5 s at a
  slide interval of 4 s plays for 2 s (`transitions.transition_seconds`). The row says so in one
  sentence, and nothing is written for the cut.
- The folder chooser (Browse) opens in the folder of the field (the draft's) when it exists. If that
  folder is missing, or the field is empty (the default is in use), it opens in the system's pictures folder (`~/Pictures` when none is
  configured or it is the home directory itself), and in the home directory only when even that
  folder does not exist; never where the chooser was last.
- A number outside its range, or text that is no number, is thrown away (the old value stays);
  GTK's default would have clamped it to the nearest limit. One corner: an emptied field reads as
  0, so it is thrown away everywhere but in the grace period, whose minimum is 0.
- The folder field is empty while the default folder is in use (D25); the line of the group names
  the folder in use, the default included. The default is the system's pictures folder itself, read recursively (the
  `XDG_PICTURES_DIR` of `~/.config/user-dirs.dirs`, `~/Képek` on a Hungarian system; `~/Pictures`
  if none is configured or if it is the home directory itself). The line says if the folder in use does not
  exist; a missing folder is not an error. A relative path and a path
  that is a file are refused. Clearing the field and saving stores the empty value again, which
  means "the default" (the system's pictures folder, not a folder that was typed earlier).
- A value changed by another process shows up in the window; an edit that is not saved yet stays.
- "Start the slideshow after" and "Lock grace period" are number fields with the word "seconds"
  next to them (`_with_unit` in `preferences.py`; read from the source, not looked at on a real
  screen).

## Structure

- `slideshow_lock/preferences_model.py` has no GTK in it: which key a field is bound to, what is
  accepted, what is shown, and whether a save really happened. `tests/test_preferences_model.py`
  tests it in the CI, including that its ranges and choices are the schema's.
- `slideshow_lock/preferences.py` puts it on the screen with libadwaita, and only what exists in
  libadwaita 1.2: `Adw.ApplicationWindow`, `HeaderBar`, `PreferencesPage` and `PreferencesGroup`,
  `ActionRow`, `ComboRow`, `EntryRow`. That is what the window was run with here (Adw 1.2.2, GTK
  4.8.3), and what the libadwaita of EL10 (1.6) has as well; `SwitchRow`, `SpinRow`, `ToolbarView`
  and `Adw.PreferencesDialog` are newer and not used. The window needs the `Adw` typelib
  (`gir1.2-adw-1`, `libadwaita`). The folder chooser is `Gtk.FileChooserNative`; `Gtk.FileDialog`
  needs GTK 4.10 and does not exist on the GTK 4.8 this was built on.
- `tools/wayland-smoke/smoke_preferences.py` drives the real window on a headless compositor:
  `SMOKE_SCRIPT=smoke_preferences.py tools/wayland-smoke/run.sh [--screenshot DIR]`. Not run by
  the CI (it needs a compositor).

## Not covered

- How the window looks on the real desktop and theme, and with the libadwaita of EL10 (1.6): it was
  run on libadwaita 1.2.2. A screenshot from the headless run is a picture of the default
  libadwaita style, not of GNOME on the reference machine.
- The translations of the new texts (hu, de, it, fr, es) were written with AI help and not read by a
  native speaker.
- The folder chooser dialog itself (no portal on a headless compositor): only its "cancel" and
  "folder chosen" paths are exercised.
- Real keyboard and mouse use of the widgets: the smoke sets the widgets by calling their setters,
  which fires the same handlers.
- This module is not under the D11 scan of `tests/test_preview.py`: its labels say "lock" and
  "session" on purpose. The preview it starts is that scanned code, and the window itself has no
  lock call.
