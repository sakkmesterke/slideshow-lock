# COPR build

How the RPM is built in COPR, and what is decided and what is not. Nothing here has been run in
COPR: the project does not exist yet. `[H]` marks background knowledge that was not measured.

COPR builds the package itself, from the spec, on its own server (`[H]` with `mock`, one build per
chroot). That build is the real build test: nothing here builds the package locally. Locally,
`rpmlint` can run on the spec and, once COPR has built them, on the RPMs. `desktop-file-validate`
and `appstreamcli validate` have nothing to check yet: the repository has no `.desktop` file and no
AppStream metainfo (the spec says a later change adds them); they run on those files once they
exist. What the COPR server does is not measurable from here, so what is said about it below is a
claim, not a fact.

## 1. Source of the build

The spec's `Source0` is the GitHub tarball of the tag `v%{version}`. The tag of the next release,
`v1.0.2`, does not exist yet (`git ls-remote --tags origin` lists `v1.0.0` and `v1.0.1` only,
2026-10-07), so COPR cannot download it. Options:

| | Source | Risk |
|---|---|---|
| (a) | COPR source type "SCM", build method "Make srpm": `.copr/Makefile` builds the tarball from the checkout with `git archive` and runs `rpmbuild -bs` | the tarball is of a commit, not of a tag: a build before the release is a test build of the named version |
| (b) | the spec stays as it is, the tag comes at the release, COPR source type "SCM" with a build method that uses the spec file as it is (`[H]` which methods there are) | cannot build at all before the tag exists, and the build downloads `Source0` from the network |

Decision: (a) now. The Makefile makes `slideshow-lock-<version>.tar.gz` (name and version are read
from the spec, the top directory is `slideshow-lock-<version>/`, which is what `%autosetup`
expects) and hands it to `rpmbuild` as the source directory, so the spec is not edited and nothing
is downloaded. It needs git, gzip, rpmbuild, sed, grep and head only and uses no secret.

Limits of (a):

- A build before the release is a test build. It is not made in the release project
  (`docs/RELEASING.md`, "What may be built in the COPR project"), and it has a lower `Release` than
  the release, such as `1.0.2-0.1.test`.
- A `git archive` tarball and the GitHub tag tarball hold the same files but are not the same
  bytes. A checksum recorded for the release belongs to one of the two (section 5).
- `rpmbuild -bs` reads the whole spec in a chroot where the build dependencies are not installed:
  an unknown macro stays as plain text. `[H]` this is how COPR source builds usually work.
- The `tools` target runs first, before the tarball and so before the source RPM. `[H]` COPR starts
  the source build in a bare chroot, where these commands may not be installed, so it checks that
  `git`, `gzip`, `rpmbuild`, `sed`, `grep` and `head` are there. If all are, it does nothing and runs
  nothing. If some are missing and the user is root and `dnf` exists, it runs `dnf -y install
  git-core rpm-build gzip` and checks again. Otherwise it stops with an error that lists what is
  missing and the `dnf install` command. It never uses `sudo`. `sed`, `grep` and `head` are only
  checked, not installed: they are part of any base image `[H]`. The target is a prerequisite of
  `tarball`, so `rpmbuild` is required for the tarball alone, too. It can be run by itself:
  `make -f .copr/Makefile tools`.
- The Makefile passes `safe.directory` to git on the command line, because the checkout may belong
  to another user than the one running the build. `[H]` It also passes `tar.tar.gz.command=gzip -cn`:
  without it, a `tar.tar.gz.command` set in the checkout's own `.git/config` would be run by
  `git archive` (measured below), and `[H]` `safe.directory='*'` is what lets git trust the config
  of a checkout of another owner. The command line takes precedence, so the tarball is always
  compressed by `gzip -cn`. The Makefile does not run `git config --global --add safe.directory
  "$(CURDIR)"` instead: that writes to the global git config of whoever runs the build, outside the
  checkout, and `--add` appends one more line each time it runs (measured: three runs, three
  identical lines). The `-c` form changes no file (measured: a run with an empty `HOME` left it
  empty).
- A relative `spec` is taken from the root of the checkout, not from the directory `make` was
  started in: from `packaging/fedora`, `spec=slideshow-lock.spec` is not found, and
  `spec=packaging/fedora/slideshow-lock.spec` is.
- The tarball is of the `HEAD` commit. A change that is not committed, and a file that git does not
  track, are not in it.
- `Name:` and `Version:` are read from the first such line of the spec and must be of the
  characters `[A-Za-z0-9._+-]`: any other value (a macro, a blank, a quote) stops the build with an
  error, because the value goes into a file name and into a shell command.
- A path with a space or a quote (the checkout, the Makefile, the spec or `outdir`) stops the build
  with an error: the recipes quote the paths, and `$(abspath)` splits them at a space, which would
  write the tarball to a different place without a word.

