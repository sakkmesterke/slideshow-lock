"""Smoke test of the real service in a headless Wayland session (run it through run_service.sh).

It starts ``python3 -m slideshow_lock.service`` as a separate process, with the real mutter idle
monitor and real windows on virtual monitors, and plays the part of the desktop services mutter
does not have: the screensaver, the session manager and login1 (``tests/fake_dbus.py``). Then:

1. the service takes the logind delay inhibitor and reports it is running;
2. with no input for ``--idle-timeout`` seconds the slideshow opens a window per monitor;
3. pointer motion, injected through mutter's remote-desktop service, ends it and the fake
   screensaver receives ``Lock()``;
4. ``PrepareForSleep(true)`` makes the fake screensaver receive a second ``Lock()`` and the
   delay inhibitor is released; ``PrepareForSleep(false)`` takes it again;
5. SIGTERM ends the service cleanly and releases the inhibitor.

What it does not prove: anything about GNOME's own shell, session manager or logind (they are
fakes), the look of the pictures, or a real suspend. Those stay on the manual list.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import GLib  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smoke_preview import Injector, check, make_pictures  # noqa: E402

from tests.fake_dbus import Desktop  # noqa: E402

RESULTS_OK = []


def wait_for(predicate, timeout: float, loop_context=None) -> bool:
    """Wait with the main context turning (the injector uses it)."""
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def main() -> int:
    work = tempfile.mkdtemp(prefix="smoke-service-")
    folder = os.path.join(work, "pictures")
    os.makedirs(folder)
    make_pictures(folder)
    results = []

    def ok(name: str, value: bool, detail: str = "") -> None:
        results.append(value)
        check(name, value, detail)

    with Desktop(
        idle_monitor=False, session_address=os.environ["DBUS_SESSION_BUS_ADDRESS"]
    ) as desktop:
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=desktop.system_address)
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "slideshow_lock.service",
                "--folder",
                folder,
                "--idle-timeout",
                "3",
                "--grace",
                "0",
                "--debug",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines = []

        def read() -> None:
            for line in process.stdout:
                lines.append(line.rstrip())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def logged(text: str) -> bool:
            return any(text in line for line in lines)

        try:
            ok(
                "the service reports it is running",
                wait_for(lambda: logged("service running"), 30),
            )
            ok("it holds the logind delay inhibitor", wait_for(lambda: desktop.held == 1, 5))
            started_at = time.monotonic()
            ok(
                "idle for 3 s starts the slideshow on every monitor",
                wait_for(lambda: logged("started (source=preview, monitors=2)"), 15),
                f"after {time.monotonic() - started_at:.1f} s",
            )
            ok(
                "the service logs the idle trigger",
                wait_for(lambda: logged("[slideshow] started (trigger=idle)"), 5),
            )
            injector = Injector()
            injector.motion()
            ok(
                "pointer motion ends the slideshow and the screensaver gets Lock()",
                wait_for(lambda: len(desktop.lock_calls) == 1, 10),
            )
            ok("the state follows the lock", wait_for(lambda: logged("[lock] session"), 5))
            desktop.set_locked(False)
            ok(
                "unlocking returns to idle watching",
                wait_for(lambda: logged("session unlocked"), 5),
            )
            desktop.prepare_for_sleep(True)
            ok(
                "PrepareForSleep(true) locks again",
                wait_for(lambda: len(desktop.lock_calls) == 2, 5),
            )
            ok("and the delay inhibitor is released", wait_for(lambda: desktop.held == 0, 5))
            desktop.prepare_for_sleep(False)
            ok(
                "the inhibitor is taken again after the wake",
                wait_for(lambda: desktop.held == 1, 5),
            )
            process.send_signal(signal.SIGTERM)
            try:
                status = process.wait(10)
            except subprocess.TimeoutExpired:
                status = None
            ok("SIGTERM ends the service with status 0", status == 0, f"status={status}")
            ok("and the inhibitor is released", wait_for(lambda: desktop.held == 0, 5))
        finally:
            if process.poll() is None:
                process.kill()
            reader.join(2)
            if not all(results):
                print("--- service log ---")
                print("\n".join(lines[-60:]))
    passed = all(results)
    print(f"SMOKE result: {'PASS' if passed else 'FAIL'} ({sum(results)}/{len(results)} checks)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
