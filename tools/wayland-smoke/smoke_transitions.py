"""Smoke test of the ten transitions in a real Wayland session (run it through run.sh):

    SMOKE_SCRIPT=smoke_transitions.py tools/wayland-smoke/run.sh --monitors 640x360

What it does: one real ``PreviewWindow`` with the real ``_Canvas``; two pictures of the window's
exact size with known colours; for each of the ten transitions the canvas is driven by hand (the
frame clock's tick is replaced by calls with chosen times, so nothing waits and nothing depends on
the machine's speed) and what the window's own renderer draws is read back as pixels:

* the first frame (progress 0) is the old picture, pixel for pixel;
* in the middle something is drawn that is neither the old nor the new picture;
* after the end the canvas is plain again: no run, no old texture, no tick, and the pixels are the
  new picture, pixel for pixel;
* a ``show_frame`` or a ``show_message`` in the middle of a run ends it at once.

What it does not prove: how a transition looks (a human judgement on the real screen), speed, GPU
time or battery, the behaviour of GTK 4.16 and of a real GPU; the clock of a real run is the unit
tests' business (``tests/test_transition_draw.py``), not this script's. Pixels are read through
ctypes because PyGObject cannot marshal ``GskRenderNode``.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from slideshow_lock import preview_window  # noqa: E402
from slideshow_lock.preview_window import PreviewWindow, software_gl  # noqa: E402
from slideshow_lock.scaling import Frame  # noqa: E402
from slideshow_lock.transitions import ALL_TRANSITIONS, BLUR, CROSSFADE, KEN_BURNS  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(f"SMOKE {name:<60} {'OK' if ok else 'FAIL'}  {detail}".rstrip(), flush=True)


gtk = ctypes.CDLL("libgtk-4.so.1")
gobject = ctypes.CDLL("libgobject-2.0.so.0")
gtk.gtk_snapshot_to_node.restype = ctypes.c_void_p
gtk.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
gtk.gsk_renderer_render_texture.restype = ctypes.c_void_p
gtk.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
gtk.gdk_texture_get_width.argtypes = [ctypes.c_void_p]
gtk.gdk_texture_get_height.argtypes = [ctypes.c_void_p]
gtk.gdk_texture_download.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
gtk.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
gobject.g_object_unref.argtypes = [ctypes.c_void_p]
gobject.g_type_name_from_instance.restype = ctypes.c_char_p
gobject.g_type_name_from_instance.argtypes = [ctypes.c_void_p]


def cptr(obj) -> int:
    """C pointer of a PyGObject wrapper (PyObject_HEAD, then GObject *obj)."""
    ptr = ctypes.c_void_p.from_address(id(obj) + 16).value
    assert gobject.g_type_name_from_instance(ptr) is not None
    return ptr


def capture(window: PreviewWindow):
    """What the canvas draws now, as a function ``(x, y) -> (r, g, b)``."""
    canvas = window._canvas
    snapshot = Gtk.Snapshot()
    canvas.do_snapshot(snapshot)
    node = gtk.gtk_snapshot_to_node(cptr(snapshot))
    renderer = window._window.get_native().get_renderer()
    texture = gtk.gsk_renderer_render_texture(cptr(renderer), node, None)
    width, height = gtk.gdk_texture_get_width(texture), gtk.gdk_texture_get_height(texture)
    stride = width * 4
    data = (ctypes.c_ubyte * (stride * height))()
    gtk.gdk_texture_download(texture, data, stride)
    gtk.gsk_render_node_unref(node)
    gobject.g_object_unref(texture)

    def pixel(x: int, y: int):
        o = y * stride + 4 * x
        return (data[o + 2], data[o + 1], data[o])  # B8G8R8A8: blue first

    pixel.size = (width, height)
    return pixel


def make_frame(path: str, width: int, height: int, kind: str) -> Frame:
    data = bytearray(3 * width * height)
    for y in range(height):
        for x in range(width):
            o = 3 * (y * width + x)
            if kind == "a":  # red grows to the right
                data[o], data[o + 1], data[o + 2] = x * 255 // width, 40, 200
            else:  # green grows downwards, red high, blue low
                data[o], data[o + 1], data[o + 2] = 220, y * 255 // height, 30
    return Frame(path, width, height, 3 * width, GLib.Bytes.new(bytes(data)), "test", (0, 0))


class Clock:
    def __init__(self, microseconds: int) -> None:
        self._now = microseconds

    def get_frame_time(self) -> int:
        return self._now


def pump(milliseconds: int = 120) -> None:
    """Let the main loop run a little (the window's size, a paint)."""
    context = GLib.MainContext.default()
    end = time.monotonic() + milliseconds / 1000.0
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def points(width: int, height: int):
    return [
        (width // 8, height // 8),
        (width // 2, height // 2),
        (width * 7 // 8, height // 8),
        (width // 8, height * 7 // 8),
        (width * 7 // 8, height * 7 // 8),
        (width // 4, height // 2),
    ]


def same(pixel, frame_pixels, pts, tolerance=2) -> bool:
    return all(
        all(abs(a - b) <= tolerance for a, b in zip(pixel(x, y), frame_pixels(x, y)))
        for x, y in pts
    )


def reader(frame: Frame):
    data = bytes(frame.pixels.get_data())

    def pixel(x: int, y: int):
        o = 3 * (y * frame.width + x)
        return (data[o], data[o + 1], data[o + 2])

    return pixel


def step_by_hand(canvas, run_seconds: float, fraction: float) -> bool:
    """Drive the canvas's own tick function: the first tick, the clock's start, then *fraction*."""
    if canvas._run_tick_id:  # take the real tick away, so only these calls drive the run
        canvas.remove_tick_callback(canvas._run_tick_id)
        canvas._run_tick_id = 0
    alive = canvas._on_transition_tick(None, Clock(1_000_000))
    alive = canvas._on_transition_tick(None, Clock(2_000_000)) and alive
    return canvas._on_transition_tick(None, Clock(2_000_000 + int(fraction * run_seconds * 1e6)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    Gtk.init()
    monitor = Gdk.Display.get_default().get_monitors().get_item(0)
    window = PreviewWindow(monitor)
    window._window.present()
    for _ in range(100):
        pump(50)
        size = window.device_size()
        if size and size[0] > 100:
            break
    size = window.device_size()
    check("the window has a size", bool(size and size[0] > 100), str(size))
    if not size:
        return 1
    width, height = size
    canvas = window._canvas
    renderer_name = window._window.get_native().get_renderer().__gtype__.name
    software = software_gl(canvas)
    check("the renderer is told (informational)", True, f"{renderer_name}, software={software}")

    a = make_frame("a", width, height, "a")
    b = make_frame("b", width, height, "b")
    pa, pb = reader(a), reader(b)
    pts = points(width, height)

    window.show_frame(a, 0.0)
    pump(80)
    check("the old picture is drawn plainly, pixel for pixel", same(capture(window), pa, pts))

    # the blur is drawn as a cross fade on software rendering: it is run a second time as if the
    # renderer were hardware, so that its own drawing (the reduced pictures and the blur node) runs
    for name, as_hardware in [(n, False) for n in ALL_TRANSITIONS] + [(BLUR, True)]:
        preview_window._software_gl_cache = False if as_hardware else software
        label = f"{name} (hardware path forced)" if as_hardware else name
        window.show_frame(a, 0.0)
        pump(60)
        seconds = 1.0
        window.show_frame(b, 4.0 if name == KEN_BURNS else 0.0, (name, seconds))
        run = canvas._run
        expected = name
        if name == BLUR and software and not as_hardware:
            expected = CROSSFADE
        check(f"{label}: a run starts", run is not None and canvas._old_texture is not None)
        if run is None:
            continue
        check(
            f"{label}: the run is the expected transition",
            run.name == expected,
            f"{run.name} (software gl: {software})",
        )
        total = run.seconds
        first = capture(window)
        check(f"{label}: the first frame is the old picture", same(first, pa, pts))
        if not same(first, pa, pts):
            print("  first frame differs:", [(first(x, y), pa(x, y)) for x, y in pts], flush=True)
        step_by_hand(canvas, total, 0.5)
        mid = capture(window)
        check(
            f"{label}: in the middle it is neither picture",
            (not same(mid, pa, pts, 1) and not same(mid, pb, pts, 1)) or run.name == BLUR,
        )
        alive = step_by_hand(canvas, total, 1.0)
        check(f"{label}: the run is over at its end and says so", alive is False)
        check(
            f"{label}: nothing of it is left (run, old texture, tick)",
            canvas._run is None
            and canvas._old_texture is None
            and canvas._run_tick_id == 0
            and not canvas._reduced,
        )
        check(
            f"{label}: the end is the new picture, pixel for pixel",
            same(capture(window), pb, pts, 0),
        )

    # a new picture, a message or a close in the middle of a run ends it at once
    for what in ("show_frame", "show_message", "close_frame"):
        window.show_frame(a, 0.0)
        pump(60)
        window.show_frame(b, 0.0, (CROSSFADE, 1.0))
        started = canvas._run is not None
        if what == "show_frame":
            window.show_frame(a, 0.0)
        elif what == "show_message":
            window.show_message("x")
            canvas.set_frame(None, 0.0)
        else:
            canvas.set_frame(None, 0.0)
        check(
            f"{what} in the middle ends the run at once",
            started
            and canvas._run is None
            and canvas._old_texture is None
            and not canvas._run_tick_id,
        )
    # the same picture again (a folder of one) and no old picture are cuts
    window.show_frame(b, 0.0)
    window.show_frame(b, 0.0, (CROSSFADE, 1.0))
    check("the same picture again is not a transition", canvas._run is None)
    canvas.set_frame(None, 0.0)
    window.show_frame(a, 0.0, (CROSSFADE, 1.0))
    check("a window with nothing on it shows the picture without a transition", canvas._run is None)
    # a Ken Burns picture that does not fill the window is a cross fade
    small = make_frame("s", width // 2, height // 2, "b")
    window.show_frame(a, 0.0)
    window.show_frame(small, 4.0, (KEN_BURNS, 0.8))
    check(
        "Ken Burns on a picture smaller than the window is a cross fade",
        canvas._run is not None and canvas._run.name == CROSSFADE,
    )

    preview_window._software_gl_cache = software
    failed = RESULTS.count(False)
    print(f"SMOKE total {len(RESULTS)} checks, {failed} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
