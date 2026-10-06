"""Smoke test of the real preview in a real Wayland session (run it through run.sh).

What it exercises: the real GTK windows (one per virtual monitor), the real scaler, the real
image source on a real folder, the real Settings (memory backend), the real GLib main loop,
and real pointer, key, button and scroll events injected through mutter's remote-desktop
service. The folder holds a landscape picture, a portrait picture, a truncated JPEG with a
valid header and a JPEG header followed by garbage; the last two must be skipped.

What it does not prove: how the picture looks (that is a human judgement on the real
monitor), real GPU behaviour, or anything about locking: the preview has no lock call, and
this script only checks that the preview ends and nothing else.

    run.sh --input motion          # inject pointer motion, expect the preview to stop
    run.sh --input motion-small    # a 1 px move must not end it, a 5 px move must (threshold 2)
    run.sh --input button          # left, rbutton: right, mbutton: middle button
    run.sh --input close           # Gtk.Window.close() on a window: close-request ends it
    run.sh --input none            # inject nothing: the preview must keep running
    run.sh --animations off --pan  # "reduce animations" on: portrait pictures do not scroll

What a check proves is said in its own name. Two things to know: ``--input none`` injects
nothing and does not park the pointer, it rests wherever the compositor put it; and the key
and the close checks fire the controller (``key-pressed``) or ``Gtk.Window.close()`` by hand,
which shows the wiring from the window to the preview, not that GTK or mutter produce those
events on their own.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import tempfile
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from slideshow_lock.preview import GLibClock, PreviewController, ThreadWorker  # noqa: E402
from slideshow_lock.preview_app import build_source  # noqa: E402
from slideshow_lock.preview_window import animations_enabled, open_monitor_windows  # noqa: E402
from slideshow_lock.scaling import ImageScaler  # noqa: E402
from slideshow_lock.settings import Settings  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(f"SMOKE {name:<52} {'OK' if ok else 'FAIL'}  {detail}".rstrip(), flush=True)


def make_pictures(folder: str) -> None:
    def gradient(width: int, height: int) -> GdkPixbuf.Pixbuf:
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        stride = pixbuf.get_rowstride()
        row = bytearray(stride)
        for x in range(width):
            row[3 * x] = x * 255 // width
            row[3 * x + 2] = 128
        data = bytearray(stride * height)
        for y in range(height):
            data[y * stride : (y + 1) * stride] = row
            for x in range(0, width, 24):  # vertical ticks, so scaling has detail to work on
                data[y * stride + 3 * x + 1] = y * 255 // height
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, False, 8, width, height, stride
        )

    gradient(3000, 2000).savev(os.path.join(folder, "a_landscape.jpg"), "jpeg", ["quality"], ["90"])
    gradient(1500, 2200).savev(os.path.join(folder, "b_portrait.png"), "png", [], [])
    with open(os.path.join(folder, "a_landscape.jpg"), "rb") as handle:
        whole = handle.read()
    with open(os.path.join(folder, "c_truncated.jpg"), "wb") as handle:
        handle.write(whole[: len(whole) * 2 // 5])
    with open(os.path.join(folder, "d_garbage.jpg"), "wb") as handle:
        handle.write(b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(600))
    for name in os.listdir(folder):  # long settled: not "still being copied"
        old = time.time() - 3600
        os.utime(os.path.join(folder, name), (old, old))


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class RecordingWindow:
    """Wraps a real PreviewWindow: notes every frame shown and counts the paints after it."""

    def __init__(self, inner, index: int, shown: list) -> None:
        self.inner = inner
        self.index = index
        self.shown = shown
        self.paints = 0
        window = inner._window
        if window.get_realized():
            self._watch_paints(window)
        else:
            window.connect("realize", self._watch_paints)

    def _watch_paints(self, window) -> None:
        window.get_frame_clock().connect("after-paint", lambda *_a: self._count())

    def _count(self) -> None:
        self.paints += 1

    def device_size(self):
        return self.inner.device_size()

    def show_frame(self, frame, pan_seconds, transition=None):
        self.shown.append((self.index, frame, time.monotonic(), self.paints))
        self.inner.show_frame(frame, pan_seconds, transition)

    def show_message(self, text):
        self.inner.show_message(text)

    def connect_input(self, callback):
        self.inner.connect_input(callback)

    def connect_size_changed(self, callback):
        self.inner.connect_size_changed(callback)

    def close(self):
        self.inner.close()


class Injector:
    """Pointer, key, button and scroll events through mutter's remote-desktop service."""

    BUS = "org.gnome.Mutter.RemoteDesktop"

    def __init__(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = self._call("/org/gnome/Mutter/RemoteDesktop", self.BUS, "CreateSession")
        self._session = reply.unpack()[0]
        self._call(self._session, self.BUS + ".Session", "Start")

    def _call(self, path, iface, method, params=None):
        return self._bus.call_sync(
            self.BUS, path, iface, method, params, None, Gio.DBusCallFlags.NONE, 5000, None
        )

    def _session_call(self, method, params):
        self._call(self._session, self.BUS + ".Session", method, params)

    def place_pointer(self) -> None:
        """Park the pointer inside the first monitor. Done before any window exists: like a
        mouse that is simply resting there when the preview appears."""
        for _ in range(8):
            self._session_call("NotifyPointerMotionRelative", GLib.Variant("(dd)", (30.0, 20.0)))
            time.sleep(0.03)

    def motion(self, steps: int = 6) -> None:
        """Move the pointer in small steps spread over time, with the main loop running: a
        burst sent from inside one callback reaches the client as one coalesced event."""
        left = {"n": steps}

        def step() -> bool:
            self._session_call("NotifyPointerMotionRelative", GLib.Variant("(dd)", (15.0, 10.0)))
            left["n"] -= 1
            return left["n"] > 0

        GLib.timeout_add(60, step)

    def key(self, windows) -> None:
        # Headless mutter without a shell gives no window keyboard focus, so a real key press is
        # never delivered. The key controller of the first window is fired by hand instead:
        # this checks the wiring from controller to preview, not the compositor's delivery.
        controllers = windows[0].inner._window.observe_controllers()
        for i in range(controllers.get_n_items()):
            controller = controllers.get_item(i)
            if isinstance(controller, Gtk.EventControllerKey):
                controller.emit("key-pressed", Gdk.KEY_space, 65, 0)
                return
        raise RuntimeError("no key controller on the window")

    def small_motion(self, dx: float, dy: float) -> None:
        """One relative step, with the main loop running."""

        def step() -> bool:
            self._session_call("NotifyPointerMotionRelative", GLib.Variant("(dd)", (dx, dy)))
            return False

        GLib.timeout_add(60, step)

    def button(self, code: int = 272) -> None:  # 272 left, 273 right, 274 middle
        for state in (True, False):
            self._session_call("NotifyPointerButton", GLib.Variant("(ib)", (code, state)))

    def scroll(self) -> None:
        self._session_call("NotifyPointerAxis", GLib.Variant("(ddu)", (0.0, 15.0, 0)))


def monitor_sizes() -> list:
    monitors = Gdk.Display.get_default().get_monitors()
    sizes = []
    for i in range(monitors.get_n_items()):
        geometry = monitors.get_item(i).get_geometry()
        scale = monitors.get_item(i).get_scale_factor()
        sizes.append((geometry.width * scale, geometry.height * scale))
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        choices=(
            "none",
            "motion",
            "motion-small",
            "key",
            "button",
            "rbutton",
            "mbutton",
            "scroll",
            "close",
        ),
        default="motion",
    )
    parser.add_argument(
        "--animations", choices=("on", "off"), default="on", help="the desktop's animation setting"
    )
    parser.add_argument("--scaling", choices=("fit", "fill"), default="fill")
    parser.add_argument("--pan", action="store_true")
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--dump", help="write every frame shown as a PNG into this folder")
    args = parser.parse_args()
    if args.pan and args.scaling == "fit":
        parser.error("--pan needs --scaling fill: the preview scrolls only a filled picture")

    recorder = Recorder()
    logging.getLogger().addHandler(recorder)
    logging.getLogger().setLevel(logging.INFO)

    folder = tempfile.mkdtemp(prefix="slideshow-smoke-")
    make_pictures(folder)

    settings = Settings()
    check(
        "settings accept the run's values",
        all(
            [
                settings.set_picture_folder(folder),
                settings.set_slide_interval_seconds(args.interval),
                settings.set_order("name"),
                settings.set_scaling(args.scaling),
                settings.set_pan_portrait_images(args.pan),
            ]
        ),
    )

    app = Gtk.Application(application_id="io.github.sakkmesterke.SlideshowLock.Smoke")
    state = {"stopped": None, "gaps": [], "gap_at": [], "status": 0}
    shown: list = []

    def finish(status: int = 0) -> None:
        state["status"] = status
        app.quit()

    def on_activate(application) -> None:
        application.hold()
        if args.animations == "off":
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        injector = Injector() if args.input not in ("none", "close") else None
        if injector is not None:
            injector.place_pointer()
        source = build_source(settings)
        source.start()

        def factory():
            return [RecordingWindow(w, i, shown) for i, w in enumerate(open_monitor_windows())]

        controller = PreviewController(
            source,
            settings,
            factory,
            ImageScaler(),
            clock=GLibClock(),
            worker=ThreadWorker(),
            animations=animations_enabled,  # as start_preview wires it
        )
        controller.connect_stopped(lambda reason: state.__setitem__("stopped", reason))
        controller.start()

        # main-loop stall probe: a 10 ms timer; the gap between two runs is the stall
        last = {"t": time.monotonic()}

        def probe() -> bool:
            now = time.monotonic()
            state["gaps"].append(now - last["t"])
            state["gap_at"].append(now)
            last["t"] = now
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(10, probe)
        began = time.monotonic()

        offsets = []  # (time, picture, vertical scroll offset in pixels) of the first window

        def sample_offset() -> bool:
            if controller.running and controller._windows:
                canvas = controller._windows[0].inner._canvas
                if canvas._frame is not None:
                    offsets.append(
                        (time.monotonic(), canvas._frame.path, canvas._offset[1], canvas._tick_id)
                    )
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(100, sample_offset)

        def evaluate() -> bool:
            order = []
            for _index, frame, _at, _paints in shown:
                name = os.path.basename(frame.path)
                if not order or order[-1] != name:
                    order.append(name)
            windows = controller._windows
            sizes = monitor_sizes()
            check(
                "one window per monitor",
                len(windows) == len(sizes),
                f"{len(windows)} windows, {len(sizes)} monitors",
            )
            check(
                "window pixel size equals the monitor's",
                [w.device_size() for w in windows] == sizes,
                f"windows {[w.device_size() for w in windows]} monitors {sizes}",
            )
            expected = ["a_landscape.jpg", "b_portrait.png"] * 2
            check("order by name, bad pictures skipped", order[:4] == expected, str(order[:4]))
            skipped = [r.getMessage() for r in recorder.records if "skipping" in r.getMessage()]
            check(
                "both damaged files skipped and logged",
                any("c_truncated" in m for m in skipped) and any("d_garbage" in m for m in skipped),
                f"{len(skipped)} log lines",
            )
            fits = []
            for index, frame, _at, _paints in shown:
                w, h = sizes[index]
                if args.scaling == "fit":
                    ok = (
                        frame.width <= w
                        and frame.height <= h
                        and (frame.width == w or frame.height == h)
                    )
                elif args.pan and frame.pan_range != (0, 0):
                    ok = (
                        frame.width == w
                        and frame.height > h
                        and frame.pan_range == (0, frame.height - h)
                    )
                else:
                    ok = (frame.width, frame.height) == (w, h)
                fits.append(ok)
            check(
                "every frame has the scaled size of its monitor",
                bool(fits) and all(fits),
                f"{len(fits)} frames",
            )
            methods = sorted({f.method for _i, f, _t, _p in shown})
            print(f"SMOKE scaling methods used: {methods}", flush=True)
            check(
                "windows painted after frames were shown",
                windows[0].paints > 0,
                f"{windows[0].paints} paints",
            )
            changes = []  # when each change of picture happened (first window that showed it)
            for _index, frame, at, _paints in shown:
                name = os.path.basename(frame.path)
                if not changes or changes[-1][0] != name:
                    changes.append((name, at))
            gaps = [round(b[1] - a[1], 2) for a, b in zip(changes[:4], changes[1:4])]
            check(
                "interval of the setting respected",
                all(args.interval - 0.4 <= g <= args.interval + 0.9 for g in gaps),
                f"gaps {gaps}",
            )
            worst = max(state["gaps"]) * 1000
            print(
                f"SMOKE longest main-loop stall: {worst:.1f} ms over {len(state['gaps'])} probes",
                flush=True,
            )
            # Where do the stalls fall? Decode and scaling run on the worker, so what is left is
            # the frame hand-over: texture upload and drawing at the moment a picture appears.
            show_times = sorted({at for _i, _f, at, _p in shown})
            near = lambda t: any(0 <= t - st <= 0.6 for st in show_times)  # noqa: E731
            inside = [g for g, t in zip(state["gaps"], state["gap_at"]) if near(t)]
            outside = [g for g, t in zip(state["gaps"], state["gap_at"]) if not near(t)]
            print(
                "SMOKE stall within 0.6 s after a picture appears: "
                f"{max(inside, default=0) * 1000:.1f} ms; at all other times: "
                f"{max(outside, default=0) * 1000:.1f} ms",
                flush=True,
            )
            # redraws of the first window between one picture appearing and the next
            per_picture = []
            first_window = [(frame, at, paints) for index, frame, at, paints in shown if index == 0]
            for (frame, _at, paints), (_next, _next_at, next_paints) in zip(
                first_window, first_window[1:]
            ):
                per_picture.append(
                    (os.path.basename(frame.path), frame.pan_range != (0, 0), next_paints - paints)
                )
            print(f"SMOKE redraws per picture (first window): {per_picture[:4]}", flush=True)
            if args.pan and args.animations == "off":
                portrait = [
                    (index, frame)
                    for index, frame, _at, _paints in shown
                    if frame.path.endswith("b_portrait.png")
                ]
                check(
                    "animations off: no tall panning frame is made, the picture is monitor-sized",
                    bool(portrait)
                    and all(
                        frame.pan_range == (0, 0) and (frame.width, frame.height) == sizes[index]
                        for index, frame in portrait
                    ),
                    f"{[(f.width, f.height, f.pan_range) for _i, f in portrait[:2]]}",
                )
                check(
                    "animations off: nothing scrolls and no tick runs",
                    len(offsets) >= 5 and all(o == 0 and tick == 0 for _t, _p, o, tick in offsets),
                    f"{len(offsets)} samples",
                )
            elif args.pan:
                runs = []  # offsets seen while each completed portrait picture was on screen
                for (frame, at, _p), (_f2, until, _p2) in zip(first_window, first_window[1:]):
                    if frame.pan_range != (0, 0):
                        runs.append([o for t, _path, o, _tick in offsets if at <= t < until])
                top_to_bottom = bool(runs) and all(
                    len(run) >= 5
                    and run == sorted(run)
                    and run[0] <= 0.1 * first_window[1][0].pan_range[1]
                    and run[-1] >= 0.9 * first_window[1][0].pan_range[1]
                    for run in runs
                )
                check(
                    "portrait picture scrolls top to bottom over the interval",
                    top_to_bottom,
                    f"{len(runs)} runs, offsets {runs[0][::4] if runs else []}",
                )
            else:
                still = [o for _t, _p, o, _tick in offsets]
                check("without --pan nothing scrolls", set(still) == {0}, f"{len(still)} samples")
            # Not a check: how long the loop stalls depends on the machine (the same code gave
            # 55 to 240 ms on clean runs on three machines), so a threshold only measured the
            # machine. The number is printed above; that picture work is off the main loop is
            # what tests/test_preview.py::test_ac7_* prove, with a negative control.
            check(
                "every window hides the pointer (cursor property, not what the compositor draws)",
                all(
                    w.inner._window.get_cursor() is not None
                    and w.inner._window.get_cursor().get_name() == "none"
                    for w in windows
                ),
            )
            if args.dump:
                os.makedirs(args.dump, exist_ok=True)
                for n, (index, frame, _t, _p) in enumerate(shown[:8]):
                    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                        frame.pixels,
                        GdkPixbuf.Colorspace.RGB,
                        False,
                        8,
                        frame.width,
                        frame.height,
                        frame.stride,
                    )
                    pixbuf.savev(
                        os.path.join(
                            args.dump, f"{n}-mon{index}-{os.path.basename(frame.path)}.png"
                        ),
                        "png",
                        [],
                        [],
                    )
            injected = time.monotonic()

            def after_input() -> bool:
                check(
                    f"{args.input} on a window ends the preview",
                    state["stopped"] == "input" and not controller.running,
                    f"stopped={state['stopped']!r} after {time.monotonic() - injected:.2f} s",
                )
                check(
                    "all windows closed",
                    all(not w.inner._window.is_visible() for w in windows),
                )
                finish()
                return GLib.SOURCE_REMOVE

            if args.input == "motion-small":
                # The first pointer event a window gets can carry coordinates of another frame
                # than the later ones (measured: (27, 66) first, then (257, 176) for a pointer
                # that had not moved but 1 px), so the baseline of the first event is useless
                # for a threshold test. The baseline is reset by hand, then taken from a real
                # 1 px event; after that, 1 px more must not end the preview and 4 + 3 px must.
                for window in windows:
                    window.inner._origin = None
                injector.small_motion(1.0, 0.0)  # becomes the baseline, never counts

                def second_step() -> bool:
                    injector.small_motion(1.0, 0.0)  # 1 px from the baseline: under 2 px
                    return GLib.SOURCE_REMOVE

                def check_quiet_then_move() -> bool:
                    check(
                        "a 1 px move from the baseline does not end the preview",
                        controller.running and state["stopped"] is None,
                    )
                    injector.small_motion(4.0, 3.0)  # 6 px from the baseline: over 2 px
                    GLib.timeout_add(1000, after_input)
                    return GLib.SOURCE_REMOVE

                GLib.timeout_add(500, second_step)
                GLib.timeout_add(1500, check_quiet_then_move)
            elif args.input == "none":
                check(
                    "no phantom input: preview still running",
                    controller.running and state["stopped"] is None,
                )
                controller.stop("requested")
                check("stop() closed the preview", state["stopped"] == "requested")
                GLib.timeout_add(500, lambda: (finish(), False)[1])
            else:
                if args.input == "key":
                    injector.key(windows)
                elif args.input == "close":
                    windows[0].inner._window.close()  # the signal path of a close from outside
                elif args.input in ("button", "rbutton", "mbutton"):
                    injector.button({"button": 272, "rbutton": 273, "mbutton": 274}[args.input])
                else:
                    getattr(injector, args.input)()
                GLib.timeout_add(1000, after_input)
            return GLib.SOURCE_REMOVE

        def wait_for_shows() -> bool:
            if len(shown) >= 2 * 4 or time.monotonic() - began > 30:
                evaluate()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(250, wait_for_shows)
        GLib.timeout_add_seconds(
            60, lambda: (print("SMOKE watchdog fired", flush=True), finish(3))[1] and False
        )

    app.connect("activate", on_activate)
    app.run([sys.argv[0]])
    ok = all(RESULTS) and state["status"] == 0
    print(
        f"SMOKE result: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)} checks)",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
