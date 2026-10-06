# Fedora spec for slideshow-lock 1.0.0.
#
# Legend for the comments in this file:
#   [K]  known: read from this repository (the file is named)
#   [H]  background knowledge about RPM and the Fedora packaging guidelines, NOT verified:
#        no rpmbuild, rpmlint, mock or fedora-review has run on this file
#
# Prerequisite: the tag v1.0.0 does not exist yet, so Source0 cannot be downloaded before it does.
# Decisions recorded outside this repository (not read from it):
#   - %%changelog carries TrenSoft's name and e-mail address. The repository is public, so they stay
#     public in the history of main. Decision: TrenSoft, 2026-10-05 22:11.
#   - License GPL-3.0-or-later. Decision: TrenSoft, 2026-10-05 22:18. The
#     repository itself has no SPDX or "or later" text yet.
# Open items, not code (they need a later step):
#   - Source0: the v1.0.0 tag does not exist; a protected or signed tag and a SHA-512 of the tarball
#     are to be fixed at release time, not in this file.
#   - A second spec (an EL one) would be ignored again by the "*.spec" line of .gitignore; the file
#     here is tracked, so it is not affected.
#
# Not in this spec yet (a later change adds them): the systemd user unit, the .desktop file, the
# AppStream metainfo and the icon.

%global app_id io.github.trensoft.slideshowlock
# [K] app_id is APP_ID of slideshow_lock/__init__.py: the GSettings schema id and the gettext domain

Name:           slideshow-lock
# [K] "slideshow-lock" is the name in pyproject.toml; the package name is not derived from APP_ID
Version:        1.0.0
# [K] pyproject.toml says version = "1.0.0"
Release:        1%{?dist}
# [H] a plain Release: with an explicit %%changelog below. %%autorelease/%%autochangelog are not
# used on purpose: the changelog would be built from the git log of this repository
Summary:        Idle slideshow screensaver for GNOME on Wayland that locks on input
License:        GPL-3.0-or-later
# Decision (TrenSoft, 2026-10-05 22:18), not read from the repository: LICENSE is the plain GPLv3
# text, pyproject.toml says license = { file = "LICENSE" } and the source files carry no SPDX header
# yet. [H] an SPDX expression in License: is what the guidelines ask for.
URL:            https://github.com/trensoft/slideshow-lock
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
# [H] the usual form of a GitHub tag tarball; it unpacks into slideshow-lock-%%{version}/

BuildArch:      noarch
# [K] pure Python: slideshow_lock/*.py only, no extension module

BuildRequires:  python3-devel
# [H] python3-devel brings in the %%pyproject_* macros; setuptools comes from
# %%pyproject_buildrequires, which reads [build-system] of pyproject.toml ([K]: setuptools>=68)
BuildRequires:  gettext
# [K] tools/i18n.sh build needs msgfmt (package: gettext, as the tool itself says)
BuildRequires:  glib2-devel
# [K] run.sh names glib2-devel for glib-compile-schemas; used in %%check only

# Needed by %%check (the import test) and by the program at run time.
# [K] the package names are the ones run.sh prints for each missing typelib:
#     gi -> python3-gobject, Gtk/Gdk 4.0 -> gtk4, Graphene 1.0 -> graphene,
#     GdkPixbuf 2.0 -> gdk-pixbuf2, Gio/GLib 2.0 -> glib2
# [K] run.sh itself says these names are "likely", not verified on RHEL 10.2
BuildRequires:  python3-gobject
BuildRequires:  gtk4
BuildRequires:  graphene
BuildRequires:  gdk-pixbuf2
BuildRequires:  glib2

Requires:       python3-gobject
Requires:       gtk4
Requires:       graphene
Requires:       gdk-pixbuf2
Requires:       glib2
# [K] the program loads these typelibs through gi.require_version (slideshow_lock/*.py);
# [H] a typelib dependency may also be written as typelib(Gtk) = 4.0, package names are used here
Recommends:     gstreamer1-plugins-base
# [K] optional: scaling.py falls back to GdkPixbuf bilinear without the GStreamer videoscale element
# (run.sh: "optional"). [H] gstreamer1-plugins-base is the package run.sh names for it.

