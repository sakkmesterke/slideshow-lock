"""Smoke test of the real settings window in a real Wayland session (UI-1; run it through
``SMOKE_SCRIPT=smoke_preferences.py run.sh``).

What it exercises: the real ``PreferencesWindow`` (real GTK widgets on headless mutter), the real
``Settings`` (memory backend), the real GLib main loop, and the real preview started from the
"Preview" button (one fullscreen window per virtual monitor). Fields are driven by calling the
widgets' own setters (``set_value``, ``set_selected``, ``set_active``, ``set_text`` + the
``entry-activated`` signal), which fires the same handlers a user's change fires. An edit is kept
in the window's draft and reaches the settings through the Save button (or the close).

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

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from smoke_preview import RESULTS, check, make_pictures  # noqa: E402

from slideshow_lock.preferences import PreferencesWindow  # noqa: E402
from slideshow_lock.preferences_model import (  # noqa: E402
    CHOICES,
    INTERVAL_POSITIONS,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
    RANDOM_POOL,
    TRANSITION_CHOICES,
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
    Adw.init()
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
        == (120, 0, interval_position_for_seconds(5), 0, 0, False),
    )
    check(
        "the transition drop-down starts at the cross-fade, the stored default",
        window.transition_drop.get_selected() == TRANSITION_CHOICES.index("crossfade"),
        str(window.transition_drop.get_selected()),
    )
    check(
        "the slide interval shows as a big HH:MM:SS and a short text above one slider",
        window.interval_total.get_label() == "00:00:05"
        and window.interval_caption.get_label() == "5 s",
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
        "the folder field is empty and the group says the XDG default is in use (D25)",
        window.folder_row.get_text() == ""
        and default_picture_folder() in window.pictures_group.get_description(),
        window.pictures_group.get_description(),
    )
    check(
        "a missing default folder is a note, not an error",
        "does not exist yet" in window.pictures_group.get_description(),
        window.pictures_group.get_description(),
    )
    check("Save is off while there is nothing to save", not window.save_button.get_sensitive())
    if args.screenshot:
        os.makedirs(args.screenshot, exist_ok=True)
        screenshot(window, os.path.join(args.screenshot, "window.png"))

    # -- every field is kept in the draft, and Save writes it -----------------------------------
    window.idle_spin.set_value(300)
    window.grace_spin.set_value(5)
    window.interval_scale.set_value(interval_position_for_seconds(20))
    window.order_drop.set_selected(CHOICES[KEY_ORDER].index("name"))
    window.scaling_drop.set_selected(CHOICES[KEY_SCALING].index("fit"))
    window.pan_switch.set_active(True)
    window.transition_drop.set_selected(TRANSITION_CHOICES.index("fade-black"))
    window.folder_row.set_text(folder)
    window.folder_row.emit("entry-activated")

    def stored_values():
        return (
            stored.get_idle_timeout_seconds(),
            stored.get_lock_grace_period_seconds(),
            stored.get_slide_interval_seconds(),
            stored.get_order(),
            stored.get_scaling(),
            stored.get_pan_portrait_images(),
            stored.get_transitions(),
            stored.get_picture_folder(),
        )

    check(
        "the edits are kept: nothing is stored before Save",
        stored_values()
        == (120, 0, 5, "random", "fill", False, ["crossfade"], default_picture_folder()),
        str(stored_values()),
    )
    check("Save is on", window.save_button.get_sensitive())
    check("the HH:MM:SS line follows", window.interval_total.get_label() == "00:00:20")
    window.save_button.emit("clicked")
    check(
        "Save writes every field",
        stored_values() == (300, 5, 20, "name", "fit", True, ["fade-black"], folder),
        str(stored_values()),
    )
    check("the status says so", window.status.get_label() == "Saved.", window.status.get_label())
    check("Save is off again", not window.save_button.get_sensitive())
    window.transition_drop.set_selected(TRANSITION_CHOICES.index("none"))
    window.save_button.emit("clicked")
    check(
        "none is saved as the empty list, not as the default",
        stored.get_transitions() == [] and window.status.get_label() == "Saved.",
        str(stored.get_transitions()),
    )
    window.transition_drop.set_selected(TRANSITION_CHOICES.index("random"))
    check("the random mix is kept, not stored", stored.get_transitions() == [])
    window.save_button.emit("clicked")
    check(
        "the random mix is saved as the eight, with the order random",
        stored.get_transitions() == list(RANDOM_POOL) and stored.get_transition_order() == "random",
        str(stored.get_transitions()),
    )
    window.transition_drop.set_selected(TRANSITION_CHOICES.index("blur"))
    window.save_button.emit("clicked")
    check("any of the ten is saved as a list of its name", stored.get_transitions() == ["blur"])
    stored.set_transitions(["crossfade"])
    pump(0.3)
    check(
        "a transition set elsewhere shows up in the window",
        window.transition_drop.get_selected() == TRANSITION_CHOICES.index("crossfade"),
    )
    stored._settings.set_strv("transitions", ["wipe"])  # one name: shows as that name
    pump(0.3)
    check(
        "a stored single name shows as that transition and stays stored",
        window.transition_drop.get_selected() == TRANSITION_CHOICES.index("wipe")
        and stored._settings.get_strv("transitions") == ["wipe"],
    )
    stored._settings.set_strv("transitions", ["wipe", "push"])  # a list written by hand
    pump(0.3)
    check(
        "a stored list of several names shows as the random mix and stays stored",
        window.transition_drop.get_selected() == TRANSITION_CHOICES.index("random")
        and stored._settings.get_strv("transitions") == ["wipe", "push"]
        and not window.save_button.get_sensitive(),
    )
    stored.set_transitions(["crossfade"])
    check(
        "the saved folder is shown as in use",
        window.pictures_group.get_description() == f"In use: {folder}",
        window.pictures_group.get_description(),
    )

    # -- what is refused is not saved, and not shown ----------------------------------------
    window.folder_row.set_text("relative/dir")
    window.folder_row.emit("entry-activated")
    check(
        "a relative folder is refused: not kept, the field goes back, the status says why",
        stored.get_picture_folder() == folder
        and window.folder_row.get_text() == folder
        and "absolute" in window.status.get_label(),
        window.status.get_label(),
    )
    for text in ("abc", "", "99999", "-3", "1.5"):
        window.idle_spin.set_text(text)
        window.idle_spin.update()
        check(
            f"{text!r} in a number field changes nothing, the old value stays",
            stored.get_idle_timeout_seconds() == 300
            and window.idle_spin.get_value_as_int() == 300
            and not window.save_button.get_sensitive(),
            f"stored {stored.get_idle_timeout_seconds()}, field {window.idle_spin.get_text()!r}",
        )
    window.grace_spin.set_text("")  # known corner: an emptied grace field reads as 0, its minimum
    window.grace_spin.update()
    check(
        "an emptied grace period field is kept as 0, its minimum, and shows it",
        window._draft.value(KEY_LOCK_GRACE_PERIOD_SECONDS)
        == window.grace_spin.get_value_as_int()
        == 0
        and stored.get_lock_grace_period_seconds() == 5,
        f"kept {window._draft.value(KEY_LOCK_GRACE_PERIOD_SECONDS)}",
    )
    window.grace_spin.set_value(5)
    scale = window.interval_scale
    keys = window._interval_keys

    def kept():
        return window._draft.interval_view().seconds

    def press(key):
        return keys.emit("key-pressed", key, 0, Gdk.ModifierType(0))

    def show(*_a):
        return f"{kept()} {window.interval_total.get_label()}"

    check(
        "the slider on a step: stored seconds, big text and caption agree",
        kept() == 20
        and window.interval_total.get_label() == "00:00:20"
        and window.interval_caption.get_label() == "20 s",
        show(),
    )
    scale.set_value(interval_position_for_seconds(60))
    press(Gdk.KEY_Right)
    check(
        "an arrow moves one step of the scale: 1 min -> 2 min",
        kept() == 120
        and window.interval_total.get_label() == "00:02:00"
        and window.interval_caption.get_label() == "2 min",
        show(),
    )
    press(Gdk.KEY_Up)
    press(Gdk.KEY_Right)
    check(
        "up and right are steps too: 3 min, 5 min",
        kept() == 300,
        show(),
    )
    press(Gdk.KEY_Left)
    press(Gdk.KEY_Down)
    press(Gdk.KEY_Left)
    check(
        "left and down go back: 3 min, 2 min, 1 min",
        kept() == 60,
        show(),
    )
    press(Gdk.KEY_Left)
    check(
        "below a minute the steps are 5 s apart: 55 s",
        kept() == 55,
        show(),
    )
    scale.set_value(interval_position_for_seconds(10))
    press(Gdk.KEY_Right)
    check(
        "an arrow from 10 s goes to 15 s (the 5-second quarter)",
        kept() == 15 and window.interval_caption.get_label() == "15 s",
        show(),
    )
    press(Gdk.KEY_Left)
    check("and back to 10 s", kept() == 10, show())
    scale.set_value(interval_position_for_seconds(3600))
    press(Gdk.KEY_Right)
    check(
        "an arrow from 1 h goes to 2 h (the hours quarter)",
        kept() == 7200 and window.interval_caption.get_label() == "2 h",
        show(),
    )
    press(Gdk.KEY_Left)
    check(
        "and back to 1 h",
        kept() == 3600 and window.interval_caption.get_label() == "1 h",
        show(),
    )
    press(Gdk.KEY_End)
    check(
        "End is the longest interval, 23:59:59 (86399 s), not 24 hours",
        kept() == 86399
        and window.interval_total.get_label() == "23:59:59"
        and "24" not in window.interval_caption.get_label()
        and scale.get_value() == INTERVAL_SLIDER_MAX,
        f"{show()} {window.interval_caption.get_label()!r}",
    )
    press(Gdk.KEY_Right)
    check("an arrow at the end stays at the end", kept() == 86399, show())
    press(Gdk.KEY_Left)
    check("one step back from the end is 12 h", kept() == 43200, show())
    press(Gdk.KEY_Home)
    check(
        "Home is the shortest interval, 00:00:01",
        kept() == 1 and window.interval_total.get_label() == "00:00:01" and scale.get_value() == 0,
        show(),
    )
    press(Gdk.KEY_Left)
    check(
        "an arrow at the start stays at 1 s (0 is not possible)",
        kept() == 1,
        show(),
    )
    window._interval_wheel.emit("scroll", 0.0, -1.0)
    check("the wheel moves one step too", kept() == 2, show())
    scale.set_value(1290)  # between 1 min (1260) and 2 min (1330): snaps to the nearer one
    pump(0.3)  # a slider set from its own handler is announced after the handler ended
    check(
        "a position between two steps snaps to the nearest and keeps it",
        scale.get_value() == 1260 and kept() == 60,
        f"{scale.get_value()} {show()}",
    )
    check(
        "the status stays empty after the echo: no message replaced",
        window.status.get_label() == "",
        window.status.get_label(),
    )
    walked = []
    for position in INTERVAL_POSITIONS:
        scale.set_value(position)
        walked.append(kept())
    check(
        "every step of the scale, set one by one, is kept as its seconds",
        walked == list(INTERVAL_STOPS),
        f"{sum(a != b for a, b in zip(walked, INTERVAL_STOPS))} differ",
    )
    # -- a stored value that is not a step: shown at the nearest, never written back -------------
    window.save_button.emit("clicked")  # the walk above left the last step kept
    check(
        "Save writes the last step of the walk and is off again",
        stored.get_slide_interval_seconds() == INTERVAL_STOPS[-1]
        and not window.save_button.get_sensitive(),
        str(stored.get_slide_interval_seconds()),
    )
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
        "an arrow from there goes one step on, to 3 min, kept and not stored",
        kept() == 180 and stored.get_slide_interval_seconds() == 100,
        show(),
    )
    stored.set_slide_interval_seconds(77)  # another process changes a value, off the scale
    pump(0.5)
    check(
        "a stored change elsewhere does not take away an edit that is not saved: 3 min stays",
        kept() == 180 and window.interval_total.get_label() == "00:03:00",
        f"{scale.get_value()} {show()}",
    )
    window.save_button.emit("clicked")
    check(
        "and Save writes it over the other process's value",
        stored.get_slide_interval_seconds() == 180,
    )
    stored.set_slide_interval_seconds(77)
    pump(1.0, until=lambda: window.interval_total.get_label() == "00:01:17")
    pump(0.5)
    check(
        "with nothing kept, a value set elsewhere shows at the nearest step and stays 77",
        scale.get_value() == interval_position_for_seconds(60)
        and stored.get_slide_interval_seconds() == 77,
        f"{scale.get_value()} {show()}",
    )
    window.idle_spin.set_text("86400")
    window.idle_spin.update()
    check(
        "the idle time still takes 86400 (kept)",
        window._draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 86400,
    )
    window.idle_spin.set_text("20")
    window.idle_spin.update()
    check(
        "a typed number is kept and not stored",
        window._draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 20
        and stored.get_idle_timeout_seconds() == 300,
    )
    window.idle_spin.set_text("99")
    window.idle_spin.update()
    stored.set_scaling("fill")  # another process changes another key: the window refreshes
    pump(0.5)
    check(
        "a change elsewhere does not take an edit out of a number field that is not saved",
        window.idle_spin.get_value_as_int() == 99
        and window._draft.value(KEY_IDLE_TIMEOUT_SECONDS) == 99
        and window.scaling_drop.get_selected() == CHOICES[KEY_SCALING].index("fill"),
        str(window.idle_spin.get_value_as_int()),
    )
    window.idle_spin.set_text("20")
    window.idle_spin.update()
    window.save_button.emit("clicked")
    check("and Save stores it", stored.get_idle_timeout_seconds() == 20)
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
        "the chooser opens in the folder of the field",
        start is not None and start.get_path() == folder,
        None if start is None else start.get_path(),
    )
    window._on_folder_chosen(window._chooser, Gtk.ResponseType.CANCEL)
    check("cancelling the chooser changes nothing", stored.get_picture_folder() == folder)
    missing = os.path.join(folder, "not-there")
    window.folder_row.set_text(missing)
    window.folder_row.emit("entry-activated")
    window.browse_button.emit("clicked")
    pump(0.5)
    start = window._chooser.get_current_folder()
    expected = default_picture_folder()
    if not os.path.isdir(expected):
        expected = os.path.expanduser("~")
    check(
        "a missing folder in the field: the chooser opens in the pictures folder, not in it",
        start is not None and start.get_path() == expected,
        None if start is None else start.get_path(),
    )
    window._on_folder_chosen(window._chooser, Gtk.ResponseType.CANCEL)
    window.folder_row.set_text(folder)
    window.folder_row.emit("entry-activated")
    other = tempfile.mkdtemp(prefix="slideshow-smoke-prefs-")
    window._on_folder_chosen(_Chosen(other), Gtk.ResponseType.ACCEPT)
    check(
        "a folder chosen in the chooser is shown and kept, not stored yet",
        stored.get_picture_folder() == folder
        and window.folder_row.get_text() == other
        and window.save_button.get_sensitive(),
    )
    window.save_button.emit("clicked")
    check("and Save stores it", stored.get_picture_folder() == other)
    window.folder_row.set_text("")
    window.folder_row.emit("entry-activated")
    window.save_button.emit("clicked")
    check(
        "emptying the field and saving goes back to the default folder",
        stored.get_picture_folder() == default_picture_folder(),
    )
    window.folder_row.set_text(folder)
    window.folder_row.emit("entry-activated")
    window.save_button.emit("clicked")

    # -- the preview ---------------------------------------------------------------------------
    window.order_drop.set_selected(CHOICES[KEY_ORDER].index("random"))  # kept, not saved
    window.transition_drop.set_selected(TRANSITION_CHOICES.index("zoom"))
    before = {k: stored._settings.get_value(k).unpack() for k in KEYS}
    before_transitions = stored._settings.get_strv("transitions")
    toplevels = fullscreen_windows()
    window.preview_button.emit("clicked")
    pump(2.0, until=lambda: fullscreen_windows() > toplevels)
    monitors = fullscreen_windows() - toplevels
    check("Preview opens one window per monitor", monitors == 2, f"{monitors} new windows")
    check("the button is off while it runs", not window.preview_button.get_sensitive())
    controller = window._preview[0]
    check(
        "the preview runs on the values of the window that are not saved: random, zoom",
        controller._settings.get_order() == "random"
        and controller._settings.get_transitions() == ["zoom"]
        and stored.get_order() == "name",
        f"{controller._settings.get_order()} {controller._settings.get_transitions()}",
    )
    check(
        "the preview changed no stored setting, and the edits are still to be saved",
        before == {k: stored._settings.get_value(k).unpack() for k in KEYS}
        and stored._settings.get_strv("transitions") == before_transitions
        and window.save_button.get_sensitive(),
    )
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
    check(
        "closing the window saves what was edited, without a question",
        stored.get_order() == "random" and stored.get_transitions() == ["zoom"],
        f"{stored.get_order()} {stored.get_transitions()}",
    )

    ok = all(RESULTS)
    print(
        f"SMOKE result: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)} checks)",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
