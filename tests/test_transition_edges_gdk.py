"""The soft edges as GTK really renders them, pixel by pixel (no display: GTK's Cairo renderer).

``test_transition_soft_edges.py`` reads the ``Draw`` values; here the canvas turns them into
``Gtk.Snapshot`` calls, the Cairo renderer draws those on two plain pictures (the old one red, the
new one green) and the green of a pixel is the opacity of the new picture there. Two things are
checked: the profile across an edge is a ramp, and it is the ramp the ``Draw`` values describe.

The masks need GTK 4.10 (``Gtk.Snapshot.push_mask``). Below that the canvas keeps the cut edges
and these tests are skipped.
"""

from __future__ import annotations

import ctypes

import pytest

pytest.importorskip("gi", reason="PyGObject (gi) is not installed in this environment")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from slideshow_lock import preview_window  # noqa: E402
from slideshow_lock import transition_draw as td  # noqa: E402
from slideshow_lock.transitions import (  # noqa: E402
    CIRCLE,
    PUSH,
    ROTATE,
    SLIDE_IN,
    WIPE,
    ZOOM,
)
from tests import soft_edge_model as model  # noqa: E402

pytestmark = pytest.mark.skipif(
    not hasattr(Gtk.Snapshot, "push_mask"),
    reason="soft edges need GTK 4.10 (Gtk.Snapshot.push_mask)",
)

W, H = 320, 180
OLD_RGB, NEW_RGB = (200, 0, 0), (0, 200, 0)
SOFT = (SLIDE_IN, PUSH, WIPE, CIRCLE, ZOOM, ROTATE)
MIDDLE = (0.15, 0.3, 0.45, 0.6)

#: See ``test_transition_soft_edges.STEP_LIMIT``; the band is as wide as a share of a small window.
STEP_LIMIT = 0.25

#: How far a rendered pixel may be from the reading of the ``Draw`` values, in opacity (the
#: renderer's own rounding to 8 bits is 0.005).
TOLERANCE = 0.03


def _pointer(obj):
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
    return get(obj.__gpointer__, None)


def _texture(rgb):
    return Gdk.MemoryTexture.new(
        W, H, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(bytes(rgb) * (W * H)), W * 3
    )


def _canvas():
    """A canvas that only paints: the two pictures, as the window holds them in a transition."""
    canvas = preview_window._Canvas.__new__(preview_window._Canvas)
    canvas._scale = lambda: 1.0
    canvas._old_texture, canvas._texture = _texture(OLD_RGB), _texture(NEW_RGB)
    canvas._old_offset = canvas._offset = (0, 0)
    canvas._reduced = {}
    return canvas


@pytest.fixture(scope="module")
def renderer():
    renderer = Gsk.CairoRenderer()
    renderer.realize(None)
    yield renderer
    renderer.unrealize()


def render(renderer, draws):
    """The opacity of the new picture at every pixel of the window, as a list of rows."""
    canvas = _canvas()
    snapshot = Gtk.Snapshot()
    black = Gdk.RGBA()
    black.alpha = 1.0
    snapshot.append_color(black, Graphene.Rect().init(0, 0, W, H))
    for draw in draws:
        canvas._paint(snapshot, draw, W, H)
    # PyGObject has no Python type for the render nodes: the C functions, through ctypes
    gtk = ctypes.CDLL("libgtk-4.so.1")
    gtk.gtk_snapshot_to_node.restype = ctypes.c_void_p
    gtk.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
    node = gtk.gtk_snapshot_to_node(_pointer(snapshot))
    rect = (ctypes.c_float * 4)(0, 0, W, H)
    gtk.gsk_renderer_render_texture.restype = ctypes.c_void_p
    gtk.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p] * 3
    texture = gtk.gsk_renderer_render_texture(_pointer(renderer), node, ctypes.addressof(rect))
    pixels = (ctypes.c_ubyte * (W * H * 4))()
    gtk.gdk_texture_download.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    gtk.gdk_texture_download(texture, pixels, W * 4)
    gobject = ctypes.CDLL("libgobject-2.0.so.0")
    gobject.g_object_unref.argtypes = [ctypes.c_void_p]
    gobject.g_object_unref(texture)
    gtk.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
    gtk.gsk_render_node_unref(node)
    # the memory order is B G R A (premultiplied): the new picture's colour is the green channel
    return [[pixels[(y * W + x) * 4 + 1] / NEW_RGB[1] for x in range(W)] for y in range(H)]


def _lines(rows):
    for fraction in (0.05, 0.25, 0.5, 0.75, 0.95):
        yield rows[int(H * fraction)]
        x = int(W * fraction)
        yield [row[x] for row in rows]


@pytest.mark.parametrize("name", SOFT)
def test_the_rendered_edge_is_a_ramp(renderer, name):
    for p in MIDDLE:
        draws = td.compose(name, p, W, H)
        opacity = max(d.opacity for d in draws if d.layer == "new")
        rows = render(renderer, draws)
        steepest = spread = 0.0
        for line in _lines(rows):
            steepest = max([steepest] + [abs(b - a) for a, b in zip(line, line[1:])])
            spread = max(spread, max(line) - min(line))
        assert spread > 0.05, (name, p)
        assert steepest / opacity <= STEP_LIMIT, (name, p, steepest)


@pytest.mark.parametrize("name", SOFT)
def test_the_rendered_picture_is_what_the_draw_values_describe(renderer, name):
    for p in MIDDLE:
        draws = td.compose(name, p, W, H)
        rows = render(renderer, draws)
        for y in range(1, H, 7):
            for x in range(1, W, 5):
                wanted = model.new_alpha(draws, x + 0.5, y + 0.5, W, H)
                assert rows[y][x] == pytest.approx(wanted, abs=TOLERANCE), (name, p, x, y)


@pytest.mark.parametrize("name", SOFT)
def test_the_first_and_the_last_frame_are_the_plain_pictures(renderer, name):
    first = render(renderer, td.compose(name, 0.0, W, H))
    assert max(max(row) for row in first) <= 0.01, name
    last = render(renderer, td.compose(name, 1.0, W, H))
    assert min(min(row) for row in last) >= 0.99, name


def test_a_rendered_ken_burns_picture_covers_the_window_the_whole_run(renderer):
    """The enlarged, shifted picture of Ken Burns, fully faded in (the old picture is red and
    would show at an edge it does not cover): all green, to the last pixel of every border."""
    for p in (1e-4, 0.25, 0.5, 0.75, 1.0):  # not 0: the picture is not faded in at the very start
        rows = render(renderer, td.compose("ken-burns", p, W, H, fade_share=1e-6))
        assert min(min(row) for row in rows) >= 0.99, p
