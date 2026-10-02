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
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from slideshow_lock import APP_ID  # noqa: E402

_LOG = logging.getLogger(__name__)

KEY_IDLE_TIMEOUT_SECONDS = "idle-timeout-seconds"
KEY_LOCK_GRACE_PERIOD_SECONDS = "lock-grace-period-seconds"
KEY_PICTURE_FOLDER = "picture-folder"
KEY_SLIDE_INTERVAL_SECONDS = "slide-interval-seconds"
KEY_ORDER = "order"
KEY_SCALING = "scaling"

#: Subfolder name under the XDG pictures directory used when the
#: "picture-folder" key is left at its default, empty value (D25). Matches
#: the package/unit name on purpose, so the user sees the same word in
#: `dnf install`, `journalctl` and the folder itself.
DEFAULT_PICTURE_SUBDIR = "slideshow-lock"


def default_picture_folder() -> str:
    """Return the XDG-derived default picture folder (D25).

    Not baked into the gschema default, because the XDG pictures directory
    depends on the user's home and locale (`~/.config/user-dirs.dirs`) and
    cannot be a static value in the schema XML.
    """
    base = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    if not base:
        # GLib itself falls back to "$HOME/Pictures" when XDG user dirs are
        # not configured; mirror that rather than invent a different default.
        base = os.path.join(GLib.get_home_dir(), "Pictures")
    return os.path.join(base, DEFAULT_PICTURE_SUBDIR)


class Settings:
    """Thin, validating wrapper around the `io.github.sakkmesterke.SlideshowLock`
    GSettings schema."""

    def __init__(self) -> None:
        self._settings = Gio.Settings.new(APP_ID)

    # -- live reload (acceptance criterion 1) ------------------------------

    def connect_changed(self, callback: Callable[[str], None]) -> int:
        """Invoke *callback(key)* whenever any key changes, no restart needed.

        Returns the GObject signal handler id (pass to `disconnect` to stop
        listening).
        """
        return self._settings.connect("changed", lambda _settings, key: callback(key))

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
