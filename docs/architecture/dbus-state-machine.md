# D-Bus Layer and State Machine Design (ARCH-1)

Status: design document, no implementation. Owner: solution architecture.
This document does not decide settings storage (tracked separately), does not implement
the state machine (`CORE-1`), and does not cover the slideshow rendering layer (`CORE-2`).

## 1. Goal

Define how the service talks to the session (D-Bus) and what states it can be in, so that
`CORE-1` can implement the state machine against a fixed contract, and `TEST-1` can test it
without a real GNOME session.

## 2. Design principle: one abstraction, two implementations

Every D-Bus interaction is defined as a small Python `Protocol` (or ABC). There are exactly
two implementations of each protocol:

- a **real** adapter, backed by `Gio.DBusProxy` / `GLib`, used in production;
- a **fake** adapter, used in tests, that lets a test inject events (idle fired, inhibitor
  present, `PrepareForSleep` signal, screensaver state change, unit-call result) without any
  bus connection.

The state machine itself only ever depends on the protocols, never on `Gio`/`GLib` directly.
This is what makes acceptance criterion 1 ("every D-Bus call behind an abstraction") and
criterion 2 ("the state machine is testable against a simulated bus") the same design
decision, not two separate ones.

```
StateMachine
   |-- IdleWatcher (protocol)        -> real: Mutter.IdleMonitor   | fake: manual trigger
   |-- InhibitionQuery (protocol)    -> real: SessionManager        | fake: manual flag
   |-- SessionLock (protocol)        -> real: ScreenSaver + login1  | fake: records calls
   |-- SleepSignal (protocol)        -> real: login1 PrepareForSleep| fake: manual trigger
   |-- UnitControl (protocol)        -> real: systemd1 user manager | fake: records calls
```

## 3. The six D-Bus starting points, plus D4's consequence

Each entry below lists: the bus interface, the abstraction method the state machine calls,
and what happens if the interface is missing at runtime (acceptance criterion 4: fail with
a clear error, never run silently degraded).

### 3.1 Idle detection

- Interface: `org.gnome.Mutter.IdleMonitor` at `/org/gnome/Mutter/IdleMonitor/Core`.
- Methods used: `AddIdleWatch(ms)`, `AddUserActiveWatch()`, `RemoveWatch(id)`.
- Abstraction: `IdleWatcher.on_idle(timeout_s, callback)`,
  `IdleWatcher.on_user_active(callback)`. Event-driven only; no polling.
- Missing interface: raise `UnsupportedSessionInterface("IdleMonitor")` at startup. The
  service logs the error and does not start the idle-triggered slideshow path. It does not
  silently fall back to polling.

### 3.2 Idle inhibition query

- Interface: `org.gnome.SessionManager`, method `IsInhibited(flag)` (idle flag = 8).
- Abstraction: `InhibitionQuery.is_idle_inhibited() -> bool`. It answers for other applications
  only: the idle inhibitor the service itself holds while its slideshow shows
  (`hold_idle_inhibit()` / `release_idle_inhibit()`, `Inhibit` with flag 8 under the service's
  application id) is left out, using `GetInhibitors` and `GetAppId`/`GetFlags` of each inhibitor.
- Scope (D28, see 6.2): this query is consulted **only** on the idle-triggered path. It is
  never consulted on the sleep-triggered path.

### 3.3 Locking

- Interface: `org.gnome.ScreenSaver`, method `Lock()`; fallback `loginctl lock-session`
  (via `org.freedesktop.login1.Session.Lock()` on the current session, not a subprocess
  call) if `org.gnome.ScreenSaver` is not present.
- Abstraction: `SessionLock.lock() -> LockResult` (success/failure + error text, not
  fire-and-forget; see 3.5 below for why this matters).
- Missing both interfaces: raise `UnsupportedSessionInterface("ScreenSaver+login1.Session")`;
  the service refuses to start the idle-triggered slideshow, because it would otherwise be
  able to show the slideshow but never lock, silently violating 3.3.

### 3.4 Lock state

