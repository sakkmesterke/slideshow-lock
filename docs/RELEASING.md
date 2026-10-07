# Releasing

The standing procedure for a release. It is a rule for the project; except for the dry run below,
nothing here has been run for 1.0.1 yet: the tag `v1.0.1` does not exist (`git ls-remote --tags origin`
lists `v1.0.0` only, 2026-10-07), so no step after "go" has run. What is known of the COPR builds of 1.0.0
is in the STATUS at the top of the spec.
`[H]` marks background knowledge or a claim about a server that was not measured.

"The releaser" below is the person who runs the release: they write the report, push the tag and
check the build.

Not touched by this procedure, and done by the maintainer only when needed: the COPR project, the
Fedora account, the webhook and the repository permissions.

## Before the tag: the report and the approval

Nothing is tagged before the maintainer has said "go". The release report to the maintainer has
four parts:

1. The version and the commit SHA that would be tagged.
2. What changed, in 3 to 5 lines.
3. Whether the commit is green: the tests, `rpmlint`, and the build on Fedora and on EPEL 10.
4. What was not checked and what has to be tried by hand.

The answer is "go" or "no". Without "go" no tag is pushed. The "go" is for the version and the
commit SHA of that report. If the commit changes after the "go", the old "go" does not count and a
new report is needed. A manual Rebuild in COPR before the tag runs on the same SHA that is
tagged, so that the build that was seen green is the build of the release.

The version in `pyproject.toml` and in `packaging/fedora/slideshow-lock.spec` is the tag without
the `v`; check both before the report. The open points about the release version and the tarball
are in `docs/copr.md`, section 4.

## After "go"

1. Tag: an annotated tag `v<version>` on the commit named in the report.
2. COPR builds from the webhook (below). Wait for the build and check every chroot.
3. In a clean container, `[H]` (background knowledge, not measured here; the project is not made
   yet):

   ```
   sudo dnf install dnf-plugins-core
   sudo dnf copr enable sakkmesterke/slideshow-lock
   sudo dnf install slideshow-lock
   ```

   `[H]` `dnf copr` needs `dnf-plugins-core`. `[H]` On AlmaLinux 10, Rocky Linux 10 and RHEL 10
   EPEL and the CRB repository have to be enabled first; how that is done differs on RHEL.
   `[H]` The owner name `sakkmesterke` is the name of the maintainer's COPR account.
4. The GitHub release, with the list of changes.
5. The report: done, per chroot, and what could not be checked.

## A failed step

If the build (step 2), the install test (step 3) or the GitHub release (step 4) fails, the tag is
not deleted and not moved. The report says what failed and proposes a fix. A fix is a new version
(for example 1.0.1) and goes through the report and the approval again.

## Pushing the tag, and what is measured

The releaser pushes the tag with `git push`, which uses the stored credential of the machine. The
procedure does not read, print or write the credential. The push was measured as a dry run with a
local annotated tag and `git push --dry-run origin refs/tags/<name>`: with the stored credential
the dry run lists `[new tag]` and exits 0; without a credential (`credential.helper` empty,
prompts off) it stops with `could not read Username` and exit 128. `git ls-remote --tags origin`
was empty after the dry run, and the local tag was deleted: no real tag was pushed.

What this does not show: a dry run does not send the tag, so it does not prove that the real push
is accepted. In particular, a tag rule or ruleset on the repository, if one is set later, is not
tried by it. The first real tag push is the real test.

The GitHub release (step 4) is an API call that needs an access token. The releaser does not do it
with the credential of the machine. It is done by a person who has an established, approved way
to make it, named in the report, or by the maintainer in the web interface.

## The webhook (tag only)

This is the setting, read from the COPR source (`fedora-copr/copr`, the frontend:
`webhooks_general.py` `webhooks_git_push`, and `packages_logic.py` `_tag_belongs_to_package` and
`commits_belong_to_package`). `[H]` That the live COPR server behaves as that source says is not
measured.

- GitHub webhook: only the event "Branch or tag creation". "Pushes" is off.
- URL: the COPR GitHub webhook address of the project, with `slideshow-lock/` added at the end.
  Without the package name a tag such as `v1.0.0` does not match. The address contains a secret
  (a uuid), so it is not written in the repository.
- COPR package: "Webhook rebuild" on. The committish `main` is used only by a manual Rebuild.

A push to a branch must not start a build; only the creation of a tag does.

Any tag creation with the package name in the URL starts a build, whatever the tag is called, and
a tag is not deleted (see "A failed step"), so a tag is never pushed to test the webhook. The
webhook is checked with the "ping" or "Redeliver" function of GitHub (COPR answers a ping with OK
and does not build) or with a push to a branch (it does not build).
