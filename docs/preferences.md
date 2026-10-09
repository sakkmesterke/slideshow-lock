# UI-1: the settings window

A GTK 4 and libadwaita window for the settings, in titled groups of rows (Pictures, Transitions, Start the slideshow, Timing), with the Preview and Donate buttons at the bottom and, small and faint at the bottom right, the program's version and its maker ("<version> by TrenSoft"). The header bar has the window controls only. Every change is saved the moment it is made: there is no Save button. The groups sit in two columns and nothing scrolls: the whole window shows at its natural size, about 1100 x 680 pixels, which fits a 1366 x 768 screen (`tools/wayland-smoke/smoke_preferences.py` measures it with `--monitors 1366x768,...`). The version is not written anywhere in the code: `slideshow_lock.version` reads it from the `pyproject.toml` beside the package (a source checkout) or from the installed package's metadata (an install, where the RPM builds the package from that same file); with neither it shows `dev`. Run it from a source checkout:

```
glib-compile-schemas data/
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences
```

`slideshow_lock.settings_app` (what `./run.sh settings` and `slideshow-lock settings` start) is the same window and also closes the shell's overview when the Preview button is pressed; see `docs/architecture/dbus-state-machine.md`, section 3.7a. The menu entry and the login start run `slideshowlock` (`slideshow_lock.control`), which starts the service unit first and then opens this window; it does not read or write any setting except `first-run-done` (section 3.7b). The `first-run-done` key is not shown in the window.

The window has one option, `--debug` (`./run.sh settings --debug`, `slideshow-lock settings --debug`, `python3 -m slideshow_lock.preferences --debug`): it logs every step to the terminal. Without it only the information, warning and error lines are logged.

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
| Show screenshots | `show-screenshots` | on, off (off by default): off leaves screenshots out of the slideshow by their names, as a convenience and not a privacy control (a renamed screenshot, or a link of another name to a screenshot folder, is shown), see `docs/image-source.md`, "Screenshots" |
| Between pictures (drop-down, in the Transitions group) | `transitions`, `transition-order` | none (an empty list); one of the ten: cross-fade (`crossfade`), fade through black (`fade-black`), slide in (`slide-in`), push (`push`), Ken Burns (`ken-burns`, the default), zoom (`zoom`), wipe (`wipe`), circle reveal (`circle`), blur (`blur`), rotate (`rotate`); or the random mix (see below) |
| Transition length (slider with the value on its left, in the Transitions group) | `transition-duration` | 0.2 to 5.0 seconds, in steps of 0.1 (1.0 by default); the same for every transition |

"Preview" runs the CORE-2 preview (`preview_app.start_preview`) on the values in the window, which
are the stored ones (see "Saved at once" below), in the same process. It never locks (D11); any key, click, scroll or mouse movement ends it, and
closing the window ends it too. While it shows, the window's `Gtk.Application` holds an idle
request for it, so the desktop's own idle delay does not blank the screen under the preview; it is
given back whichever way the preview ends (`docs/preview.md`, section 2.1). While it holds the
request the desktop's idle-based blanking and automatic lock do not run, for two minutes at most:
the preview ends by itself then (`PREVIEW_LIMIT_SECONDS`; not measured on a real GNOME session).
The exception is a session manager that never answers the request: the call has no time limit,
the settings window freezes with it, and the limit cannot end the preview then
(`docs/preview.md`, section 2.1).
Each "Preview" click makes its own `Settings` object and wraps it in
`preview_app.SessionSettings` (with nothing to replace: no edit is left unsaved, `Draft.preview_values`
is empty; a folder typed and not yet confirmed is stored first): the image source and the preview
read the stored values, and the preview writes nothing. The image source and the
preview controller each keep a change listener on it, and both are dropped (`Settings.disconnect_changed`) when the
preview ends, whichever way: input, the time limit, the window closing, no monitor, or a start
that failed. The window's own change listener is not part of that: it stays on the window's own
settings after the window closes, and does nothing from then on (`_closed`).
There is no on/off switch: that goes through the systemd user unit
(D4) and is not part of this window. No key of its own was added to the schema.

## How it behaves

