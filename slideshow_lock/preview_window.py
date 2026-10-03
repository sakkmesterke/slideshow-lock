"""GTK4 windows for the slideshow preview (CORE-2): one fullscreen window per monitor.

The only module of the preview that imports GTK. The controller
(``slideshow_lock.preview``) talks to the small interface of ``PreviewWindow`` and does
not know GTK, so everything it decides is tested without a display; what this module
does on a real screen is checked with ``tools/wayland-smoke`` and, for how it looks,
by eye on the reference machine.

How a picture is drawn: the frame is already scaled to the window's exact pixel size, so
it goes to the screen 1:1 as a memory texture, centred on black. A texture node at its
own pixel size, on a device pixel boundary, is not filtered again. A frame taller than
the window (a panning portrait picture) is scrolled by whole pixels from a frame-clock
tick, which only runs while there is something to scroll.

Input: the window's own GTK controllers (motion, click, key, scroll) call
``on_input``, and so does a ``close-request`` (the window was closed from outside, from the
overview for example: without that the process would run on with no window, changing
pictures nobody sees). Any one of them is enough, on any monitor's window (state machine
document, section 6.1). A pointer that merely rests under a window that appears is not
input: the first position the window sees is the baseline, and pointer motion counts
only once the pointer is ``MOTION_THRESHOLD_PIXELS`` away from it. (Not a timer after
the window appears: on a slow first frame the compositor's first pointer event can
arrive late, and a timer would end the preview at once.)
"""

from __future__ import annotations

import math
from typing import Any, Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

from slideshow_lock import _  # noqa: E402
from slideshow_lock.preview import (  # noqa: E402
    INPUT_BUTTON,
    INPUT_CLOSE,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
)
from slideshow_lock.scaling import Frame, device_size  # noqa: E402

#: The pointer must move this far from where the window first saw it to count as input.
MOTION_THRESHOLD_PIXELS = 2.0


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def animations_enabled() -> bool:
    """The desktop's "reduce animations" choice, as GTK reports it (``gtk-enable-animations``).

    Read for every picture, so a change takes effect with the next one. On GTK 4.8 the property
    exists and can be switched (measured); that the GNOME setting reaches it was not measured
    here (no GNOME session), so on the reference machine this is part of the manual test.
    """
    settings = Gtk.Settings.get_default()
    return settings is None or bool(settings.get_property("gtk-enable-animations"))