- Interface: `org.gnome.ScreenSaver`, property `GetActive`, signal `ActiveChanged`.
- Abstraction: `SessionLock.is_active() -> bool`, `SessionLock.on_active_changed(callback)`.
- Used to implement 3.6 ("if the session is already locked, the service does nothing"): the
  idle-triggered path is gated on `not is_active()`.

### 3.5 Sleep signal

- Interface: `org.freedesktop.login1`, signal `PrepareForSleep(bool)` on the `Manager`
  object.
- Abstraction: `SleepSignal.on_prepare_for_sleep(callback)`, callback receives the boolean.
- Missing interface: raise `UnsupportedSessionInterface("login1.PrepareForSleep")`; this is
  fatal to the whole service, not just one path, because requirement 3.5 cannot be met at
  all without it (there is no polling alternative that is correct).

### 3.6 Delay inhibitor (D1)

`PrepareForSleep` alone does not guarantee our lock call runs before the machine actually
sleeps; `logind` does not wait for listeners by default. D1 adds a concrete mechanism:

- Interface: `org.freedesktop.login1.Manager`, method
  `Inhibit("sleep", app_id, reason, "delay")`, which returns a file descriptor.
- The service takes this inhibitor **once at startup** and holds the fd open.
- On `PrepareForSleep(true)`: stop the slideshow if running, call `SessionLock.lock()`,
  then close the fd, in a `finally` block so the fd is always released even if `lock()`
  raises. Holding the fd longer than necessary blocks system suspend for everyone.
- Abstraction: `SleepSignal.acquire_delay_inhibitor() -> fd`, released via a context
  manager the caller controls explicitly around the stop+lock sequence.

**D32, the measurable form of this design:** the inhibitor has a ceiling, named
`InhibitDelayMaxSec` in `logind`, 5 seconds by default. If the stop+lock sequence does not
finish inside that window, `logind` proceeds with suspend anyway, **without an error
signal** — the machine sleeps unlocked, silently. Consequences for the design:

- The actual value must be **read from the running system**, not assumed: the
  `InhibitDelayMaxUSec` property on `org.freedesktop.login1.Manager` (microseconds),
  via `Properties.Get`, not parsed from `logind.conf` (which may be overridden).
- The abstraction exposes this as `SleepSignal.inhibit_delay_max() -> float` (seconds).
- The stop+lock sequence is the only thing that may run inside the held inhibitor; nothing
  else is allowed to add latency there.
- This design document does not implement the "what if we miss the deadline" logging (that
  is `CORE-1`/`OPS-1`'s D34: a WARNING-level log entry if `PrepareForSleep(false)` — i.e.
  wake — arrives before the lock round trip is confirmed done). The abstraction must expose
  enough to make that detectable: `SessionLock.lock()` returns a result object with a
  timestamp, and the sleep handler records when it called `lock()` and when it returned.

### 3.7 D4's consequence: enabling/disabling the unit

D4 moved "turn the service on/off" out of the RPM post-install script and into the
preferences app's toggle. That toggle needs a real mechanism:

- Interface: `org.freedesktop.systemd1` (user service manager bus), methods
  `EnableUnitFiles`/`DisableUnitFiles`, `StartUnit`/`StopUnit`.
