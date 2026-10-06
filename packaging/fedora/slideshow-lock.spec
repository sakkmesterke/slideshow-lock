# Fedora and EL spec for slideshow-lock 1.0.0.
#
# Legend for the comments in this file:
#   [K]  known: read from this repository (the file is named) or from a source named in the comment
#   [M]  measured: a tool was run (in a scratch root or in COPR), the tool and its version, or the
#        COPR build, are named in the comment
#   [H]  background knowledge about RPM, systemd and the Fedora packaging guidelines, NOT verified
#
# STATUS (2026-10-06): built in COPR (project sakkmesterke/slideshow-lock) in all four chroots, in two
# builds: 11084376 of main 0ac5c23 before the fix of the cairo typelib, which failed on the three Fedora
# chroots, and 11084605 of main 2f45728 (the committish in the log of the SRPM build) after the fix,
# which succeeded on all four. COPR runs rpmbuild in one mock build root per chroot. [M] read from the
# COPR API (api_3) and the builder logs of the build 11084605, which started at 2026-10-06 12:05 UTC
# and whose last chroot finished at 12:10 UTC:
#   epel-10-x86_64, fedora-43-x86_64, fedora-44-x86_64, fedora-rawhide-x86_64 (the last one made fc46
#                       packages): all four SUCCEEDED; %%check ran to the end in each of them (the
#                       import check of 16 modules, glib-compile-schemas --strict --dry-run,
#                       desktop-file-validate, appstreamcli validate --no-net: no error; the last
#                       one prints "developer-info-missing" as its one info and says "infos: 1,
#                       pedantic: 2", the two pedantic tags are not named in the log) and the
#                       noarch RPM was written
# Build 11084376 (before the fix) SUCCEEDED on epel-10-x86_64; on fedora-43, fedora-44
# and fedora-rawhide (x86_64) it FAILED in %%check, all three with the same error:
# %%pyproject_check_import, "Typelib file for namespace 'cairo', version '1.0' not found", for
# slideshow_lock.preferences, .preview_app, .preview_window and .service. The cause, and the
# BuildRequires and Requires that fix it, are named at those two lines below. Before the second COPR
# build the fix was also tried in rootless Fedora 43 and CentOS Stream 10 build roots made of the
# packages (exact versions) of the COPR logs, with rpmbuild 6.0.2 and 4.19.1.1 of those roots, no
# scriptlets run: the build, %%check included, passed on both.
# [M] rpmlint 2.8.0 and fedora-review ran in the COPR build 11084605 on the three Fedora chroots (the
# results are in the fedora-review/ directory of each: review.txt, rpmlint.txt). rpmlint ran with the
# Fedora configuration (/etc/xdg/rpmlint/fedora.toml) on the built noarch RPM and on the source RPM,
# and printed the same in fedora-43, fedora-44 and fedora-rawhide: 0 errors, 2 warnings
# (no-manual-page-for-binary and empty-%%postun), 7 filtered. fedora-review (rc 0) wrote its template;
# the review.txt of the three chroots lists 70 items (counted with the pattern "^\[.\]:" so that the two
# legend lines are not counted): 35 marked "[x]", 2 marked "[!]" (the download of Source0, which fails
# because the tag v1.0.0 does not exist yet, and the reminder to test the build in mock) and 33 marked
# "[ ]" (manual review needed, still open); it has 1 entry under "Issues:" (the systemd user unit
# scriptlets, which the %%post and %%preun of this spec provide). There is no fedora-review/ directory
# and no rpmlint output on epel-10, and none for the build 11084376 (no such directory in any of its
# chroots).
# NOT run: mock by hand, rpmlint and fedora-review on epel-10, an install of the RPM on a Fedora or EL
# machine, the test suite (--with tests), a real GNOME session. The review.txt says "[x]: Package
# installs properly" because fedora-review installed the built package in a mock root of COPR (the log
# says "Installing built package(s)", with the mock configuration of the build); that is not an
# install on a Fedora or EL machine, and whether the root held the BuildRequires is not read from
# the log.
#
# Prerequisites that are not in this file:
#   - The tag v1.0.0 does not exist yet, so Source0 cannot be downloaded before it does. A protected
#     or signed tag and a SHA-512 of the tarball are to be fixed at release time. The release has to
#     be cut after po/*.po (the catalogs are in the repository) and after the files listed next.
#   - The files this spec installs are in the repository and must stay there:
#     data/io.github.sakkmesterke.SlideshowLock.desktop.in,
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
#     generated (python-leftover-require), not measured again here; whether python3-gobject provides it on
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
# installation (tools/i18n.sh says so). [M] in the COPR build 11084605 "bash tools/i18n.sh data
# generated-data" (it runs msgfmt --desktop and msgfmt --xml, [K] tools/i18n.sh) ended without error
# in %%install in all four chroots, and appstreamcli validate accepted the metainfo it wrote, so the
# gettext of those four build roots has what it needs.
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
BuildRequires:  gobject-introspection
# [M] cairo-1.0.typelib is a file of the package gobject-introspection (the rpm of Fedora 43, 44 and
# rawhide and the filelists of the Fedora 43 and CentOS Stream 10 repositories, read). No module of
# this package imports cairo: the typelibs of Gtk 4.0 and Gdk 4.0 depend on cairo 1.0, and loading
# them loads it. [M] In the Fedora 43 updates repository (primary metadata read) gtk4 4.20.4-1.fc43 and
# python3-gobject-base 3.54.5-4.fc43 do not require gobject-introspection, so the minimal Fedora build
# root of COPR (build 11084376) had no cairo typelib and the import check of %%check failed there. In
# the Fedora 43 release repository the python3-gobject-base 3.54.3-1.fc43 still requires it
# (gobject-introspection(x86-64)); its gtk4 4.20.2-1.fc43 does not. python3-gobject-base 3.46.0-7.el10
# requires it as well (read in the primary metadata of the CentOS Stream 10 BaseOS repository), which is
# why the epel-10 chroot passed.
%if %{with tests}
# [K] the test run (.github/workflows/ci.yml) needs pytest, the GStreamer typelib and dbus-daemon
# next to the above; [H] the package names
BuildRequires:  python3-pytest
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  dbus-daemon
%endif

