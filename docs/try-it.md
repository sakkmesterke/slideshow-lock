# Try it from a checkout

No installation, no RPM. One script, `run.sh` in the root of the repository, runs the slideshow
preview and the settings window straight from the source tree.

Status: automated tests green, live trial still to be done. Nobody has run this on the target
machine (RHEL 10.2, GNOME, Wayland) yet; what the pictures look like on a real monitor is exactly
what the first trial is for.

## 1. Get the code

```
git clone --branch ui-1-preferences-window https://github.com/trensoft/slideshow-lock.git
cd slideshow-lock
```

or download the branch as an archive from GitHub and unpack it. (Once the branch is merged, use
`main` instead.) If the unpacked `run.sh` is not executable, start it as `bash run.sh ...`.

## 2. Check the dependencies

```
./run.sh check
```

It looks for python3, PyGObject (`gi`), the GTK 4, Gdk, Graphene, GdkPixbuf and Gio typelibs,
`glib-compile-schemas` and a Wayland session (`WAYLAND_DISPLAY`), and prints one `ok:` line per
item found and one `MISSING:` block per item missing. It installs nothing. The exit code is 0 only
if everything required is there. GStreamer with the `videoscale` element is optional: without it
the pictures are scaled bilinear instead of Lanczos, and `check` says so in a warning.

For every missing item the output names a likely `dnf` package. **These are likely names, not
verified on RHEL 10.2**; the script does not detect the distribution and does not install anything.

## 3. Run the preview

```
./run.sh preview
```

You should see one fullscreen window per monitor, showing the pictures of the picture folder, each
scaled to the monitor. Any key, click, scroll or mouse movement ends it. The preview never locks
the session.

**Which folder.** Without `--folder` it uses the picture folder of your settings. Until you choose
one, that is your system's pictures folder itself, the `XDG_PICTURES_DIR` of
`~/.config/user-dirs.dirs` (`~/Képek` on a Hungarian system), or `~/Pictures` if the system has
none configured. Its subfolders are read too, so nothing has to be created. If it holds no
picture, the preview shows "No pictures to show" with the exact path it looked at, and its log
line names the same path. To use another folder, point the run at it:

```
./run.sh preview --folder /path/to/some/pictures
```

Other options, which apply to that run only and are never stored: `--interval SECONDS`,
`--order random|name`, `--scaling fit|fill`, `--pan`, `--debug`. All of them are described in
[`preview.md`](preview.md), section 6.

## 4. Open the settings window

```
./run.sh settings
```

The window of [`preferences.md`](preferences.md): the seven stored settings and a Preview button.
Unlike the preview, **this window stores what you change** in your user settings (GSettings
under the application id), so the preview afterwards uses it.

## What the script writes

- The compiled settings schema goes to `${XDG_CACHE_HOME:-$HOME/.cache}/slideshow-lock/schemas`.
  The repository tree is not touched, and the schema is not installed system-wide.
- `settings` stores the changed settings (see above). `preview` stores nothing.
- Nothing is installed, and nothing locks the session.

## What this does not prove

- Automated tests are green; a trial on the real desktop is still to be done: how the pictures
  look, whether 2 px of mouse movement is the right threshold on your mouse or touchpad, how the
  settings window looks with the real GNOME theme.
- The package names in `check` are unverified on RHEL 10.2.
- `run.sh` was exercised on a headless Wayland compositor with a software renderer, not on a real
  GPU and monitor. Locking, the idle timer and the service are not part of this script.
