"""A Ken Burns picture is never still: from the frame it appears on, through the Ken Burns
transition that brings it in, while it is shown, and through whichever transition takes it out, it
moves a little between every two frames (``transition_draw.base_pose``, carried by ``Draw.pose``).
The incoming picture starts its own move in the first moment of the transition, the outgoing one
goes on with the move it has. No display.

``life`` plays the life of one Ken Burns picture on a frame clock of a given rate, the way the
window does: ``compose`` for the incoming Ken Burns transition, the plain picture, ``compose`` for
the outgoing one, each with the picture's pose at that moment. ``problems`` reads the frames it
returns. The broken variants (``Mutant``) are the ways it could go wrong: the check must find each
of them, or it proves nothing about the real thing.
"""

from __future__ import annotations

import math
from typing import Callable, List, NamedTuple, Optional, Tuple

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    KEN_BURNS,
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
    name: str, interval: float, hz: int, mutant: Mutant = REAL
) -> List[Optional[Tuple[float, ...]]]:
    """The state of a Ken Burns picture at every frame of its life: it comes in with Ken Burns,
    is shown for *interval* seconds and goes out with transition *name* (None where *name* does not
    draw it: the fade through black shows one picture at a time)."""
    seconds = transition_seconds(name, interval, DURATION)
    span = mutant.span(interval)
    frames: List[Optional[Tuple[float, ...]]] = []

    def pose(age: float) -> td.Pose:
        return td.base_pose(age, span, W)

    def pick(draws, layer):
        return next((d for d in draws if d.layer == layer), None)

    total = interval + seconds
    for i in range(int(total * hz) + 1):
        t = i / hz
        if t < seconds:  # it comes in with Ken Burns
            age = t
            draws = td.with_poses(td.compose(KEN_BURNS, t / seconds, W, H), td.STILL, pose(age))
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


@pytest.mark.parametrize("hz", RATES)
@pytest.mark.parametrize("interval", INTERVALS)
@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_a_picture_moves_between_every_two_frames_of_its_whole_life(name, interval, hz):
    assert problems(life(name, interval, hz)) == []


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.name)
@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_check_finds_each_broken_variant(name, mutant):
    """Control: with the old rest at the end, a still outgoing picture, or a jump when the
    incoming run ends, the check is red (on the long and the short picture time, 60 Hz)."""
    for interval in (3.0, 60.0):
        assert problems(life(name, interval, 60, mutant)), (name, interval)


def test_every_transition_gives_the_incoming_and_the_outgoing_picture_a_frame_to_move_in():
    """The life of a picture has frames in it for all ten names (fade-black shows one picture at a
    time, so some of its frames are not drawn, but the in-between ones are)."""
    for name in ALL_TRANSITIONS:
        frames = life(name, 20.0, 60)
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


# -- the incoming picture starts its move with the transition, the outgoing one goes on ----------


def test_the_incoming_picture_starts_at_the_beginning_of_its_move_in_the_first_moment():
    """The first frame of the Ken Burns transition has the new picture at its start pose (enlarged
    by the whole zoom, shifted by the whole drift): it does not wait for the cross fade."""
    new = td.with_poses(td.compose(KEN_BURNS, 0.0, W, H), td.STILL, td.base_pose(0.0, 10.0, W))[-1]
    assert new.pose == td.Pose(1.0 + td.KEN_BURNS_ZOOM, -W * td.KEN_BURNS_DRIFT)
    later = td.base_pose(0.5, 10.0, W)
    assert later.scale < new.pose.scale and later.dx > new.pose.dx  # and it has begun to move


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_the_outgoing_picture_goes_on_from_where_it_was_at_the_end_of_its_time(name):
    """The outgoing picture's pose at the first frame of its transition is the pose it has at its
    age then, not the start pose: the move is not begun again."""
    interval, span = 10.0, td.picture_seconds(10.0)
    here = td.base_pose(interval, span, W)
    start = td.base_pose(0.0, span, W)
    assert here != start
    old = [d for d in td.with_poses(td.compose(name, 0.0, W, H), here, start) if d.layer == OLD]
    assert old and all(d.pose == here for d in old)