- Abstraction: `UnitControl.set_enabled(bool) -> UnitControlResult` (success/failure +
  error text). This is a synchronous, result-returning call, not fire-and-forget: the
  preferences window must only show "on" once the result confirms success (`UI-1`'s
  acceptance criterion already states the toggle "must not show an enabled state on
  failure" — this is the layer that makes that possible).
- Rationale for going through D-Bus instead of shelling out to `systemctl --user`: it stays
  inside the same abstraction/fake-bus pattern as every other interface here, so `TEST-1`
  can exercise failure paths (e.g. unit file missing, permission denied) without a real
  systemd user session.

### 3.7a The shell's overview (after 1.0.0)

Not one of the six starting points: the state machine does not know it. `PreviewSlideshow`, the
`SlideshowControl` of `service.py`, closes the overview before it opens the windows.

- Interface: `org.gnome.Shell` at `/org/gnome/Shell`, the property `OverviewActive` (`readwrite`),
  through `org.freedesktop.DBus.Properties`. Abstraction: `OverviewControl.close_if_open()`;
  adapter `GnomeShellOverview` in `dbus_adapters.py`.
- Why: a slideshow window that opens while the overview (Super) is up comes out as a third
  window in the overview, not as a full screen one. The lock is not affected.
- What it does: `Get`; if true, `Set(false)`, then `Get` every 50 ms until false. The property
  stays true until the closing animation is over (250 ms in the shell's `overview.js`, read, not
  measured here).
- It never raises and waits about 0.5 s in all, calls included (each call gets what is left of
  the time as its timeout; one more call still runs after the last 50 ms sleep). No shell on the bus (not GNOME): DEBUG only. Anything else
  (a refused call, a shell that does not answer, an overview that stays open): one WARNING with
  the tag `[slideshow]`, and the slideshow starts as it would have.
- Only the service path (idle start) calls it. The settings window's Preview button does not:
  that window has the focus, so the overview is presumably not open then (an inference, not
  measured), and the preview modules may not name the bus (`tests/test_preview.py`).

### 3.8 Unit readiness and `Type=` (answers OPS-1's open question)

`docs/logging-and-lifecycle.md` (OPS-1, section 4) left the unit's `Type=` undecided
pending this design, on the grounds that it depends on whether a concrete "the service
is ready" point exists in the D-Bus setup sequence. It does.

- **`Type=notify`**, not `simple`.
- Readiness point: the service calls `sd_notify("READY=1")` (via `python-systemd`'s
  `systemd.daemon.notify`, or a raw `$NOTIFY_SOCKET` write if that dependency is rejected
  later) only after *all* of the following have succeeded, in this order, during startup:
  1. session bus connection established;
  2. `IdleWatcher` registered (3.1) and `IdleInhibitionQuery` reachable (3.2);
  3. `PrepareForSleep` signal subscription active (3.5);
  4. the initial sleep-delay inhibitor lock acquired (3.6 / D1) — this is the step most
     likely to fail (e.g. `login1` not present, or `InhibitDelayMaxSec` unreadable), and
     it is also the one decision whose absence would make pre-sleep locking silently
     unreliable, so it must gate readiness, not just be logged;
  5. the state machine has entered its steady state, `IDLE_WATCHING` (section 4).
- If any of these steps fails, the service does **not** notify readiness. It follows
  acceptance criterion 4 (section 7): log the specific missing interface at ERROR and
  exit non-zero, rather than limping on in an undefined state. Under `Type=notify` this
  surfaces as a `systemd --user` **startup failure** (bounded by `TimeoutStartSec`), which
  is strictly more informative than `Type=simple` — under `simple`, systemd would mark the
  unit "active" the moment the process forks, even though the D-Bus setup that makes the
  service actually do anything had already failed. That gap is exactly what sections 7 and
  5 of `logging-and-lifecycle.md` (ERROR-level missing-interface logging, manual-only
  verification) are trying to make visible; `Type=notify` makes it visible to `systemd`
  itself, not only to the journal.
- Consequence for `PKG-1` (unit file author) and `OPS-1` (review gate, `systemd-analyze
  verify`): the unit file needs `Type=notify` and a `TimeoutStartSec` wide enough to cover
  steps 1-4 above (a fixed value is not proposed here — that is a measurement, not a
  design decision; see section 9).
- Consequence for `CORE-1`: the sd_notify call is a startup-sequence detail of the service
  entry point, not of the state machine itself. It belongs in whatever wires the real
  adapters together and starts the state machine (`main()`/service bootstrap), after the
  bootstrap confirms step 4 above, before entering the event loop. The state machine
  object has no systemd dependency, consistent with section 2.

## 4. State machine

States:

| State | Meaning |
|---|---|
| `DISABLED` | Service toggled off (3.7 / D4). No idle watch, no sleep handling. |
| `IDLE_WATCHING` | Enabled, session unlocked, waiting for the idle timeout. |
| `SLIDESHOW_RUNNING` | Slideshow visible. Carries `trigger_source ∈ {IDLE, MANUAL_PREVIEW}`. |
| `LOCKING` | Transient: slideshow being torn down, `SessionLock.lock()` in flight. |
| `LOCKED` | `ScreenSaver.GetActive` is true; service does nothing (3.6). |

Transitions (abbreviated; guards in brackets):

| From | Event | Guard | Action | To |
|---|---|---|---|---|
| `IDLE_WATCHING` | idle timeout fires | `not is_idle_inhibited()` (3.4) | show slideshow, all monitors | `SLIDESHOW_RUNNING(IDLE)` |
| `IDLE_WATCHING` | idle timeout fires | `is_idle_inhibited()` | — | `IDLE_WATCHING` (no-op) |
| `SLIDESHOW_RUNNING(IDLE)` | inhibitor appears while running | `not grace_elapsed` | stop slideshow, **no lock** | `IDLE_WATCHING` |
| `SLIDESHOW_RUNNING(IDLE)` | inhibitor appears while running | `grace_elapsed` (the same strict `<` and the same elapsed time as for input) | stop slideshow, lock at once, through the lock path of the input row | `LOCKING` → `LOCKED` |
| `SLIDESHOW_RUNNING(IDLE)` | first input | `grace_elapsed` (strict `<`, D16) | stop slideshow, lock | `LOCKING` → `LOCKED` |
| `SLIDESHOW_RUNNING(IDLE)` | first input | `not grace_elapsed` | stop slideshow, **no lock** | `IDLE_WATCHING` |
| `SLIDESHOW_RUNNING(MANUAL_PREVIEW)` | any input | — (D11: never locks) | stop slideshow, **no lock** | `IDLE_WATCHING` |
| any state except `LOCKED`/`DISABLED` | `PrepareForSleep(true)` | — (independent of slideshow state, D10; **ignores inhibitor**, D28) | stop slideshow if running, lock (inside held delay-inhibitor, 3.6) | `LOCKED` |
| `LOCKED` | `ActiveChanged(false)` | — | — | `IDLE_WATCHING` |
| any | preferences toggle off | — | release watches/inhibitor | `DISABLED` |
| `DISABLED` | preferences toggle on | — | re-acquire idle watch + delay inhibitor | `IDLE_WATCHING` |

This table is the direct answer to acceptance criterion 3: 3.3 (lock only for a running,
service-started slideshow — the `grace_elapsed` guard on the `IDLE` row), 3.4 (inhibition —
its own guarded transition, and explicitly absent from the sleep row), and 3.5 (sleep lock —
its own row, reachable from every non-locked, non-disabled state) are three separate rows,
not one merged branch.

## 5. Trigger source and manual preview (D11)

`SLIDESHOW_RUNNING` carries an enum, not a boolean, because a third source is conceivable
later (there is none planned now, but a boolean would make that an implicit redesign):

```
class TriggerSource(Enum):
    IDLE = "idle"
    MANUAL_PREVIEW = "manual_preview"
```

The "first input stops the slideshow" transition is the same transition in both cases; only
the guard differs — `MANUAL_PREVIEW` never satisfies the locking guard, by construction, not
by a runtime check that could be bypassed. This directly implements D11 ("Preview never
locks, no matter what input arrives") and keeps the already-approved exception (6. szakasz /
settings app "Preview" button) from needing a special case anywhere else in the machine.

## 6. Two independent concerns that look similar and are not

### 6.1 Input detection differs by trigger source (D22)

`IdleWatcher.on_user_active()` only fires on an idle → active **transition**. That is exactly
right for the `IDLE` path: the user was idle (that is why the slideshow started), and the
monitor correctly fires the moment they stop being idle.

It is **wrong** for `MANUAL_PREVIEW`: the user is already active (they just clicked the
Preview button), so no idle → active transition will ever occur, and a freshly registered
`AddUserActiveWatch` will not fire on the input that is supposed to end the preview.

Resolution: input detection is wired differently per `trigger_source`, even though both
feed the same internal `input_detected` state machine event:

- `IDLE`: subscribe to `IdleWatcher.on_user_active()`.
- `MANUAL_PREVIEW`: subscribe directly to the slideshow window's own GTK input
  controllers (motion, click, key, scroll), one per monitor window, any one firing is sufficient.
  As built in CORE-2 (`docs/preview.md`) a close request on a preview window counts as input too
  (a window closed from outside would otherwise leave a process with no window), and a pointer
  that merely rests under a new window does not count: the first position is the baseline and
  motion counts from 2 px away. Whether 2 px is enough on a touchpad or on a desk that shakes
  is open and only a live trial can decide it.

### 6.2 Inhibition scope is per-path, not a single flag (D28)

`InhibitionQuery.is_idle_inhibited()` is read in exactly one place: the `IDLE_WATCHING` →
`SLIDESHOW_RUNNING(IDLE)` transition guard, and the "inhibitor appeared while running"
transition. The `PrepareForSleep` handler does not call it, does not check it, and has no
code path that could call it — these are two separate subscriptions in the implementation,
not one shared conditional. Without this separation, an application holding an idle-inhibit
(e.g. a video call) would silently suppress the pre-sleep lock as well, which would be a
security gap: closing the laptop lid during a video call would leave the session unlocked at
wake.

## 7. Failure mode summary (acceptance criterion 4)

| Missing interface | Effect |
|---|---|
| `Mutter.IdleMonitor` | Idle-triggered slideshow disabled; service logs a clear error and keeps running (manual preview and sleep-lock still work). |
| `ScreenSaver` and `login1.Session.Lock` both absent | Idle-triggered slideshow disabled entirely (would otherwise show without ever locking). |
| `login1.PrepareForSleep` | Fatal at startup: requirement 3.5 has no fallback. |
| `login1.Manager.Inhibit("sleep", ...)` | Fatal at startup: without the delay inhibitor D1/D32 cannot be met, and starting anyway would silently violate 3.5 under fast suspend. |
| `org.gnome.Shell` (the overview) | Nothing is closed, the slideshow starts as before; DEBUG when the shell is not on the bus, one WARNING for any other failure (3.7a). |
| `systemd1` user manager | Preferences toggle fails with a reported error (D4's `UnitControlResult`); the service itself can still run if already started. |

In every case the failure is detected once, at startup or at first use, by a capability
probe (a cheap read-only call per interface, e.g. `Introspect` or a harmless property read),
cached for the process lifetime. None of these degrade silently; "silent" here specifically
means "runs as if nothing were wrong" — logging and refusing/limiting functionality is the
required behaviour, not an error dialog aimed at the end user (there is no end user watching
a background service).

## 8. Testability (acceptance criterion 2, no automated tests yet)

Because the state machine only depends on the protocols in section 2, `TEST-1` can drive
every row of the table in section 4 by calling the fake adapters directly: fire a fake idle
timeout, toggle a fake inhibitor, fire a fake `PrepareForSleep`, assert on calls recorded by
the fake `SessionLock`/`UnitControl`. No GNOME session, no real D-Bus connection, and no
wall-clock sleep are required for any of this. Real timing (the `InhibitDelayMaxSec` budget,
the visual multi-monitor behaviour) is explicitly **not** coverable this way — see the open
question in section 9 — and is left to the manual test list (`DOC-2`), not claimed here as
automated.

## 9. Explicitly out of scope / open questions

- Settings storage (GSettings vs. TOML) is not decided here; whichever is chosen, it only
  feeds parameters into this state machine (idle timeout, grace period) and does not change
  the shape above.
- Real-time verification that the stop+lock sequence fits inside `InhibitDelayMaxSec` on a
  live system is a manual-test item, not something this document or an automated test can
  certify.
- Cross-GNOME-version interface differences (the RHEL 9 / GNOME 40 question) are absorbed by
  the capability-probe pattern in section 7, not by a parallel implementation; this document
  takes no position on whether that target ships at all.
- A concrete `TimeoutStartSec` for the `Type=notify` unit (3.8) is not proposed here — it
  needs a measured startup time on real hardware, not a guessed constant; owned by `PKG-1`/
  `OPS-1`.
