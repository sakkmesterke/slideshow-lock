"""A picture is never still: from the frame it appears on, through the transition that brings it
in, while it is shown plainly, and through the transition that takes it out, it moves a little
between every two frames (``transition_draw.base_pose``, carried by ``Draw.pose``). No display.

``life`` plays the life of one picture on a frame clock of a given rate, the way the window does:
``compose`` for the incoming transition, the plain picture, ``compose`` for the outgoing one, each
with the picture's pose at that moment. ``problems`` reads the frames it returns. The broken
variants (``Mutant``) are the ways it could go wrong: the check must find each of them, or it
proves nothing about the real thing.
"""

from __future__ import annotations

import math
from typing import Callable, List, NamedTuple, Optional, Tuple

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    NEW,
    OLD,
    transition_seconds,
)

W, H = 1280.0, 720.0
INTERVALS = (3.0, 60.0, 300.0)  # a short and two long times a picture is shown
RATES = (60, 30)  # the frame clock; the move is redrawn at every frame of it
DURATION = 1.0  # the transition-duration setting


class Mutant(NamedTuple):
    """A broken way to play a picture's life; the default is the real one."""

    name: str
    span: Callable[[float], float] = td.picture_seconds
    still_outgoing: bool = False  # the outgoing picture stands still (the old ``Draw(OLD)``)
    restart_after_incoming: bool = False  # the clock starts again when the incoming run ends


REAL = Mutant("real")
#: The move used to take 90 % of the picture's time (PAN_FRACTION) and then stand still.
PAN_FRACTION_90 = Mutant("pan-fraction-0.9", span=lambda interval: 0.9 * interval)
STATIC_OLD = Mutant("static-outgoing", still_outgoing=True)
JUMP_AT_END = Mutant("jump-after-incoming", restart_after_incoming=True)
MUTANTS = (PAN_FRACTION_90, STATIC_OLD, JUMP_AT_END)


def _state(draw: td.Draw) -> Tuple[float, float, float, float]:
    """Where the picture is: scale, turn and shift, the slow move and the transition together."""
    return (
        draw.scale * draw.pose.scale,
        draw.angle,
        draw.dx + draw.scale * draw.pose.dx,
        draw.dy,
    )


def life(
    name: str, interval: float, hz: int, fills: bool, mutant: Mutant = REAL
) -> List[Optional[Tuple[float, ...]]]:
    """The picture's state at every frame of its life (None where transition *name* does not draw
    it: the fade through black shows one picture at a time)."""
    seconds = transition_seconds(name, interval, DURATION)
    span = mutant.span(interval)
    frames: List[Optional[Tuple[float, ...]]] = []

    def pose(age: float) -> td.Pose:
        return td.base_pose(age, span, W, fills)

    def pick(draws, layer):
        return next((d for d in draws if d.layer == layer), None)

    total = interval + seconds
    for i in range(int(total * hz) + 1):
        t = i / hz
        if t < seconds:  # it comes in
            age = t
            draws = td.with_poses(td.compose(name, t / seconds, W, H), td.STILL, pose(age))
            draw = pick(draws, NEW)
        elif t < interval:  # it is shown
            age = t - seconds if mutant.restart_after_incoming else t
            draw = td.Draw(NEW, pose=pose(age))
        else:  # it goes out
            old = td.STILL if mutant.still_outgoing else pose(t)
            draws = td.with_poses(td.compose(name, (t - interval) / seconds, W, H), old, td.STILL)
            draw = pick(draws, OLD)
        frames.append(_state(draw) if draw is not None else None)
    return frames


