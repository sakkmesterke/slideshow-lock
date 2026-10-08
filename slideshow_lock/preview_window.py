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

A picture that comes in with the Ken Burns transition moves slowly (``_Move``,
``transition_draw.base_pose``: a zoom and a drift): from the frame it appears on until the
transition that takes it away has finished it, also under a transition, as the incoming and as the
outgoing picture, so that it does not stop before the next one comes. So does the first picture of
a window, which has no transition to come in with, when the transition chosen for it is Ken Burns
(``first_transition`` of ``set_frame``). Such a picture is redrawn at every frame of the frame
clock, as a transition is, until its move has run its span (then it stands in its last pose and
nothing is drawn more). Every other picture is drawn once, 1:1, and not again, as in 1.0.1.

A transition (``slideshow_lock.transitions``, ``slideshow_lock.transition_draw``) keeps the old
picture's texture next to the new one for its short time. What each of the ten looks like at a
given moment is decided without GTK (``transition_draw.compose``: which picture, how opaque, how
large, turned, moved, clipped, blurred); this module turns each of those into ``Gtk.Snapshot``
calls from a tick of its own, always on black. Its end is a moment in time, not a number of frames,
so a slow machine draws fewer frames and not a longer transition. The first frame is drawn at
progress 0 with both pictures, which is where the new texture is uploaded, out of sight; the clock
of the transition starts at the second tick. When it ends the tick is removed and the picture is
drawn as always (a scrolling picture 1:1, the others with their move). The blur works on pictures
reduced to a quarter of their size (it is the costly one), and on software rendering it is drawn
as a cross fade (``transition_draw.effective_name``).

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

import logging
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gtk  # noqa: E402

from slideshow_lock import _  # noqa: E402
from slideshow_lock.preview import (  # noqa: E402
    INPUT_BUTTON,
    INPUT_CLOSE,
    INPUT_KEY,
    INPUT_MOTION,
    INPUT_SCROLL,
)
from slideshow_lock.scaling import Frame, device_size  # noqa: E402
from slideshow_lock.transition_draw import (  # noqa: E402
    STILL,
    Draw,
    Pose,
    TransitionRun,
    base_pose,
    compose,
    effective_name,
    picture_seconds,
    software_renderer,
)
from slideshow_lock.transitions import BLUR, CROSSFADE, KEN_BURNS, NEW, OLD  # noqa: E402

_LOG = logging.getLogger(__name__)

#: A picture that scrolls (taller or wider than the window) moves over this share of the time it is
#: shown; the rest it rests. (A Ken Burns picture moves all the time it is on screen: ``_Move``.)
PAN_FRACTION = 0.9

#: The blur works on pictures this many times smaller in each direction.
BLUR_REDUCTION = 4

#: The pointer must move this far from where the window first saw it to count as input.
MOTION_THRESHOLD_PIXELS = 2.0


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


class _Move:
    """The slow move of one Ken Burns picture (``transition_draw.base_pose``): its clock starts at
    the first frame it is on screen for and runs for ``span`` seconds, whatever is drawn with it
    (a transition as the incoming picture, the plain picture, a transition as the outgoing one)."""

    def __init__(self, span: float) -> None:
        self.span = span
        self.born: Optional[int] = None  # the frame clock's time, microseconds

    def over(self, now: Optional[int]) -> bool:
        """True once the move has run its ``span``: the pose stays what it is from then on."""
        if self.born is None or now is None:
            return False
        return (now - self.born) / 1_000_000 >= self.span

    def pose(self, now: Optional[int], width: float) -> Pose:
        age = 0.0 if now is None or self.born is None else max(0, now - self.born) / 1_000_000
        return base_pose(age, self.span, width)


def animations_enabled() -> bool:
    """The desktop's "reduce animations" choice, as GTK reports it (``gtk-enable-animations``).

    Read for every picture, so a change takes effect with the next one. On GTK 4.8 the property
    exists and can be switched (measured); that the GNOME setting reaches it was not measured
    here (no GNOME session), so on the reference machine this is part of the manual test.
    """
    settings = Gtk.Settings.get_default()
    return settings is None or bool(settings.get_property("gtk-enable-animations"))


_software_gl_cache: Optional[bool] = None


