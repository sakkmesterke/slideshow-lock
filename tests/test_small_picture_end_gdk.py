"""A picture that does not fill the window (``fit`` scaling), as GTK really renders the end of a
transition: the outgoing picture moves on under the incoming one, and the last frame of the
transition must be the plain frame that follows it (no display: GTK's Cairo renderer).

The outgoing picture is larger than the incoming one at that moment (it has been growing longer),
so it used to stick out round the incoming one all through the transition and vanish with the
last frame. ``test_transition_edges_gdk.py`` has the same helpers for the pictures that fill it.
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
    ALL_TRANSITIONS,
    NEW,
    OLD,
    transition_seconds,
)

pytestmark = pytest.mark.skipif(
    not hasattr(Gtk.Snapshot, "push_mask"),
    reason="soft edges need GTK 4.10 (Gtk.Snapshot.push_mask)",
)

W, H = 480, 270  # the window
PW, PH = 384, 216  # the pictures: 80 %, centred
INTERVALS = (3.0, 60.0)
HZ = 60
DURATION = 1.0


def _pointer(obj):
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
    return get(obj.__gpointer__, None)


def _texture(color, width=PW, height=PH):
    """A picture of *color* with a lighter diagonal, so that its scale shows in the pixels."""
    rows = []
    for y in range(height):
        rows.append(
            b"".join(
                bytes(min(255, c + 55 * ((x + y) % 64 < 8)) for c in color) for x in range(width)
            )
        )
    return Gdk.MemoryTexture.new(
        width, height, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(b"".join(rows)), width * 3
    )


def _canvas(width=PW, height=PH):
    canvas = preview_window._Canvas.__new__(preview_window._Canvas)
    canvas._scale = lambda: 1.0
    canvas._old_texture = _texture((190, 20, 20), width, height)
    canvas._texture = _texture((20, 190, 20), width, height)
    canvas._old_offset = canvas._offset = (0, 0)
    canvas._reduced = {}
    return canvas


@pytest.fixture(scope="module")
def renderer():
    renderer = Gsk.CairoRenderer()
    renderer.realize(None)
    yield renderer
    renderer.unrealize()


def _render(renderer, canvas, draws):
    snapshot = Gtk.Snapshot()
    black = Gdk.RGBA()
    black.alpha = 1.0
    snapshot.append_color(black, Graphene.Rect().init(0, 0, W, H))
    for draw in draws:
        canvas._paint(snapshot, draw, W, H)
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
    return bytes(pixels)


def _difference(a: bytes, b: bytes) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


def _frames(renderer, name: str, interval: float):
    """The frames at the end of the transition that takes the old picture out, with the poses the
    window gives them at *HZ* frames a second: the last two frames of the transition (progress
    just below 1 and exactly 1) and the plain frame that follows."""
    seconds = transition_seconds(name, interval, DURATION)
    span = td.picture_seconds(interval)
    canvas = _canvas()
    end = interval + seconds

    def transition(t):  # the frame at *t* seconds into the old picture's life
        draws = td.compose(name, min(1.0, (t - interval) / seconds), W, H)
        old = td.base_pose(t, span, W, False)
        new = td.base_pose(t - interval, span, W, False)
        return _render(renderer, canvas, td.with_poses(draws, old, new))

    def plain(t):
        pose = td.base_pose(t - interval, span, W, False)
        return _render(renderer, canvas, [td.Draw(NEW, pose=pose)])

    step = 1.0 / HZ
    return transition(end - step), transition(end), plain(end)


@pytest.mark.parametrize("interval", INTERVALS)
@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_last_frame_of_a_transition_is_the_plain_frame_for_a_small_picture(
    renderer, name, interval
):
    previous, last, plain = _frames(renderer, name, interval)
    # the same picture, to the rounding, and no bigger a step than the one into the last frame
    assert _difference(last, plain) <= max(2, _difference(previous, last)), (name, interval)
    assert _difference(last, plain) <= 2, (name, interval)


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_nothing_of_the_old_picture_sticks_out_round_the_new_one_at_the_end(renderer, name):
    """The old picture is red, the new one green: near the end of the transition (new picture all
    but whole) no red is left outside the new picture's own area, which is what the window shows
    from the next frame on."""
    interval = 3.0
    seconds = transition_seconds(name, interval, DURATION)
    span = td.picture_seconds(interval)
    canvas = _canvas()
    end = interval + seconds
    draws = td.compose(name, 1.0, W, H)
    pixels = _render(
        renderer,
        canvas,
        td.with_poses(
            draws, td.base_pose(end, span, W, False), td.base_pose(seconds, span, W, False)
        ),
    )
    plain = _render(
        renderer, canvas, [td.Draw(NEW, pose=td.base_pose(end - interval, span, W, False))]
    )
    red = [
        (x, y)
        for y in range(H)
        for x in range(W)
        if pixels[(y * W + x) * 4 + 2] > plain[(y * W + x) * 4 + 2] + 2  # BGRA: the red channel
    ]
    assert red == [], (name, len(red), red[:5])


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_first_frame_is_the_moving_old_picture_as_it_was_drawn_before(renderer, name):
    """Nothing is cut yet at the first frame: the pixels are those of the old picture drawn
    plainly with its slow move."""
    interval = 3.0
    span = td.picture_seconds(interval)
    canvas = _canvas()
    old = td.base_pose(interval, span, W, False)
    first = _render(
        renderer,
        canvas,
        td.with_poses(td.compose(name, 0.0, W, H), old, td.base_pose(0.0, span, W, False)),
    )
    plain_old = _render(renderer, canvas, [td.Draw(OLD, pose=old)])
    assert _difference(first, plain_old) <= 2, name


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_a_picture_that_fills_the_window_is_drawn_as_it_was_before_the_cut(renderer, name):
    """The cut is for the pictures that do not fill the window: with two that do, the pixels of
    every frame are the pixels without it, to the last bit."""
    interval = 3.0
    span = td.picture_seconds(interval)
    canvas = _canvas(W, H)
    for p in (0.0, 0.2, 0.5, 0.8, 0.97, 1.0):
        draws = td.with_poses(
            td.compose(name, p, W, H),
            td.base_pose(interval + p, span, W, True),
            td.base_pose(p, span, W, True),
        )
        uncut = [d._replace(settle=None) for d in draws]
        assert _render(renderer, canvas, draws) == _render(renderer, canvas, uncut), (name, p)