%description
slideshow-lock starts a fullscreen picture slideshow on the screens of a GNOME session on Wayland
when the session has been idle, and locks the session when the user touches the keyboard or the
mouse. The pictures come from a folder, the settings have a GTK 4 window of their own, and the
interface follows the language of the session.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires
# [H] writes the build dependencies of pyproject.toml into the build root instead of a hand-kept list

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files slideshow_lock
# [K] [tool.setuptools.packages.find] include = ["slideshow_lock*"]: 16 .py files, no subpackage,
# no data files; data/ and po/ are not part of the wheel, so they are installed below

# The settings schema. [K] data/%%{app_id}.gschema.xml, run.sh compiles the same file into a cache
install -Dpm 0644 data/%{app_id}.gschema.xml \
    %{buildroot}%{_datadir}/glib-2.0/schemas/%{app_id}.gschema.xml
# [H] glib2 compiles installed schemas by itself (file trigger), so there is no scriptlet here

# The commands. [K] pyproject.toml has no [project.scripts] and the package has no __main__.py:
# run.sh starts the three programs as python3 -m slideshow_lock.<module>, and the same line is
# written into three small commands. The names slideshow-lock-<what> are new in this spec.
# [H] -P keeps the current directory out of sys.path: plain "python3 -m" puts it first, so a user who
# starts a command inside a directory with a hostile slideshow_lock/ or gi/ would run that code.
# Measured on Python 3.11.2: the hostile package loads without -P and is not found with it. -P exists
# from Python 3.11 on; [H] EL10 and current Fedora ship 3.12 or newer (not measured here).
install -d %{buildroot}%{_bindir}
printf '#!/bin/sh\nexec %%s -P -m slideshow_lock.service "$@"\n' '%{python3}' \
    > %{buildroot}%{_bindir}/%{name}-service
printf '#!/bin/sh\nexec %%s -P -m slideshow_lock.preferences "$@"\n' '%{python3}' \
    > %{buildroot}%{_bindir}/%{name}-settings
printf '#!/bin/sh\nexec %%s -P -m slideshow_lock.preview_app "$@"\n' '%{python3}' \
    > %{buildroot}%{_bindir}/%{name}-preview
chmod 0755 %{buildroot}%{_bindir}/%{name}-service \
    %{buildroot}%{_bindir}/%{name}-settings \
    %{buildroot}%{_bindir}/%{name}-preview

# The translations. [K] tools/i18n.sh build DIR writes DIR/<lang>/LC_MESSAGES/<APP_ID>.mo for the
# languages of po/LINGUAS; slideshow_lock/i18n.py reads <prefix>/share/locale by default
bash tools/i18n.sh build %{buildroot}%{_datadir}/locale
%find_lang %{app_id} --allow-no-translations
# [H] %%find_lang collects the .mo files into %%{app_id}.lang. [H] --allow-no-translations is
# there because po/LINGUAS may list no language yet; if rpm does not know the option, drop it
# once the catalogs are in the repository

%check
# [H] imports every module of the package: the typelibs must load, no display is needed to import
%pyproject_check_import
# [H] a dry run (nothing is written): the schema must compile; run.sh runs glib-compile-schemas on it too
glib-compile-schemas --strict --dry-run %{buildroot}%{_datadir}/glib-2.0/schemas

%files -f %{pyproject_files} -f %{app_id}.lang
%license LICENSE
# [H] %%pyproject_save_files may list the dist-info license file as well; harmless if so, not verified
%doc README.md
%{_bindir}/%{name}-service
%{_bindir}/%{name}-settings
%{_bindir}/%{name}-preview
%{_datadir}/glib-2.0/schemas/%{app_id}.gschema.xml

%changelog
* Tue Oct 06 2026 TrenSoft <trensoft@fedoraproject.org> - 1.0.0-1
- Initial package