def software_gl(widget) -> bool:
    """True if the window is known to be drawn by the CPU (see ``transition_draw.software_renderer``
    for what is known). Asked once for the life of the process, at the first transition that
    cares; a failure to find out counts as hardware, which only means the blur is tried."""
    global _software_gl_cache
    if _software_gl_cache is None:
        renderer_class = ""
        try:
            native = widget.get_native()
            renderer = native.get_renderer() if native is not None else None
            renderer_class = renderer.__gtype__.name if renderer is not None else ""
        except Exception as error:  # noqa: BLE001 - a failed probe must never stop the slideshow
            _LOG.debug("[slideshow] could not ask for the renderer: %s", error)
        _software_gl_cache = software_renderer(renderer_class, os.environ)
        _LOG.info(
            "[slideshow] renderer %s: %s",
            renderer_class or "unknown",
            "software, so no blur transition" if _software_gl_cache else "not known to be software",
        )
    return _software_gl_cache


def _reduced_texture(frame: Frame, factor: int) -> Gdk.Texture:
    """*frame* as a texture 1/factor of its size in each direction (the blur's picture)."""
    pixels = frame.pixels
    if not isinstance(pixels, GLib.Bytes):
        pixels = GLib.Bytes.new(pixels)
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        pixels, GdkPixbuf.Colorspace.RGB, False, 8, frame.width, frame.height, frame.stride
    )
    small = pixbuf.scale_simple(
        max(1, frame.width // factor), max(1, frame.height // factor), GdkPixbuf.InterpType.BILINEAR
    )
    return Gdk.MemoryTexture.new(
        small.get_width(),
        small.get_height(),
        Gdk.MemoryFormat.R8G8B8,
        small.read_pixel_bytes(),
        small.get_rowstride(),
    )


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
        self._old_texture: Optional[Gdk.Texture] = None  # the picture going out, in a transition
        self._old_frame: Optional[Frame] = None
        self._old_offset = (0, 0)
        self._reduced: Dict[str, Gdk.Texture] = {}  # the blur's pictures, by layer
        self._run: Optional[TransitionRun] = None
        self._run_tick_id = 0
        self._move: Optional[_Move] = None  # the slow move of the picture on screen
        self._old_move: Optional[_Move] = None  # and of the one going out, in a transition
        self._move_tick_id = 0
        self._now: Optional[int] = None  # the frame clock's time at the last tick

    def set_frame(
        self,
        frame: Optional[Frame],
        seconds: float,
        transition: Optional[Tuple[str, float]] = None,
        first_transition: Optional[Tuple[str, float]] = None,
    ) -> None:
        """Show *frame*, which is on screen for *seconds*. With *transition* ``(name, seconds)`` and
        a picture already on screen, the old one goes out through the transition; any call, with
        or without one, first ends a transition that is still running. A window with nothing on
        screen has nothing to come in over: *first_transition* is the one chosen for such a picture,
        no transition is drawn for it, it only decides whether the picture has the slow move (Ken
        Burns) by the rule that holds for every other picture."""
        pan_seconds = seconds * PAN_FRACTION
        self._stop_pan()
        previous_move = self._move
        same_frame = frame is not None and frame is self._frame
        empty_before = self._texture is None
        self._end_transition()
        outgoing = None
        if transition is not None and frame is not None and frame is not self._frame:
            outgoing = (self._texture, self._frame)
        old_offset = self._offset
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
        chosen = None
        if outgoing is not None and outgoing[0] is not None:
            chosen = transition
        elif empty_before and frame is not None:
            chosen = first_transition
        ken_burns = (
            chosen is not None and chosen[1] > 0 and self._drawn_name(chosen[0]) == KEN_BURNS
        )
        self._start_move(frame, seconds, previous_move if same_frame else None, ken_burns)
        if frame is not None and frame.pan_range != (0, 0):
            if pan_seconds > 0 and animations_enabled():
                self._pan_seconds = pan_seconds
                self._pan_t0 = None
                self._tick_id = self.add_tick_callback(self._on_tick)
            else:  # animations are off: the middle of the picture, like the centre crop
                self._offset = (frame.pan_range[0] // 2, frame.pan_range[1] // 2)
        if outgoing is not None and outgoing[0] is not None and self._texture is not None:
            self._begin_transition(outgoing, old_offset, previous_move, *transition)
        self.queue_draw()

    def _stop_pan(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _drawn_name(self, name: str) -> str:
        """The transition that is really drawn for *name* with the picture now set (see
        ``transition_draw.effective_name``)."""
        scale = self._scale()
        size = (round(self.get_width() * scale), round(self.get_height() * scale))
        frame = self._frame
        return effective_name(
            name,
            (frame.width, frame.height),
            size,
            frame.pan_range,
            software_gl(self) if name == BLUR else False,
        )

    def _start_move(
        self, frame: Optional[Frame], seconds: float, old_move: Optional[_Move], ken_burns: bool
    ) -> None:
        """Give *frame* its slow move: a picture that comes in with the Ken Burns transition, or is
        the first on its window with Ken Burns as the chosen one (*ken_burns*), has one, while the
        desktop allows animations. The very same frame again (a folder of one) keeps the one it
        has, so nothing jumps back (*old_move* is that one, None for any other frame)."""
        if old_move is not None:  # the very same frame
            self._move = old_move
            return
        self._stop_move()
        if ken_burns and frame is not None and seconds > 0 and animations_enabled():
            self._move = _Move(picture_seconds(seconds))
            self._move_tick_id = self.add_tick_callback(self._on_move_tick)

    def _stop_move(self) -> None:
        if self._move_tick_id:
            self.remove_tick_callback(self._move_tick_id)
            self._move_tick_id = 0
        self._move = None

    def _on_move_tick(self, _widget, clock) -> bool:
        """Keeps the clock of the slow move and redraws it at every frame (the move is as smooth
        as the screen allows, as in a transition). A running transition draws the frame itself
        (its tick is on the same clock); a move that has run its span is drawn the last time and
        its tick ends: a picture that stands on (a folder of one) draws nothing more."""
        self._sync(clock)
        if self._run is None:
            self.queue_draw()
        if self._move.over(self._now):
            self._move_tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _sync(self, clock) -> None:
        self._now = clock.get_frame_time()
        for move in (self._move, self._old_move):
            if move is not None and move.born is None:
                move.born = self._now

    def _pose(self, move: Optional[_Move], width: float) -> Pose:
        if move is None:
            return STILL
        return move.pose(self._now, width)

    def _begin_transition(self, outgoing, offset, old_move, name: str, seconds: float) -> None:
        """Start *name* from the old picture *outgoing* ``(texture, frame)``, which moves on with
        *old_move* if it has one, to the one just set. *seconds* is how long it takes."""
        frame = self._frame
        name = self._drawn_name(name)
        reduced: Dict[str, Gdk.Texture] = {}
        if name == BLUR:
            try:
                reduced = {
                    OLD: _reduced_texture(outgoing[1], BLUR_REDUCTION),
                    NEW: _reduced_texture(frame, BLUR_REDUCTION),
                }
            except Exception as error:  # noqa: BLE001 - no reduced pictures: a plain cross fade
                _LOG.warning("[slideshow] blur transition not possible (%s), cross fade", error)
                name = CROSSFADE
        if not compose(name, 0.0, 1.0, 1.0) or seconds <= 0:  # nothing to draw: a cut
            return
        self._old_texture, self._old_frame = outgoing
        self._old_offset = (
            offset  # the old picture stands where it stopped, the new one starts at 0
        )
        self._old_move = old_move
        self._reduced = reduced
        self._run = TransitionRun(name, seconds)
        self._run_tick_id = self.add_tick_callback(self._on_transition_tick)

    def _end_transition(self) -> None:
        """Forget the transition and the old texture: the next draw is the plain one."""
        if self._run_tick_id:
            self.remove_tick_callback(self._run_tick_id)
            self._run_tick_id = 0
        self._run = None
        self._old_texture = self._old_frame = None
        self._old_move = None
        self._reduced = {}

    def _on_transition_tick(self, _widget, clock) -> bool:
        self._sync(clock)
        if self._run.tick(clock.get_frame_time()):
            self.queue_draw()
            return GLib.SOURCE_CONTINUE
        self._run_tick_id = 0
        self._end_transition()
        self.queue_draw()
        return GLib.SOURCE_REMOVE

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
        new_pose = self._pose(self._move, width)
        if self._run is not None and self._old_texture is not None:
            old_pose = self._pose(self._old_move, width)
            for draw in self._run.draws(width, height, old_pose, new_pose):
                self._paint(snapshot, draw, width, height)
        elif self._texture is not None and new_pose != STILL:
            self._paint(snapshot, Draw(NEW, pose=new_pose), width, height)
        elif self._texture is not None:
            self._append(snapshot, self._texture, self._offset, width, height)

    def _rect(self, texture, offset, width, height) -> Graphene.Rect:
        """Where *texture* goes: 1:1 and centred, or at *offset* if it is larger than the window."""
        scale = self._scale()
        dev_w, dev_h = round(width * scale), round(height * scale)
        tex_w, tex_h = texture.get_width(), texture.get_height()
        x = (dev_w - tex_w) // 2 if tex_w <= dev_w else -offset[0]
        y = (dev_h - tex_h) // 2 if tex_h <= dev_h else -offset[1]
        return Graphene.Rect().init(x / scale, y / scale, tex_w / scale, tex_h / scale)

    def _append(self, snapshot, texture, offset, width, height) -> None:
        snapshot.append_texture(texture, self._rect(texture, offset, width, height))

    def _paint(self, snapshot, draw: Draw, width, height) -> None:
        """One ``Draw`` of the running transition: cut, faded, blurred, then moved/turned/scaled
        around the window's centre (the cut is in the window's own coordinates)."""
        if draw.opacity <= 0.0:  # nothing of it shows (and GTK drops such a node anyway)
            return
        if draw.layer == OLD:
            texture, offset = self._old_texture, self._old_offset
        else:
            texture, offset = self._texture, self._offset
        rect = self._rect(texture, offset, width, height)
        if draw.blur > 0 and draw.layer in self._reduced:
            texture = self._reduced[draw.layer]  # same place, a quarter of the pixels
        pose = draw.pose
        closers: List[Callable[[], None]] = []
        if draw.clip is not None:
            snapshot.push_clip(Graphene.Rect().init(*draw.clip))
            closers.append(snapshot.pop)
        if draw.circle is not None:
            cx, cy, radius = draw.circle
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(
                Graphene.Rect().init(cx - radius, cy - radius, 2 * radius, 2 * radius), radius
            )
            snapshot.push_rounded_clip(rounded)
            closers.append(snapshot.pop)
        if draw.opacity < 1.0:
            snapshot.push_opacity(draw.opacity)
            closers.append(snapshot.pop)
        if draw.blur > 0:
            snapshot.push_blur(draw.blur)
            closers.append(snapshot.pop)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(width / 2 + draw.dx, height / 2 + draw.dy))
        if draw.angle:
            snapshot.rotate(draw.angle)
        if draw.scale != 1.0:
            snapshot.scale(draw.scale, draw.scale)
        snapshot.translate(Graphene.Point().init(-width / 2, -height / 2))
        if pose != STILL:  # the slow move stays inside the picture's place
            snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
            snapshot.save()
            snapshot.translate(Graphene.Point().init(width / 2 + pose.dx, height / 2))
            snapshot.scale(pose.scale, pose.scale)
            snapshot.translate(Graphene.Point().init(-width / 2, -height / 2))
        snapshot.append_texture(texture, rect)
        if pose != STILL:
            snapshot.restore()
            snapshot.pop()
        snapshot.restore()
        for close in reversed(closers):
            close()


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
        self._message = Gtk.Label(
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            justify=Gtk.Justification.CENTER,
            visible=False,
        )
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

    @property
    def gtk_window(self) -> Gtk.Window:
        """The GTK window, for the one thing outside the controller that needs it: the request
        that keeps the desktop's idle delay from blanking the screen (``preview_app.IdleHold``)."""
        return self._window

    # -- interface used by the controller ------------------------------------------------

    def device_size(self) -> Optional[Tuple[int, int]]:
        """Size of the window in device pixels, or None until the compositor has sized it."""
        surface = self._window.get_surface()
        if surface is None or surface.get_width() <= 0 or surface.get_height() <= 0:
            return None
        return device_size(surface.get_width(), surface.get_height(), self._scale())

    def show_frame(
        self,
        frame: Frame,
        seconds: float,
        transition: Optional[Tuple[str, float]] = None,
        first_transition: Optional[Tuple[str, float]] = None,
    ) -> None:
        self._message.set_visible(False)
        self._canvas.set_frame(frame, seconds, transition, first_transition)

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
