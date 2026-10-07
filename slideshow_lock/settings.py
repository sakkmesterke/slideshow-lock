"""GSettings-backed configuration for the slideshow-lock service (CORE-3).

Storage choice: GSettings, not TOML (decided default, see brief B4). This
module is a thin, validating wrapper around a single `Gio.Settings` instance
bound to the schema whose id is :data:`slideshow_lock.APP_ID` (D17).

Scope note: the brief's "on/off switch" (section 5) is deliberately NOT a key
here. Per D4 it is implemented as a systemd user unit enable/disable call
(see docs/architecture/dbus-state-machine.md, section 3.7, `UnitControl`),
wired up by the preferences window and the state machine, not by this layer.

Live reload: `Gio.Settings` emits a `changed` signal for every write that
reaches the backend (regardless of which process, or which `Gio.Settings`
instance in this process, made it), which is how acceptance criterion 1 (no
restart needed) is met: callers subscribe via :meth:`Settings.connect_changed`
instead of polling.

Invalid values: GSettings itself enforces the schema's `<range>` and
`<choices>` constraints at the `set_*` call -- an out-of-range or not-listed
value is rejected (the call returns `False` and the stored value is left
untouched). This module never writes around that guarantee; it only adds a
WARNING-level log entry (acceptance criterion 2, and the "[config]" event tag
from docs/logging-and-lifecycle.md section 1).
"""

from __future__ import annotations

import logging
import os
from typing import Callable, List

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from slideshow_lock import APP_ID  # noqa: E402
from slideshow_lock.transitions import clamp_duration, clean, is_valid  # noqa: E402

_LOG = logging.getLogger(__name__)

KEY_IDLE_TIMEOUT_SECONDS = "idle-timeout-seconds"
KEY_LOCK_GRACE_PERIOD_SECONDS = "lock-grace-period-seconds"
KEY_PICTURE_FOLDER = "picture-folder"
KEY_SLIDE_INTERVAL_SECONDS = "slide-interval-seconds"
KEY_ORDER = "order"
KEY_SCALING = "scaling"
KEY_PAN_PORTRAIT_IMAGES = "pan-portrait-images"
KEY_SHOW_SCREENSHOTS = "show-screenshots"
KEY_FIRST_RUN_DONE = "first-run-done"
KEY_TRANSITIONS = "transitions"
KEY_TRANSITION_ORDER = "transition-order"
KEY_TRANSITION_DURATION = "transition-duration"


def default_picture_folder() -> str:
    """Return the XDG-derived default picture folder (D25).

    This is the user's own system pictures folder (`~/Képek` on a Hungarian
    system), not a subfolder of it: it is walked recursively, so a user who
    already keeps pictures there sees them with nothing to create or configure.

    Not baked into the gschema default, because the XDG pictures directory
    depends on the user's home and locale (`~/.config/user-dirs.dirs`: on a
    Hungarian system it is `~/Képek`) and cannot be a static value in the
    schema XML. The key's default is the empty string, which means this.

    The XDG pictures directory is used when it is configured. Two cases fall
    back to `$HOME/Pictures`: it is not configured (GLib returns None, measured
    with GLib 2.74), and it is the home directory itself, which is how the XDG
    user-dirs convention says "switched off" (GLib returns the home directory
    as it is, measured). Either way the folder used is never the home
    directory itself, which would put every file the user owns under the walk.

    GLib reads `user-dirs.dirs` once per process and keeps it: a change made
    while a process runs is seen after a restart.
    """
    home = GLib.get_home_dir()
    base = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    if not base or os.path.normpath(base) == os.path.normpath(home):
        base = os.path.join(home, "Pictures")
    return base


