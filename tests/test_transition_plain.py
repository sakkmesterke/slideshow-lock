"""The plain drawing is what 1.0.1 drew.

``compose_1_0_1.json`` was made by running 1.0.1's own ``compose`` and ``first_frame`` (and its
canvas's ``_paint`` on a recording snapshot) for every transition at many moments:
``tools/dump_compose_1_0_1.py`` made it, run on a checkout of the ``v1.0.1`` tag. Nothing in it was
written by hand. ``compose_plain`` must give the very same ``Draw`` values, field for field and
without rounding, and the canvas the same drawing calls (``test_preview_window_logic``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import ALL_TRANSITIONS, KEN_BURNS

FIXTURE = json.loads(Path(__file__).with_name("compose_1_0_1.json").read_text())

#: 1.0.1's ``Draw`` had these nine fields; the ones added since are checked to be at rest.
OLD_FIELDS = 9


def _tuples(value):
    """JSON has lists where ``Draw`` has tuples (clip, circle)."""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    return value


def _id(row):
    return "%s-%gx%g-%s-%g" % (row["name"], *row["size"], row["progress"], row["fade_share"])


ROWS = pytest.mark.parametrize("row", FIXTURE, ids=[_id(row) for row in FIXTURE])


def test_the_fixture_covers_every_transition_and_a_name_that_is_none():
    names = {row["name"] for row in FIXTURE}
    assert set(ALL_TRANSITIONS) <= names and "nonsense" in names
    assert {row["progress"] for row in FIXTURE} >= {0.0, 0.5, 1.0, "first"}


def _plain_draws(row):
    width, height = row["size"]
    if row["progress"] == "first":
        return td.first_frame(row["name"], width, height, True, row["fade_share"])
    return td.compose_plain(row["name"], row["progress"], width, height, row["fade_share"])


@ROWS
def test_compose_plain_is_what_1_0_1_composed(row):
    got = _plain_draws(row)
    assert [tuple(draw)[:OLD_FIELDS] for draw in got] == [_tuples(draw) for draw in row["draws"]]


@ROWS
def test_a_plain_draw_has_none_of_what_came_after_1_0_1(row):
    for draw in _plain_draws(row):
        assert draw.soft is None and draw.settle is None and draw.pose == td.STILL


def test_the_plain_ken_burns_is_1_0_1s_weaker_zoom_not_the_new_one():
    assert td.PLAIN_KEN_BURNS_ZOOM == 0.08 and td.PLAIN_KEN_BURNS_DRIFT == 0.02
    assert td.KEN_BURNS_ZOOM > td.PLAIN_KEN_BURNS_ZOOM
    new = td.compose_plain(KEN_BURNS, 0.0, 1000.0, 500.0)[1]
    assert new.scale == pytest.approx(1.08) and new.dx == pytest.approx(-20.0)


def test_the_full_compose_is_not_the_plain_one():
    """Control: the fixture would be satisfied by any code if the two drawings did not differ."""
    differs = 0
    for row in FIXTURE:
        if row["progress"] == "first" or row["name"] == "nonsense":
            continue
        width, height = row["size"]
        full = [
            tuple(d)[:OLD_FIELDS] for d in td.compose(row["name"], row["progress"], width, height)
        ]
        if full != [_tuples(draw) for draw in row["draws"]]:
            differs += 1
    assert differs > 40


def test_a_plain_run_draws_the_plain_pictures_and_ignores_the_poses():
    run = td.TransitionRun("wipe", 1.0, plain=True)
    run.tick(1_000_000)
    run.tick(1_000_000)
    run.tick(1_500_000)
    posed = run.draws(100.0, 50.0, td.Pose(1.2, -4.0), td.Pose(1.1, -2.0))
    assert posed == td.compose_plain("wipe", run.progress, 100.0, 50.0)
    assert all(draw.pose == td.STILL for draw in posed)


def test_a_plain_run_uses_the_first_frame_with_the_upload_draw_too():
    run = td.TransitionRun("fade-black", 1.0, plain=True)
    assert run.first
    draws = run.draws(100.0, 50.0)
    assert draws == td.first_frame("fade-black", 100.0, 50.0, True)
    assert draws[-1].opacity == td.UPLOAD_OPACITY


def test_a_plain_run_gives_its_ken_burns_the_fade_share():
    run = td.TransitionRun(KEN_BURNS, 10.0, plain=True, fade_share=0.1)
    run.tick(0)
    run.tick(0)
    run.tick(500_000)  # 0.5 s of 10
    assert run.draws(100.0, 50.0) == td.compose_plain(KEN_BURNS, 0.05, 100.0, 50.0, 0.1)


def test_a_full_run_is_the_default_and_unchanged():
    run = td.TransitionRun("wipe", 1.0)
    assert run.plain is False
    run.tick(0)
    run.tick(0)
    run.tick(400_000)
    assert run.draws(100.0, 50.0) == td.with_poses(td.compose("wipe", 0.4, 100.0, 50.0))
