# slideshow-lock
GNOME/Wayland screensaver that starts a fullscreen slideshow after idle and locks the session on user input, with a GTK4 settings app and RPM packages for RHEL 10, AlmaLinux, Rocky and Fedora.

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

## Commands

`slideshowlock` starts the service and opens the settings window (the same as `slideshow-lock control`). `slideshow-lock` starts the programs one by one: `slideshow-lock service`, `slideshow-lock settings` (the window alone), `slideshow-lock preview`; `slideshow-lock --help` lists them.

## Picture formats

On RHEL 10, AlmaLinux and Rocky, JPEG, PNG, GIF and TIFF have a loader from the base repositories (the package requires the one that has the TIFF and GIF loaders; installing the package there was not measured). BMP and WebP files need an extra gdk-pixbuf loader that those systems have only in EPEL 10 (`gdk-pixbuf2-modules-extra`, `webp-pixbuf-loader`): without EPEL enabled they are not shown, and the journal gets one WARNING for such a file (`journalctl --user -u slideshow-lock`); the other pictures play on. JPEG 2000 is not supported: no repository that was looked at has a gdk-pixbuf loader for it. On Fedora 43 and later the formats come through glycin; how that behaves was not measured. Details: `docs/image-source.md`, "Picture formats".
