# slideshow-lock
GNOME/Wayland screensaver that starts a fullscreen slideshow after idle and locks the session on user input, with a GTK4 settings app and RPM packages for RHEL 10, AlmaLinux, Rocky and Fedora.

## Using it

After the installation the service is not running and not enabled (the package ships no preset for it). Switch it on and off from a terminal:

```
systemctl --user enable --now slideshow-lock
systemctl --user disable --now slideshow-lock
```

The settings window opens from the application menu ("Slideshow Lock") or with `slideshow-lock settings`. In 1.0.0 the settings window has no on/off switch: only the two commands above switch the service.

## Commands

`slideshowlock` opens the settings window (the same as `slideshow-lock settings`). `slideshow-lock` starts the other programs: `slideshow-lock service` and `slideshow-lock preview`; `slideshow-lock --help` lists them.
