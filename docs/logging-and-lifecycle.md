# Logging Convention and Service Lifecycle (OPS-1)

Owner: SRE / operations. Card: OPS-1. Related decisions: D7, D14, D32, D34.

Status note (D7/section 9 of the brief): no agent container has a real GNOME/Wayland
session. Everything below marked "automatable" is testable with the simulated D-Bus
used by TEST-1/CORE-1. Everything marked "manual-only" can only be verified on
the reference machine (RHEL 10.2, GNOME, Wayland). Where automated tests exist, the
correct claim is **"automated tests green, live verification pending"** -- never
"works" based on simulated-bus results alone.

## 1. Log entry structure

- All log output goes through Python's standard `logging` module and is forwarded
  to journald, so `journalctl --user -u slideshow-lock.service` shows native
  journald fields (`PRIORITY`, `SYSLOG_IDENTIFIER`, `MESSAGE`).
- `SYSLOG_IDENTIFIER=slideshow-lock`.
- Every message is prefixed with a bracketed event tag, e.g. `[idle-trigger]`,
  `[lock]`, `[sleep-inhibit]`, `[config]`, `[slideshow-dir]`, `[slideshow]`, `[samples]`, so
  filtering works even without parsing structured fields.
- Single-line messages only.

Level mapping:

| Level   | Used for |
|---------|----------|
| DEBUG   | idle/active watch churn, D-Bus call round-trips |
| INFO    | service start/stop, slideshow start/stop (with trigger source: idle \| preview), lock performed, config reload applied, the language at start of each program (`[config] language ...`, see `translations.md`) |
| WARNING | D34 sleep/lock race (see below); missing/empty picture folder (3.7); invalid config value replaced with default |
| ERROR   | a required D-Bus interface is missing at runtime (section 2 of the brief: fail with a clear error, don't guess) |

## 2. D34 -- sleep/lock race warning

Condition: `PrepareForSleep(false)` (resume) arrives before the `Lock` round-trip
(triggered by `PrepareForSleep(true)`) has completed.

- Level: **WARNING**
- Message template:
  `[sleep-inhibit] resume received before lock sequence completed (elapsed=<ms>ms, limit=InhibitDelayMaxSec); session may have resumed unlocked`
- This logs the race, it does not fix it -- the underlying `InhibitDelayMaxSec`
  limit (default 5s, D32) is outside this project's control.
- Automatable: simulated-bus test with an artificial delay exceeding the configured
  limit must assert this exact WARNING is emitted. Implementation belongs to CORE-1
  (software engineer); this document is the acceptance spec.
- Manual-only: the real timing of this race on actual hardware belongs on the
  manual test list (DOC-2) -- automated tests cannot measure real wall-clock
  suspend/resume timing.

## 3. Lifecycle events to log

INFO unless noted:

- service start / stop
- idle watch fired -> slideshow start (`source=idle`)
- preview requested -> slideshow start (`source=preview`)
- first input during slideshow -> slideshow stop, plus whether a lock was
  performed or skipped (grace period), and the trigger source (D11: preview
  never locks)
- inhibition present at start time -> slideshow not started (INFO: expected
  behaviour, not a fault)
- inhibition appearing while slideshow is running -> immediate stop; a lock too if the slideshow
  ran for the grace period or longer (the same measure as for input), none within it
  (D28: this must never suppress the sleep-lock branch)
- `PrepareForSleep(true)` -> stop + lock attempt
- `PrepareForSleep(false)` / resume -> see D34
- session already locked -> no-op (DEBUG, to avoid log spam on repeated checks)
- missing or empty picture folder -> **WARNING**, service keeps running (3.7)
- config reload applied -> INFO, naming the changed keys
- invalid config value rejected -> **WARNING**, naming the key and the fallback used

## 4. Unit file requirements (spec only -- PKG-1 authors the file)

- `PartOf=graphical-session.target`, `WantedBy=graphical-session.target`
- `Restart=on-failure` with a bounded `RestartSec` so a crash loop cannot spam
  D-Bus or the journal
- Must pass `systemd-analyze verify` with zero warnings and zero errors
- Per D14, the unit file itself is owned by PKG-1 (build engineer); this card
  (OPS-1) is the review gate and runs `systemd-analyze verify` once PKG-1
  delivers the file -- it does not author the unit.

## 5. Automatable vs. manual-only (D7)

Automatable (simulated bus / unit-level, no real session needed):
- log message structure/content assertions (section 1-3 above)
- D34 WARNING emission under an artificial delay
- unit file syntax (`systemd-analyze verify`), once PKG-1 delivers the file

Manual-only (reference machine, DOC-2):
- actual GNOME/Wayland session lifecycle (real idle detection, real lock,
  real multi-monitor behaviour)
- real timing of the D34 race under the actual `InhibitDelayMaxSec`
- the actual `journalctl --user -u slideshow-lock.service` output as seen by a
  real user

No claim beyond the automatable list is a claim of "works". For everything in
the manual-only list the correct phrasing is: **automated tests green, live
verification pending.**

## 6. The sample pictures (`[samples]`, after 1.0.0)

`slideshowlock` (the menu and the login start) copies the pictures the package installs under
`<datadir>/slideshow-lock/pictures` into `Pictures/trensoft` once (`slideshow_lock/sample_pictures.py`,
started from `control.py` on a thread of its own, not in the service). Only when the user has not
chosen another picture folder; the `picture-folder` key is never written.

What the user can rely on, and what the log says:

- A picture or the whole folder the user deletes does not come back: the names that were dealt with
  are in `$XDG_STATE_HOME/slideshow-lock/sample-pictures.json` (`~/.local/state/...`). A name that a
  later package adds is copied once. Nothing is overwritten; a file that is there is left as it is.
- With nothing left to copy nothing is created and nothing is written: no folder, no cleanup, no
  state rewrite. A start with everything done is silent (DEBUG at most).
- The order inside `install`: the package and its file list; the lock (`flock` on
  `sample-pictures.json.lock`); the state; what is still to do; then, only if there is something:
  free space (the files plus a 16 MiB margin, measured on the nearest existing folder, so before any
  folder is made), a trial write of the state, the folders, the removal of hidden leftovers of a cut
  copy (`.<name>.part-<pid>`, only with the lock), the copies. A copy is written to the hidden name,
  gets the time of the source, is flushed, and is given its name by a `link` that never replaces a
  file (a file system without hard links gets a checked `rename`: a rare, documented race).
- Log lines, one line each, never a path, a picture name or the content of the state: INFO
  `[samples] copied N, already there M, dealt with before K`; WARNING `[samples] not copied: <reason>
  (<errno name>)` with the reason `state-unreadable`, `state-unwritable`, `foreign-dir` (the subfolder
  is a link, a file or belongs to another user), `pictures-unwritable`, `space-unknown`,
  `copy-failed`, or the free-space line with the bytes needed and free; WARNING for a damaged state
  file (it is replaced by the trial write, so the line comes once). The exit status of the command
  does not depend on any of it.
