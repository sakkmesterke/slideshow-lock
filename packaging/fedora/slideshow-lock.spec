# Fedora and EL spec for slideshow-lock 1.0.0.
#
# Legend for the comments in this file:
#   [K]  known: read from this repository (the file is named) or from a source named in the comment
#   [M]  measured: a tool was run in a scratch container, the tool and its version are named in the
#        comment (the commands and the results are in the pull request)
#   [H]  background knowledge about RPM, systemd and the Fedora packaging guidelines, NOT verified
#
# STATUS: NOT BUILT. rpmbuild -bb, mock and fedora-review have never run on this file, and no Fedora
# or EL machine has seen it. What was run (rpm 4.18.0 from a Debian package, with the macro files of
# Fedora's python-rpm-macros and pyproject-rpm-macros and of systemd v256 put next to it): rpmspec
# --parse, rpmspec -q, rpmbuild -bs on a scratch source archive, the %%install lines on a scratch tree
# with empty stand-ins for the files of the other changes, and rpmlint 2.10.0 with its DEFAULT
# configuration (not Fedora's) on the spec and the source RPM: no error that comes from the file
# itself; what it still prints (no-signature, no-packager-tag, no-group-tag, no-buildroot-tag,
# invalid-license for GPL-3.0-or-later) belongs to that configuration [H], not measured with
# Fedora's. "It parses and lints" is not "it builds".
#
# Prerequisites that are not in this file:
#   - The tag v1.0.0 does not exist yet, so Source0 cannot be downloaded before it does. A protected
#     or signed tag and a SHA-512 of the tarball are to be fixed at release time. The release has to
#     be cut after po/*.po (the catalogs are in the repository now) and after the files listed next.
#   - The files this spec installs come from other changes and must be on main before this one is
#     merged: data/io.github.sakkmesterke.SlideshowLock.desktop.in,
#     data/io.github.sakkmesterke.SlideshowLock.metainfo.xml.in (the only copies in git: the installed
#     files are generated from them in %%install) and the two icons under
#     data/icons/hicolor/. packaging/slideshow-lock and data/slideshow-lock.service are the launcher
#     and the user unit.
# Decisions of the maintainer, taken outside this repository (not read from it):
#   - The name and e-mail address in %%changelog are the maintainer's choice (2026-10-05). The
#     repository is public, so they stay in the history of main.
#   - License GPL-3.0-or-later is the maintainer's decision (2026-10-05). The repository itself
#     has no SPDX or "or later" text yet.
# Open items, not code (they need a later step):
#   - A second spec (an EL one) would be ignored again by the "*.spec" line of .gitignore; the file
#     here is tracked, so it is not affected.
#   - The unit is not enabled by the package by itself. %%systemd_user_post runs "systemctl --no-reload
#     preset --global" on the first install [M: systemd-update-helper.in of systemd v256, read], so
#     the unit is enabled for everybody only if a preset of the distribution says so; no preset is
#     shipped here; the on/off toggle that the settings window is meant to get (docs/service.md,
#     section 7) would enable it.
#   - %%systemd_user_postun is empty in systemd v256 [M: macros.systemd.in, read]; the upgrade does not
#     restart the running service (%%systemd_user_postun_with_restart would). Whether it should is a
#     decision, not taken here.
#   - %%check runs the whole test suite only with "--with tests" (off by default): it needs a private
#     dbus-daemon and the GTK 4 typelibs, and no build root has been tried with it.
# Open questions, NOT measured (background knowledge only, do not read them as verified):
#   - [H] PyGObject>=3.42 in the dependencies makes the generated Requires ask for
#     python3dist(pygobject); [M] a Fedora 43 rpmlint run of the earlier spec says the requirement is
#     generated (python-leftover-require), not measured by me; whether python3-gobject provides it on
#     EL10 is not known.
#   - [H] %%{_metainfodir} is not defined by rpm itself [M: not in macros.in of rpm 4.18.2, 4.19.1
#     and 4.20.0, read]; the line below defines it when nothing else does.
#   - [H] the package names of the build requirements and of the typelibs on RHEL 10, AlmaLinux 10,
#     Rocky 10 (run.sh says itself that its names are "likely, not verified on RHEL 10.2").
#   - [H] setuptools of EL10: [M] setuptools 84.0.0 builds the wheel of this repository with one
#     deprecation warning for license = { file = "LICENSE" } (a deadline of 2027-Feb-18 in the
#     message), no failure; the version on EL10 and Fedora is not known.

%global app_id io.github.sakkmesterke.SlideshowLock
# [K] app_id is APP_ID of slideshow_lock/__init__.py: the GSettings schema id and the gettext domain,
# and the name of the .desktop file, the metainfo file and the icons (the app id of the program)

%{!?_metainfodir:%global _metainfodir %{_datadir}/metainfo}
# [H] the usual place of AppStream metainfo files is /usr/share/metainfo