# [K] the program loads these typelibs through gi.require_version (slideshow_lock/*.py);
# [H] a typelib dependency may also be written as typelib(Gtk) = 4.0, package names are used here
# Not listed on purpose: python3-gobject and glib2 (no "Requires: python3-gobject", no "Requires:
# glib2"). [M: in a Fedora 43 container, with the earlier spec, which had both lines; not measured
# again here] rpmlint printed python-leftover-require for python3-gobject (the generated
# python3dist(pygobject) requirement covers it, see the open question above) and
# explicit-lib-dependency for glib2. [H] glib2 arrives through gtk4 and gdk-pixbuf2, which link its
# shared libraries, and it is what python3-gobject needs as well. [M] the COPR build 11084605 passed
# on all four chroots, but its build roots have python3-gobject and glib2 installed (BuildRequires), so
# it does not show that nothing is missing at run time; only an install of the RPM on a minimal Fedora
# or EL machine can (NOT run, see STATUS). The one typelib package that is listed by name,
# gobject-introspection, is explained at its own line.
Requires:       gtk4
Requires:       graphene
Requires:       gdk-pixbuf2
# [M] the cairo typelib is needed at run time as well: in a Fedora 43 root made of the requirements of
# this package, with the dependencies resolved by hand inside the package set of the COPR build,
# gtk4 and python3-gobject-base do not bring gobject-introspection, and "from gi.repository import
# Gtk" fails with the missing cairo typelib; with the line below the import of Gtk and of
# slideshow_lock.preferences, .service, .preview_app and .preview_window works. EL10 gets the package
# through python3-gobject-base already; the line is for Fedora.
Requires:       gobject-introspection
Requires:       hicolor-icon-theme
# [H] the package that owns the hicolor directories the icons go into
Recommends:     gstreamer1-plugins-base
# [K] optional: scaling.py falls back to GdkPixbuf bilinear without the GStreamer videoscale element
# (run.sh: "optional"). [H] gstreamer1-plugins-base is the package run.sh names for it.

# Picture formats (docs/image-source.md, "Picture formats"): on EL10 the loaders for TIFF and GIF are in
# gdk-pixbuf2-modules (AppStream, base repositories), and those for BMP and WebP are in packages that
# only EPEL 10 has: gdk-pixbuf2-modules-extra and webp-pixbuf-loader. [M: by the solution architect, from
# the RPM file lists and primary.xml of Rocky 10.2 and EPEL 10; not measured again here] The EPEL ones
# are weak dependencies on purpose: the package must not need EPEL. [H] dnf installs a weak dependency
# that no enabled repository has without a message and without failing; not measured. Fedora needs none
# of these (gdk-pixbuf2 pulls in glycin, which loads the formats); NOT measured on Fedora 43 or later.
%if 0%{?rhel}
Requires:       gdk-pixbuf2-modules
Recommends:     webp-pixbuf-loader
Recommends:     gdk-pixbuf2-modules-extra
%endif

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
# launcher: "slideshow-lock service|settings|preview" starts slideshow_lock.service, .settings_app
# or .preview_app (the sub-commands of run.sh). The unit and the .desktop file call it.
install -Dpm 0755 packaging/%{name} %{buildroot}%{_bindir}/%{name}
# The short command. [K] packaging/slideshowlock is a small sh launcher: "slideshowlock" is
# "slideshow-lock settings" (the settings window); a separate file, no symlink, no argv0 test.
install -Dpm 0755 packaging/slideshowlock %{buildroot}%{_bindir}/slideshowlock

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

# [M] from the systemd v256 macros and systemd-update-helper (read), and the macros and the helper of
# systemd 258.11 of Fedora 43 (read): %%systemd_user_post runs "systemctl --no-reload preset --global
# <unit>" on the first install only; %%systemd_user_preun, on removal (not on upgrade), runs
# "systemctl --global disable --no-warn <unit>" and, only when /run/systemd/system exists (systemd is
# the running init), also "systemctl --user -M <uid>@ disable --now --no-warn <unit>" for every
# logged-in user; %%systemd_user_postun is empty. [H] the macros of the systemd-rpm-macros package on
# the target distributions are those of the systemd of that distribution.

%post
%systemd_user_post %{name}.service

%preun
%systemd_user_preun %{name}.service

%postun
%systemd_user_postun %{name}.service

%files -f %{pyproject_files} -f %{app_id}.lang
%doc README.md
%{_bindir}/%{name}
%{_bindir}/slideshowlock
%{_datadir}/glib-2.0/schemas/%{app_id}.gschema.xml
%{_userunitdir}/%{name}.service
%{_datadir}/applications/%{app_id}.desktop
%{_metainfodir}/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg

%changelog
* Tue Oct 06 2026 Attila Alexovics <info@alexovicsattila.com> - 1.0.0-1
- Initial package
