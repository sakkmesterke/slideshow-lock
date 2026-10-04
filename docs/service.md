# The service: state machine, sleep guard, entry point (CORE-1)

Source of truth for the behaviour: `docs/req-1-acceptance-criteria.md` (3.1 to 3.7) and
`docs/architecture/dbus-state-machine.md` (ARCH-1). This document says what was built, where it
differs from the design text, what is automated, and what only a real session can show.

**Status wording.** Automated tests green; live verification pending. Nothing below is claimed to
work on a real GNOME session: the adapters were checked against a fake desktop and a headless
mutter, not against the shell, the session manager or logind of a real machine.

## 1. Run it from a source checkout

No RPM, no systemd:

    glib-compile-schemas data/
    GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.service \
        --folder ~/Pictures --idle-timeout 30 --grace 3

- Idle for `--idle-timeout` seconds: one fullscreen window per monitor (the CORE-2 preview windows).
- The first input ends it. If at least `--grace` seconds have passed since the slideshow was
  started, the session is locked; sooner, it is not (strict `<`: `--grace 0` always locks). The
  time is taken just before the windows are opened, so it includes the time the start itself
  takes: 0.1 s to 0.8 s in headless runs (the "after N s" of the log line is counted the same way,
  so it is a little more than the time since the windows appeared). Not measured on a real session.
- Before the machine suspends the session is locked, whatever else is going on.
- `Ctrl+C` or `SIGTERM` ends the service. The other options (`--interval`, `--order`,
  `--scaling`, `--pan`, `--debug`) are those of `preview_app`. Nothing is written to the stored
  settings; without an option the stored value of the GSettings key is used.
- Log lines go to stderr, tagged as in `docs/logging-and-lifecycle.md` (`[idle-trigger]`,
  `[slideshow]`, `[lock]`, `[sleep-inhibit]`, `[slideshow-dir]`). `--debug` adds the D-Bus watch churn.
- The service is a `Gtk.Application` with its own application id, so a second copy should hand
  over to the first and exit without a message (not measured).
- Exit status 1 if the sleep path cannot be set up (section 5), 2 for a bad option or a missing
  schema.

### Trial on a real session (what only the reference machine can show)

1. Start it with a short timeout (`--idle-timeout 20 --grace 0`) in a terminal and leave the
   machine alone: the slideshow should cover every monitor after about 20 s.
2. Touch the mouse: the slideshow goes and the lock screen shows. Unlock.
3. Repeat with `--grace 10`: input within 10 s of the start must not lock; input after must.
4. Play a video (or use `systemd-inhibit --what=idle sleep 600` as the inhibitor) and wait the
   timeout: no slideshow. Start the slideshow first, then raise the inhibitor: it must stop, and
   lock if it had run for `--grace` seconds or longer (use `--grace 0` to see the lock, `--grace
   60` to see none). The slideshow does not wait for the next input.
5. `systemctl suspend` with the slideshow running, and again with an idle inhibitor held: on
   wake the lock screen must be there, no slideshow, no unlocked desktop visible (AC-3.5-4).
6. Look for the D34 warning in the output after a suspend (`resume received before lock
   sequence completed`); it should not appear.

## 2. The parts

| File | What it is |
|---|---|
| `session.py` | The protocols (`IdleWatcher`, `InhibitionQuery`, `SessionLock`, `SleepSignal`, `SlideshowControl`, ...), `LockResult`, `UnsupportedSessionInterface`. No `Gio`. |
| `state_machine.py` | The states and transitions of ARCH-1 section 4. Depends on the protocols only. |
| `sleep_guard.py` | The lock before suspend (`SleepGuard`) and the thread it runs on (`GuardThread`). |
| `dbus_adapters.py` | The real adapters on `Gio.DBusConnection`. |
| `service.py` | Wiring (`build_service`), the preview as a `SlideshowControl` (`PreviewSlideshow`), the program. |
| `loop.py` | Posting a function to a GLib main context from any thread. |

The state machine holds no bus name, no `Gio`, no systemd call; `tests/fakes.py` has a fake of
every protocol, so every row of the transition table runs without a bus (`tests/test_state_machine.py`).

## 3. How each acceptance criterion is met