class Settings:
    """Thin, validating wrapper around the `io.github.sakkmesterke.SlideshowLock`
    GSettings schema."""

    def __init__(self) -> None:
        self._settings = Gio.Settings.new(APP_ID)
        self._changed_handlers: List[int] = []

    # -- live reload (acceptance criterion 1) ------------------------------

    def connect_changed(self, callback: Callable[[str], None]) -> int:
        """Invoke *callback(key)* whenever any key changes, no restart needed.

        Returns the GObject signal handler id. :meth:`disconnect_changed` stops every callback
        given here at once.
        """
        handler = self._settings.connect("changed", lambda _settings, key: callback(key))
        self._changed_handlers.append(handler)
        return handler

    def disconnect_changed(self) -> None:
        """Stop every callback given to :meth:`connect_changed` on this object. Idempotent."""
        handlers, self._changed_handlers = self._changed_handlers, []
        for handler in handlers:
            self._settings.disconnect(handler)

    # -- idle-timeout-seconds ------------------------------------------------

    def get_idle_timeout_seconds(self) -> int:
        return self._settings.get_uint(KEY_IDLE_TIMEOUT_SECONDS)

    def set_idle_timeout_seconds(self, value: int) -> bool:
        return self._set_uint(KEY_IDLE_TIMEOUT_SECONDS, value)

    # -- lock-grace-period-seconds -------------------------------------------

    def get_lock_grace_period_seconds(self) -> int:
        return self._settings.get_uint(KEY_LOCK_GRACE_PERIOD_SECONDS)

    def set_lock_grace_period_seconds(self, value: int) -> bool:
        return self._set_uint(KEY_LOCK_GRACE_PERIOD_SECONDS, value)

    # -- slide-interval-seconds ----------------------------------------------

    def get_slide_interval_seconds(self) -> int:
        return self._settings.get_uint(KEY_SLIDE_INTERVAL_SECONDS)

    def set_slide_interval_seconds(self, value: int) -> bool:
        return self._set_uint(KEY_SLIDE_INTERVAL_SECONDS, value)

    # -- order ---------------------------------------------------------------

    def get_order(self) -> str:
        return self._settings.get_string(KEY_ORDER)

    def set_order(self, value: str) -> bool:
        return self._set_string(KEY_ORDER, value)

    # -- scaling ---------------------------------------------------------------

    def get_scaling(self) -> str:
        return self._settings.get_string(KEY_SCALING)

    def set_scaling(self, value: str) -> bool:
        return self._set_string(KEY_SCALING, value)

    # -- pan-portrait-images ----------------------------------------------------

    def get_pan_portrait_images(self) -> bool:
        return self._settings.get_boolean(KEY_PAN_PORTRAIT_IMAGES)

    def set_pan_portrait_images(self, value: bool) -> bool:
        return self._set_boolean(KEY_PAN_PORTRAIT_IMAGES, value)

    # -- show-screenshots ------------------------------------------------------------

    def get_show_screenshots(self) -> bool:
        return self._settings.get_boolean(KEY_SHOW_SCREENSHOTS)

    def set_show_screenshots(self, value: bool) -> bool:
        return self._set_boolean(KEY_SHOW_SCREENSHOTS, value)

    # -- transitions ---------------------------------------------------------------

    def get_transitions(self) -> List[str]:
        """The chosen transitions, each once, in the stored order. A name that is not one of the
        ten (written by another version, or by hand) is left out and logged once; an empty list is
        a real choice (no transition)."""
        return clean(self._settings.get_strv(KEY_TRANSITIONS))

    def set_transitions(self, value) -> bool:
        """Save the list of transition names. Only a list or tuple of known names, none twice, is
        saved: the schema cannot list the choices of an array key, so this is the check."""
        ok = (
            isinstance(value, (list, tuple))
            and all(is_valid(name) for name in value)
            and len(set(value)) == len(value)
            and self._settings.set_strv(KEY_TRANSITIONS, list(value))
        )
        if not ok:
            _LOG.warning(
                "[config] invalid value '%s' for key '%s' rejected, keeping '%s'",
                value,
                KEY_TRANSITIONS,
                self.get_transitions(),
            )
        return ok

    def get_transition_order(self) -> str:
        return self._settings.get_string(KEY_TRANSITION_ORDER)

    def set_transition_order(self, value: str) -> bool:
        return self._set_string(KEY_TRANSITION_ORDER, value)

    def get_transition_duration(self) -> float:
        """Seconds a transition takes, from 0.2 to 5.0. A stored value outside the range (or not a
        number) is brought into it and logged once, like an unknown transition name."""
        return clamp_duration(self._settings.get_double(KEY_TRANSITION_DURATION))

    def set_transition_duration(self, value: float) -> bool:
        # A bool is a number to Python but not a duration, and GSettings accepts NaN into a range
        # (measured), so the number is checked here; the schema's range does the rest.
        ok = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == value
            and self._settings.set_double(KEY_TRANSITION_DURATION, float(value))
        )
        if not ok:
            _LOG.warning(
                "[config] invalid value '%s' for key '%s' rejected, keeping '%s'",
                value,
                KEY_TRANSITION_DURATION,
                self.get_transition_duration(),
            )
        return ok

    # -- picture-folder (acceptance criterion 4 / D25 / brief 3.7) ----------

    def get_picture_folder(self) -> str:
        """Return the configured picture folder, resolving the XDG default.

        A missing folder is not an error (brief 3.7): this logs a single
        WARNING with the "[slideshow-dir]" event tag and still returns the
        path, so the caller (the slideshow layer) keeps running and simply
        finds no images.
        """
        raw = self._settings.get_string(KEY_PICTURE_FOLDER)
        path = raw if raw else default_picture_folder()
        if not os.path.isdir(path):
            _LOG.warning(
                "[slideshow-dir] picture folder '%s' is missing, slideshow will not start, "
                "service keeps running",
                path,
            )
        return path

    def set_picture_folder(self, value: str) -> bool:
        return self._set_string(KEY_PICTURE_FOLDER, value)

    # -- first-run-done (the login start opens the settings window once) ---------------

    def get_first_run_done(self) -> bool:
        return self._settings.get_boolean(KEY_FIRST_RUN_DONE)

    def set_first_run_done(self, value: bool) -> bool:
        return self._set_boolean(KEY_FIRST_RUN_DONE, value)

    def has_chosen_picture_folder(self) -> bool:
        """True if the user has stored a value for the picture folder (any value, an empty one
        included): the schema default does not count, so a user who never chose is told apart from
        one who chose."""
        return self._settings.get_user_value(KEY_PICTURE_FOLDER) is not None

    def uses_default_picture_folder(self) -> bool:
        """True if the folder in use is the default one: the key is empty, or holds the default's
        own path (compared as written, normalised, so a trailing slash does not matter). Does not
        go through ``get_picture_folder()``, so a missing folder is not a warning here."""
        raw = self._settings.get_string(KEY_PICTURE_FOLDER)
        return not raw or os.path.normpath(raw) == os.path.normpath(default_picture_folder())

    # -- shared validation plumbing ------------------------------------------

    def _set_uint(self, key: str, value: int) -> bool:
        ok = self._settings.set_uint(key, value)
        if not ok:
            fallback = self._settings.get_uint(key)
            _LOG.warning(
                "[config] invalid value '%s' for key '%s' rejected, keeping '%s'",
                value,
                key,
                fallback,
            )
        return ok

    def _set_string(self, key: str, value: str) -> bool:
        ok = self._settings.set_string(key, value)
        if not ok:
            fallback = self._settings.get_string(key)
            _LOG.warning(
                "[config] invalid value '%s' for key '%s' rejected, keeping '%s'",
                value,
                key,
                fallback,
            )
        return ok

    def _set_boolean(self, key: str, value: bool) -> bool:
        # `Gio.Settings.set_boolean` takes any truthy Python object, so "no", 0 or [] would be
        # saved as a boolean. Only a real bool counts as a valid value.
        ok = isinstance(value, bool) and self._settings.set_boolean(key, value)
        if not ok:
            fallback = self._settings.get_boolean(key)
            _LOG.warning(
                "[config] invalid value '%s' for key '%s' rejected, keeping '%s'",
                value,
                key,
                fallback,
            )
        return ok
