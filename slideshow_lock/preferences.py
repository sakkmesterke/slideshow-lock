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

The slide interval is three sliders (hours, minutes, seconds) and the resulting HH:MM:SS line;
it is stored in seconds in the same key as before, from 00:00:01 to 23:59:59. The idle time and
the lock grace period are plain number fields.

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
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    PreferencesModel,
    format_hms,
    split_hms,
)
from slideshow_lock.preview_app import build_source, start_preview  # noqa: E402
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
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
.sl-value { font-feature-settings: "tnum"; }
.sl-time { font-size: 1.5em; font-weight: bold; font-feature-settings: "tnum"; }
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

        # -- timing: the slide interval as hours, minutes, seconds; the grace period -----------
        self.interval_total = Gtk.Label(
            label=format_hms(INTERVAL_MIN_SECONDS), valign=Gtk.Align.CENTER
        )
        self.interval_total.add_css_class("sl-time")
        self.interval_hours = self._slider(23)
        self.interval_minutes = self._slider(59)
        self.interval_seconds = self._slider(59)
        self.grace_spin = self._spin(KEY_LOCK_GRACE_PERIOD_SECONDS)
        timing_rows = [
            self._row(
                _("Show each picture for"),
                _("How long a picture stays before the next one, from %(low)s to %(high)s.")
                % {
                    "low": format_hms(INTERVAL_MIN_SECONDS),
                    "high": format_hms(INTERVAL_MAX_SECONDS),
                },
                control=self.interval_total,
            )
        ]
        for title, slider in (
            (_("Hours"), self.interval_hours),
            (_("Minutes"), self.interval_minutes),
            (_("Seconds"), self.interval_seconds),
        ):
            timing_rows.append(self._slider_row(title, slider))
        timing_rows.append(
            self._row(
                _("Lock grace period"),
                _(
                    "Input sooner than this after the slideshow starts does not lock the "
                    "session (strictly sooner; 0 means every input locks)."
                ),
                control=self._with_unit(self.grace_spin, _("seconds")),
            )
        )
        page.append(self._group(_("Timing"), timing_rows))

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

    def _slider_row(self, title: str, slider: Gtk.Scale):
        label = self._label(title)
        label.set_width_chars(10)
        # The value is a label of fixed width, not the scale's own: that one makes a slider with a
        # two-digit value shorter than its neighbours.
        value = Gtk.Label(label="0", xalign=1, valign=Gtk.Align.CENTER, width_chars=3)
        value.add_css_class("sl-value")
        slider.connect("value-changed", lambda scale: value.set_label("%d" % scale.get_value()))
        value.set_label("%d" % slider.get_value())
        box = Gtk.Box(spacing=ROW_SPACING)
        box.append(label)
        box.append(slider)
        box.append(value)
        return self._padded(box)

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

    def _slider(self, high: int) -> Gtk.Scale:
        """One of the interval sliders: whole numbers from 0 to *high*; the row shows the value."""
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, high, 1)
        scale.set_digits(0)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.set_size_request(260, -1)
        scale.connect("value-changed", lambda _scale: self._on_interval())
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
        """Put the three sliders and the HH:MM:SS line to the stored slide interval."""
        was_updating = self._updating
        self._updating = True
        try:
            seconds = self._model.get(KEY_SLIDE_INTERVAL_SECONDS)
            hours, minutes, secs = split_hms(seconds)
            self.interval_hours.set_value(hours)
            self.interval_minutes.set_value(minutes)
            self.interval_seconds.set_value(secs)
            self.interval_total.set_label(format_hms(seconds))
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
        """A slider moved: save the slide interval, then show what is stored (which also puts the
        sliders to 00:00:01 when all three were at zero).

        A slider set from inside its own handler is announced again after the handler ended, when
        the ``_updating`` guard is down (measured). That echo shows what is stored already, so
        it is dropped here: nothing is saved twice and "Saved." does not replace the message."""
        if self._updating:
            return
        shown = (
            int(self.interval_hours.get_value()),
            int(self.interval_minutes.get_value()),
            int(self.interval_seconds.get_value()),
        )
        if shown == split_hms(self._model.get(KEY_SLIDE_INTERVAL_SECONDS)):
            return
        self._report(self._model.set_interval(*shown))
        self._show_interval()

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
            controller = start_preview(settings, source)
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
