# Releasing

The standing procedure for a release. It is a rule for the team; nothing here has been run yet:
no tag exists, no COPR project is made and no COPR build has run. `[H]` marks background
knowledge or a claim about a server that was not measured.

Not touched by this procedure, and done by the maintainer only when needed: the COPR project, the
Fedora account, the webhook and the repository permissions.

## Before the tag: the report and the approval

Nothing is tagged before the maintainer has said "go". The release report to the maintainer has
four parts:

1. The version and the commit SHA that would be tagged.
2. What changed, in 3 to 5 lines.
3. Whether the commit is green: the tests, `rpmlint`, and the build on Fedora and on EPEL 10.
4. What was not checked and what has to be tried by hand.

The answer is "go" or "no". Without "go" no tag is pushed. The version in `pyproject.toml` and in
`packaging/fedora/slideshow-lock.spec` is the tag without the `v`; check both before the report.
The open points about the release version and the tarball are in `docs/copr.md`, section 4.

## After "go"

1. Tag: an annotated tag `v<version>` on the commit named in the report.
2. COPR builds from the webhook (below). Wait for the build and check every chroot.
3. In a clean container: `dnf copr enable trensoft/slideshow-lock`, then
   `dnf install slideshow-lock`. The `dnf copr` subcommand needs `dnf-plugins-core`, and the
   EPEL 10 distributions need EPEL and CRB first (see the Installation section of `README.md`).
4. The GitHub release, with the list of changes.
5. The report: done, per chroot, and what could not be checked.

## A failed build

The tag is not deleted and not moved. The report says what failed and proposes a fix. The fix is a
new version (for example 1.0.1) and goes through the report and the approval again.

## Who pushes the tag, and what is measured

The tag is pushed with `git push`, which uses the stored credential of the machine; no token is
read or written for it. This was measured with a local annotated tag and
`git push --dry-run origin refs/tags/<name>`: with the stored credential the dry run lists
`[new tag]` and exits 0; without a credential (`credential.helper` empty, prompts off) it stops
with `could not read Username` and exit 128. `git ls-remote --tags origin` was empty before and
after, and the local tag was deleted: no real tag was pushed.

What this does not show: a dry run does not send the tag, so it does not prove that the real push
is accepted. In particular, a tag rule or ruleset on the repository, if one is set later, is not
tried by it. The first real tag push is the real test.

The GitHub release (step 4) is an API call with a token. It is not done by the release manager,
because of the rule on credentials: it is done by the person who has the established way for it,
or by the maintainer in the web interface.

## The webhook (tag only)

This is the setting, read from the COPR source by the team (`fedora-copr/copr`, the frontend:
`webhooks_general.py` `webhooks_git_push`, and `packages_logic.py` `_tag_belongs_to_package` and
`commits_belong_to_package`). `[H]` That the live COPR server behaves as that source says is not
measured; a test push is checked before the first release.

- GitHub webhook: only the event "Branch or tag creation". "Pushes" is off.
- URL: the COPR GitHub webhook address of the project, with `slideshow-lock/` added at the end.
  Without the package name a tag such as `v1.0.0` does not match.
- COPR package: "Webhook rebuild" on. The committish `main` is used only by a manual Rebuild.

A push to a branch must not start a build; only the creation of a tag does.
