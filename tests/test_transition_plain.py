"""Nine of the ten transitions are drawn as 1.0.1 drew them.

``compose_1_0_1.json`` was made by running 1.0.1's own ``compose`` and ``first_frame`` (and its
canvas's ``_paint`` on a recording snapshot) for every transition at many moments:
``tools/dump_compose_1_0_1.py`` made it, run on a checkout of the ``v1.0.1`` tag. Nothing in it was
written by hand. ``compose`` must give the very same ``Draw`` values for every transition but Ken
Burns, field for field and without rounding, and the canvas the same drawing calls
(``test_preview_window_logic``). Ken Burns is the one that is not 1.0.1's: it is a cross fade of
pictures that move (``test_picture_motion``), and the control below shows the file can tell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import ALL_TRANSITIONS, CROSSFADE, KEN_BURNS

FIXTURE = json.loads(Path(__file__).with_name("compose_1_0_1.json").read_text())

#: 1.0.1's ``Draw`` had these nine fields; the ``pose`` added since is checked to be at rest.
OLD_FIELDS = 9

SAME = [row for row in FIXTURE if row["name"] != KEN_BURNS]
KEN_BURNS_ROWS = [row for row in FIXTURE if row["name"] == KEN_BURNS]


def _tuples(value):
    """JSON has lists where ``Draw`` has tuples (clip, circle)."""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    return value


def _id(row):
    return "%s-%gx%g-%s" % (row["name"], *row["size"], row["progress"])


ROWS = pytest.mark.parametrize("row", SAME, ids=[_id(row) for row in SAME])


def test_the_fixture_covers_every_transition_and_a_name_that_is_none():
    names = {row["name"] for row in FIXTURE}
    assert set(ALL_TRANSITIONS) <= names and "nonsense" in names
    assert {row["progress"] for row in FIXTURE} >= {0.0, 0.5, 1.0, "first"}


def _draws(row):
    width, height = row["size"]
    if row["progress"] == "first":
        return td.first_frame(row["name"], width, height)
    return td.compose(row["name"], row["progress"], width, height)


@ROWS
def test_compose_is_what_1_0_1_composed(row):
    got = _draws(row)
    assert [tuple(draw)[:OLD_FIELDS] for draw in got] == [_tuples(draw) for draw in row["draws"]]


@ROWS
def test_a_draw_has_none_of_what_came_after_1_0_1(row):
    for draw in _draws(row):
        assert draw.pose == td.STILL
        assert not {"soft", "settle"} & set(draw._fields)  # no soft edge, no settling, any more


def test_ken_burns_is_the_cross_fade_of_moving_pictures_not_1_0_1s_run():
    """Control: the comparison above is not satisfied by any code; for Ken Burns it fails."""
    assert KEN_BURNS_ROWS
    differs = 0
    for row in KEN_BURNS_ROWS:
        width, height = row["size"]
        if row["progress"] == "first":
            continue
        got = td.compose(KEN_BURNS, row["progress"], width, height)
        assert got == td.compose(CROSSFADE, row["progress"], width, height)
        if [tuple(draw)[:OLD_FIELDS] for draw in got] != [_tuples(d) for d in row["draws"]]:
            differs += 1
    assert differs > 10
    assert td.KEN_BURNS_ZOOM == 0.14 and td.KEN_BURNS_DRIFT == 0.035  # the 1.0.2 strength, kept


def test_a_run_gives_its_pictures_the_poses_and_the_cuts_stay_hard():
    run = td.TransitionRun("wipe", 1.0)
    run.tick(1_000_000)
    run.tick(1_000_000)
    run.tick(1_500_000)
    old, new = td.Pose(1.2, -4.0), td.Pose(1.1, -2.0)
    posed = run.draws(100.0, 50.0, old, new)
    assert posed == td.with_poses(td.compose("wipe", run.progress, 100.0, 50.0), old, new)
    assert [d.pose for d in posed] == [old, new]


def test_a_run_uses_the_first_frame_with_the_upload_draw_too():
    run = td.TransitionRun("fade-black", 1.0)
    assert run.first
    draws = run.draws(100.0, 50.0)
    assert draws == td.with_poses(td.first_frame("fade-black", 100.0, 50.0))
    assert draws[-1].opacity == td.UPLOAD_OPACITY


def test_a_run_without_poses_draws_the_still_pictures():
    run = td.TransitionRun("wipe", 1.0)
    run.tick(0)
    run.tick(0)
    run.tick(400_000)
    assert run.draws(100.0, 50.0) == td.compose("wipe", 0.4, 100.0, 50.0)
