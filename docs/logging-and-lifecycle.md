# Logging Convention and Service Lifecycle (OPS-1)

Owner: sreoperations (Ria). Card: OPS-1. Related decisions: D7, D14, D32, D34.

Status note (D7/section 9 of the brief): no agent container has a real GNOME/Wayland
session. Everything below marked "automatable" is testable with the simulated D-Bus
used by TEST-1/CORE-1. Everything marked "manual-only" can only be verified on
Attila's hardware (RHEL 10.2, GNOME, Wayland). Where automated tests exist, the
correct claim is **"automated tests green, live verification pending"** -- never
"works" based on simulated-bus results alone.

## 1. Log entry structure

- All log output goes through Python's standard `logging` module and is forwarded
  to journald, so `journalctl --user -u slideshow-lock.service` shows native
  journald fields (`PRIORITY`, `SYSLOG_IDENTIFIER`, `MESSAGE`).
- `SYSLOG_IDENTIFIER=slideshow-lock`.
- Every message is prefixed with a bracketed event tag, e.g. `[idle-trigger]`,
  `[lock]`, `[sleep-inhibit]`, `[config]`, `[slideshow-dir]`, so filtering works
  even without parsing structured fields.
- Single-line messages only.

Level mapping:

| Level   | Used for |
|---------|----------|
| DEBUG   | idle/active watch churn, D-Bus call round-trips |
| INFO    | service start/stop, slideshow start/stop (with trigger source: idle \| preview), lock performed, config reload applied |
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
- inhibition appearing while slideshow is running -> immediate stop, no lock
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

Manual-only (Attila's hardware, DOC-2):
- actual GNOME/Wayland session lifecycle (real idle detection, real lock,
  real multi-monitor behaviour)
- real timing of the D34 race under the actual `InhibitDelayMaxSec`
- the actual `journalctl --user -u slideshow-lock.service` output as seen by a
  real user

No claim beyond the automatable list is a claim of "works". For everything in
the manual-only list the correct phrasing is: **automated tests green, live
verification pending.**
