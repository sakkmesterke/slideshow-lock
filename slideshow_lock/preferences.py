"""The settings window (UI-1): the stored settings in one plain GTK 4 window.

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

Plain Gtk widgets, not libadwaita: the CI has no libadwaita typelib. The folder chooser is
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

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import APP_ID, _  # noqa: E402
from slideshow_lock.preferences_model import CHOICES, INT_RANGES, PreferencesModel  # noqa: E402
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

MARGIN = 18


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
        self.set_default_size(640, -1)
        self.set_resizable(False)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_top(MARGIN)
        grid.set_margin_bottom(MARGIN)
        grid.set_margin_start(MARGIN)
        grid.set_margin_end(MARGIN)
        row = 0

        # -- picture folder ----------------------------------------------------------------
        grid.attach(self._label(_("Picture folder")), 0, row, 1, 1)
        self.folder_entry = Gtk.Entry(hexpand=True)
        self.folder_entry.connect("activate", lambda _entry: self._commit_folder())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _controller: self._commit_folder())
        self.folder_entry.add_controller(focus)
        grid.attach(self.folder_entry, 1, row, 1, 1)
        self.browse_button = Gtk.Button(label=_("Browse..."))
        self.browse_button.connect("clicked", lambda _button: self._browse())
        grid.attach(self.browse_button, 2, row, 1, 1)
        row += 1
        self.folder_note = self._hint("")
        grid.attach(self.folder_note, 1, row, 2, 1)
        row += 1

        # -- timing ------------------------------------------------------------------------
        self.idle_spin = self._spin(KEY_IDLE_TIMEOUT_SECONDS)
        row = self._attach_spin(
            grid, row, _("Start the slideshow after"), self.idle_spin, _("seconds without input")
        )
        self.grace_spin = self._spin(KEY_LOCK_GRACE_PERIOD_SECONDS)
        row = self._attach_spin(
            grid,
            row,
            _("Lock grace period"),
            self.grace_spin,
            _("seconds"),
            hint=_(
                "Input sooner than this after the slideshow starts does not lock the session "
                "(strictly sooner; 0 means every input locks)."
            ),
        )
        self.interval_spin = self._spin(KEY_SLIDE_INTERVAL_SECONDS)
        row = self._attach_spin(
            grid, row, _("Show each picture for"), self.interval_spin, _("seconds")
        )

        # -- order, scaling, pan -----------------------------------------------------------
        labels = _choice_labels()
        self.order_drop = Gtk.DropDown.new_from_strings(list(labels[KEY_ORDER]))
        self.order_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_ORDER))
        grid.attach(self._label(_("Picture order")), 0, row, 1, 1)
        grid.attach(self.order_drop, 1, row, 2, 1)
        row += 1
        self.scaling_drop = Gtk.DropDown.new_from_strings(list(labels[KEY_SCALING]))
        self.scaling_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_SCALING))
        grid.attach(self._label(_("Scaling")), 0, row, 1, 1)
        grid.attach(self.scaling_drop, 1, row, 2, 1)
        row += 1
        self.pan_switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.pan_switch.connect("notify::active", lambda *_a: self._on_pan())
        grid.attach(self._label(_("Scroll tall pictures")), 0, row, 1, 1)
        grid.attach(self.pan_switch, 1, row, 2, 1)
        row += 1
        grid.attach(
            self._hint(
                _(
                    "Portrait pictures on a landscape screen move slowly from top to bottom "
                    "(only with Fill). Off by default: it uses more battery."
                )
            ),
            1,
            row,
            2,
            1,
        )
        row += 1

        # -- preview and status ------------------------------------------------------------
        self.preview_button = Gtk.Button(label=_("Preview"), halign=Gtk.Align.START)
        self.preview_button.connect("clicked", lambda _button: self._start_preview())
        grid.attach(self.preview_button, 0, row, 1, 1)
        self.status = self._hint("")
        self.status.set_wrap(True)
        grid.attach(self.status, 1, row, 2, 1)

        self.set_child(grid)
        self.connect("close-request", self._on_close_request)
        settings.connect_changed(lambda _key: self.refresh() if not self._closed else None)
        self.refresh()

    # -- building blocks ---------------------------------------------------------------------

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0, valign=Gtk.Align.CENTER)

    @staticmethod
    def _hint(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=60)
        label.add_css_class("dim-label")
        return label

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

    def _attach_spin(self, grid, row, label, spin, unit, hint=None) -> int:
        grid.attach(self._label(label), 0, row, 1, 1)
        box = Gtk.Box(spacing=8)
        box.append(spin)
        box.append(self._label(unit))
        grid.attach(box, 1, row, 2, 1)
        row += 1
        if hint:
            grid.attach(self._hint(hint), 1, row, 2, 1)
            row += 1
        return row

    # -- showing the stored values -------------------------------------------------------------

    def refresh(self) -> None:
        """Put every field to the stored value. Also called when another process changes one."""
        self._updating = True
        try:
            view = self._model.folder_view()
            self.folder_entry.set_text(view.text)
            self.folder_entry.set_placeholder_text(view.default)
            self.folder_note.set_label(view.note)
            for key, spin in (
                (KEY_IDLE_TIMEOUT_SECONDS, self.idle_spin),
                (KEY_LOCK_GRACE_PERIOD_SECONDS, self.grace_spin),
                (KEY_SLIDE_INTERVAL_SECONDS, self.interval_spin),
            ):
                spin.set_value(self._model.get(key))
            self.order_drop.set_selected(CHOICES[KEY_ORDER].index(self._model.get(KEY_ORDER)))
            self.scaling_drop.set_selected(CHOICES[KEY_SCALING].index(self._model.get(KEY_SCALING)))
            # set_property, not set_active: the D11 scan flags that name (a screensaver call too)
            self.pan_switch.set_property("active", self._model.get(KEY_PAN_PORTRAIT_IMAGES))
        finally:
            self._updating = False

    def _report(self, result) -> None:
        """Say what happened to the last change; a refused one puts the fields back."""
        self.status.set_label(result.message)
        if not result.ok:
            self.refresh()

    # -- changes made in the window ------------------------------------------------------------

    def _on_int(self, key: str, spin: Gtk.SpinButton) -> None:
        if not self._updating:
            self._report(self._model.set_int(key, spin.get_value_as_int()))

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
        current = self._model.folder_view()
        folder = current.text or current.default
        try:
            if folder and Gio.File.new_for_path(folder).query_exists(None):
                chooser.set_file(Gio.File.new_for_path(folder))
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
