# COPR build

How the RPM is built in COPR, and what is decided and what is not. Nothing here has been run in
COPR: the project does not exist yet. `[H]` marks background knowledge that was not measured.

COPR builds the package itself, from the spec, on its own server (`[H]` with `mock`, one build per
chroot). That build is the real build test: nothing here builds the package locally. What runs
locally is `rpmlint`, `desktop-file-validate` and `appstreamcli validate`. What the COPR server
does is not measurable from here, so what is said about it below is a claim, not a fact.

## 1. Source of the build

The spec's `Source0` is the GitHub tarball of the tag `v%{version}`. The tag does not exist yet, so
COPR cannot download it. Options:

| | Source | Risk |
|---|---|---|
| (a) | COPR source type "Make srpm": `.copr/Makefile` builds the tarball from the checkout with `git archive` and runs `rpmbuild -bs` | the tarball is of a commit, not of a tag: a build before the release is a test build of the named version |
| (b) | the spec stays as it is, the tag comes at the release, COPR source type "SCM" with the spec file | cannot build at all before the tag exists, and the build downloads `Source0` from the network |

Decision: (a) now. The Makefile makes `slideshow-lock-<version>.tar.gz` (name and version are read
from the spec, the top directory is `slideshow-lock-<version>/`, which is what `%autosetup`
expects) and hands it to `rpmbuild` as the source directory, so the spec is not edited and nothing
is downloaded. It needs git and rpmbuild only and uses no secret.

Limits of (a):

- The version in a build before the release is `1.0.0-1`, built from whatever commit COPR checked
  out. Such a build must not be mistaken for the release.
- A `git archive` tarball and the GitHub tag tarball hold the same files but are not the same
  bytes. A checksum recorded for the release belongs to one of the two (section 5).
- `rpmbuild -bs` reads the whole spec in a chroot where the build dependencies are not installed:
  an unknown macro stays as plain text. `[H]` this is how COPR source builds usually work.
- The Makefile passes `safe.directory` to git on the command line, because the checkout may belong
  to another user than the one running the build. `[H]`

Measured (a container with git, make and tar; no rpmbuild): `make -f .copr/Makefile tarball
outdir=DIR` writes `DIR/sources/slideshow-lock-1.0.0.tar.gz` with every file under
`slideshow-lock-1.0.0/` and no `.git`, also when started from a subdirectory, with a relative or an
absolute spec path, and inside `unshare -rn` (no network). A missing spec or a directory that is not
a git checkout stops with a non-zero exit. Not measured: the `rpmbuild -bs` step, and the way COPR
itself calls the Makefile.

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

Translations. The `.desktop` file and the AppStream metainfo are translated with `msgfmt` at build
time, so `gettext` must be in every chroot. The spec lists it as `BuildRequires: gettext`, and COPR
installs the build requirements into the chroot `[H]`; the source RPM step of section 1 does not
need it. The check is the same first build in each chroot.

## 3. Creating the project

One step for the owner of the Fedora account, nothing is stored in the repository:

1. Project name `slideshow-lock`, owned by the maintainer's Fedora account.
2. Chroots: the list of section 2.
3. Package source type: SCM (git), clone URL `https://github.com/sakkmesterke/slideshow-lock.git`,
   committish `main`, subdirectory empty, spec file `packaging/fedora/slideshow-lock.spec`, build
   method "Make srpm" (the file is `.copr/Makefile`).
4. Webhook: none for now. Automatic rebuilds: off. Every build is started by hand until this is
   decided otherwise.
5. Internet access during the build: off (the default). The build needs none: `Source0` comes from
   the Makefile.
6. A short description and instructions text for the project page: what the package is, and that
   a build made before the release tag is a test build.

## 4. Before the release

These are open and are not part of this change:

- the tag `v1.0.0`, and the maintainer's approval of it;
- a protected or signed tag, so that it cannot be moved later;
- the SHA-512 of the release tarball (which of the two tarballs of section 1 is meant, decided
  first);
- the release date in the AppStream metainfo, when that file exists;
- a COPR build in each chroot (section 2) that succeeded, and `rpmlint` on the result.
