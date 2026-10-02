#!/usr/bin/env python3
"""On-target check of the GTK4 scaling paths, to be run on a machine with a real display.

Shows one picture fitted into a fullscreen window in five ways and reports what the real
renderer does with them:

  0  GPU, texture node   snapshot.append_texture(): what a plain Gtk.Picture draws (linear
                         filtering, plus automatic mipmaps for a big reduction)
  1  GPU, linear         append_scaled_texture() with an explicit LINEAR filter
  2  GPU, trilinear      append_scaled_texture() with TRILINEAR (mipmaps requested explicitly)
  3  CPU, bilinear       GdkPixbuf scale_simple to the exact device-pixel size, shown 1:1
  4  CPU, Lanczos-3      GStreamer videoscale (lanczos, envelope 3) to the exact size, shown 1:1

Keys: Space = next mode, Q or Esc = quit. Compare modes 0-4 by eye on a photo you know well
(look at fine detail: branches, text, hair, brick patterns). The frame statistics are printed
for the automatic pass only; Space is for flicking between modes by eye afterwards.

Usage: python3 target_check.py [--once] [PHOTO.jpg]
Without a photo a 23 MP synthetic test scene is generated (needs python3-numpy).
--once quits after the automatic pass instead of waiting for keys (for scripted runs).
"""

import os
import statistics
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gsk", "4.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gst, Gtk  # noqa: E402

Gst.init(None)

MODES = [
    ("0  GPU texture node (plain Gtk.Picture)", "texture"),
    ("1  GPU linear (explicit)", "linear"),
    ("2  GPU trilinear (mipmaps)", "trilinear"),
    ("3  CPU pixbuf bilinear, 1:1", "pixbuf"),
    ("4  CPU GStreamer Lanczos-3, 1:1", "lanczos"),
]
SECONDS_PER_MODE = 4


def gst_lanczos(pb, w, h):
    pipe = Gst.parse_launch(
        "appsrc name=src format=time ! videoscale method=lanczos envelope=3 n-threads=2 "
        "! video/x-raw,format=RGB,width=%d,height=%d ! appsink name=sink sync=false" % (w, h)
    )
    src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
    src.set_property(
        "caps",
        Gst.Caps.from_string(
            "video/x-raw,format=RGB,width=%d,height=%d,framerate=1/1"
            % (pb.get_width(), pb.get_height())
        ),
    )
    pipe.set_state(Gst.State.PLAYING)
    src.emit("push-buffer", Gst.Buffer.new_wrapped_bytes(pb.read_pixel_bytes()))
    buf = sink.emit("pull-sample").get_buffer()
    data = buf.extract_dup(0, buf.get_size())
    pipe.set_state(Gst.State.NULL)
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3
    )


def load_source(path):
    if path:
        pb = GdkPixbuf.Pixbuf.new_from_file(path)
        return pb.apply_embedded_orientation() or pb
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scene

    arr = scene.render(6400, True)
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(arr.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, 6400, 3600, 6400 * 3
    )


class Viewer(Gtk.Widget):
    def __init__(self, pixbuf):
        super().__init__(hexpand=True, vexpand=True)
        self.pixbuf = pixbuf
        self.full = Gdk.Texture.new_for_pixbuf(pixbuf)
        self.mode = "linear"
        self.prepared = {}
        self.prep_ms = {}

    def fit(self):
        scale = self.get_native().get_surface().get_scale()
        pw, ph = self.get_width() * scale, self.get_height() * scale
        k = min(pw / self.pixbuf.get_width(), ph / self.pixbuf.get_height())
        dw, dh = round(self.pixbuf.get_width() * k), round(self.pixbuf.get_height() * k)
        return scale, dw, dh, round((pw - dw) / 2), round((ph - dh) / 2)

    def prepare(self, mode):
        scale, dw, dh, _, _ = self.fit()
        key = (mode, dw, dh)
        if key not in self.prepared:
            t0 = time.perf_counter()
            if mode == "pixbuf":
                pb = self.pixbuf.scale_simple(dw, dh, GdkPixbuf.InterpType.BILINEAR)
            else:
                pb = gst_lanczos(self.pixbuf, dw, dh)
            self.prepared[key] = Gdk.Texture.new_for_pixbuf(pb)
            self.prep_ms[mode] = (time.perf_counter() - t0) * 1000
        return self.prepared[key]

    def do_snapshot(self, snapshot):
        scale, dw, dh, ox, oy = self.fit()
        rect = Graphene.Rect().init(ox / scale, oy / scale, dw / scale, dh / scale)
        black = Gdk.RGBA()
        black.alpha = 1.0
        snapshot.append_color(
            black, Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
        )
        if self.mode == "texture":
            snapshot.append_texture(self.full, rect)
        elif self.mode in ("linear", "trilinear"):
            flt = Gsk.ScalingFilter.LINEAR if self.mode == "linear" else Gsk.ScalingFilter.TRILINEAR
            snapshot.append_scaled_texture(self.full, flt, rect)
        else:
            snapshot.append_texture(self.prepare(self.mode), rect)


