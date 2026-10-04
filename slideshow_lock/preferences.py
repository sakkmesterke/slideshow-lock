"""The settings window (UI-1): the stored settings in one GTK 4 window, in titled groups of rows.

    glib-compile-schemas data/
    GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences

One field per setting, saved the moment it is changed (the service picks changes up live, so
there is no "Apply"). What the fields accept and what counts as saved is decided in
``slideshow_lock.preferences_model``, which has no GTK in it and is tested by the CI; this
module only puts that on the screen. The window shows a value as saved only when the model
said so: a refused value puts the field back to the stored one and says why.

The "Preview" button runs the slideshow preview of CORE-2 (``preview_app.start_preview``) on
the stored settings, in this process. It never locks the session (D11); any key, click, scroll
or mouse movement ends it. There is no on/off switch here: that goes through the systemd user
unit (D4), which is not part of this window.

The slide interval is one slider and a big HH:MM:SS line above it; it is stored in seconds in
the same key as before, from 00:00:01 to 23:59:59. The slider is four equal quarters: every second
from 1 to 10, every 5 seconds from 10 to 60, round minutes from 1 to 60, round hours from 1 to 24
(see ``INTERVAL_STOPS``); an arrow key moves one step of that scale. A stored value that is not a
step is shown at the nearest one and stays stored until the user moves the slider. The idle time
and the lock grace period are plain number fields.

Plain Gtk widgets, not libadwaita: the CI has no libadwaita typelib and the spec lists no
libadwaita package for the CI, so the groups are drawn with a few CSS rules of this module
(``_CSS``) instead of ``Adw.PreferencesGroup``. The folder chooser is
``Gtk.FileChooserNative``, which exists in every GTK 4 (``Gtk.FileDialog`` needs 4.10; the
GTK 4.8 this was built and measured on has none). Not covered by the tests of the CI: what
this module draws (checked with ``tools/wayland-smoke/smoke_preferences.py``, and by eye on
the reference machine).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import APP_ID, _  # noqa: E402
from slideshow_lock.preferences_model import (  # noqa: E402
    CHOICES,
    INT_RANGES,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
    PreferencesModel,
    format_hms,
    interval_position_for_seconds,
    snap_interval_position,
    step_interval_position,
)
from slideshow_lock.preview_app import build_source, start_preview  # noqa: E402
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_SCALING,
    Settings,
)

_LOG = logging.getLogger(__name__)

MARGIN = 18  # the window's edge and the inside of a row
GROUP_SPACING = 24  # between the groups
ROW_SPACING = 8
ROW_PADDING = 12  # above and below the content of a row

_CSS = b"""
.sl-group { border: 1px solid alpha(currentColor, 0.18); border-radius: 12px; }
.sl-group-title { font-weight: bold; }
.sl-subtitle { font-size: 0.9em; }
.sl-time-big { font-size: 2.6em; font-weight: bold; font-feature-settings: "tnum"; }
"""
_css_installed = False


def _install_css() -> None:
    """Load the few style rules of the window, once per process, for the whole display."""
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def _choice_labels():
    """What the drop-downs list, in the order of ``CHOICES`` (the values stay English)."""
    return {
        KEY_ORDER: (_("Random"), _("By file name")),
        KEY_SCALING: (_("Fill the screen (crops the picture)"), _("Fit (shows the whole picture)")),
    }


class PreferencesWindow(Gtk.Window):
    """The settings window. *settings* is a ``Settings``; the window reads and writes only through
    ``PreferencesModel``."""

    def __init__(self, settings: Settings, application: Optional[Gtk.Application] = None) -> None:
        super().__init__(title=_("Slideshow Lock settings"), application=application)
        self._settings = settings
        self._model = PreferencesModel(settings)
        self._updating = False  # True while the fields are being set from the stored values
        self._preview = None  # (controller, source) while the preview runs
        self._chooser = None
        self._closed = False
        self.set_default_size(620, -1)
        _install_css()

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=GROUP_SPACING)
        page.set_margin_top(MARGIN)
        page.set_margin_bottom(MARGIN)
        page.set_margin_start(MARGIN)
        page.set_margin_end(MARGIN)

        # -- pictures: folder, order, scaling, pan -----------------------------------------
        self.folder_entry = Gtk.Entry(hexpand=True)
        self.folder_entry.connect("activate", lambda _entry: self._commit_folder())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _controller: self._commit_folder())
        self.folder_entry.add_controller(focus)
        self.browse_button = Gtk.Button(label=_("Browse..."))
        self.browse_button.connect("clicked", lambda _button: self._browse())
        entry_line = Gtk.Box(spacing=ROW_SPACING)
        entry_line.append(self.folder_entry)
        entry_line.append(self.browse_button)
        self.folder_note = self._subtitle("")
        folder_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        folder_body.append(entry_line)
        folder_body.append(self.folder_note)

        labels = _choice_labels()
        self.order_drop = Gtk.DropDown.new_from_strings(list(labels[KEY_ORDER]))
        self.order_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_ORDER))
        self.scaling_drop = Gtk.DropDown.new_from_strings(list(labels[KEY_SCALING]))
        self.scaling_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_SCALING))
        self.pan_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.pan_switch.connect("notify::active", lambda *_a: self._on_pan())

        page.append(
            self._group(
                _("Pictures"),
                [
                    self._stacked_row(_("Picture folder"), folder_body),
                    self._row(_("Picture order"), control=self.order_drop),
                    self._row(_("Scaling"), control=self.scaling_drop),
                    self._row(
                        _("Scroll tall pictures"),
                        _(
                            "Portrait pictures on a landscape screen move slowly from top to "
                            "bottom (only with Fill). Off by default: it uses more battery."
                        ),
                        control=self.pan_switch,
                    ),
                ],
            )
        )

        # -- start the slideshow: the idle time ---------------------------------------------
        self.idle_spin = self._spin(KEY_IDLE_TIMEOUT_SECONDS)
        page.append(
            self._group(
                _("Start the slideshow"),
                [
                    self._row(
                        _("Idle time"),
                        _("How long without input before the slideshow starts."),
                        control=self._with_unit(self.idle_spin, _("seconds")),
                    )
                ],
            )
        )

        # -- timing: the slide interval on one slider; the grace period -------------------------
        self.interval_total = Gtk.Label(label=format_hms(1), halign=Gtk.Align.CENTER)
        self.interval_total.add_css_class("sl-time-big")
        self.interval_caption = self._subtitle("")
        self.interval_caption.set_halign(Gtk.Align.CENTER)
        self.interval_caption.set_justify(Gtk.Justification.CENTER)
        self.interval_scale = self._interval_slider()
        interval_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ROW_SPACING)
        interval_body.append(self.interval_total)
        interval_body.append(self.interval_caption)
        interval_body.append(self.interval_scale)
        self.grace_spin = self._spin(KEY_LOCK_GRACE_PERIOD_SECONDS)
        page.append(
            self._group(
                _("Timing"),
                [
                    self._stacked_row(_("Show each picture for"), interval_body),
                    self._row(
                        _("Lock grace period"),
                        _(
                            "Input sooner than this after the slideshow starts does not lock the "
                            "session (strictly sooner; 0 means every input locks)."
                        ),
                        control=self._with_unit(self.grace_spin, _("seconds")),
                    ),
                ],
            )
        )

        # -- preview and status ------------------------------------------------------------
        self.preview_button = Gtk.Button(label=_("Preview"), valign=Gtk.Align.CENTER)
        self.preview_button.add_css_class("suggested-action")
        self.preview_button.connect("clicked", lambda _button: self._start_preview())
        self.status = self._subtitle("")
        self.status.set_hexpand(True)
        footer = Gtk.Box(spacing=ROW_SPACING)
        footer.append(self.preview_button)
        footer.append(self.status)
        page.append(footer)

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        scrolled.set_child(page)
        self.set_child(scrolled)
        self.connect("close-request", self._on_close_request)
        settings.connect_changed(lambda _key: self.refresh() if not self._closed else None)
        self.refresh()

    # -- building blocks ---------------------------------------------------------------------

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0, valign=Gtk.Align.CENTER)

    @staticmethod
    def _subtitle(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=60)
        label.add_css_class("dim-label")
        label.add_css_class("sl-subtitle")
        return label

    @staticmethod
    def _padded(child: Gtk.Widget) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        child.set_margin_top(ROW_PADDING)
        child.set_margin_bottom(ROW_PADDING)
        child.set_margin_start(MARGIN)
        child.set_margin_end(MARGIN)
        row.set_child(child)
        return row

    def _row(self, title: str, subtitle: str = "", control: Optional[Gtk.Widget] = None):
        """A row of a group: title (and a dimmed line under it) on the left, the control right."""
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.set_valign(Gtk.Align.CENTER)
        text.append(self._label(title))
        if subtitle:
            text.append(self._subtitle(subtitle))
        box = Gtk.Box(spacing=MARGIN)
        box.append(text)
        if control is not None:
            control.set_valign(Gtk.Align.CENTER)
            box.append(control)
        return self._padded(box)

    def _stacked_row(self, title: str, body: Gtk.Widget):
        """A row whose control needs the full width: the title on top, the control under it."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ROW_SPACING)
        box.append(self._label(title))
        box.append(body)
        return self._padded(box)

    def _with_unit(self, widget: Gtk.Widget, unit: str) -> Gtk.Box:
        box = Gtk.Box(spacing=ROW_SPACING)
        box.append(widget)
        box.append(self._label(unit))
        return box

    @staticmethod
    def _group(title: str, rows) -> Gtk.Box:
        """A titled group of rows in one rounded frame, like the groups of the GNOME settings."""
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("sl-group-title")
        frame = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        frame.add_css_class("sl-group")
        for row in rows:
            frame.append(row)
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ROW_SPACING)
        group.append(heading)
        group.append(frame)
        return group

    def _interval_slider(self) -> Gtk.Scale:
        """The slide-interval slider. Its position is not the seconds: it is four equal quarters of
        seconds, 5-second steps, minutes and hours (``INTERVAL_STOPS``). Every position is
        snapped to a step; the keys and the wheel move one step, which the default handling of a
        scale could not (a one-unit move would snap back)."""
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, INTERVAL_SLIDER_MAX, 1)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(380, -1)
        for seconds, label in (  # the ends of the quarters and of the slider
            (10, _("10 s")),
            (60, _("1 min")),
            (3600, _("1 h")),
            (INTERVAL_STOPS[-1], _("24 h")),
        ):
            scale.add_mark(interval_position_for_seconds(seconds), Gtk.PositionType.BOTTOM, label)
        scale.connect("value-changed", lambda _scale: self._on_interval())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_interval_key)
        scale.add_controller(keys)
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        wheel.connect("scroll", self._on_interval_wheel)
        scale.add_controller(wheel)
        self._interval_keys = keys
        self._interval_wheel = wheel
        return scale

    def _spin(self, key: str) -> Gtk.SpinButton:
        low, high = INT_RANGES[key]
        spin = Gtk.SpinButton.new_with_range(low, high, 1)
        spin.set_numeric(True)
        # A number outside the range, or text that is no number, is thrown away and the old value
        # stays (GTK's default would clamp it to the nearest limit). Known corner: an emptied
        # field reads as 0, so it is thrown away for every field but the grace period, whose
        # minimum is 0: there it becomes 0, which is shown and stored alike.
        spin.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
        spin.connect("value-changed", lambda button: self._on_int(key, button))
        return spin

    # -- showing the stored values -------------------------------------------------------------

    def refresh(self) -> None:
        """Put every field to the stored value. Also called when another process changes one."""
        self._updating = True
        try:
            view = self._model.folder_view()
            self.folder_entry.set_text(view.text)
            self.folder_entry.set_placeholder_text(view.default)
            self.folder_note.set_label(view.note)
            self._show_interval()
            for key, spin in (
                (KEY_IDLE_TIMEOUT_SECONDS, self.idle_spin),
                (KEY_LOCK_GRACE_PERIOD_SECONDS, self.grace_spin),
            ):
                spin.set_value(self._model.get(key))
            self.order_drop.set_selected(CHOICES[KEY_ORDER].index(self._model.get(KEY_ORDER)))
            self.scaling_drop.set_selected(CHOICES[KEY_SCALING].index(self._model.get(KEY_SCALING)))
            # set_property, not set_active: the D11 scan flags that name (a screensaver call too)
            self.pan_switch.set_property("active", self._model.get(KEY_PAN_PORTRAIT_IMAGES))
        finally:
            self._updating = False

    def _show_interval(self) -> None:
        """Put the slider, the big HH:MM:SS line and the caption to the stored slide interval.
        Only shows: a stored value that is not a step stays as it is (the slider sits at the
        nearest step, and ``_updating`` keeps the signal this raises from saving it)."""
        was_updating = self._updating
        self._updating = True
        try:
            view = self._model.interval_view()
            self.interval_scale.set_value(view.position)
            self.interval_total.set_label(view.text)
            self.interval_caption.set_label(view.caption)
        finally:
            self._updating = was_updating

    def _report(self, result) -> None:
        """Say what happened to the last change; a refused one puts the fields back."""
        self.status.set_label(result.message)
        if not result.ok:
            self.refresh()

    # -- changes made in the window ------------------------------------------------------------

    def _on_int(self, key: str, spin: Gtk.SpinButton) -> None:
        if not self._updating:
            self._report(self._model.set_int(key, spin.get_value_as_int()))

    def _on_interval(self) -> None:
        """The slider moved (by the user): snap it to its step and save that step.

        A slider set from inside its own handler is announced again after the handler ended, when
        the ``_updating`` guard is down (measured). That echo sits on a step and the step is what
        is stored by then, so it is dropped here: nothing is saved twice and "Saved." does not
        replace a message."""
        if self._updating:
            return
        raw = int(round(self.interval_scale.get_value()))
        snapped = snap_interval_position(raw)
        if snapped != raw:
            self._move_interval_slider(snapped)
        stored = self._model.interval_view()
        if stored.on_scale and stored.position == snapped:
            return
        self._report(self._model.set_interval_position(snapped))
        self._show_interval()

    def _move_interval_slider(self, position: int) -> None:
        was_updating = self._updating
        self._updating = True
        try:
            self.interval_scale.set_value(position)
        finally:
            self._updating = was_updating

    def _step_interval(self, steps: int) -> None:
        """Move the slider *steps* steps of the scale; this saves like a drag does."""
        current = snap_interval_position(int(round(self.interval_scale.get_value())))
        self.interval_scale.set_value(step_interval_position(current, steps))

    def _on_interval_key(self, _controller, keyval, _keycode, _state) -> bool:
        steps = {
            Gdk.KEY_Left: -1,
            Gdk.KEY_Down: -1,
            Gdk.KEY_Right: 1,
            Gdk.KEY_Up: 1,
            Gdk.KEY_Page_Down: -5,
            Gdk.KEY_Page_Up: 5,
        }
        if keyval in steps:
            self._step_interval(steps[keyval])
            return True
        if keyval in (Gdk.KEY_Home, Gdk.KEY_End):
            self._step_interval(
                -len(INTERVAL_STOPS) if keyval == Gdk.KEY_Home else len(INTERVAL_STOPS)
            )
            return True
        return False

    def _on_interval_wheel(self, _controller, _dx, dy) -> bool:
        if dy:
            self._step_interval(1 if dy < 0 else -1)
        return True

    def _on_choice(self, key: str) -> None:
        if self._updating:
            return
        drop = self.order_drop if key == KEY_ORDER else self.scaling_drop
        index = drop.get_selected()
        if 0 <= index < len(CHOICES[key]):
            self._report(self._model.set_choice(key, CHOICES[key][index]))

    def _on_pan(self) -> None:
        if not self._updating:
            self._report(self._model.set_pan_portrait_images(self.pan_switch.get_active()))

    def _commit_folder(self) -> None:
        """Save the folder field if it holds something other than what is stored."""
        if self._updating:
            return
        text = self.folder_entry.get_text()
        if text.strip() == self._model.folder_view().text:
            return
        self._report(self._model.set_folder(text))

    def _browse(self) -> None:
        chooser = Gtk.FileChooserNative(
            title=_("Choose the picture folder"),
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label=_("Select"),
            cancel_label=_("Cancel"),
        )
        try:
            chooser.set_current_folder(Gio.File.new_for_path(self._model.chooser_start_folder()))
        except GLib.Error:
            pass  # the chooser opens where it likes
        chooser.connect("response", self._on_folder_chosen)
        self._chooser = chooser  # a native dialog is closed when its last reference goes
        chooser.show()

    def _on_folder_chosen(self, chooser, response) -> None:
        if response == Gtk.ResponseType.ACCEPT and chooser.get_file() is not None:
            self.choose_folder(chooser.get_file().get_path())
        self._chooser = None

    def choose_folder(self, path: Optional[str]) -> None:
        """A folder was picked in the chooser: show it and save it."""
        if path:
            self.folder_entry.set_text(path)
            self._report(self._model.set_folder(path))

    # -- the preview -----------------------------------------------------------------------------

    def _start_preview(self) -> None:
        if self._preview is not None:
            return
        self._commit_folder()
        try:
            # Its own Settings object: the source keeps a change listener on the one it is given,
            # and it should not outlive the preview on the window's own.
            settings = Settings()
            source = build_source(settings)
            source.start()
            controller = start_preview(settings, source, self.get_application())
        except Exception:
            _LOG.exception("[slideshow] the preview could not be started")
            self.status.set_label(_("The preview could not be started, see the log."))
            return
        if not controller.running:  # no monitor
            source.stop()
            self.status.set_label(_("There is no monitor to show the preview on."))
            return
        self._preview = (controller, source)
        self.preview_button.set_sensitive(False)
        self.status.set_label(_("Preview running: press any key or move the mouse to end it."))
        controller.connect_stopped(lambda _reason: GLib.idle_add(self._preview_finished))

    def _preview_finished(self) -> bool:
        if self._preview is not None:
            _controller, source = self._preview
            self._preview = None
            source.stop()
            self.preview_button.set_sensitive(True)
            self.status.set_label("")
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _window) -> bool:
        if self._preview is not None:
            self._preview[0].stop("settings window closed")
            self._preview_finished()
        self._closed = True  # the settings listener stays, but does nothing from now on
        return False


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m slideshow_lock.preferences",
        description=_("Change the slideshow settings."),
    )
    parser.add_argument("--debug", action="store_true", help=_("log every step"))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    schema = Gio.SettingsSchemaSource.get_default()
    if schema is None or schema.lookup(APP_ID, True) is None:
        print(
            _(
                "The settings schema is not installed. Compile it and point GSettings at it:\n"
                "  glib-compile-schemas data/\n"
                "  GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences"
            ),
            file=sys.stderr,
        )
        return 2

    app = Gtk.Application(application_id=APP_ID + ".Preferences")

    def on_activate(application: Gtk.Application) -> None:
        window = PreferencesWindow(Settings(), application)
        window.present()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
