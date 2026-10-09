# SPDX-FileCopyrightText: 2026 TrenSoft
# SPDX-License-Identifier: GPL-3.0-or-later

"""The settings window (UI-1): the settings in one libadwaita window, in titled groups of rows.

    glib-compile-schemas data/
    GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preferences

One field per setting, saved the moment it is changed (the service picks changes up live, so
there is no "Save" button). A change is checked when it is made (``preferences_model.Draft``) and
stored at once; the text field of the folder counts as changed when it is left or Enter is pressed,
not at every key. What the fields accept and what counts as saved is decided in
``slideshow_lock.preferences_model``, which has no GTK in it and is tested by the CI; this module
only puts that on the screen. A refused value, or one that could not be stored, puts the field back
to the stored value and says why; a save says "Saved." only for what read back.

The "Preview" button runs the slideshow preview of CORE-2 (``preview_app.start_preview``) on the
values in the window, which are the stored ones: nothing is left unsaved (a folder typed and not yet
confirmed is stored first). It never locks the session (D11); any key, click, scroll or mouse
movement ends it. There is no on/off switch here: that goes through the systemd user unit (D4),
which is not part of this window.

The window has no scrolled area: the groups sit in two columns and the whole window shows at its
natural size, which fits a 1366 x 768 screen (``tools/wayland-smoke/smoke_preferences.py`` measures
it).

The slide interval is one slider and a big HH:MM:SS line above it; it is stored in seconds in
the same key as before, from 00:00:01 to 23:59:59. The slider is four equal quarters: every second
from 1 to 10, every 5 seconds from 10 to 60, round minutes from 1 to 60, round hours from 1 to 24
(see ``INTERVAL_STOPS``); an arrow key moves one step of that scale. A stored value that is not a
step is shown at the nearest one and stays stored until the user moves the slider. The idle time
and the lock grace period are plain number fields.

libadwaita, and only what exists in libadwaita 1.2 (``Adw.ApplicationWindow``, ``HeaderBar``,
``PreferencesGroup``, ``ActionRow``, ``ComboRow``, ``EntryRow``): that is
what the window was run with, and what EL10's libadwaita (1.6) has as well. Newer rows
(``SwitchRow``, ``SpinRow``) and ``Adw.PreferencesDialog`` are not used. The one exception is the
About window of the main menu in the header bar, which takes ``Adw.AboutDialog`` where libadwaita
has it (1.5 and later) and ``Adw.AboutWindow`` otherwise (``slideshow_lock/about.py``). The folder
chooser is ``Gtk.FileChooserNative``, which exists in every GTK 4 (``Gtk.FileDialog`` needs 4.10;
the GTK 4.8 this was built and measured on has none). Not covered by the tests of the CI: what this
module draws (checked with ``tools/wayland-smoke/smoke_preferences.py``, and by eye on the reference
machine).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, List, Optional

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from slideshow_lock import APP_ID, _, about, i18n  # noqa: E402
from slideshow_lock.preferences_model import (  # noqa: E402
    CHOICES,
    DURATION_MAX_SECONDS,
    DURATION_MIN_SECONDS,
    DURATION_STEP_SECONDS,
    INT_RANGES,
    INTERVAL_SLIDER_MAX,
    INTERVAL_STOPS,
    TRANSITION_CHOICES,
    Draft,
    PreferencesModel,
    interval_position_for_seconds,
    snap_interval_position,
    step_interval_position,
)
from slideshow_lock.preview_app import (  # noqa: E402
    SessionSettings,
    build_source,
    start_preview,
)
from slideshow_lock.settings import (  # noqa: E402
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_SCALING,
    KEY_SHOW_SCREENSHOTS,
    KEY_TRANSITION_DURATION,
    KEY_TRANSITIONS,
    Settings,
)
from slideshow_lock.version import program_version  # noqa: E402

_LOG = logging.getLogger(__name__)

MARGIN = 18  # the window's edge
COLUMN_SPACING = 24  # between the two columns of groups
COLUMN_WIDTH = 520  # the widest a column gets: the text of a row wraps there
ROW_SPACING = 8
DURATION_SCALE_WIDTH = 260  # the transition-length slider, in pixels


def donate_button_visible() -> bool:
    """Whether the Donate button is shown: only when the donation address of ``about.py`` is one
    that ``about.donation_link()`` accepts (the one check of the program), otherwise there is no
    button."""
    return about.donation_link() is not None


def version_text() -> str:
    """The small line at the bottom right: the version of the package and the maker, "1.0.10 by
    TrenSoft". Not a translated string: a number and a name."""
    return f"{program_version()} by {about.DEVELOPER}"