%bcond tests 0
# [H] "%%bcond tests 0" defines the switch off by default; "rpmbuild --with tests" turns it on

Name:           slideshow-lock
# [K] "slideshow-lock" is the name in pyproject.toml; the package name is not derived from APP_ID
Version:        1.0.0
# [K] pyproject.toml says version = "1.0.0"
Release:        1%{?dist}
# [H] a plain Release: with an explicit %%changelog below. %%autorelease/%%autochangelog are not
# used on purpose: the changelog would be built from the git log of this repository
Summary:        Idle slideshow screensaver for GNOME on Wayland that locks on input
License:        GPL-3.0-or-later
# The maintainer's decision (2026-10-05), not read from the repository: LICENSE is the plain GPLv3
# text, pyproject.toml says license = { file = "LICENSE" } and the source files carry no SPDX header
# yet. [H] an SPDX expression in License: is what the guidelines ask for.
URL:            https://github.com/sakkmesterke/slideshow-lock
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
# [H] the usual form of a GitHub tag tarball; it unpacks into slideshow-lock-%%{version}/
# The tag v1.0.0 does not exist yet (see the prerequisites at the top).

BuildArch:      noarch
# [K] pure Python: slideshow_lock/*.py only, no extension module

BuildRequires:  python3-devel
# [H] python3-devel brings in the %%pyproject_* macros; setuptools comes from
# %%pyproject_buildrequires, which reads [build-system] of pyproject.toml ([K]: setuptools>=68)
BuildRequires:  gettext
# [K] tools/i18n.sh build and data need msgfmt (package: gettext, as the tool itself says)
# [H] msgfmt --xml finds the ITS rules of the metainfo in the data directory of the gettext
# installation (tools/i18n.sh says so); whether the Fedora and EL 10 gettext package has them is not
# measured: the build on those systems has to show it.
BuildRequires:  glib2-devel
# [K] run.sh names glib2-devel for glib-compile-schemas; used in %%check only
BuildRequires:  systemd-rpm-macros
# [H] the package of %%{_userunitdir} and %%systemd_user_*; its macros were read in the source of
# systemd v256 (src/rpm/macros.systemd.in), which is where the package takes them from
BuildRequires:  desktop-file-utils
# [H] desktop-file-validate is in desktop-file-utils; used in %%check only
BuildRequires:  appstream
# [H] appstreamcli is in the package appstream; used in %%check only

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
%if %{with tests}
# [K] the test run (.github/workflows/ci.yml) needs pytest, the GStreamer typelib and dbus-daemon
# next to the above; [H] the package names
BuildRequires:  python3-pytest
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  dbus-daemon
%endif

Requires:       gtk4
Requires:       graphene
Requires:       gdk-pixbuf2
# [K] the program loads these typelibs through gi.require_version (slideshow_lock/*.py);
# [H] a typelib dependency may also be written as typelib(Gtk) = 4.0, package names are used here
# There is no "Requires: python3-gobject" and no "Requires: glib2" on purpose. [M: by the maintainer's
# team in a Fedora 43 container, with the earlier spec, which had both lines; not measured by me]
# rpmlint printed python-leftover-require for python3-gobject (the generated python3dist(pygobject)
# requirement covers it, see the open question above) and explicit-lib-dependency for glib2. [H] glib2
# arrives through gtk4 and gdk-pixbuf2, which link its shared libraries, and it is what python3-gobject
# needs as well; the Fedora 43 and EPEL 10 builds of this spec have to show that nothing is missing.
Requires:       hicolor-icon-theme
# [H] the package that owns the hicolor directories the icons go into
Recommends:     gstreamer1-plugins-base
# [K] optional: scaling.py falls back to GdkPixbuf bilinear without the GStreamer videoscale element
# (run.sh: "optional"). [H] gstreamer1-plugins-base is the package run.sh names for it.

%description
slideshow-lock starts a full-screen picture slideshow on the screens of a
GNOME session on Wayland when the session has been idle, and locks the
session when the user touches the keyboard or the mouse. The pictures come
from a folder, the settings have a GTK 4 window of their own, and the
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
%pyproject_save_files -l slideshow_lock
# [K] [tool.setuptools.packages.find] include = ["slideshow_lock*"]: 16 .py files, no subpackage,
# no data files; data/, packaging/ and po/ are not part of the wheel, so they are installed below
# [M] the wheel built by setuptools 84.0.0 has METADATA with "License-File: LICENSE" and the file
# in slideshow_lock-1.0.0.dist-info/licenses/LICENSE. [K] %%pyproject_save_files marks the files
# named in License-File as %%license (pyproject_save_files.py of pyproject-rpm-macros, read), so
# there is no %%license line in %%files; -l asks the macro to fail the build if it finds none.

