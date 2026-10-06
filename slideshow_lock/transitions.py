"""The transitions between two pictures, without GTK.

What a transition is called, how long it takes, and what it looks like at a given moment, as
plain numbers: the preview window (``slideshow_lock.preview_window``) only turns these numbers
into drawing calls, so everything decided here is tested without a display.

The ten identifiers are the contract of the ``transitions`` setting: they are stored as they are
written here, and a later version may draw more of them than this one does. ``DRAWABLE`` are the
ones this version draws; the others are valid in the setting (they are kept, never rewritten) but
this version shows nothing for them.

A transition runs for ``transition_seconds`` from the moment the new picture appears, and never
longer than a quarter of the slide interval, so a short interval does not turn into one long
dissolve. Below ``MIN_TRANSITION_SECONDS`` a transition is not worth drawing: the picture is cut.

``layers(name, p)`` says what is on screen at progress ``p`` (0 = the old picture alone, 1 = the new
one alone) as a cross fade between two layers: ``(start, end, progress)`` where start and end are
``OLD``, ``NEW`` or ``BLACK``. A cross fade between the old picture and the new one is the
"crossfade"; the "fade-black" is the old picture fading to black, then black fading to the new
one. Both are the same drawing call, so the window needs no second code path.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, NamedTuple, Optional, Sequence

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

#: The ones this version of the program can draw.
DRAWABLE = (CROSSFADE, FADE_BLACK)

#: What the ``transitions`` setting holds until it is changed.
DEFAULT_TRANSITIONS = (CROSSFADE,)

#: How the picture to change with is chosen from several (``transition-order``).
ORDER_RANDOM = "random"
ORDER_SEQUENCE = "sequence"
ORDERS = (ORDER_RANDOM, ORDER_SEQUENCE)
DEFAULT_ORDER = ORDER_RANDOM

#: The value of ``--transition`` that means "no transition, cut" (never stored: an empty list is).
NONE = "none"

#: How long each transition takes, in seconds, before the limit of the slide interval.
#: The Ken Burns figure is only its cross fade into the picture; the slow move that goes with it
#: lasts for the whole time the picture is shown.
BASE_SECONDS = {
    CROSSFADE: 1.0,
    FADE_BLACK: 1.2,
    SLIDE_IN: 0.8,
    PUSH: 0.8,
    KEN_BURNS: 0.8,
    ZOOM: 1.0,
    WIPE: 0.8,
    CIRCLE: 1.0,
    BLUR: 1.2,
    ROTATE: 1.0,
}

#: A transition may take at most this share of the slide interval.
INTERVAL_SHARE = 0.25

#: A transition shorter than this is not drawn: the new picture simply replaces the old one.
MIN_TRANSITION_SECONDS = 0.2

# What a layer of the drawing is.
OLD = "old"
NEW = "new"
BLACK = "black"


class Layers(NamedTuple):
    """A cross fade: ``start`` is shown at progress 0, ``end`` at 1, mixed by ``progress``."""

    start: str
    end: str
    progress: float


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


def transition_seconds(name: str, interval: float) -> float:
    """How long *name* runs between two pictures shown *interval* seconds each: its own time, cut
    to a quarter of the interval; 0.0 (no transition) when that is shorter than
    ``MIN_TRANSITION_SECONDS`` or *name* is not a known transition."""
    base = BASE_SECONDS.get(name) if isinstance(name, str) else None
    if base is None:
        return 0.0
    seconds = min(base, INTERVAL_SHARE * float(interval))
    return seconds if seconds >= MIN_TRANSITION_SECONDS else 0.0


def choose(names: Sequence[str]) -> Optional[str]:
    """The transition to use among the chosen *names*: the first one this version can draw, in the
    order of ``ALL_TRANSITIONS``; None when there is none (a cut). The window offers one choice
    only, so this is what decides if the stored list ever holds more than one."""
    for name in ALL_TRANSITIONS:
        if name in names and name in DRAWABLE:
            return name
    return None


def layers(name: str, progress: float) -> Optional[Layers]:
    """What *name* shows at *progress* (0 to 1, clamped): None for a name this version cannot draw.

    At 0 only the old picture is visible, at 1 only the new one; in between the new picture never
    becomes less visible and the old one never more.
    """
    p = min(1.0, max(0.0, float(progress)))
    if name == CROSSFADE:
        return Layers(OLD, NEW, p)
    if name == FADE_BLACK:
        if p < 0.5:
            return Layers(OLD, BLACK, 2.0 * p)
        return Layers(BLACK, NEW, 2.0 * p - 1.0)
    return None