def _choice_labels():
    """What the drop-downs list, in the order of ``CHOICES`` (the values stay English)."""
    return {
        KEY_ORDER: (_("Random"), _("By file name")),
        KEY_SCALING: (_("Fill the screen (crops the picture)"), _("Fit (shows the whole picture)")),
    }


def _transition_labels():
    """What the transition drop-down lists, in the order of ``TRANSITION_CHOICES``: none, the ten
    transitions, the random mix."""
    return (
        _("None (change at once)"),
        _("Cross-fade"),
        _("Fade through black"),
        _("Slide in"),
        _("Push"),
        _("Ken Burns"),
        _("Zoom"),
        _("Wipe"),
        _("Circle reveal"),
        _("Blur"),
        _("Rotate"),
        _("Random mix"),
    )


class PreferencesWindow(Adw.ApplicationWindow):
    """The settings window. *settings* is a ``Settings``; the window reads and writes only through
    ``PreferencesModel`` and its ``Draft``."""

    def __init__(
        self,
        settings: Settings,
        application: Optional[Gtk.Application] = None,
        before_preview: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(title=_("Slideshow Lock settings"), application=application)
        self._settings = settings
        self._before_preview = before_preview  # run by the Preview button, given by the caller
        self._model = PreferencesModel(settings)
        self._draft = Draft(self._model)  # an edit is in it only for the moment it is stored
        self._updating = False  # True while the fields are being set from the values in effect
        self._preview = None  # (controller, source, settings) while the preview runs
        self._chooser = None
        self._closed = False

        # -- pictures: folder, order, scaling, pan -----------------------------------------
        self.folder_row = Adw.EntryRow(title=_("Picture folder"))
        self.folder_row.connect("entry-activated", lambda _row: self._commit_folder())
        focus = Gtk.EventControllerFocus()
        focus.connect("notify::contains-focus", self._on_folder_focus)
        self.folder_row.add_controller(focus)
        self.browse_button = Gtk.Button(label=_("Browse..."), valign=Gtk.Align.CENTER)
        self.browse_button.connect("clicked", lambda _button: self._browse())
        self.folder_row.add_suffix(self.browse_button)

        labels = _choice_labels()
        self.order_drop = self._combo(_("Picture order"), labels[KEY_ORDER])
        self.order_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_ORDER))
        self.scaling_drop = self._combo(_("Scaling"), labels[KEY_SCALING])
        self.scaling_drop.connect("notify::selected", lambda *_a: self._on_choice(KEY_SCALING))
        self.pan_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.pan_switch.connect("notify::active", lambda *_a: self._on_pan())
        pan_row = Adw.ActionRow(
            title=_("Scroll tall pictures"),
            subtitle=_(
                "Portrait pictures on a landscape screen move slowly from top to "
                "bottom (only with Fill). Off by default: it uses more battery."
            ),
        )
        pan_row.add_suffix(self.pan_switch)
        pan_row.set_activatable_widget(self.pan_switch)

        self.screenshots_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.screenshots_switch.connect("notify::active", lambda *_a: self._on_screenshots())
        screenshots_row = Adw.ActionRow(title=_("Show screenshots"))
        screenshots_row.add_suffix(self.screenshots_switch)
        screenshots_row.set_activatable_widget(self.screenshots_switch)

        self.pictures_group = Adw.PreferencesGroup(title=_("Pictures"))
        for row in (
            self.folder_row,
            self.order_drop,
            self.scaling_drop,
            pan_row,
            screenshots_row,
        ):
            self.pictures_group.add(row)

        # -- transitions: how one picture changes into the next ---------------------------------
        self.transition_drop = self._combo(
            _("Between pictures"),
            _transition_labels(),
            _(
                'How a picture changes into the next. "Random mix" picks one of eight at each '
                "change (not Blur and Ken Burns). Without desktop animations the pictures change "
                "at once."
            ),
        )
        self.transition_drop.connect("notify::selected", lambda *_a: self._on_transition())
        # The length of a change: one value for every transition. The engine cuts it to half of the
        # slide interval at most; the slider always shows the stored value (docs/preferences.md).
        self.duration_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            DURATION_MIN_SECONDS,
            DURATION_MAX_SECONDS,
            DURATION_STEP_SECONDS,
        )
        self.duration_scale.set_digits(1)
        self.duration_scale.set_round_digits(1)
        self.duration_scale.set_draw_value(True)
        self.duration_scale.set_value_pos(Gtk.PositionType.LEFT)
        self.duration_scale.set_size_request(DURATION_SCALE_WIDTH, -1)
        self.duration_scale.set_valign(Gtk.Align.CENTER)
        self.duration_scale.connect("value-changed", lambda _scale: self._on_duration())
        duration_row = Adw.ActionRow(
            title=_("Transition length"),
            subtitle=_(
                "How long one change takes, the same for every transition. It is never longer "
                "than half of the time a picture is shown."
            ),
        )
        duration_row.add_suffix(self._with_unit(self.duration_scale, _("seconds")))
        transitions_group = Adw.PreferencesGroup(title=_("Transitions"))
        transitions_group.add(self.transition_drop)
        transitions_group.add(duration_row)

        # -- start the slideshow: the idle time ---------------------------------------------
        self.idle_spin = self._spin(KEY_IDLE_TIMEOUT_SECONDS)
        idle_row = Adw.ActionRow(
            title=_("Idle time"), subtitle=_("How long without input before the slideshow starts.")
        )
        idle_row.add_suffix(self._with_unit(self.idle_spin, _("seconds")))
        idle_group = Adw.PreferencesGroup(title=_("Start the slideshow"))
        idle_group.add(idle_row)

        # -- timing: the slide interval on one slider; the grace period -------------------------
        self.interval_total = Gtk.Label(label="00:00:01", halign=Gtk.Align.CENTER)
        self.interval_total.add_css_class("title-1")
        self.interval_total.add_css_class("numeric")
        self.interval_caption = Gtk.Label(
            halign=Gtk.Align.CENTER, justify=Gtk.Justification.CENTER, wrap=True
        )
        self.interval_caption.add_css_class("dim-label")
        self.interval_scale = self._interval_slider()
        interval_title = Gtk.Label(label=_("Show each picture for"), xalign=0)
        interval_title.add_css_class("heading")
        interval_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ROW_SPACING)
        for margin in ("top", "bottom", "start", "end"):
            getattr(interval_body, "set_margin_" + margin)(12)
        for widget in (
            interval_title,
            self.interval_total,
            self.interval_caption,
            self.interval_scale,
        ):
            interval_body.append(widget)
        interval_row = Adw.PreferencesRow(title=_("Show each picture for"), activatable=False)
        interval_row.set_child(interval_body)
        self.grace_spin = self._spin(KEY_LOCK_GRACE_PERIOD_SECONDS)
        grace_row = Adw.ActionRow(
            title=_("Lock grace period"),
            subtitle=_(
                "Input sooner than this after the slideshow starts does not lock the "
                "session (strictly sooner; 0 means every input locks)."
            ),
        )
        grace_row.add_suffix(self._with_unit(self.grace_spin, _("seconds")))
        timing_group = Adw.PreferencesGroup(title=_("Timing"))
        timing_group.add(interval_row)
        timing_group.add(grace_row)

        # Two columns of groups side by side and no scrolled area: the window is as tall as the
        # taller column, so everything shows at the natural size and 1366 x 768 is enough.
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=COLUMN_SPACING)
        columns.set_homogeneous(True)
        for margin in ("top", "bottom", "start", "end"):
            getattr(columns, "set_margin_" + margin)(MARGIN)
        for groups in ((self.pictures_group, idle_group), (transitions_group, timing_group)):
            column = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=MARGIN,
                hexpand=True,
                valign=Gtk.Align.START,
            )
            for group in groups:
                column.append(group)
            # Without the clamp a row's long text would not wrap and the window would be as wide as
            # its longest line.
            clamp = Adw.Clamp(maximum_size=COLUMN_WIDTH, tightening_threshold=COLUMN_WIDTH)
            clamp.set_child(column)
            columns.append(clamp)

        # -- the header bar, the Preview button and the status -------------------------------
        header = Adw.HeaderBar()  # the window controls only: no menu

        self.preview_button = Gtk.Button(label=_("Preview"), valign=Gtk.Align.CENTER)
        self.preview_button.connect("clicked", lambda _button: self._start_preview())
        # Next to Preview, and as plain: opens the donation page in the browser. Not shown without
        # a valid address (``about.donation_link()``).
        self.donate_button = Gtk.Button(
            label=_("Donate"), valign=Gtk.Align.CENTER, visible=donate_button_visible()
        )
        self.donate_button.connect("clicked", lambda _button: self._open_donation())
        self.status = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.status.add_css_class("dim-label")
        footer = Gtk.Box(spacing=ROW_SPACING)
        for margin in ("top", "bottom", "start", "end"):
            getattr(footer, "set_margin_" + margin)(MARGIN if margin in ("start", "end") else 12)
        footer.append(self.preview_button)
        footer.append(self.donate_button)
        footer.append(self.status)
        # The version and the maker, small and faint at the bottom right; the version comes from
        # the package itself.
        self.version_label = Gtk.Label(
            label=version_text(), xalign=1, valign=Gtk.Align.END, selectable=True
        )
        self.version_label.add_css_class("dim-label")
        self.version_label.add_css_class("caption")
        footer.append(self.version_label)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(header)
        content.append(columns)
        content.append(footer)
        self.set_content(content)
        self.connect("close-request", self._on_close_request)
        settings.connect_changed(lambda _key: self.refresh() if not self._closed else None)
        self.refresh()

    # -- building blocks ---------------------------------------------------------------------

    @staticmethod
    def _combo(title: str, labels, subtitle: str = "") -> Adw.ComboRow:
        """A row with a drop-down of *labels*; the selected index is the index of the choice."""
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(list(labels)))
        if subtitle:
            row.set_subtitle(subtitle)
        return row

    @staticmethod
    def _with_unit(widget: Gtk.Widget, unit: str) -> Gtk.Box:
        box = Gtk.Box(spacing=ROW_SPACING, valign=Gtk.Align.CENTER)
        box.append(widget)
        box.append(Gtk.Label(label=unit, xalign=0))
        return box

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
        spin.set_valign(Gtk.Align.CENTER)
        # A number outside the range, or text that is no number, is thrown away and the old value
        # stays (GTK's default would clamp it to the nearest limit). Known corner: an emptied
        # field reads as 0, so it is thrown away for every field but the grace period, whose
        # minimum is 0: there it becomes 0, which is shown and kept alike.
        spin.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
        spin.connect("value-changed", lambda button: self._on_int(key, button))
        return spin

    # -- showing the values in effect: the draft's, otherwise the stored ones -----------------

    def refresh(self) -> None:
        """Put every field to the stored value. Also called when another process changes one."""
        self._updating = True
        try:
            view = self._model.folder_view()
            self.folder_row.set_text(self._draft.folder_text())
            self.pictures_group.set_description(view.note)
            self._show_interval()
            for key, spin in (
                (KEY_IDLE_TIMEOUT_SECONDS, self.idle_spin),
                (KEY_LOCK_GRACE_PERIOD_SECONDS, self.grace_spin),
            ):
                spin.set_value(self._draft.value(key))
            self.order_drop.set_selected(CHOICES[KEY_ORDER].index(self._draft.value(KEY_ORDER)))
            self.scaling_drop.set_selected(
                CHOICES[KEY_SCALING].index(self._draft.value(KEY_SCALING))
            )
            # set_property, not set_active: the D11 scan flags that name (a screensaver call too)
            self.pan_switch.set_property("active", self._draft.value(KEY_PAN_PORTRAIT_IMAGES))
            self.screenshots_switch.set_property("active", self._draft.value(KEY_SHOW_SCREENSHOTS))
            self.transition_drop.set_selected(
                TRANSITION_CHOICES.index(self._draft.value(KEY_TRANSITIONS))
            )
            self.duration_scale.set_value(self._draft.value(KEY_TRANSITION_DURATION))
        finally:
            self._updating = False

    def _show_interval(self) -> None:
        """Put the slider, the big HH:MM:SS line and the caption to the slide interval in effect.
        Only shows: a stored value that is not a step stays as it is (the slider sits at the
        nearest step, and ``_updating`` keeps the signal this raises from keeping it)."""
        was_updating = self._updating
        self._updating = True
        try:
            view = self._draft.interval_view()
            self.interval_scale.set_value(view.position)
            self.interval_total.set_label(view.text)
            self.interval_caption.set_label(view.caption)
        finally:
            self._updating = was_updating

    def _report(self, result) -> None:
        """Store the edit the window has just made (*result* is its check) and say what happened. A
        refused edit, or one that could not be stored, is thrown away and the fields go back to the
        stored values."""
        if result.ok:
            result = self._draft.save()
            if not result.ok:
                self._draft.discard()
        self.status.set_label(result.message)
        if not result.ok:
            self.refresh()

    # -- changes made in the window ------------------------------------------------------------

    def _on_int(self, key: str, spin: Gtk.SpinButton) -> None:
        if not self._updating:
            self._report(self._draft.edit_int(key, spin.get_value_as_int()))

    def _on_duration(self) -> None:
        if not self._updating:
            self._report(self._draft.edit_duration(self.duration_scale.get_value()))

    def _on_interval(self) -> None:
        """The slider moved (by the user): snap it to its step and keep that step.

        A slider set from inside its own handler is announced again after the handler ended, when
        the ``_updating`` guard is down (measured). That echo sits on a step and the step is what
        is kept by then, so it is dropped here: nothing is kept twice and a message is not
        replaced."""
        if self._updating:
            return
        raw = int(round(self.interval_scale.get_value()))
        snapped = snap_interval_position(raw)
        if snapped != raw:
            self._move_interval_slider(snapped)
        current = self._draft.interval_view()
        if current.on_scale and current.position == snapped:
            return
        self._report(self._draft.edit_interval_position(snapped))
        self._show_interval()

    def _move_interval_slider(self, position: int) -> None:
        was_updating = self._updating
        self._updating = True
        try:
            self.interval_scale.set_value(position)
        finally:
            self._updating = was_updating

    def _step_interval(self, steps: int) -> None:
        """Move the slider *steps* steps of the scale; this keeps the value like a drag does."""
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
            self._report(self._draft.edit_choice(key, CHOICES[key][index]))

    def _on_transition(self) -> None:
        if self._updating:
            return
        index = self.transition_drop.get_selected()
        if 0 <= index < len(TRANSITION_CHOICES):
            self._report(self._draft.edit_transition(TRANSITION_CHOICES[index]))

    def _on_pan(self) -> None:
        if not self._updating:
            self._report(self._draft.edit_pan_portrait_images(self.pan_switch.get_active()))

    def _on_screenshots(self) -> None:
        if not self._updating:
            self._report(self._draft.edit_show_screenshots(self.screenshots_switch.get_active()))

    def _on_folder_focus(self, controller, _pspec) -> None:
        if not controller.get_property("contains-focus"):
            self._commit_folder()

    def _commit_folder(self) -> None:
        """Keep the folder field if it holds something other than the folder in effect."""
        if self._updating:
            return
        text = self.folder_row.get_text()
        if text.strip() == self._draft.folder_text():
            return
        self._report(self._draft.edit_folder(text))
        self._show_folder()

    def _show_folder(self) -> None:
        """The field shows the folder as it would be stored (``~`` expanded, a trailing slash
        gone)."""
        was_updating = self._updating
        self._updating = True
        try:
            self.folder_row.set_text(self._draft.folder_text())
        finally:
            self._updating = was_updating

    def _browse(self) -> None:
        chooser = Gtk.FileChooserNative(
            title=_("Choose the picture folder"),
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label=_("Select"),
            cancel_label=_("Cancel"),
        )
        try:
            chooser.set_current_folder(
                Gio.File.new_for_path(self._model.chooser_start_folder(self._draft.folder_text()))
            )
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
        """A folder was picked in the chooser: show it and store it."""
        if path:
            self.folder_row.set_text(path)
            self._report(self._draft.edit_folder(path))
            self._show_folder()

    # -- the donation page -----------------------------------------------------------------------

    def _open_donation(self) -> None:
        """Open the donation address in the browser, after the one check of the address
        (``about.donation_link()``), with Gtk.UriLauncher (GTK 4.10). Nothing is raised: a failure,
        also a GTK without the launcher, is logged and said in the status line."""
        uri = about.donation_link()
        if uri is None:
            return
        try:
            Gtk.UriLauncher(uri=uri).launch(self, None, self._donation_opened)
        except Exception:
            _LOG.exception("[slideshow] the donation page could not be opened")
            self.status.set_label(_("The donation page could not be opened, see the log."))

    def _donation_opened(self, launcher, result) -> None:
        """The answer of the launcher: a failure is logged and said in the status line."""
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            _LOG.warning("[slideshow] the donation page could not be opened: %s", error.message)
            self.status.set_label(_("The donation page could not be opened, see the log."))

    # -- the preview -----------------------------------------------------------------------------

    def _start_preview(self) -> None:
        if self._preview is not None:
            return
        self._commit_folder()
        if self._before_preview is not None:
            try:
                self._before_preview()
            except Exception:  # whatever it was, the preview starts anyway
                _LOG.exception("[slideshow] the step before the preview failed")
        settings = source = None  # what a failed start has to take down again
        try:
            # Its own Settings object: the source keeps a change listener on the one it is given,
            # and it should not outlive the preview on the window's own. Every value of the window
            # is stored (``_report``), so the preview shows what the window shows; nothing is
            # written by the preview itself.
            settings = Settings()
            run_settings = SessionSettings(settings, self._draft.preview_values())
            source = build_source(run_settings)
            source.start()
            controller = start_preview(run_settings, source, self.get_application())
        except Exception:
            _LOG.exception("[slideshow] the preview could not be started")
            self._release_preview(source, settings)
            self.status.set_label(_("The preview could not be started, see the log."))
            return
        if not controller.running:  # no monitor
            self._release_preview(source, settings)
            self.status.set_label(_("There is no monitor to show the preview on."))
            return
        self._preview = (controller, source, settings)
        self.preview_button.set_sensitive(False)
        self.status.set_label(_("Preview running: press any key or move the mouse to end it."))
        controller.connect_stopped(lambda _reason: GLib.idle_add(self._preview_finished))

    @staticmethod
    def _release_preview(source, settings) -> None:
        """Stop a preview's source and drop the change listeners on its own settings. Either may
        be None (the start failed before it was made); a failure is logged, never raised."""
        if source is not None:
            try:
                source.stop()
            except Exception:
                _LOG.exception("[slideshow] stopping the preview source failed")
        if settings is not None:
            try:
                settings.disconnect_changed()
            except Exception:
                _LOG.exception("[slideshow] dropping the preview's change listeners failed")

    def _preview_finished(self) -> bool:
        if self._preview is not None:
            _controller, source, settings = self._preview
            self._preview = None
            self._release_preview(source, settings)
            self.preview_button.set_sensitive(True)
            self.status.set_label("")
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _window) -> bool:
        """Closing stores a folder that was typed and not confirmed, without a question. A value
        that cannot be stored is logged: the window closes all the same (it cannot ask, and a
        window that will not close is worse)."""
        if self._preview is not None:
            self._preview[0].stop("settings window closed")
            self._preview_finished()
        self._commit_folder()
        result = self._draft.save()
        if not result.ok:
            _LOG.warning("[config] the changes could not be saved at close: %s", result.message)
        self._closed = True  # the settings listener stays, but does nothing from now on
        return False


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m slideshow_lock.preferences",
        description=_("Change the slideshow settings."),
    )
    parser.add_argument("--debug", action="store_true", help=_("log every step"))
    return parser.parse_args(argv)


def main(
    argv: Optional[List[str]] = None, before_preview: Optional[Callable[[], None]] = None
) -> int:
    """Run the settings window. *before_preview* is called each time the Preview button is
    pressed, before the preview starts (the lock-side entry point, ``settings_app``, gives it)."""
    language = i18n.setup()  # before the command line is parsed: --help is translated too
    args = _parse(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    i18n.log_status(language)
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

    app = Adw.Application(application_id=APP_ID + ".Preferences")

    def on_activate(application: Gtk.Application) -> None:
        window = PreferencesWindow(Settings(), application, before_preview)
        window.present()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