| Criterion | Where | Test |
|---|---|---|
| 3.1-1 idle starts the slideshow (not inhibited, not locked, none running) | `StateMachine._on_idle` | `test_state_machine.py` |
| 3.2-1 first input stops it at once | `_input_detected` (idle monitor's active transition, or a window's input) | same |
| 3.2-2 grace period, strict `<` (D16) | `_input_detected`: `elapsed < grace` means no lock | the boundary table, `G = 0` and `G > 0` |
| 3.3-1/2/3 lock only for a running, idle-triggered slideshow; a preview never locks (D11) | `_LOCKING_SOURCES = {IDLE}`: a preview has no path to the lock call | the preview with every grace and timing; input with nothing running |
| 3.4-1 inhibit blocks the start | `_on_idle` | `test_state_machine.py` |
| 3.4-2 one raised during the run stops it, and locks at once if the grace period is over (the same strict `<` and elapsed time as input), none within it; a preview is left alone | `_on_inhibit_changed`, `_end_slideshow_and_lock_if_due` | the boundary table for both ends of a slideshow, a control that compares them, a failing lock, no running slideshow, the preview |
| 3.5-1 sleep stops the slideshow and locks, independent of how it started (D10, D35) | `SleepGuard` locks, `StateMachine.sleep_started` stops | `test_sleep_guard.py`, `test_service_dbus.py` |
| 3.5-2 an idle inhibit never keeps the sleep lock back (D28) | `SleepGuard` has no inhibition query, by its constructor and by its imports | an AST test on the module; an end-to-end test with the inhibit set |
| 3.5-3 resume before the lock round trip ended: WARNING (D32, D34) | `SleepGuard._after_wake` | with fakes and with the real adapters and an artificial delay |
| 3.6-1 locked: nothing happens | `LOCKED` ignores idle, input and inhibit | `test_ac_3_6_1_...` |
| 3.7-1 no pictures: no slideshow, WARNING, the service keeps running | `PreviewSlideshow.start` returns the reason, the machine logs it | `test_ac_3_7_1_...` |

Not automatable, and not claimed: AC-3.1-2 (one window per monitor on a real multi-monitor
setup), AC-3.5-4 (a real suspend and wake). The headless-mutter smoke below shows two windows on
two virtual monitors, which is not the same as the reference machine.

## 4. The sleep path is on its own thread (the security condition)

The image source reads folders and file headers on the GLib main loop (`os.scandir`, `open`). A
stuck network mount blocks one such call for minutes. If the `PrepareForSleep` handler ran on that
loop, the lock would miss logind's `InhibitDelayMaxSec` window (5 s by default) and the machine
would sleep unlocked, with no error. So:

- `GuardThread` runs the `SleepGuard` on a thread with its **own GLib main context**. Its adapters
  (`Login1Sleep`, and its own `SessionLock`) are created on that thread, and Gio delivers their
  signals and call answers there.
- On `PrepareForSleep(true)` the guard (1) posts "sleep started" to the main loop, which stops the
  slideshow there, and does not wait for it; (2) starts the `Lock()` call, asynchronous so the
  thread can still see the wake signal (every time: the guard does not remember that a lock was
  made, see below); (3) when the answer is in, releases the delay inhibitor;
  (4) posts the result. The inhibitor is held until the round trip is over, not until the call
  was made (ARCH-1 section 3.6: release in a `finally`).
- When the thread ends (the machine is disabled, or the service closes) it releases the inhibitor
  and closes both adapters, so their signal subscriptions go with it.
- Nothing in the guard touches the image source, the windows, the settings or the state machine's
  state. The only messages to the main loop are the two posts.
- The state machine follows (`sleep_started`, `sleep_lock_finished`) but never makes this lock.

Order differs a little from the design text ("stop the slideshow, then lock"): the stop is
*queued* before the lock call and not waited for, because waiting would put the lock behind the
main loop. The stop and the lock are not ordered against each other on the screen; the shell's
lock screen is drawn above client windows, so the order should not matter for what the user sees
(not measured).

Tests: `test_the_lock_before_suspend_goes_out_while_the_main_loop_is_stuck_in_the_image_source`
(twice: guard and fakes, and the whole service on the fake desktop) blocks the main loop inside
`os.scandir` of a real `ImageSource` and asserts that the `Lock()` call is made and the inhibitor
released while it is stuck; its negative control wires the same guard onto the stuck loop and shows
the lock waiting.

