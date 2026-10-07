"""The transitions between two pictures, without GTK.

What a transition is called and how long it takes, as plain numbers, so that everything decided
here is tested without a display. What each looks like is ``slideshow_lock.transition_draw``; the
preview window (``slideshow_lock.preview_window``) only turns that into drawing calls.

The ten identifiers are the contract of the ``transitions`` setting: they are stored as they are
written here. The slideshow draws all ten (``slideshow_lock.transition_draw`` says what each looks
like and which comes next, the preview window draws it); a name that is not one of them is dropped
and logged once.

A transition runs for ``transition_seconds`` from the moment the new picture appears: the one
``transition-duration`` of the settings, for every transition, and never longer than half of the
slide interval, so a short interval does not turn into one long dissolve. Below
``MIN_TRANSITION_SECONDS`` a transition is not worth drawing: the picture is cut.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

_LOG = logging.getLogger(__name__)

CROSSFADE = "crossfade"
FADE_BLACK = "fade-black"
SLIDE_IN = "slide-in"
PUSH = "push"
KEN_BURNS = "ken-burns"
ZOOM = "zoom"
WIPE = "wipe"
CIRCLE = "circle"
BLUR = "blur"
ROTATE = "rotate"

#: Every identifier the ``transitions`` setting may hold, in the order the window lists them.
ALL_TRANSITIONS = (
    CROSSFADE,
    FADE_BLACK,
    SLIDE_IN,
    PUSH,
    KEN_BURNS,
    ZOOM,
    WIPE,
    CIRCLE,
    BLUR,
    ROTATE,
)

#: The two the settings window's single-choice drop-down offers (and ``choose`` picks from). The
#: slideshow itself draws all ten and chooses with ``transition_draw.TransitionChooser``: this
#: goes away with the drop-down, when the window lists the ten.
DRAWABLE = (CROSSFADE, FADE_BLACK)

#: What "random" in the window and on the command line (``--transition random``) stores in the
#: ``transitions`` setting: the eight that are cheap and always look right. The blur is heavy and
#: Ken Burns only suits a picture that fills the window, so both are for choosing by name.
RANDOM_POOL = tuple(name for name in ALL_TRANSITIONS if name not in (BLUR, KEN_BURNS))

#: What the ``transitions`` setting holds until it is changed.
DEFAULT_TRANSITIONS = (CROSSFADE,)

#: How the picture to change with is chosen from several (``transition-order``).
ORDER_RANDOM = "random"
ORDER_SEQUENCE = "sequence"
ORDERS = (ORDER_RANDOM, ORDER_SEQUENCE)
DEFAULT_ORDER = ORDER_RANDOM

#: The values of ``--transition`` that mean "no transition, cut" (never stored: an empty list is)
#: and "a different one each time from ``RANDOM_POOL``" (stored as that list with ``ORDER_RANDOM``).
NONE = "none"
RANDOM = "random"

#: The length of a transition is one setting for all ten (``transition-duration``), in seconds:
#: the default and the range the setting keeps to. The Ken Burns figure is only its cross fade into
#: the picture; the slow move that goes with it lasts for the whole time the picture is shown.
DEFAULT_DURATION = 1.0
MIN_DURATION = 0.2
MAX_DURATION = 5.0

#: A transition may take at most this share of the slide interval.
INTERVAL_SHARE = 0.5

#: A transition shorter than this is not drawn: the new picture simply replaces the old one.
MIN_TRANSITION_SECONDS = 0.2

# The two pictures of a transition.
OLD = "old"
NEW = "new"


def is_valid(name) -> bool:
    """True for one of the ten identifiers; anything else (another type, a spelling) is not."""
    return isinstance(name, str) and name in ALL_TRANSITIONS


#: The unknown names already reported, so that a setting read for every picture logs each once.
_reported: set = set()


def clean(names: Iterable) -> List[str]:
    """The valid identifiers of *names*, in the order given, each once. A name that is not one
    (written by another version of the program, or by hand) is dropped and logged, once per name
    for the life of the process: the setting keeps working and the log says why a choice did not
    count, without a line for every picture."""
    kept: List[str] = []
    for name in names:
        if is_valid(name):
            if name not in kept:
                kept.append(name)
        else:
            shown = repr(name)
            if shown not in _reported:
                _reported.add(shown)
                _LOG.warning(
                    "[config] ignoring the unknown transition name %s in the setting", shown
                )
    return kept


#: The values of ``transition-duration`` already reported as out of range, once each.
_reported_durations: set = set()


def clamp_duration(value) -> float:
    """*value* as a ``transition-duration``: a number from ``MIN_DURATION`` to ``MAX_DURATION``.
    A number outside the range is brought to the nearest end, anything else (not a number, NaN, a
    bool) becomes ``DEFAULT_DURATION``; either is logged once per value, like an unknown
    transition name, so the setting keeps working and the log says why it did not count as it
    stood."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        kept = DEFAULT_DURATION
    else:
        kept = min(MAX_DURATION, max(MIN_DURATION, float(value)))
    if kept != value:
        shown = repr(value)
        if shown not in _reported_durations:
            _reported_durations.add(shown)
            _LOG.warning(
                "[config] the transition duration %s is not from %s to %s seconds, using %s",
                shown,
                MIN_DURATION,
                MAX_DURATION,
                kept,
            )
    return kept


def transition_seconds(name: str, interval: float, duration: float = DEFAULT_DURATION) -> float:
    """How long *name* runs between two pictures shown *interval* seconds each: *duration* (the
    ``transition-duration``, brought into its range), cut to ``INTERVAL_SHARE`` of the interval;
    0.0 (no transition) when that is shorter than ``MIN_TRANSITION_SECONDS`` or *name* is not a
    known transition."""
    if not is_valid(name):
        return 0.0
    seconds = min(clamp_duration(duration), INTERVAL_SHARE * float(interval))
    return seconds if seconds >= MIN_TRANSITION_SECONDS else 0.0


def choose(names: Sequence[str]) -> Optional[str]:
    """The first of the chosen *names* that is in ``DRAWABLE``, in the order of ``ALL_TRANSITIONS``;
    None when there is none (a cut). What the settings window's single-choice drop-down shows; the
    slideshow itself chooses with ``transition_draw.TransitionChooser``."""
    for name in ALL_TRANSITIONS:
        if name in names and name in DRAWABLE:
            return name
    return None