Measured on the tree of 1.0.0, not repeated for 1.0.1 (a container with git, GNU make 4.3, gzip and
tar; no rpmbuild): `make -f .copr/Makefile tarball outdir=DIR` writes
`DIR/sources/slideshow-lock-1.0.0.tar.gz` with 123 entries (106 files and 17 directories), all under
`slideshow-lock-1.0.0/` and none of them `.git`, also when started from a subdirectory, with a relative
or an absolute spec path, and inside `unshare -rn` (no network).
These stop with a non-zero exit: a missing spec, a directory that is not a git checkout, a path
with a space or a quote, and a `Version:` or `Name:` with a quote or a macro (a `Version:` that
closes the quote and runs a command made no marker file). A `tar.tar.gz.command` in the checkout's
`.git/config` was not run. Not measured: the `rpmbuild -bs` step, a checkout of another owner (only
the setting in the checkout's own config was tried), and the way COPR itself calls the Makefile.

The `tools` target was measured with a private `PATH` of only the commands under test, a stub
`rpmbuild` and a stub `dnf` that records its calls, and with `id -u` stubbed for the root case. With
all commands present it exits 0 and the `dnf` stub is not called, as root too. With `rpmbuild`
missing, as root with `dnf`: the `dnf` stub is called once with `-y install git-core rpm-build gzip`
and the build goes on; if the stub installs nothing, or fails, the build stops with an error. With
`rpmbuild` missing and not root, or with no `dnf`: the build stops with an error, `dnf` is not
called and no tarball is written (also under `make -j8`). Not measured: a real `dnf` install, a
real `rpmbuild`, and a bare Fedora 43 container: that run is the real test of this target.

## 2. Targets

The package is `noarch`, so one architecture is enough: the same RPM serves the others.

| Requirement | COPR chroot `[H]` (the names are read from the COPR chroot list when the project is made) |
|---|---|
| Fedora | `fedora-rawhide-x86_64`, and the Fedora releases that are supported at that time (for example `fedora-44-x86_64`, `fedora-43-x86_64`) |
| RHEL 10, AlmaLinux 10, Rocky 10 | `epel-10-x86_64` |

AlmaLinux and Rocky are rebuilds of RHEL, so one EPEL 10 build is meant for all three; RHEL 10
itself takes the same package. `[H]` the relation of the RHEL rebuilds to EPEL 10 and to the COPR
chroot is background knowledge. It is not stated as working until a build in the chroot has run.

Dependencies. The spec asks for `python3-gobject`, `gtk4`, `graphene`, `gdk-pixbuf2` and `glib2`,
and for `glib2-devel` and `gettext` at build time. That they exist under EPEL 10 (and in which
repository) comes from the package names printed by `run.sh`, which the script itself calls
"likely". It is not measured and is not claimed. It is measured by a `mock` build of the SRPM in
`epel-10-x86_64` (and in a Fedora chroot), which also shows whether the `%pyproject_*` macros are
there. That mock build is the first COPR build in each chroot: it is not repeated locally. Until it
has run, no EPEL 10 or RHEL 10 target is claimed to work.

Translations. The `.mo` catalogs are compiled with `msgfmt` at build time (`tools/i18n.sh build`,
called from the `%install` of the spec), so `gettext` must be in every chroot. The spec lists it as
`BuildRequires: gettext`, and COPR installs the build requirements into the chroot `[H]`; the source
RPM step of section 1 does not need it. The check is the same first build in each chroot. A
`.desktop` file or metainfo that a later change translates the same way needs nothing more.

## 3. Creating the project

One step for the owner of the Fedora account, nothing is stored in the repository:

1. Project name `slideshow-lock`, owned by the maintainer's Fedora account.
2. Chroots: the list of section 2.
3. Package source type: SCM (git), clone URL `https://github.com/trensoft/slideshow-lock.git`,
   subdirectory empty, spec file `packaging/fedora/slideshow-lock.spec`, build method "Make srpm"
   (the file is `.copr/Makefile`). The committish is not `main`: a build is of the tag that started
   it (the source log of build 11084848 shows `'committish': 'v1.0.0'`).
4. Webhook: only the creation of a tag (`docs/RELEASING.md`, "The webhook (tag only)"). No build is
   started by hand (`docs/RELEASING.md`, "What may be built in the COPR project").
5. Internet access during the build: off (the default). `[H]` The build then needs none, because
   `Source0` is the tarball the Makefile made and is in the source RPM. Measured is only that the
   Makefile itself works without a network (section 1); what COPR does with this setting is not.
6. A short description and instructions text for the project page: what the package is, and that
   a build made before the release tag is a test build.

## 4. Before the release

These are open and are not part of this change:

- the tag `v1.0.2`, and the maintainer's approval of it;
- a protected or signed tag, so that it cannot be moved later;
- the SHA-512 of the release tarball (which of the two tarballs of section 1 is meant, decided
  first);
- the release date in the AppStream metainfo, when that file exists;
- a COPR build in each chroot (section 2) that succeeded, and `rpmlint` on the result;
- the version of a test build and of the release: decided, see `docs/RELEASING.md`, "What may be
  built in the COPR project". The release is `1.0.2-1`; a test build is lower and is not made in
  this project.
