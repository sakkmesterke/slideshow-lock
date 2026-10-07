"""What the settings window decides, without GTK (UI-1).

``PreferencesModel`` sits between the window's fields and ``Settings`` (CORE-3): which key a
field is bound to, which values it accepts, what it shows, and whether a value was really
saved. It imports no GTK, so the CI tests it without a display; what the real window does
with it is checked with ``tools/wayland-smoke/smoke_preferences.py`` and, for how it looks,
by eye on the reference machine.

The window edits a ``Draft``: a change is checked when it is made and kept in the draft, and it
reaches the settings only when the user saves or closes the window (``Draft.save``). Every
``Draft.edit_*`` returns a ``SaveResult``; the window puts the field back to the value in effect
when the edit is refused, so it never shows a value that is neither stored nor in the draft.
``PreferencesModel.set_*`` check and store at once (what ``Draft.save`` uses, key by key); each says
"saved" only if the value reads back. The ranges and choices here are the ones of the schema
(``data/*.gschema.xml``); a test compares them, so a change on one side fails loudly.
"""

from __future__ import annotations

import os
from typing import Any, Dict, NamedTuple, Optional

from slideshow_lock import _
from slideshow_lock.settings import (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITIONS,
    default_picture_folder,
)
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BLUR,
    KEN_BURNS,
    ORDER_RANDOM,
    ORDERS,
)

#: The slide interval runs from 00:00:01 to 23:59:59, in seconds (the schema's range). Zero is no
#: interval at all. The end of the slider is "24 hours" to the eye but 86399 s (23:59:59) to the
#: settings: a full day, 86400, is not valid.
INTERVAL_MIN_SECONDS = 1
INTERVAL_MAX_SECONDS = 23 * 3600 + 59 * 60 + 59

#: The steps of the slide-interval slider, in seconds. The slider is split into four equal
#: quarters of its length:
#:   1st: 1 to 10 s, every second;
#:   2nd: 10 to 60 s, every 5 seconds;
#:   3rd: 1 to 60 minutes, round values (1, 2, 3, 5, 10, 15, 20, 30, 45, 60);
#:   4th: 1 to 24 hours, round values (1, 2, 3, 4, 6, 8, 12, 24); the end is the longest interval,
#:        23:59:59 (86399 s), shown as "24 h" under the slider.
#: A step where two quarters meet (10 s, 1 minute, 1 hour) is one step, not two: 36 steps in all.
INTERVAL_STOPS = (
    *range(1, 11),
    *range(15, 61, 5),
    *(m * 60 for m in (2, 3, 5, 10, 15, 20, 30, 45, 60)),
    *(h * 3600 for h in (2, 3, 4, 6, 8, 12)),
    INTERVAL_MAX_SECONDS,
)

#: A quarter of the slider's length, in slider positions. 630 is the smallest length that every
#: quarter's steps divide evenly: the quarters hold 9, 10, 9 and 7 equal gaps between steps.
_QUARTER = 630


