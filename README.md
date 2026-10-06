# slideshow-lock
GNOME/Wayland screensaver that starts a fullscreen slideshow after idle and locks the session on user input, with a GTK4 settings app and RPM packages for RHEL 10, AlmaLinux, Rocky and Fedora.

## Installation

The package comes from the COPR project `sakkmesterke/slideshow-lock`. No COPR build has run yet and the project is not made yet, so the package is available only after the first COPR build. Until then, the commands below are not tested.

### Fedora

```
sudo dnf copr enable sakkmesterke/slideshow-lock
sudo dnf install slideshow-lock
```

### RHEL 10, AlmaLinux 10, Rocky Linux 10

These need EPEL: in a test with the `almalinux:10` image, the Python dependency of the package was resolved from EPEL. Enable EPEL for your distribution first, then run the same two commands as for Fedora:

```
sudo dnf copr enable sakkmesterke/slideshow-lock
sudo dnf install slideshow-lock
```

`[H]` The `dnf copr` command needs the `dnf-plugins-core` package (background knowledge, not measured here).

`[H]` That a RHEL rebuild takes the `epel-10-x86_64` build of COPR is background knowledge, not measured. A test with the `almalinux:10` image and EPEL installed the package with `dnf` (exit code 0), but that test did not use a COPR build.
