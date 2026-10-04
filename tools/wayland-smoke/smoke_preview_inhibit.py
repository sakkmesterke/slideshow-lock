"""Smoke test of the idle request of a manual preview in a headless Wayland session (run it with
``SMOKE_SCRIPT=smoke_preview_inhibit.py run_service.sh``).

Real GTK, real windows on mutter's virtual monitors, and a fake session manager
(``tests/fake_dbus.py``) on the session bus that records what ``Gtk.Application.inhibit`` sends it:

1. ``python3 -m slideshow_lock.preview_app`` as a separate process: while its windows show, the
   session manager lists one idle inhibitor of its application id; pointer motion ends the
   preview, the process exits 0 and the inhibitor is given back (``Uninhibit`` with its cookie);
   the same with SIGTERM instead of the input;
2. the settings window (``PreferencesWindow`` with its ``Gtk.Application``) and its Preview
   button: the inhibitor is there while the preview shows, gone when the preview is stopped, there
   again for a second preview, and gone when the settings window is closed under it;
3. the two minute limit, with ``PREVIEW_LIMIT_SECONDS`` set to 3 s in the process under test (the
   real GLib timer and the real windows; nothing waits two minutes): ``preview_app`` ends by itself
   with status 0 and gives its inhibitor back, and so does the settings window's preview.

What it does not prove: anything about GNOME's own session manager (a fake stands in for it), that
the screen really stays on, or what happens to the inhibitor when the process is killed (the fake
does not watch the bus name; a real session manager is not asked here). Not part of pytest or CI.
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

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smoke_preview import Injector, check, make_pictures  # noqa: E402
from smoke_service import wait_for  # noqa: E402

from slideshow_lock import APP_ID, preview_app  # noqa: E402
from slideshow_lock.preferences import PreferencesWindow  # noqa: E402
from slideshow_lock.settings import Settings  # noqa: E402
from tests.fake_dbus import Desktop  # noqa: E402

RESULTS = []
IDLE = 8


def ok(name: str, value: bool, detail: str = "") -> None:
    RESULTS.append(bool(value))
    check(name, bool(value), detail)


def idle_inhibitors(desktop: Desktop, app_id: str) -> list:
    """The idle inhibitors of *app_id*. One that is given back between the list and the read is
    not there any more: it is skipped, not an error."""
    found = []
    for path in desktop.inhibitors_of(app_id):
        inhibitor = desktop.inhibitors.get(path)
        if inhibitor is not None and inhibitor["flags"] == IDLE:
            found.append(inhibitor)
    return found


def preview_process(desktop: Desktop, folder: str) -> None:
    app_id = APP_ID + ".Preview"
    injector = Injector()
    injector.place_pointer()  # parked before any window exists, as a resting mouse is
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "slideshow_lock.preview_app", "--folder", folder, "--debug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    reader = threading.Thread(
        target=lambda: [lines.append(line.rstrip()) for line in process.stdout], daemon=True
    )
    reader.start()
    try:
        ok(
            "preview_app opens a window per monitor",
            wait_for(lambda: any("started (source=preview, monitors=2)" in x for x in lines), 30),
        )
        ok(
            "and the session manager lists one idle inhibitor of its application id",
            wait_for(lambda: len(idle_inhibitors(desktop, app_id)) == 1, 5),
            f"{desktop.inhibit_requests}",
        )
        # the first pointer events of a new window only set its baseline: wait for a picture
        wait_for(lambda: any("] showing '" in x for x in lines), 20)
        wait_for(lambda: False, 1.0)
        injector.motion()
        ok(
            "pointer motion ends the preview and the process exits 0",
            wait_for(lambda: process.poll() == 0, 15),
            f"status={process.poll()}",
        )
        ok(
            "the inhibitor is given back with the cookie it was handed",
            idle_inhibitors(desktop, app_id) == [] and len(desktop.uninhibit_calls) >= 1,
            f"uninhibit={desktop.uninhibit_calls}",
        )
        reader.join(2)
        ok("and GTK logged no critical error on the way", not any("CRITICAL" in x for x in lines))
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(2)
        if not all(RESULTS):
            print("--- preview_app log ---")
            print("\n".join(lines[-40:]))


def preview_process_terminated(desktop: Desktop, folder: str) -> None:
    """The same process, ended by SIGTERM while the preview shows."""
    app_id = APP_ID + ".Preview"
    given_back_before = len(desktop.uninhibit_calls)
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "slideshow_lock.preview_app", "--folder", folder, "--debug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    reader = threading.Thread(
        target=lambda: [lines.append(line.rstrip()) for line in process.stdout], daemon=True
    )
    reader.start()
    try:
        ok(
            "a second preview_app holds its inhibitor again",
            wait_for(lambda: len(idle_inhibitors(desktop, app_id)) == 1, 30),
        )
        process.send_signal(signal.SIGTERM)
        ok(
            "SIGTERM while it shows ends it with status 0",
            wait_for(lambda: process.poll() == 0, 15),
            f"status={process.poll()}",
        )
        ok(
            "and the inhibitor is given back",
            idle_inhibitors(desktop, app_id) == []
            and len(desktop.uninhibit_calls) == given_back_before + 1,
            f"uninhibit={desktop.uninhibit_calls}",
        )
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(2)
        if not all(RESULTS):
            print("--- preview_app log (SIGTERM) ---")
            print("\n".join(lines[-40:]))


def preview_process_limit(desktop: Desktop, folder: str) -> None:
    """The same process with the limit shortened to 3 s: nobody touches it."""
    app_id = APP_ID + ".Preview"
    given_back_before = len(desktop.uninhibit_calls)
    code = (
        "import sys; from slideshow_lock import preview_app as p; p.PREVIEW_LIMIT_SECONDS = 3; "
        "sys.exit(p.main(['--folder', sys.argv[1], '--debug']))"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, folder],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    reader = threading.Thread(
        target=lambda: [lines.append(line.rstrip()) for line in process.stdout], daemon=True
    )
    reader.start()
    try:
        ok(
            "a preview_app with a 3 s limit holds its inhibitor while it shows",
            wait_for(lambda: len(idle_inhibitors(desktop, app_id)) == 1, 30),
        )
        started = time.monotonic()
        ok(
            "and ends by itself with status 0, nobody touching it",
            wait_for(lambda: process.poll() == 0, 20),
            f"status={process.poll()} after {time.monotonic() - started:.1f}s",
        )
        ok(
            "the log names the limit as the reason",
            any("reason=time limit" in x for x in lines),
        )
        ok(
            "and the inhibitor is given back",
            idle_inhibitors(desktop, app_id) == []
            and len(desktop.uninhibit_calls) == given_back_before + 1,
            f"uninhibit={desktop.uninhibit_calls}",
        )
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(2)
        if not all(RESULTS):
            print("--- preview_app log (limit) ---")
            print("\n".join(lines[-40:]))


def settings_window(desktop: Desktop, folder: str) -> None:
    app_id = APP_ID + ".Preferences"
    settings = Settings()
    assert settings.set_picture_folder(folder)
    application = Gtk.Application(application_id=app_id)
    application.register(None)
    window = PreferencesWindow(Settings(), application)
    window.present()

    def pump(seconds, until=None) -> bool:
        context = GLib.MainContext.default()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if until is not None and until():
                return True
            if not context.iteration(False):
                time.sleep(0.005)
        return bool(until()) if until is not None else True

    def held() -> int:
        return len(idle_inhibitors(desktop, app_id))

    pump(3.0, until=lambda: window.get_mapped() and window.get_width() > 0)
    window.preview_button.emit("clicked")
    ok(
        "the settings window's preview holds one idle inhibitor",
        pump(5.0, until=lambda: held() == 1) and window._preview is not None,
        f"{desktop.inhibit_requests}",
    )
    window._preview[0].stop("smoke")
    ok("and gives it back when the preview ends", pump(5.0, until=lambda: held() == 0))
    pump(5.0, until=window.preview_button.get_sensitive)
    preview_app.PREVIEW_LIMIT_SECONDS = 3
    try:
        window.preview_button.emit("clicked")
        ok(
            "a preview of the settings window under a 3 s limit holds one inhibitor",
            pump(5.0, until=lambda: held() == 1 and window._preview is not None),
        )
        ok(
            "ends by itself and gives it back, the button is usable again",
            pump(10.0, until=lambda: held() == 0 and window._preview is None)
            and window.preview_button.get_sensitive(),
        )
    finally:
        preview_app.PREVIEW_LIMIT_SECONDS = 120
    window.preview_button.emit("clicked")
    ok(
        "a second preview holds one again",
        pump(5.0, until=lambda: held() == 1 and window._preview is not None),
    )
    window.close()
    ok(
        "closing the settings window under a preview gives it back",
        pump(5.0, until=lambda: held() == 0),
    )
    ok(
        "the session manager lists no idle inhibitor of the window's application at the end",
        idle_inhibitors(desktop, app_id) == [],
    )


def main() -> int:
    work = tempfile.mkdtemp(prefix="smoke-preview-inhibit-")
    folder = os.path.join(work, "pictures")
    os.makedirs(folder)
    make_pictures(folder)
    with Desktop(
        idle_monitor=False, session_address=os.environ["DBUS_SESSION_BUS_ADDRESS"]
    ) as desktop:
        preview_process(desktop, folder)
        preview_process_terminated(desktop, folder)
        preview_process_limit(desktop, folder)
        settings_window(desktop, folder)
    passed = all(RESULTS)
    print(f"SMOKE result: {'PASS' if passed else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)} checks)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