**No remembered lock state.** The guard calls `Lock()` before every suspend, also when the session
looks locked already. Skipping the call after an earlier success would be wrong: on the login1
fallback a successful `Lock()` only means that logind sent its `Lock` signal to the session's
clients (as its documentation describes it; not measured on a real session), with no lock screen
necessarily behind it, and a remembered "locked" would keep every later suspend from being
locked, silently. Tests: three sleeps against a facility that answers success and shows nothing
give three `Lock()` calls (`test_sleep_guard.py`), and the login1 fallback on the fake desktop
locks again when a sleep comes while the session is locked (`test_service_dbus.py`). Whether
repeated `Lock()` calls to the GNOME ScreenSaver are harmless is not measured on a real session.

**A failed lock after an inhibitor stop.** When an application starts inhibiting idle during a
slideshow that has run for the grace period, the slideshow stops and the lock goes through the same
method as the lock after input. If that `Lock()` fails the result is the same as after input: ERROR
in the log, the state goes back to idle-watching, and the session stays unlocked until the next
idle period or sleep (there is no retry).

**The login1 fallback is not a guarantee.** Without `org.gnome.ScreenSaver` the guard locks with
`login1.Session.Lock()`. Its success means that the call returned, not that a lock screen is up:
logind answers at once and sends a `Lock` signal to the session's clients. The guard then releases
the delay inhibitor, so on this path the delay inhibitor does not keep the machine awake until the
session is locked, and the machine may suspend before the lock screen is drawn. Where
`org.gnome.ScreenSaver` is offered the guard takes that path instead (by `make_session_lock`; that
a given GNOME session offers it is not measured). How the fallback behaves is not measured on a
real session, and no test can show it: the fake desktop sets `LockedHint` in the
same step as the call. Taking "locked" from `LockedHint` with a time limit instead is a possible
change, not made here.

**What this does not cover:** the *D-Bus daemon* or the *shell* being slow, and `Lock()` taking
longer than the window: that is the D34 case, logged, not fixed. What "confirmed" means here is the
`Lock()` answer, not the lock screen being drawn.

## 5. Failure at startup (ARCH-1 section 7)

| Missing | Effect |
|---|---|
| `org.gnome.Mutter.IdleMonitor` | ERROR; no idle-triggered slideshow; the sleep lock still works |
| `org.gnome.SessionManager` | ERROR; same (idle inhibition cannot be asked, so no slideshow on a guess) |
| `org.gnome.ScreenSaver` | falls back to `login1.Session.Lock` and its `LockedHint`; WARNING |
| both lock facilities | fatal: the sleep path has nothing to lock with |
| `login1` (`PrepareForSleep`, `Inhibit`, `InhibitDelayMaxUSec`) | fatal: exit status 1, ERROR |
| any other error while the sleep path is set up (a failed bus call, say) | fatal: exit status 1, ERROR; the program does not keep running without the guard |

A running guard that cannot take the delay inhibitor again after a wake logs ERROR (the next
suspend would not wait for the lock).

## 6. What was measured

- Automated: the unit and fake-bus tests (`test_state_machine.py`, `test_sleep_guard.py`), the
  adapters and the whole service against a fake desktop on private `dbus-daemon` processes
  (`test_dbus_adapters.py`, `test_service_dbus.py`; they need `dbus-daemon`, CI installs it).
- Smoke, not in CI: `tools/wayland-smoke/run_service.sh` starts `python3 -m slideshow_lock.service`
  in a headless mutter. The idle monitor is mutter's own, the windows are real, the pointer is
  injected through mutter's remote-desktop service; the screensaver, session manager and logind are
  fakes on the session bus and on a second private bus. It checks: the inhibitor is taken, idle
  starts the slideshow on both monitors, injected pointer motion ends it and the fake screensaver
  receives `Lock()`, `PrepareForSleep(true)` locks and releases the inhibitor, `PrepareForSleep(false)`
  takes it again, `SIGTERM` ends the service with status 0.

## 7. Not done here

- `sd_notify` readiness and the unit file (PKG-1); `UnitControl`, the unit enable and disable
  call (UI-1). The state machine has `enable()` and `disable()`, which the toggle will call.
- A manual preview as a service function (`StateMachine.start_preview`): the transitions and the D11
  guarantee are there and tested; nothing calls it yet (the settings window's Preview button will).
- Real timing: whether the lock fits into `InhibitDelayMaxSec` on the reference machine.
- A real `InhibitorAdded` flow from GNOME's session manager (the adapter re-asks `IsInhibited(8)` on
  every add and remove, and reports only a change).