class App(Gtk.Application):
    def __init__(self, path, once):
        super().__init__(application_id="io.github.sakkmesterke.SlideshowLock.ScalingCheck")
        self.path = path
        self.once = once
        self.index = -1
        self.stamps = []
        self.results = {}

    def do_activate(self):
        self.viewer = Viewer(load_source(self.path))
        self.win = Gtk.ApplicationWindow(application=self, child=self.viewer)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.win.add_controller(keys)
        self.win.fullscreen()
        self.win.present()
        self.viewer.add_tick_callback(self.on_tick)
        GLib.timeout_add_seconds(SECONDS_PER_MODE, self.next_mode)
        GLib.idle_add(lambda: self.next_mode() and False)

    def report_environment(self):
        surface = self.win.get_surface()
        renderer = self.win.get_renderer()
        print("renderer :", type(renderer).__name__, "| scale:", surface.get_scale())
        monitor = surface.get_display().get_monitor_at_surface(surface)
        if monitor is None:  # can happen on Wayland before the surface has entered an output
            print("monitor  : unknown (surface not on an output yet)")
        else:
            geometry = monitor.get_geometry()
            print("monitor  :", geometry.width, "x", geometry.height)
        print("source   :", self.viewer.pixbuf.get_width(), "x", self.viewer.pixbuf.get_height())
        for pkg in (
            "gtk4",
            "gdk-pixbuf2",
            "cairo",
            "pixman",
            "gstreamer1-plugins-base",
            "python3-gobject",
            "mesa-dri-drivers",
        ):
            out = subprocess.run(["rpm", "-q", pkg], capture_output=True, text=True).stdout.strip()
            print("package  :", out or pkg + " (rpm -q failed)")

    def next_mode(self):
        if self.index >= 0:
            self.finish_mode()
        self.index += 1
        if self.index == 0:
            self.report_environment()
        if self.index >= len(MODES):
            print("\nAutomatic pass done. Press Space to flick through the modes, Q to quit.")
            self.index = 0
            if self.once:
                self.quit()
            return False
        name, mode = MODES[self.index]
        self.stamps = []
        self.mode_started = time.perf_counter()
        self.viewer.mode = mode
        self.win.set_title(name)
        self.viewer.queue_draw()
        return True

    def finish_mode(self):
        name, mode = MODES[self.index]
        gaps = [b - a for a, b in zip(self.stamps, self.stamps[1:])]
        if not gaps:
            print("%-34s no frame-clock ticks received (window not mapped / not visible?)" % name)
        else:
            gaps_ms = sorted(g / 1000.0 for g in gaps)
            print(
                "%-34s frames=%3d  median=%5.1f ms  p95=%5.1f ms  max=%6.1f ms  prescale=%s"
                % (
                    name,
                    len(gaps) + 1,
                    statistics.median(gaps_ms),
                    gaps_ms[int(0.95 * (len(gaps_ms) - 1))],
                    gaps_ms[-1],
                    "%.0f ms" % self.viewer.prep_ms[mode] if mode in self.viewer.prep_ms else "-",
                )
            )

    def on_tick(self, widget, clock):
        self.stamps.append(clock.get_frame_time())
        widget.queue_draw()  # keep rendering so that frame times are measurable
        return GLib.SOURCE_CONTINUE

    def on_key(self, _ctl, keyval, _code, _state):
        if keyval in (Gdk.KEY_q, Gdk.KEY_Escape):
            self.quit()
        elif keyval == Gdk.KEY_space:
            self.index = (self.index + 1) % len(MODES)
            self.viewer.mode = MODES[self.index][1]
            self.win.set_title(MODES[self.index][0])
        return True


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(App(args[0] if args else None, "--once" in sys.argv).run([]))
