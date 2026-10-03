"""What the settings window decides, without GTK (UI-1).

``PreferencesModel`` sits between the window's fields and ``Settings`` (CORE-3): which key a
field is bound to, which values it accepts, what it shows, and whether a value was really
saved. It imports no GTK, so the CI tests it without a display; what the real window does
with it is checked with ``tools/wayland-smoke/smoke_preferences.py`` and, for how it looks,
by eye on the reference machine.

A value is saved the moment it is changed (no "Apply": the service picks changes up live,
CORE-3). Every ``set_*`` returns a ``SaveResult``; the window says "saved" only for ``ok``
and puts the field back to the stored value otherwise, so it never shows a value that did
not reach the settings. The ranges and choices here are the ones of the schema
(``data/*.gschema.xml``); a test compares them, so a change on one side fails loudly.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from slideshow_lock import _
from slideshow_lock.settings import (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    default_picture_folder,
)

#: (minimum, maximum) of the whole-number keys, in seconds. The same as the schema's ranges.
INT_RANGES = {
    KEY_IDLE_TIMEOUT_SECONDS: (1, 86400),
    KEY_LOCK_GRACE_PERIOD_SECONDS: (0, 86400),
    KEY_SLIDE_INTERVAL_SECONDS: (1, 3600),
}

#: The values of the choice keys, in the order the window lists them. The schema's choices.
CHOICES = {
    KEY_ORDER: ("random", "name"),
    KEY_SCALING: ("fill", "fit"),
}

_GETTERS = {
    KEY_IDLE_TIMEOUT_SECONDS: "get_idle_timeout_seconds",
    KEY_LOCK_GRACE_PERIOD_SECONDS: "get_lock_grace_period_seconds",
    KEY_SLIDE_INTERVAL_SECONDS: "get_slide_interval_seconds",
    KEY_ORDER: "get_order",
    KEY_SCALING: "get_scaling",
    KEY_PAN_PORTRAIT_IMAGES: "get_pan_portrait_images",
    KEY_PICTURE_FOLDER: "get_picture_folder",
}

_SETTERS = {key: name.replace("get_", "set_", 1) for key, name in _GETTERS.items()}


class SaveResult(NamedTuple):
    """``ok`` is True only when the value is stored (and reads back as it was written)."""

    ok: bool
    message: str


class FolderView(NamedTuple):
    """The folder field: its text, the default shown as its hint, and the line under it."""

    text: str
    default: str
    note: str


def _saved() -> SaveResult:
    return SaveResult(True, _("Saved."))


class PreferencesModel:
    """The window's view of a ``Settings`` object (or anything with the same getters/setters)."""

    def __init__(self, settings) -> None:
        self._settings = settings

    # -- reading ---------------------------------------------------------------------------

    def get(self, key: str):
        """The stored value of *key* (the folder: the one in use, see ``folder_view``)."""
        return getattr(self._settings, _GETTERS[key])()

    def folder_view(self) -> FolderView:
        """What the folder field shows. One read of the folder, so a missing folder is logged
        once (``Settings.get_picture_folder`` warns), not once per line of the window."""
        folder = self._settings.get_picture_folder()
        default = default_picture_folder()
        if os.path.isdir(folder):
            note = _("In use: %s") % folder
        else:  # a missing folder is not an error: the slideshow just finds no pictures
            note = _("In use: %s (the folder does not exist yet, nothing will be shown)") % folder
        # Empty while the default is in use, so the field can show it as a hint instead of
        # pretending the user typed it.
        return FolderView("" if folder == default else folder, default, note)

    # -- writing ---------------------------------------------------------------------------

    def set_int(self, key: str, value) -> SaveResult:
        low, high = INT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return SaveResult(False, _("Enter a whole number of seconds."))
        if not low <= value <= high:
            return SaveResult(
                False,
                _("The value must be between %(low)d and %(high)d.") % {"low": low, "high": high},
            )
        return self._store(key, value)

    def set_choice(self, key: str, value) -> SaveResult:
        if value not in CHOICES[key]:
            return SaveResult(False, _("This choice is not available."))
        return self._store(key, value)

    def set_pan_portrait_images(self, value) -> SaveResult:
        if not isinstance(value, bool):
            return SaveResult(False, _("This must be on or off."))
        return self._store(KEY_PAN_PORTRAIT_IMAGES, value)

    def set_folder(self, text) -> SaveResult:
        """Save the folder typed or chosen. Empty means the default folder (D25). The folder
        need not exist (brief 3.7); a path that is not absolute, or that is a file, is refused."""
        if not isinstance(text, str):
            return SaveResult(False, _("Enter a folder path."))
        text = text.strip()
        if "\0" in text:
            return SaveResult(False, _("The path contains a character that is not allowed."))
        if text == "":
            return self._store_folder("")
        path = os.path.expanduser(text)
        if not os.path.isabs(path):
            return SaveResult(False, _("Enter an absolute path (it starts with / or ~)."))
        path = os.path.normpath(path)
        if os.path.exists(path) and not os.path.isdir(path):
            return SaveResult(False, _("This path is a file, not a folder."))
        return self._store_folder(path)

    # -- internals -----------------------------------------------------------------------------

    def _store(self, key: str, value) -> SaveResult:
        if not getattr(self._settings, _SETTERS[key])(value):
            return SaveResult(False, _("The value was not accepted, the old one is kept."))
        if (
            getattr(self._settings, _GETTERS[key])() != value
        ):  # saved is saved only if it reads back
            return SaveResult(False, _("The value could not be saved."))
        return _saved()

    def _store_folder(self, path: str) -> SaveResult:
        if not self._settings.set_picture_folder(path):
            return SaveResult(False, _("The folder was not accepted, the old one is kept."))
        expected = path if path else default_picture_folder()
        if self._settings.get_picture_folder() != expected:
            return SaveResult(False, _("The folder could not be saved."))
        return _saved()