- **Saved at once.** There is no Save button, and no draft the user can see: every edit is checked
  when it is made (`preferences_model.Draft`) and stored right after (`PreferencesWindow._report`),
  so the running service follows the window. This is how the window saved in 1.0.0; 1.0.1 added the
  Save button and the draft, and 1.0.4 took the button away again and kept the checks. A number
  field, a drop-down, a switch, the slider and the length slider store when their value changes; the
  folder field stores when it is left or Enter is pressed (not at every key), and the folder chosen
  in the chooser at once. Only the edited key is written, so a stored value the window cannot show as
  it is (a slide interval that is not a step, a list of several transitions) is not rewritten by
  changing something else. If a value cannot be stored (the settings refuse it, or it does not read
  back), the status line says so, the edit is thrown away and the fields go back to the stored values.
  Closing the window stores a folder that was typed and not confirmed (and logs a value that could
  not be stored); it asks nothing.
- **Preview** runs on the values in the window, which are the stored ones, and stores nothing.
- The window says "Saved." only for a value that is stored and reads back as written. A refused
  value is not kept, the field goes back to the stored value, and the status line says why.
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
- A value changed by another process shows up in the window.
- "Start the slideshow after" and "Lock grace period" are number fields with the word "seconds"
  next to them (`_with_unit` in `preferences.py`; read from the source, not looked at on a real
  screen).

## The Donate button

Next to Preview, as plain as it, the footer has a "Donate" button (since 1.0.10). It opens the
donation page in the browser with `Gtk.UriLauncher` (GTK 4.10; EL10 has 4.16, Fedora 43 4.20) and
the window as the parent. The address is `about.DONATION_URL`, and the only check of it is
`about.donation_link()`: the button is shown (`preferences.donate_button_visible()`) only when that
accepts the address, so with an empty value, the placeholder `<DONATION_URL>`, another scheme than
`https`, a blank or a user name in the address there is no button, and the click handler refuses
on its own as well. A GTK older than 4.10 has no `Gtk.UriLauncher`: there the button is still shown
and `Gtk.show_uri(window, address, Gdk.CURRENT_TIME)` opens the address, as a fallback only (it is
deprecated from 4.10). Nothing else is opened by the program itself: no subprocess, no shell. When
either way fails (no browser, no portal) the log gets the reason and the status line says "The
donation page could not be opened, see the log."; it is not an exception. The button label "Donate" and that sentence are `_()` strings in all 40
catalogs. The tests of this are in `tests/test_preferences.py` (a stand-in launcher: exactly one
call, with exactly the address) and `tools/wayland-smoke/smoke_preferences.py` (the real button).

## The About window

There is none. The main menu (a button in the header bar, one entry, "About Slideshow Lock") and
its `win.about` action went with 1.0.10, and the code of the window (`show_about()`, the link rows,
the description) and its five strings in the Hungarian catalog were taken out after it. The version
and the maker are the line "<version> by TrenSoft" at the bottom of the settings window.
`slideshow_lock/about.py` holds what is left of it:

- **The donation link.** `about.DONATION_URL` is the only place that holds it; it is the address of
  the donation page, and the Donate button of the settings window is shown only for a valid one.
  Only a plain
  `https://` address counts (a host name with a dot, printable ASCII, no blanks, no `<`, `>`, quote
  or backslash, no user name in the address); an empty value, blanks, the placeholder
  `<DONATION_URL>` or any other scheme is not a link, and the window then has no Donate button and
  nothing of it in its texts. The README has a "Support" section only when the constant is a valid
  address, and then with that address: `tests/test_support_readme.py` fails for either alone, and
  for the placeholder `<DONATION_URL>` anywhere in the README.
- **The maker.** `about.DEVELOPER` ("TrenSoft"), used by `preferences.version_text()`.

## Structure

- `slideshow_lock/preferences_model.py` has no GTK in it: which key a field is bound to, what is
  accepted, what is shown, and whether a save really happened. `tests/test_preferences_model.py`
  tests it in the CI, including that its ranges and choices are the schema's.
- `slideshow_lock/preferences.py` puts it on the screen with libadwaita, and only what exists in
  libadwaita 1.2: `Adw.ApplicationWindow`, `HeaderBar`, `PreferencesGroup` (in two `Adw.Clamp`ed
  columns, no `PreferencesPage`: it scrolls), `ActionRow`, `ComboRow`, `EntryRow`. That is what the window was run with here (Adw 1.2.2, GTK
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
