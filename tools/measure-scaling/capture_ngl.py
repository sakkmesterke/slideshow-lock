"""Render the GSK scaled-texture path with the window's own renderer and keep the pixels.

PyGObject 3.46 cannot marshal GskRenderNode, so the few C calls that touch nodes go through
ctypes. Run it as a client of a compositor (a real session, or mutter --headless).
"""

import ctypes
import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

import numpy as np  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402
import scene  # noqa: E402

OUT = os.environ.get("SCALING_OUT", "out")
gtk = ctypes.CDLL("libgtk-4.so.1")
gobject = ctypes.CDLL("libgobject-2.0.so.0")


class Rect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("w", ctypes.c_float),
        ("h", ctypes.c_float),
    ]


gtk.gtk_snapshot_new.restype = ctypes.c_void_p
gtk.gtk_snapshot_append_scaled_texture.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.POINTER(Rect),
]
gtk.gtk_snapshot_append_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Rect)]
gtk.gtk_snapshot_free_to_node.restype = ctypes.c_void_p
gtk.gtk_snapshot_free_to_node.argtypes = [ctypes.c_void_p]
gtk.gsk_renderer_render_texture.restype = ctypes.c_void_p
gtk.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Rect)]
gtk.gdk_texture_get_width.argtypes = [ctypes.c_void_p]
gtk.gdk_texture_get_height.argtypes = [ctypes.c_void_p]
gtk.gdk_texture_download.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
gtk.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
gobject.g_object_unref.argtypes = [ctypes.c_void_p]
gobject.g_type_name_from_instance.restype = ctypes.c_char_p
gobject.g_type_name_from_instance.argtypes = [ctypes.c_void_p]


def cptr(obj):
    """C pointer of a PyGObject wrapper (PyObject_HEAD, then GObject *obj)."""
    ptr = ctypes.c_void_p.from_address(id(obj) + 16).value
    assert gobject.g_type_name_from_instance(ptr) is not None
    return ptr


FILTERS = {"linear": 0, "nearest": 1, "trilinear": 2}


def render(renderer_ptr, texture_ptr, filt, w, h):
    snap = gtk.gtk_snapshot_new()
    rect = Rect(0, 0, w, h)
    if filt == "default":  # what a plain Gtk.Picture / GdkPaintable snapshot produces
        gtk.gtk_snapshot_append_texture(snap, texture_ptr, ctypes.byref(rect))
    else:
        gtk.gtk_snapshot_append_scaled_texture(snap, texture_ptr, FILTERS[filt], ctypes.byref(rect))
    node = gtk.gtk_snapshot_free_to_node(snap)
    tex = gtk.gsk_renderer_render_texture(renderer_ptr, node, ctypes.byref(rect))
    gtk.gsk_render_node_unref(node)
    tw, th = gtk.gdk_texture_get_width(tex), gtk.gdk_texture_get_height(tex)
    buf = (ctypes.c_ubyte * (tw * th * 4))()
    gtk.gdk_texture_download(tex, buf, tw * 4)
    gobject.g_object_unref(tex)
    bgra = np.frombuffer(buf, np.uint8).reshape(th, tw, 4)
    return bgra[..., 2::-1].copy()  # B8G8R8A8 premultiplied (opaque here) -> RGB


def run(app):
    win = Gtk.ApplicationWindow(application=app)
    win.set_default_size(640, 360)
    win.present()

    def go():
        renderer = win.get_renderer()
        name = type(renderer).__name__
        print("renderer", name, flush=True)
        rptr = cptr(renderer)
        timings = {}
        for case, src_w, fine in (("A", 6400, True), ("B", 800, False)):
            pb = bench.pixbuf_from_rgb(scene.render(src_w, fine))
            tex = Gdk.Texture.new_for_pixbuf(pb)
            tptr = cptr(tex)
            for filt in ("default", "linear", "nearest", "trilinear"):
                t0 = time.perf_counter()
                first = render(rptr, tptr, filt, 2560, 1440)
                cold = (time.perf_counter() - t0) * 1000
                warm = []
                for _ in range(5):
                    t0 = time.perf_counter()
                    render(rptr, tptr, filt, 2560, 1440)
                    warm.append((time.perf_counter() - t0) * 1000)
                timings.setdefault(case, {})[filt] = round(sorted(warm)[len(warm) // 2])
                np.save("%s/%s_gsk-%s_%s.npy" % (OUT, case, name, filt), first)
                print(
                    "%s %s %-9s first=%.0f ms  repeat median=%.0f ms"
                    % (case, name, filt, cold, sorted(warm)[len(warm) // 2]),
                    flush=True,
                )
        with open(os.path.join(OUT, "gl_timings.json"), "w") as fh:
            json.dump(timings, fh, indent=1)
        app.quit()
        return False

    GLib.timeout_add_seconds(2, go)


app = Gtk.Application(application_id="x.y.Capture")
app.connect("activate", run)
app.run([])