class _Canvas(Gtk.Widget):
    """Draws black, and the current frame 1:1 on top of it."""

    __gtype_name__ = "SlideshowLockPreviewCanvas"

    def __init__(self, scale: Callable[[], float]) -> None:
        super().__init__(hexpand=True, vexpand=True)
        self._scale = scale
        self._texture: Optional[Gdk.Texture] = None
        self._frame: Optional[Frame] = None
        self._offset = (0, 0)
        self._pan_seconds = 0.0
        self._pan_t0: Optional[int] = None
        self._tick_id = 0

    def set_frame(self, frame: Optional[Frame], pan_seconds: float) -> None:
        self._stop_pan()
        if frame is None:
            self._frame = None
            self._texture = None
        elif frame is not self._frame:  # the very same frame again (one picture): keep the texture
            pixels = frame.pixels
            if not isinstance(pixels, GLib.Bytes):
                pixels = GLib.Bytes.new(pixels)
            self._texture = Gdk.MemoryTexture.new(
                frame.width, frame.height, Gdk.MemoryFormat.R8G8B8, pixels, frame.stride
            )
            self._frame = frame
        self._offset = (0, 0)
        if frame is not None and frame.pan_range != (0, 0):
            if pan_seconds > 0 and animations_enabled():
                self._pan_seconds = pan_seconds
                self._pan_t0 = None
                self._tick_id = self.add_tick_callback(self._on_tick)
            else:  # animations are off: the middle of the picture, like the centre crop
                self._offset = (frame.pan_range[0] // 2, frame.pan_range[1] // 2)
        self.queue_draw()

    def _stop_pan(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if self._pan_t0 is None:
            self._pan_t0 = now
        progress = min(1.0, (now - self._pan_t0) / 1_000_000 / self._pan_seconds)
        range_x, range_y = self._frame.pan_range
        eased = _smoothstep(progress)
        offset = (round(range_x * eased), round(range_y * eased))
        if offset != self._offset:
            self._offset = offset
            self.queue_draw()
        if progress >= 1.0:
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def do_snapshot(self, snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        black = Gdk.RGBA()
        black.alpha = 1.0
        snapshot.append_color(black, Graphene.Rect().init(0, 0, width, height))
        if self._texture is None:
            return
        scale = self._scale()
        dev_w, dev_h = round(width * scale), round(height * scale)
        tex_w, tex_h = self._texture.get_width(), self._texture.get_height()
        x = (dev_w - tex_w) // 2 if tex_w <= dev_w else -self._offset[0]
        y = (dev_h - tex_h) // 2 if tex_h <= dev_h else -self._offset[1]
        snapshot.append_texture(
            self._texture, Graphene.Rect().init(x / scale, y / scale, tex_w / scale, tex_h / scale)
        )


class PreviewWindow:
    """One fullscreen window on one monitor, with the interface the controller expects."""

    def __init__(self, monitor: Gdk.Monitor) -> None:
        self._input_callbacks: List[Callable[[str], None]] = []
        self._size_callbacks: List[Callable[[], None]] = []
        self._last_geometry: Optional[Tuple[int, int, float]] = None
        self._origin: Optional[Tuple[float, float]] = None  # where the window first saw the pointer
        self._entered = False
        self._closed = False

        self._window = Gtk.Window(title=_("Slideshow Lock preview"), decorated=False)
        self._canvas = _Canvas(self._scale)
        self._message = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False)
        overlay = Gtk.Overlay()
        overlay.set_child(self._canvas)
        overlay.add_overlay(self._message)
        self._window.set_child(overlay)
        self._window.set_cursor_from_name("none")

        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect("enter", self._on_enter)
        motion.connect("motion", self._on_motion)
        click = Gtk.GestureClick()
        click.set_button(0)  # any button
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_args: self._input(INPUT_BUTTON))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        for controller in (motion, click, keys, scroll):
            self._window.add_controller(controller)

        self._window.connect("realize", self._on_realize)
        self._window.connect("close-request", self._on_close_request)
        self._window.fullscreen_on_monitor(monitor)
        self._window.present()

    # -- interface used by the controller ------------------------------------------------

    def device_size(self) -> Optional[Tuple[int, int]]:
        """Size of the window in device pixels, or None until the compositor has sized it."""
        surface = self._window.get_surface()
        if surface is None or surface.get_width() <= 0 or surface.get_height() <= 0:
            return None
        return device_size(surface.get_width(), surface.get_height(), self._scale())

    def show_frame(self, frame: Frame, pan_seconds: float) -> None:
        self._message.set_visible(False)
        self._canvas.set_frame(frame, pan_seconds)

    def show_message(self, text: str) -> None:
        """Black screen with a short message; an empty string shows plain black."""
        self._canvas.set_frame(None, 0.0)
        self._message.set_markup(
            '<span foreground="white" size="x-large">%s</span>' % GLib.markup_escape_text(text)
        )
        self._message.set_visible(bool(text))

    def connect_input(self, callback: Callable[[str], None]) -> None:
        self._input_callbacks.append(callback)

    def connect_size_changed(self, callback: Callable[[], None]) -> None:
        self._size_callbacks.append(callback)

    def close(self) -> None:
        """Close the window. Deferred: this is called from inside the window's own event handler."""
        if not self._closed:
            self._closed = True
            self._canvas.set_frame(None, 0.0)
            GLib.idle_add(self._destroy)

    # -- internals ------------------------------------------------------------------------------

    def _destroy(self) -> bool:
        self._window.destroy()
        return GLib.SOURCE_REMOVE

    def _scale(self) -> float:
        surface = self._window.get_surface()
        if surface is None:
            return 1.0
        getter = getattr(surface, "get_scale", None)  # fractional, GTK 4.12 and later
        return float(getter() if getter is not None else surface.get_scale_factor())

    def _on_realize(self, _window) -> None:
        surface = self._window.get_surface()
        surface.connect("layout", lambda *_args: self._geometry_changed())
        for prop in ("notify::scale-factor", "notify::scale"):
            try:
                surface.connect(prop, lambda *_args: self._geometry_changed())
            except TypeError:  # property does not exist in this GTK version
                pass

    def _geometry_changed(self) -> None:
        size = self.device_size()
        if size is None:
            return
        geometry = (size[0], size[1], self._scale())
        if geometry != self._last_geometry:
            self._last_geometry = geometry
            for callback in list(self._size_callbacks):
                callback()

    def _input(self, kind: str) -> None:
        for callback in list(self._input_callbacks):
            callback(kind)

    def _on_enter(self, _controller, x: float, y: float) -> None:
        # The first enter is a pointer that was resting under the window when it appeared.
        # Entering again later is the pointer arriving from elsewhere: movement.
        if self._entered:
            self._input(INPUT_MOTION)
        self._entered = True
        if self._origin is None:
            self._origin = (x, y)

    def _on_motion(self, _controller, x: float, y: float) -> None:
        if self._origin is None:  # no enter seen: this position is all we know
            self._origin = (x, y)
            return
        if math.hypot(x - self._origin[0], y - self._origin[1]) >= MOTION_THRESHOLD_PIXELS:
            self._input(INPUT_MOTION)

    def _on_close_request(self, _window) -> bool:
        """Closed from outside: that ends the preview, and the controller closes every window.
        True keeps GTK from destroying this one itself; ``close()`` does it, once."""
        if not self._input_callbacks:
            return False
        self._input(INPUT_CLOSE)
        return True

    def _on_key(self, *_args) -> bool:
        self._input(INPUT_KEY)
        return True

    def _on_scroll(self, *_args) -> bool:
        self._input(INPUT_SCROLL)
        return True


def open_monitor_windows() -> List[Any]:
    """One ``PreviewWindow`` per monitor of the default display. GTK must be initialised."""
    display = Gdk.Display.get_default()
    monitors = display.get_monitors()
    return [PreviewWindow(monitors.get_item(i)) for i in range(monitors.get_n_items())]