# The command. [K] pyproject.toml has no [project.scripts]; packaging/slideshow-lock is the one
# launcher: "slideshow-lock service|settings|preview" starts slideshow_lock.service, .preferences
# or .preview_app (the sub-commands of run.sh). The unit and the .desktop file call it.
install -Dpm 0755 packaging/%{name} %{buildroot}%{_bindir}/%{name}

# The settings schema. [K] data/%%{app_id}.gschema.xml, run.sh compiles the same file into a cache
install -Dpm 0644 data/%{app_id}.gschema.xml \
    %{buildroot}%{_datadir}/glib-2.0/schemas/%{app_id}.gschema.xml
# [H] glib2 compiles installed schemas by itself (file trigger), so there is no scriptlet here

# The user unit. [K] data/slideshow-lock.service runs "/usr/bin/slideshow-lock service"
install -Dpm 0644 data/%{name}.service %{buildroot}%{_userunitdir}/%{name}.service
# [M] %%{_userunitdir} is set from USER_DATA_UNIT_DIR, which is prefixdir / 'lib/systemd/user' in
# meson.build of systemd v256 (read): /usr/lib/systemd/user; [H] the same value on the target distributions

# The desktop entry, the AppStream metainfo and the icons. [K] the app id names all four files.
# [K] the .desktop file and the metainfo are generated from data/<app id>.desktop.in and
# data/<app id>.metainfo.xml.in with the translations of the catalogs: "tools/i18n.sh data DIR" writes
# DIR/<app id>.desktop and DIR/<app id>.metainfo.xml (msgfmt --desktop and msgfmt --xml). The directory is
# relative to the source tree, which is the current directory in %%install.
bash tools/i18n.sh data generated-data
install -Dpm 0644 generated-data/%{app_id}.desktop \
    %{buildroot}%{_datadir}/applications/%{app_id}.desktop
install -Dpm 0644 generated-data/%{app_id}.metainfo.xml \
    %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
install -Dpm 0644 data/icons/hicolor/scalable/apps/%{app_id}.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
install -Dpm 0644 data/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg \
    %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg
# [H] the icon cache is refreshed by a file trigger of the icon theme packages: no scriptlet here

# The translations. [K] tools/i18n.sh build DIR writes DIR/<lang>/LC_MESSAGES/<APP_ID>.mo for the
# languages of po/LINGUAS (de, es, fr, hu, it); slideshow_lock/i18n.py reads <prefix>/share/locale
bash tools/i18n.sh build %{buildroot}%{_datadir}/locale
%find_lang %{app_id}
# [M] find-lang.sh of rpm 4.18.0 (the same option parsing as in rpm 4.19.1, read): with the five
# catalogs in a scratch build root it writes %%{app_id}.lang with five %%lang(..) lines, exit 0.
# Without a catalog it prints "No translations found" and exits 1, so a build that lost its
# catalogs fails instead of shipping an English-only package. The option --allow-no-translations of
# the earlier version of this spec is NOT in find-lang.sh of rpm 4.18.0 or 4.19.1: it is taken as the
# name of the output file, and the build stops with "No translations found" even when the catalogs
# are there [M: run with the option, exit 1]. It is gone. [H] a Fedora rpm may carry a patch for it.

%check
# [H] imports every module of the package: the typelibs must load, no display is needed to import
%pyproject_check_import
# [H] a dry run (nothing is written): the schema must compile; run.sh runs glib-compile-schemas on it too
glib-compile-schemas --strict --dry-run %{buildroot}%{_datadir}/glib-2.0/schemas
# [H] the validators of the files that were installed (the installed paths, not the source tree)
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
%if %{with tests}
%pytest
# [K] pyproject.toml: testpaths = ["tests"]; [H] the suite needs no display (CI runs it headless)
%endif

# [M] from the systemd v256 macros and systemd-update-helper (read): %%systemd_user_post runs
# "systemctl --no-reload preset --global <unit>" on the first install only;
# %%systemd_user_preun, on removal (not on upgrade), runs "systemctl --user -M <uid>@ disable --now"
# for every logged-in user; %%systemd_user_postun is empty. [H] the macros of the systemd-rpm-macros
# package on the target distributions are those of the systemd of that distribution.

%post
%systemd_user_post %{name}.service

%preun
%systemd_user_preun %{name}.service

%postun
%systemd_user_postun %{name}.service

%files -f %{pyproject_files} -f %{app_id}.lang
%doc README.md
%{_bindir}/%{name}
%{_datadir}/glib-2.0/schemas/%{app_id}.gschema.xml
%{_userunitdir}/%{name}.service
%{_datadir}/applications/%{app_id}.desktop
%{_metainfodir}/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg

%changelog
* Tue Oct 06 2026 Attila Alexovics <info@alexovicsattila.com> - 1.0.0-1
- Initial package