def _spread(start: int, gaps: int) -> tuple:
    """The positions of the *gaps* steps after *start*: one quarter, cut into equal gaps."""
    assert _QUARTER % gaps == 0, "a quarter must divide evenly into its gaps"
    return tuple(start + _QUARTER // gaps * k for k in range(1, gaps + 1))


#: Where each step sits on the slider, which runs 0 to ``INTERVAL_SLIDER_MAX``. Each quarter is a
#: quarter of the length (25 %, 50 %, 75 % and 100 % are 10 s, 1 minute, 1 hour and the end), and
#: its steps are spread evenly inside it, so the gap between steps is 70, 63, 70 and 90 positions
#: in the four quarters. A step of the slider is a step of the scale.
INTERVAL_SLIDER_MAX = 4 * _QUARTER
INTERVAL_POSITIONS = (
    0,
    *_spread(0, 9),  # 2 s ... 10 s
    *_spread(_QUARTER, 10),  # 15 s ... 1 min
    *_spread(2 * _QUARTER, 9),  # 2 min ... 1 h
    *_spread(3 * _QUARTER, 7),  # 2 h ... the end
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

#: What "Random mix" stores: the eight transitions without Blur and Ken Burns (the two that do
#: more than a short change between two pictures), in the order of the list.
RANDOM_POOL = tuple(name for name in ALL_TRANSITIONS if name not in (BLUR, KEN_BURNS))

#: The entry of the transition list that stands for ``RANDOM_POOL`` with the order "random".
RANDOM_CHOICE = "random"

#: What the transition drop-down offers, in order: "none" (the cut, stored as an empty list), the
#: ten transitions, and the random mix. Every choice but "none" and "random" is stored as a list of
#: that one name. A stored list of two or more names reads back as "random".
TRANSITION_CHOICES = ("none", *ALL_TRANSITIONS, RANDOM_CHOICE)

#: The values of ``transition-order``, the schema's choices. Not in ``CHOICES``, which is what the
#: window's plain drop-downs are made from: it is written by the random mix, not chosen on its own.
TRANSITION_ORDER_CHOICES = ORDERS

_GETTERS = {
    KEY_IDLE_TIMEOUT_SECONDS: "get_idle_timeout_seconds",
    KEY_LOCK_GRACE_PERIOD_SECONDS: "get_lock_grace_period_seconds",
    KEY_SLIDE_INTERVAL_SECONDS: "get_slide_interval_seconds",
    KEY_ORDER: "get_order",
    KEY_SCALING: "get_scaling",
    KEY_PAN_PORTRAIT_IMAGES: "get_pan_portrait_images",
    KEY_PICTURE_FOLDER: "get_picture_folder",
    KEY_TRANSITIONS: "get_transitions",
    KEY_TRANSITION_ORDER: "get_transition_order",
}

_SETTERS = {key: name.replace("get_", "set_", 1) for key, name in _GETTERS.items()}


def transition_list(choice: str) -> list:
    """The ``transitions`` list a drop-down entry stands for: [] for "none", the pool for the random
    mix, otherwise the one name."""
    if choice == "none":
        return []
    return list(RANDOM_POOL) if choice == RANDOM_CHOICE else [choice]


class SaveResult(NamedTuple):
    """``ok`` is True only when the value is stored (and reads back as it was written)."""

    ok: bool
    message: str


class FolderView(NamedTuple):
    """The folder field: its text, the default shown as its hint, and the line under it."""

    text: str
    default: str
    note: str


class Checked(NamedTuple):
    """A value checked but not stored: ``error`` is the refusal (None when it is accepted),
    ``value`` the value as it would be stored."""

    error: Optional[str]
    value: Any

    @property
    def ok(self) -> bool:
        return self.error is None


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
    """Seconds of the step nearest to a raw slider position: 0 -> 1, the slider's end -> 86399."""
    return INTERVAL_STOPS[interval_index_for_position(position)]


def snap_interval_position(position) -> int:
    """A raw slider position moved to the position of its nearest step."""
    return INTERVAL_POSITIONS[interval_index_for_position(position)]


def step_interval_position(position, steps: int) -> int:
    """The position *steps* steps of the scale from *position* (negative: towards the short end),
    stopping at the ends. One step is the next value of the scale, not a distance on the slider."""
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

    def interval_view(self, seconds: Optional[int] = None) -> IntervalView:
        """What the slider shows for the slide interval (the stored one unless *seconds* is given:
        the draft's). Reading never writes: a value that is not a step is shown at the nearest one
        and stays as it is."""
        if seconds is None:
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

    def chooser_start_folder(self, text: Optional[str] = None) -> str:
        """Where the folder chooser opens: the folder in the field (*text*, the draft's; the folder
        in use when it is not given) when it exists, otherwise the system pictures folder (the
        default, ``~/Képek`` on a Hungarian system; ``~/Pictures`` when the system has none or it is
        the home directory), and the home directory only when even that is missing. Never "wherever
        the chooser was last": it starts at the pictures."""
        view = self.folder_view()
        for candidate in (view.text if text is None else text, view.default):
            if candidate and os.path.isdir(candidate):
                return candidate
        return os.path.expanduser("~")

    # -- checking: what a value would be, without storing it ----------------------------------

    @staticmethod
    def check_int(key: str, value) -> "Checked":
        low, high = INT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return Checked(_("Enter a whole number of seconds."), None)
        if not low <= value <= high:
            return Checked(
                _("The value must be between %(low)d and %(high)d.") % {"low": low, "high": high},
                None,
            )
        return Checked(None, value)

    @staticmethod
    def check_interval_position(position) -> "Checked":
        """The seconds of the step at (or nearest to) a slider position. A position off the slider
        is refused; the seconds of a step are always inside the schema's range."""
        if isinstance(position, bool) or not isinstance(position, int):
            return Checked(_("Enter a whole number for the slider position."), None)
        if not 0 <= position <= INTERVAL_SLIDER_MAX:
            return Checked(_("This position is not on the slider."), None)
        return Checked(None, interval_seconds_for_position(position))

    @staticmethod
    def check_choice(key: str, value) -> "Checked":
        if value not in CHOICES[key]:
            return Checked(_("This choice is not available."), None)
        return Checked(None, value)

    @staticmethod
    def check_transition(value) -> "Checked":
        if not isinstance(value, str) or value not in TRANSITION_CHOICES:
            return Checked(_("This choice is not available."), None)
        return Checked(None, value)

    @staticmethod
    def check_pan_portrait_images(value) -> "Checked":
        if not isinstance(value, bool):
            return Checked(_("This must be on or off."), None)
        return Checked(None, value)

    @staticmethod
    def check_folder(text) -> "Checked":
        """The folder typed or chosen, as it would be stored. Empty means the default folder (D25).
        The folder need not exist (brief 3.7); a path that is not absolute, or that is a file, is
        refused."""
        if not isinstance(text, str):
            return Checked(_("Enter a folder path."), None)
        text = text.strip()
        if "\0" in text:
            return Checked(_("The path contains a character that is not allowed."), None)
        if text == "":
            return Checked(None, "")
        path = os.path.expanduser(text)
        if not os.path.isabs(path):
            return Checked(_("Enter an absolute path (it starts with / or ~)."), None)
        path = os.path.normpath(path)
        if os.path.exists(path) and not os.path.isdir(path):
            return Checked(_("This path is a file, not a folder."), None)
        return Checked(None, path)

    # -- writing ---------------------------------------------------------------------------

    def set_int(self, key: str, value) -> SaveResult:
        checked = self.check_int(key, value)
        return self._store(key, checked.value) if checked.ok else SaveResult(False, checked.error)

    def set_interval_position(self, position) -> SaveResult:
        """Save the step at (or nearest to) a slider position."""
        checked = self.check_interval_position(position)
        if not checked.ok:
            return SaveResult(False, checked.error)
        return self._store(KEY_SLIDE_INTERVAL_SECONDS, checked.value)

    def set_choice(self, key: str, value) -> SaveResult:
        checked = self.check_choice(key, value)
        return self._store(key, checked.value) if checked.ok else SaveResult(False, checked.error)

    def transition_choice(self) -> str:
        """The drop-down entry for the stored list: "none" for the empty list, the transition for a
        list of one name, "random" for two or more (the random mix, or a list written by hand: the
        window cannot show more). Reading never changes the stored list."""
        names = self._settings.get_transitions()
        if not names:
            return "none"
        return names[0] if len(names) == 1 else RANDOM_CHOICE

    def set_transition(self, value) -> SaveResult:
        """Save one of ``TRANSITION_CHOICES``: "none" stores the empty list (no transition), a
        transition the list of that one name, and "random" the list of ``RANDOM_POOL`` with the
        order "random"."""
        checked = self.check_transition(value)
        if not checked.ok:
            return SaveResult(False, checked.error)
        names = transition_list(value)
        if not self._settings.set_transitions(names):
            return SaveResult(False, _("The value was not accepted, the old one is kept."))
        if self._settings.get_transitions() != names:  # saved is saved only if it reads back
            return SaveResult(False, _("The value could not be saved."))
        if value == RANDOM_CHOICE:
            return self._store(KEY_TRANSITION_ORDER, ORDER_RANDOM)
        return _saved()

    def set_transition_order(self, value) -> SaveResult:
        if not isinstance(value, str) or value not in TRANSITION_ORDER_CHOICES:
            return SaveResult(False, _("This choice is not available."))
        return self._store(KEY_TRANSITION_ORDER, value)

    def set_pan_portrait_images(self, value) -> SaveResult:
        checked = self.check_pan_portrait_images(value)
        return (
            self._store(KEY_PAN_PORTRAIT_IMAGES, checked.value)
            if checked.ok
            else SaveResult(False, checked.error)
        )

    def set_folder(self, text) -> SaveResult:
        """Save the folder typed or chosen (see ``check_folder``)."""
        checked = self.check_folder(text)
        return self._store_folder(checked.value) if checked.ok else SaveResult(False, checked.error)

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


#: The order in which a draft writes its keys. Every key a ``Draft`` can hold is here (a test checks
#: it), so a key can never be left out of a save.
SAVE_ORDER = (
    KEY_PICTURE_FOLDER,
    KEY_ORDER,
    KEY_SCALING,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_TRANSITIONS,
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_LOCK_GRACE_PERIOD_SECONDS,
    KEY_SLIDE_INTERVAL_SECONDS,
)


class Draft:
    """The edits of the settings window that are not saved yet.

    An edit is checked when it is made (``PreferencesModel.check_*``) and kept here, per key; one
    that is refused changes nothing. An edit that brings a field back to the stored value is
    dropped, so ``dirty`` is true only when saving would change something. Nothing reaches the
    settings before ``save``: the window calls it for the Save button and when it closes. ``save``
    writes the kept keys one by one through the model; a key that is stored leaves the draft, a key
    that is not stays in it, and the first failure is what ``save`` reports.

    The transition key holds the drop-down entry (``TRANSITION_CHOICES``), not the list; the folder
    holds the text the way it would be stored ("" is the default folder).
    """

    def __init__(self, model: PreferencesModel) -> None:
        self._model = model
        self._pending: Dict[str, Any] = {}

    @property
    def dirty(self) -> bool:
        return bool(self._pending)

    @property
    def pending(self) -> Dict[str, Any]:
        return dict(self._pending)

    def discard(self) -> None:
        self._pending.clear()

    # -- what is in effect: the draft's value, otherwise the stored one --------------------------

    def _stored(self, key: str):
        if key == KEY_TRANSITIONS:
            return self._model.transition_choice()
        if key == KEY_PICTURE_FOLDER:
            return self._model.folder_view().text
        return self._model.get(key)

    def value(self, key: str):
        return self._pending[key] if key in self._pending else self._stored(key)

    def folder_text(self) -> str:
        return self.value(KEY_PICTURE_FOLDER)

    def interval_view(self) -> IntervalView:
        return self._model.interval_view(self.value(KEY_SLIDE_INTERVAL_SECONDS))

    # -- editing -------------------------------------------------------------------------------

    def _edit(self, key: str, checked: Checked) -> SaveResult:
        if not checked.ok:
            return SaveResult(False, checked.error)
        if checked.value == self._stored(key):
            self._pending.pop(key, None)
        else:
            self._pending[key] = checked.value
        return SaveResult(True, "")

    def edit_int(self, key: str, value) -> SaveResult:
        return self._edit(key, self._model.check_int(key, value))

    def edit_interval_position(self, position) -> SaveResult:
        return self._edit(KEY_SLIDE_INTERVAL_SECONDS, self._model.check_interval_position(position))

    def edit_choice(self, key: str, value) -> SaveResult:
        return self._edit(key, self._model.check_choice(key, value))

    def edit_transition(self, value) -> SaveResult:
        return self._edit(KEY_TRANSITIONS, self._model.check_transition(value))

    def edit_pan_portrait_images(self, value) -> SaveResult:
        return self._edit(KEY_PAN_PORTRAIT_IMAGES, self._model.check_pan_portrait_images(value))

    def edit_folder(self, text) -> SaveResult:
        return self._edit(KEY_PICTURE_FOLDER, self._model.check_folder(text))

    # -- saving --------------------------------------------------------------------------------

    def _write(self, key: str, value) -> SaveResult:
        if key == KEY_PICTURE_FOLDER:
            return self._model.set_folder(value)
        if key == KEY_TRANSITIONS:
            return self._model.set_transition(value)
        if key == KEY_PAN_PORTRAIT_IMAGES:
            return self._model.set_pan_portrait_images(value)
        if key in CHOICES:
            return self._model.set_choice(key, value)
        return self._model.set_int(key, value)

    def save(self) -> SaveResult:
        """Write every kept edit. ``ok`` when all are stored (or there was nothing to store)."""
        if not self._pending:
            return SaveResult(True, "")
        failed: Optional[SaveResult] = None
        for key in SAVE_ORDER:
            if key not in self._pending:
                continue
            result = self._write(key, self._pending[key])
            if result.ok:
                del self._pending[key]
            elif failed is None:
                failed = result
        if failed is not None:
            return failed
        return _saved()

    # -- the preview -----------------------------------------------------------------------------

    def preview_values(self) -> Dict[str, Any]:
        """The kept edits as the preview's settings read them (``preview_app.SessionSettings``):
        the folder resolved ("" is the default folder), the transition entry as its list. Never
        written."""
        values: Dict[str, Any] = {}
        for key, value in self._pending.items():
            if key == KEY_PICTURE_FOLDER:
                values[key] = value or default_picture_folder()
            elif key == KEY_TRANSITIONS:
                values[key] = transition_list(value)
                if value == RANDOM_CHOICE:
                    values[KEY_TRANSITION_ORDER] = ORDER_RANDOM
            else:
                values[key] = value
        return values
