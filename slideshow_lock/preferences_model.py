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

#: The slide interval runs from 00:00:01 to 23:59:59, in seconds (the schema's range). Zero is no
#: interval at all. The end of the slider is "24 hours" to the eye but 86399 s (23:59:59) to the
#: settings: a full day, 86400, is not valid.
INTERVAL_MIN_SECONDS = 1
INTERVAL_MAX_SECONDS = 23 * 3600 + 59 * 60 + 59

#: The steps of the slide-interval slider, in seconds. The left half is every second from 1 to 60;
#: the right half, above one minute, snaps to round values (2, 3, 5, 10, 15, 20, 30, 45 minutes,
#: then 1, 2, 3, 4, 6, 8, 12 hours) and ends at the longest interval, 23:59:59 ("24 hours").
#: One minute is the last step of the left half only: it is not on the scale twice. Whole hours
#: from 1 to 24 would be 24 steps for a half that is meant to be coarse; the list above is kept
#: because it has the round values people ask for, and it needs no more room.
INTERVAL_STOPS = (
    *range(1, 61),
    *(m * 60 for m in (2, 3, 5, 10, 15, 20, 30, 45)),
    *(h * 3600 for h in (1, 2, 3, 4, 6, 8, 12)),
    INTERVAL_MAX_SECONDS,
)

#: Where each step sits on the slider, which runs 0 to ``INTERVAL_SLIDER_MAX``. The left half
#: (0-240) holds the 60 one-second steps 4 apart; the right half (240-480) holds the 16 steps
#: above one minute, 15 apart, the last one at the very end. The slider is therefore half seconds
#: and half the round values, as asked, and a step of the slider is a step of the scale.
INTERVAL_SLIDER_MAX = 480
INTERVAL_POSITIONS = (
    *(4 * n for n in range(60)),
    *(240 + 15 * (k + 1) for k in range(len(INTERVAL_STOPS) - 60)),
)

#: (minimum, maximum) of the whole-number keys, in seconds. The same as the schema's ranges.
INT_RANGES = {
    KEY_IDLE_TIMEOUT_SECONDS: (1, 86400),
    KEY_LOCK_GRACE_PERIOD_SECONDS: (0, 86400),
    KEY_SLIDE_INTERVAL_SECONDS: (INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS),
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


class IntervalView(NamedTuple):
    """The slide-interval slider: the stored seconds, the position of the nearest step, the big
    HH:MM:SS text, a short human text ("45 s") or, for a stored value that is not a step, a note
    that says so."""

    seconds: int
    position: int
    text: str
    caption: str
    on_scale: bool


def split_hms(seconds: int):
    """Seconds as (hours, minutes, seconds): 3661 -> (1, 1, 1)."""
    return seconds // 3600, seconds % 3600 // 60, seconds % 60


def format_hms(seconds: int) -> str:
    """Seconds as HH:MM:SS, the way the window shows the slide interval: 300 -> '00:05:00'."""
    return "%02d:%02d:%02d" % split_hms(seconds)


def _nearest(values, target):
    """Index of the value nearest to *target*; halfway between two, the lower one."""
    return min(range(len(values)), key=lambda i: (abs(values[i] - target), i))


def interval_index_for_seconds(seconds) -> int:
    """The step nearest to *seconds*, in seconds (not in slider distance); the lower one halfway
    (90 s -> 60 s). Below the scale it is the first step, above it the last."""
    return _nearest(INTERVAL_STOPS, seconds)


def interval_position_for_seconds(seconds) -> int:
    """Slider position of the step nearest to *seconds* (a stored 100 s sits at the 2 minutes)."""
    return INTERVAL_POSITIONS[interval_index_for_seconds(seconds)]


def interval_index_for_position(position) -> int:
    """The step whose position is nearest to a raw slider position; the lower one halfway."""
    return _nearest(INTERVAL_POSITIONS, position)


def interval_seconds_for_position(position) -> int:
    """Seconds of the step nearest to a raw slider position: 0 -> 1, 480 -> 86399."""
    return INTERVAL_STOPS[interval_index_for_position(position)]


def snap_interval_position(position) -> int:
    """A raw slider position moved to the position of its nearest step."""
    return INTERVAL_POSITIONS[interval_index_for_position(position)]


def step_interval_position(position, steps: int) -> int:
    """The position *steps* steps of the scale from *position* (negative: towards the short end),
    stopping at the ends. One step is one second up to a minute, then the next round value."""
    index = interval_index_for_position(position) + steps
    return INTERVAL_POSITIONS[max(0, min(index, len(INTERVAL_STOPS) - 1))]


def describe_interval(seconds: int) -> str:
    """A short human form of a step: '45 s', '10 min', '2 h'; '' for anything else."""
    if seconds == INTERVAL_MAX_SECONDS:
        return _("the longest")
    if seconds < 60:
        return _("%d s") % seconds
    if seconds < 3600 and seconds % 60 == 0:
        return _("%d min") % (seconds // 60)
    if seconds % 3600 == 0:
        return _("%d h") % (seconds // 3600)
    return ""


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

    def interval_view(self) -> IntervalView:
        """What the slider shows for the stored slide interval. Reading never writes: a stored
        value that is not a step is shown at the nearest one and stays stored as it is."""
        seconds = self._settings.get_slide_interval_seconds()
        index = interval_index_for_seconds(seconds)
        on_scale = INTERVAL_STOPS[index] == seconds
        caption = (
            describe_interval(seconds)
            if on_scale
            else _(
                "Not a step of the slider: it shows the nearest one. The saved value stays until "
                "you move the slider."
            )
        )
        return IntervalView(
            seconds, INTERVAL_POSITIONS[index], format_hms(seconds), caption, on_scale
        )

    def chooser_start_folder(self) -> str:
        """Where the folder chooser opens: the folder in use when it exists, otherwise the system
        pictures folder (the default, ``~/Képek`` on a Hungarian system; ``~/Pictures`` when the
        system has none or it is the home directory), and the home directory only when even that
        is missing. Never "wherever the chooser was last": it starts at the pictures."""
        view = self.folder_view()
        for candidate in (view.text, view.default):
            if candidate and os.path.isdir(candidate):
                return candidate
        return os.path.expanduser("~")

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

    def set_interval_position(self, position) -> SaveResult:
        """Save the step at (or nearest to) a slider position. A position off the slider is
        refused; the seconds of a step are always inside the schema's range."""
        if isinstance(position, bool) or not isinstance(position, int):
            return SaveResult(False, _("Enter a whole number for the slider position."))
        if not 0 <= position <= INTERVAL_SLIDER_MAX:
            return SaveResult(False, _("This position is not on the slider."))
        return self._store(KEY_SLIDE_INTERVAL_SECONDS, interval_seconds_for_position(position))

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
