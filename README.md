# slideshow-lock
GNOME/Wayland screensaver that starts a fullscreen slideshow after idle and locks the session on user input, with a GTK4 settings app and RPM packages for RHEL 10, AlmaLinux, Rocky and Fedora. Developed by TrenSoft.

## Using it

After the installation, log out and in again, or start "Slideshow Lock" from the application menu: both start the service (the systemd user unit `slideshow-lock`). At the first login the settings window opens once, if no picture folder has been chosen. The package enables nothing and ships no preset; the start at login is an XDG autostart entry, `/etc/xdg/autostart/io.github.sakkmesterke.SlideshowLock.desktop`, which runs `slideshowlock autostart` (GNOME only).

Stop the service until the next login:

```
systemctl --user stop slideshow-lock
```

To keep it from starting at every login, override the autostart entry for your user: create `~/.config/autostart/io.github.sakkmesterke.SlideshowLock.desktop` with this content (the settings window has no switch for this yet):

```
[Desktop Entry]
Type=Application
Name=Slideshow Lock
Exec=/usr/bin/slideshowlock autostart
Hidden=true
X-GNOME-Autostart-enabled=false
```

`systemctl --user disable --now slideshow-lock` alone does not do this: the autostart entry starts the service again at the next login (and every time `slideshowlock` is run). `systemctl --user enable slideshow-lock` is not needed.

## Updating

The packages come from the COPR project `sakkmesterke/slideshow-lock`. To update, run `sudo dnf upgrade --refresh`. A plain `dnf upgrade` can miss a new version for up to 48 hours, because dnf keeps the repository metadata in a cache; this was seen when updating 1.0.0 to 1.0.1.

## Translations

The interface is available in 40 languages besides English. The 35 languages added in 1.0.1 were translated with AI assistance and have not been reviewed by a native speaker (the header of each catalog says so). Corrections are welcome: open an issue, or a pull request on `po/<lang>.po`; see `docs/translations.md`.

## Licence

The program is GPL-3.0-or-later (`LICENSE`). The seven sample pictures in `data/pictures` are released under CC BY-SA 4.0 (`packaging/licenses/CC-BY-SA-4.0.txt`); the credit is in `data/pictures/CREDITS.txt`: "Fraktálképek: sakkmesterke (Alexovics Attila), CC BY-SA 4.0". The package carries both licences. At the first login the pictures are copied once into `Pictures/sakkmesterke` (see `docs/logging-and-lifecycle.md`, section 6); the files are JPEG with the metadata taken out losslessly by `tools/strip_jpeg_metadata.py`, but for the author and the licence, which each picture carries in its Exif (Artist, Copyright) and XMP (creator, rights, web statement); `tests/test_pictures_clean.py` keeps it that way.

## Commands

`slideshowlock` starts the service and opens the settings window (the same as `slideshow-lock control`). `slideshow-lock` starts the programs one by one: `slideshow-lock service`, `slideshow-lock settings` (the window alone), `slideshow-lock preview`; `slideshow-lock --help` lists them.

## Picture formats

On RHEL 10, AlmaLinux and Rocky, JPEG, PNG, GIF and TIFF have a loader from the base repositories (the package requires the one that has the TIFF and GIF loaders; installing the package there was not measured). BMP and WebP files need an extra gdk-pixbuf loader that those systems have only in EPEL 10 (`gdk-pixbuf2-modules-extra`, `webp-pixbuf-loader`): without EPEL enabled they are not shown, and the journal gets one WARNING for such a file (`journalctl --user -u slideshow-lock`); the other pictures play on. JPEG 2000 is not supported: no repository that was looked at has a gdk-pixbuf loader for it. On Fedora 43 and later the formats come through glycin; how that behaves was not measured. Details: `docs/image-source.md`, "Picture formats".