def problems(frames: List[Optional[Tuple[float, ...]]]) -> List[str]:
    """What is wrong with the frames of a life: two frames in a row with no move between them, or a
    step much bigger than both its neighbours (a jump)."""
    steps = []
    for i, (a, b) in enumerate(zip(frames, frames[1:])):
        if a is None or b is None:
            steps.append(None)
            continue
        steps.append(math.dist(a, b))
    found = []
    for i, step in enumerate(steps):
        if step is None:
            continue
        if step == 0.0:
            found.append(f"frame {i}: no move")
            continue
        around = [
            s
            for s in (steps[i - 1] if i else None, steps[i + 1] if i + 1 < len(steps) else None)
            if s
        ]
        if around and step > 2.0 * max(around) + 1e-9:
            found.append(f"frame {i}: a jump of {step:.6f} between steps of {around}")
    return found


@pytest.mark.parametrize("fills", [True, False], ids=["fills", "small"])
@pytest.mark.parametrize("hz", RATES)
@pytest.mark.parametrize("interval", INTERVALS)
@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_a_picture_moves_between_every_two_frames_of_its_whole_life(name, interval, hz, fills):
    assert problems(life(name, interval, hz, fills)) == []


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.name)
@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_check_finds_each_broken_variant(name, mutant):
    """Control: with the old rest at the end, a still outgoing picture, or a jump when the
    incoming run ends, the check is red (on the long and the short picture time, 60 Hz)."""
    for interval in (3.0, 60.0):
        assert problems(life(name, interval, 60, True, mutant)), (name, interval)


def test_every_transition_gives_the_incoming_and_the_outgoing_picture_a_frame_to_move_in():
    """The life of a picture has frames in it for all ten names (fade-black shows one picture at a
    time, so some of its frames are not drawn, but the in-between ones are)."""
    for name in ALL_TRANSITIONS:
        frames = life(name, 20.0, 60, True)
        assert sum(1 for f in frames if f is not None) > len(frames) // 2, name


# -- the transitions themselves start and stop softly -------------------------------------------


def _speed(name: str, p0: float, p1: float) -> float:
    a, b = td.compose(name, p0, W, H), td.compose(name, p1, W, H)
    total = 0.0
    for x, y in zip(a, b):
        total += math.dist(_state(x) + (x.opacity,), _state(y) + (y.opacity,))
        if x.clip and y.clip:
            total += math.dist(x.clip, y.clip) / W
        if x.circle and y.circle:
            total += math.dist(x.circle, y.circle) / W
        total += abs(x.blur - y.blur) / td.BLUR_RADIUS
    return total / (p1 - p0)


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_every_transition_starts_and_stops_softly(name):
    """The speed in the first and the last hundredth of the run is a small share of the fastest
    speed in the middle: the easing is smoothstep for all ten, nothing starts or stops jerkily."""
    peak = max(_speed(name, i / 100.0, (i + 1) / 100.0) for i in range(100))
    assert peak > 0.0
    assert _speed(name, 0.0, 0.01) <= 0.05 * peak, name
    assert _speed(name, 0.99, 1.0) <= 0.05 * peak, name


# -- the outgoing picture is drawn in towards where the incoming one ends -----------------------

SETTLED = ("crossfade", "slide-in", "ken-burns", "zoom", "wipe", "circle", "rotate")


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_outgoing_picture_that_stays_under_the_new_one_settles_by_the_easing(name):
    """The pictures that do not fill the window keep their move under the incoming one, so that
    the outgoing one is cut to the incoming one's area by the end (``Draw.settle``): its share is
    the transition's easing, 0 at the first frame (nothing cut) and 1 at the last. The fade
    through black, the push and the blur have no outgoing picture under the new one at the end."""
    new = td.Pose(1.01)
    for p in (0.0, 0.3, 0.7, 1.0):
        olds = [
            d
            for d in td.with_poses(td.compose(name, p, W, H), td.Pose(1.04), new)
            if d.layer == OLD
        ]
        for old in olds:
            if name in SETTLED:
                assert old.settle == td.Settle(td.ease(p), new), (name, p)
            else:
                assert old.settle is None, (name, p)
    assert all(d.settle is None for d in td.compose(name, 1.0, W, H) if d.layer == NEW)
