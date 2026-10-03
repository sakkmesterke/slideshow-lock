"""Smoke test of the real settings window in a real Wayland session (UI-1; run it through
``SMOKE_SCRIPT=smoke_preferences.py run.sh``).

What it exercises: the real ``PreferencesWindow`` (real GTK widgets on headless mutter), the real
``Settings`` (memory backend), the real GLib main loop, and the real preview started from the
"Preview" button (one fullscreen window per virtual monitor). Fields are driven by calling the
widgets' own setters (``set_value``, ``set_selected``, ``set_active``, ``set_text`` + the
``activate`` signal), which fires the same handlers a user's change fires.

What it does not prove: how the window looks (a human judgement on the real monitor: use
``--screenshot DIR`` to get a picture of the window as GTK draws it here), real keyboard and
mouse use of the widgets, the folder chooser dialog itself (a headless compositor has no portal;
only its "cancel" and its "folder chosen" paths are exercised), the GNOME theme, or anything
about locking: the window and the preview contain no lock call.

    SMOKE_SCRIPT=smoke_preferences.py run.sh                       # all checks
    SMOKE_SCRIPT=smoke_preferences.py run.sh --screenshot /tmp/ui  # also write window.png
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402
from smoke_preview import RESULTS, check, make_pictures  # noqa: E402

from slideshow_lock.preferences import PreferencesWindow  # noqa: E402
from slideshow_lock.preferences_model import (  # noqa: E402
    CHOICES,
    INTERVAL_POSITIONS,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
    interval_position_for_seconds,
)
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    Settings,
    default_picture_folder,
)

KEYS = (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_ORDER,
    KEY_SCALING,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
)


def pump(seconds: float = 0.3, until=None) -> bool:
    """Run the main loop for *seconds*, or until *until()* is true."""
    context = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while GLib.get_monotonic_time() < end:
        if until is not None and until():
            return True
        if not context.iteration(False):
            GLib.usleep(5000)
    return bool(until()) if until is not None else True


def fullscreen_windows() -> int:
    return sum(
        1 for w in Gtk.Window.get_toplevels() if isinstance(w, Gtk.Window) and w.is_visible()
    )


def screenshot(window: Gtk.Window, path: str) -> None:
    """Write the window as GTK draws it to *path* (PNG). PyGObject cannot hand over a render
    node (no Python type for GskClipNode), so this goes through the C functions with ctypes."""
    import ctypes

    def pointer(obj):
        get = ctypes.pythonapi.PyCapsule_GetPointer
        get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
        return get(obj.__gpointer__, None)

    gtk = ctypes.CDLL("libgtk-4.so.1")
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    gtk.gtk_snapshot_to_node.restype = ctypes.c_void_p
    gtk.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
    node = gtk.gtk_snapshot_to_node(pointer(snapshot))
    rect = (ctypes.c_float * 4)(0, 0, width, height)  # a graphene_rect_t: x, y, width, height
    gtk.gsk_renderer_render_texture.restype = ctypes.c_void_p
    gtk.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p] * 3
    texture = gtk.gsk_renderer_render_texture(
        pointer(window.get_native().get_renderer()), node, ctypes.addressof(rect)
    )
    gtk.gdk_texture_save_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    gtk.gdk_texture_save_to_png(texture, path.encode())


class _Chosen:
    """Stands in for the chooser dialog after "Select": it only has to answer ``get_file``."""

    def __init__(self, path: str) -> None:
        from gi.repository import Gio

        self._file = Gio.File.new_for_path(path)

    def get_file(self):
        return self._file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", metavar="DIR", help="write window.png into DIR")
    args = parser.parse_args()

    Gtk.init()
    stored = Settings()  # a second handle on the same store: what the service would read
    folder = tempfile.mkdtemp(prefix="slideshow-smoke-prefs-")
    make_pictures(folder)

    window = PreferencesWindow(Settings())
    window.present()
    pump(3.0, until=lambda: window.get_mapped() and window.get_width() > 0)
    pump(0.5)  # the first frame
    check(
        "the window opens",
        window.is_visible() and window.get_width() > 0,
        f"{window.get_width()}x{window.get_height()}",
    )

    # -- what it shows at the start -------------------------------------------------------
    check(
        "the fields start from the stored defaults",
        (
            window.idle_spin.get_value_as_int(),
            window.grace_spin.get_value_as_int(),
            window.interval_scale.get_value(),
            window.order_drop.get_selected(),
            window.scaling_drop.get_selected(),
            window.pan_switch.get_active(),
        )
        == (120, 0, interval_position_for_seconds(10), 0, 0, False),
    )
    check(
        "the slide interval shows as a big HH:MM:SS and a short text above one slider",
        window.interval_total.get_label() == "00:00:10"
        and window.interval_caption.get_label() == "10 s",
        f"{window.interval_total.get_label()!r} {window.interval_caption.get_label()!r}",
    )
    check(
        "the slider runs over the whole scale",
        (
            window.interval_scale.get_adjustment().get_lower(),
            window.interval_scale.get_adjustment().get_upper(),
        )
        == (0, INTERVAL_SLIDER_MAX),
    )
    user_value = stored._settings.get_user_value("slide-interval-seconds")
    check("opening the window wrote nothing", user_value is None, str(user_value))
    check(
        "the idle time and the grace period are plain number fields",
        window.idle_spin.get_adjustment().get_upper() == 86400
        and window.grace_spin.get_adjustment().get_upper() == 86400,
    )
    check(
        "the folder field is empty and hints at the XDG default (D25)",
        window.folder_entry.get_text() == ""
        and window.folder_entry.get_placeholder_text() == default_picture_folder(),
        window.folder_entry.get_placeholder_text(),
    )
    check(
        "a missing default folder is a note, not an error",
        "does not exist yet" in window.folder_note.get_label(),
        window.folder_note.get_label(),
    )
    if args.screenshot:
        os.makedirs(args.screenshot, exist_ok=True)
        screenshot(window, os.path.join(args.screenshot, "window.png"))

    # -- every field reaches the settings --------------------------------------------------
    window.idle_spin.set_value(300)
    window.grace_spin.set_value(5)
    window.interval_scale.set_value(interval_position_for_seconds(20))
    check(
        "the two number fields and the slide interval slider are saved",
        (
            stored.get_idle_timeout_seconds(),
            stored.get_lock_grace_period_seconds(),
            stored.get_slide_interval_seconds(),
        )
        == (300, 5, 20),
    )
    check("the HH:MM:SS line follows", window.interval_total.get_label() == "00:00:20")
    check("the status says so", window.status.get_label() == "Saved.", window.status.get_label())
    window.order_drop.set_selected(CHOICES[KEY_ORDER].index("name"))
    window.scaling_drop.set_selected(CHOICES[KEY_SCALING].index("fit"))
    check(
        "order and scaling are saved", (stored.get_order(), stored.get_scaling()) == ("name", "fit")
    )
    window.pan_switch.set_active(True)
    check("the pan switch is saved", stored.get_pan_portrait_images() is True)
    window.folder_entry.set_text(folder)
    window.folder_entry.emit("activate")
    check(
        "a typed folder is saved and shown as in use",
        stored.get_picture_folder() == folder
        and window.folder_note.get_label() == f"In use: {folder}",
        window.folder_note.get_label(),
    )

    # -- what is refused is not saved, and not shown ----------------------------------------
    window.folder_entry.set_text("relative/dir")
    window.folder_entry.emit("activate")
    check(
        "a relative folder is refused: not stored, the field goes back, the status says why",
        stored.get_picture_folder() == folder
        and window.folder_entry.get_text() == folder
        and "absolute" in window.status.get_label(),
        window.status.get_label(),
    )
    for text in ("abc", "", "99999", "-3", "1.5"):
        window.idle_spin.set_text(text)
        window.idle_spin.update()
        check(
            f"{text!r} in a number field changes nothing, the old value stays",
            stored.get_idle_timeout_seconds() == 300 and window.idle_spin.get_value_as_int() == 300,
            f"stored {stored.get_idle_timeout_seconds()}, field {window.idle_spin.get_text()!r}",
        )
    window.grace_spin.set_text("")  # known corner: an emptied grace field reads as 0, its minimum
    window.grace_spin.update()
    check(
        "an emptied grace period field shows what is stored (0, its minimum)",
        stored.get_lock_grace_period_seconds() == window.grace_spin.get_value_as_int() == 0,
        f"stored {stored.get_lock_grace_period_seconds()}",
    )
    window.grace_spin.set_value(5)
    scale = window.interval_scale
    keys = window._interval_keys

    def press(key):
        return keys.emit("key-pressed", key, 0, Gdk.ModifierType(0))

    def show(*_a):
        return f"{stored.get_slide_interval_seconds()} {window.interval_total.get_label()}"

    check(
        "the slider on a step: stored seconds, big text and caption agree",
        stored.get_slide_interval_seconds() == 20
        and window.interval_total.get_label() == "00:00:20"
        and window.interval_caption.get_label() == "20 s",
        show(),
    )
    scale.set_value(interval_position_for_seconds(60))
    press(Gdk.KEY_Right)
    check(
        "an arrow moves one step of the scale: 1 min -> 2 min",
        stored.get_slide_interval_seconds() == 120
        and window.interval_total.get_label() == "00:02:00"
        and window.interval_caption.get_label() == "2 min",
        show(),
    )
    press(Gdk.KEY_Up)
    press(Gdk.KEY_Right)
    check(
        "up and right are steps too: 3 min, 5 min",
        stored.get_slide_interval_seconds() == 300,
        show(),
    )
    press(Gdk.KEY_Left)
    press(Gdk.KEY_Down)
    press(Gdk.KEY_Left)
    check(
        "left and down go back: 3 min, 2 min, 1 min",
        stored.get_slide_interval_seconds() == 60,
        show(),
    )
    press(Gdk.KEY_Left)
    check(
        "below a minute a step is one second: 59 s",
        stored.get_slide_interval_seconds() == 59,
        show(),
    )
    press(Gdk.KEY_End)
    check(
        "End is the longest interval, 23:59:59 (86399 s), not 24 hours",
        stored.get_slide_interval_seconds() == 86399
        and window.interval_total.get_label() == "23:59:59"
        and "24" not in window.interval_caption.get_label()
        and scale.get_value() == INTERVAL_SLIDER_MAX,
        f"{show()} {window.interval_caption.get_label()!r}",
    )
    press(Gdk.KEY_Right)
    check(
        "an arrow at the end stays at the end", stored.get_slide_interval_seconds() == 86399, show()
    )
    press(Gdk.KEY_Left)
    check(
        "one step back from the end is 12 h", stored.get_slide_interval_seconds() == 43200, show()
    )
    press(Gdk.KEY_Home)
    check(
        "Home is the shortest interval, 00:00:01",
        stored.get_slide_interval_seconds() == 1
        and window.interval_total.get_label() == "00:00:01"
        and scale.get_value() == 0,
        show(),
    )
    press(Gdk.KEY_Left)
    check(
        "an arrow at the start stays at 1 s (0 is not possible)",
        stored.get_slide_interval_seconds() == 1,
        show(),
    )
    window._interval_wheel.emit("scroll", 0.0, -1.0)
    check("the wheel moves one step too", stored.get_slide_interval_seconds() == 2, show())
    scale.set_value(240)  # between 1 min (236) and 2 min (255): snaps to the nearer one
    pump(0.3)  # a slider set from its own handler is announced after the handler ended
    check(
        "a position between two steps snaps to the nearest and saves it",
        scale.get_value() == 236 and stored.get_slide_interval_seconds() == 60,
        f"{scale.get_value()} {show()}",
    )
    check(
        "the status still says Saved. after the echo",
        window.status.get_label() == "Saved.",
        window.status.get_label(),
    )
    walked = []
    for position in INTERVAL_POSITIONS:
        scale.set_value(position)
        walked.append(stored.get_slide_interval_seconds())
    check(
        "every step of the scale, set one by one, is saved as its seconds",
        walked == list(INTERVAL_STOPS),
        f"{sum(a != b for a, b in zip(walked, INTERVAL_STOPS))} differ",
    )
    # -- a stored value that is not a step: shown at the nearest, never written back -------------
    stored.set_slide_interval_seconds(100)
    pump(1.0, until=lambda: window.interval_total.get_label() == "00:01:40")
    pump(0.5)
    check(
        "a stored 100 s: the slider sits at the 2 min step, the big text says 00:01:40",
        scale.get_value() == interval_position_for_seconds(120)
        and window.interval_total.get_label() == "00:01:40",
        f"{scale.get_value()} {window.interval_total.get_label()}",
    )
    check(
        "...and the stored value is still 100 (nothing was written back)",
        stored.get_slide_interval_seconds() == 100
        and stored._settings.get_user_value("slide-interval-seconds").get_uint32() == 100,
        show(),
    )
    check(
        "the caption says it is not a step",
        "nearest" in window.interval_caption.get_label(),
        window.interval_caption.get_label(),
    )
    press(Gdk.KEY_Right)
    check(
        "an arrow from there goes one step on: 3 min",
        stored.get_slide_interval_seconds() == 180,
        show(),
    )
    stored.set_slide_interval_seconds(77)  # another process changes a value, off the scale
    pump(1.0, until=lambda: window.interval_total.get_label() == "00:01:17")
    pump(0.5)
    check(
        "a value set elsewhere shows at the nearest step and stays 77",
        scale.get_value() == interval_position_for_seconds(60)
        and stored.get_slide_interval_seconds() == 77,
        f"{scale.get_value()} {show()}",
    )
    window.idle_spin.set_text("86400")
    window.idle_spin.update()
    check("the idle time still takes 86400", stored.get_idle_timeout_seconds() == 86400)
    window.idle_spin.set_text("20")
    window.idle_spin.update()
    check("a typed number is saved", stored.get_idle_timeout_seconds() == 20)
    stored.set_idle_timeout_seconds(77)
    pump(1.0, until=lambda: window.idle_spin.get_value_as_int() == 77)
    check(
        "an idle time set elsewhere shows up in the window",
        window.idle_spin.get_value_as_int() == 77,
    )

    # -- the folder chooser: only the paths a headless run can reach ------------------------
    window.browse_button.emit("clicked")
    pump(0.5)
    check("Browse opens a chooser", window._chooser is not None)
    start = window._chooser.get_current_folder()
    check(
        "the chooser opens in the folder in use",
        start is not None and start.get_path() == folder,
        None if start is None else start.get_path(),
    )
    window._on_folder_chosen(window._chooser, Gtk.ResponseType.CANCEL)
    check("cancelling the chooser changes nothing", stored.get_picture_folder() == folder)
    missing = os.path.join(folder, "not-there")
    window.folder_entry.set_text(missing)
    window.folder_entry.emit("activate")
    window.browse_button.emit("clicked")
    pump(0.5)
    start = window._chooser.get_current_folder()
    expected = default_picture_folder()
    if not os.path.isdir(expected):
        expected = os.path.expanduser("~")
    check(
        "a missing folder in use: the chooser opens in the pictures folder, not in that folder",
        start is not None and start.get_path() == expected,
        None if start is None else start.get_path(),
    )
    window._on_folder_chosen(window._chooser, Gtk.ResponseType.CANCEL)
    window.folder_entry.set_text(folder)
    window.folder_entry.emit("activate")
    other = tempfile.mkdtemp(prefix="slideshow-smoke-prefs-")
    window._on_folder_chosen(_Chosen(other), Gtk.ResponseType.ACCEPT)
    check(
        "a folder chosen in the chooser is saved and shown",
        stored.get_picture_folder() == other and window.folder_entry.get_text() == other,
    )
    window.folder_entry.set_text("")
    window.folder_entry.emit("activate")
    check(
        "emptying the field goes back to the default folder",
        stored.get_picture_folder() == default_picture_folder(),
    )
    window.folder_entry.set_text(folder)
    window.folder_entry.emit("activate")

    # -- the preview ---------------------------------------------------------------------------
    before = {k: stored._settings.get_value(k).unpack() for k in KEYS}
    toplevels = fullscreen_windows()
    window.preview_button.emit("clicked")
    pump(2.0, until=lambda: fullscreen_windows() > toplevels)
    monitors = fullscreen_windows() - toplevels
    check("Preview opens one window per monitor", monitors == 2, f"{monitors} new windows")
    check("the button is off while it runs", not window.preview_button.get_sensitive())
    check(
        "the preview changed no stored setting",
        before == {k: stored._settings.get_value(k).unpack() for k in KEYS},
    )
    controller = window._preview[0]
    controller.stop("smoke")
    pump(1.0, until=lambda: window.preview_button.get_sensitive())
    check(
        "when it ends the button is back and the windows are closed",
        window.preview_button.get_sensitive() and fullscreen_windows() == toplevels,
        f"{fullscreen_windows() - toplevels} left open",
    )
    window.preview_button.emit("clicked")
    pump(2.0, until=lambda: fullscreen_windows() > toplevels)
    check("a second preview starts the same way", fullscreen_windows() - toplevels == 2)
    window.close()  # the settings window is closed while the preview runs
    pump(1.0, until=lambda: fullscreen_windows() == 0)
    check(
        "closing the window ends a running preview",
        fullscreen_windows() == 0,
        f"{fullscreen_windows()} windows left",
    )

    ok = all(RESULTS)
    print(
        f"SMOKE result: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)} checks)",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
