# slideshow-lock
GNOME/Wayland screensaver that starts a fullscreen slideshow after idle and locks the session on user input, with a GTK4 settings app and RPM packages for RHEL 10, AlmaLinux, Rocky and Fedora.

## Installation

The package comes from the COPR project `trensoft/slideshow-lock`. No COPR build has run yet and the project is not made yet, so the package is available only after the first COPR build. Until then, the commands below are not tested.

### Fedora

```
sudo dnf copr enable trensoft/slideshow-lock
sudo dnf install slideshow-lock
```

### AlmaLinux 10, Rocky Linux 10

These need EPEL and the CRB repository. The order below was measured on AlmaLinux 10 (a container with the `almalinux:10` image; the package was installed with `dnf`, exit code 0, and its Python dependency was resolved from EPEL). `[H]` Rocky Linux 10 is expected to be the same; it was not measured.

```
sudo dnf install epel-release
sudo dnf config-manager --set-enabled crb
sudo dnf install dnf-plugins-core
sudo dnf copr enable trensoft/slideshow-lock
sudo dnf install slideshow-lock
```

The `dnf copr` subcommand needs the `dnf-plugins-core` package; in the AlmaLinux container it had to be installed separately.

That test did not use a COPR build: the state it measured is `main` at `1111a7c` plus the changes #46, #47 and #48.

### RHEL 10

The commands above were not measured on RHEL 10, and the first two steps are not the same there. `[H]` On RHEL the CRB repository is enabled with `subscription-manager`, and the EPEL package is installed from a URL, not with `dnf install epel-release` (background knowledge, not measured, no link checked). After EPEL and CRB are enabled, the last three commands are the same `[H]`.
