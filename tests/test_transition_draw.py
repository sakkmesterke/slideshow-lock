"""The shape of the ten transitions and the choice between them, as numbers: no display."""

from __future__ import annotations

import math
import random

import pytest

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import (
    ALL_TRANSITIONS,
    BLUR,
    CIRCLE,
    CROSSFADE,
    FADE_BLACK,
    KEN_BURNS,
    NEW,
    OLD,
    ORDER_RANDOM,
    ORDER_SEQUENCE,
    PUSH,
    ROTATE,
    SLIDE_IN,
    WIPE,
    ZOOM,
)

W, H = 1280.0, 720.0


def _new_visible(draw: td.Draw) -> bool:
    """True if this Draw paints some of the new picture on the screen."""
    if draw.layer != NEW or draw.opacity <= 0.0:
        return False
    if draw.clip is not None and (draw.clip[2] <= 0 or draw.clip[3] <= 0):
        return False
    if draw.circle is not None and draw.circle[2] <= 0:
        return False
    if abs(draw.dx) >= W or abs(draw.dy) >= H:  # moved out of the window
        return False
    return True


def _new_whole(draw: td.Draw) -> bool:
    """True if this Draw paints the new picture as it is, over the whole window."""
    if draw.layer != NEW:
        return False
    full = (0.0, 0.0, W, H)
    clip_ok = draw.clip is None or tuple(draw.clip) == full
    circle_ok = draw.circle is None or draw.circle[2] >= math.hypot(W, H) / 2 - 1e-9
    return (
        draw.opacity == 1.0
        and draw.scale == 1.0
        and draw.angle == 0.0
        and draw.dx == 0.0
        and draw.dy == 0.0
        and draw.blur == 0.0
        and clip_ok
        and circle_ok
    )


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_at_progress_zero_the_old_picture_is_alone(name):
    draws = td.compose(name, 0.0, W, H)
    assert draws, name
    assert not any(_new_visible(d) for d in draws)
    first = draws[0]
    assert first.layer == OLD and first.opacity == 1.0 and first.scale == 1.0
    assert first.angle == 0.0 and first.dx == 0.0 and first.blur == 0.0


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_at_progress_one_the_new_picture_is_alone_and_untouched(name):
    draws = td.compose(name, 1.0, W, H, fade_share=0.1)
    assert draws, name
    assert draws[-1].layer == NEW and draws[-1].opacity == 1.0
    assert _new_whole(draws[-1])  # Ken Burns too: its last frame is the plain picture, no jump


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_every_value_is_in_range_for_every_progress(name):
    for step in range(0, 101):
        for draw in td.compose(name, step / 100.0, W, H):
            assert draw.layer in (OLD, NEW)
            assert 0.0 <= draw.opacity <= 1.0
            assert draw.scale > 0.0
            assert draw.blur >= 0.0
            if draw.circle is not None:
                assert draw.circle[2] >= 0.0


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_progress_outside_zero_to_one_is_clamped(name):
    assert td.compose(name, -3.0, W, H) == td.compose(name, 0.0, W, H)
    assert td.compose(name, 7.0, W, H) == td.compose(name, 1.0, W, H)


def test_ease_keeps_the_ends_is_monotonic_and_clamps():
    assert td.ease(0.0) == 0.0 and td.ease(1.0) == 1.0 and td.ease(0.5) == 0.5
    assert td.ease(-1.0) == 0.0 and td.ease(2.0) == 1.0
    values = [td.ease(i / 200.0) for i in range(201)]
    assert values == sorted(values)


def _coverage(name: str, p: float) -> float:
    """How much of the new picture has come in at progress p, 0 to 1 (a measure per transition)."""
    new = [d for d in td.compose(name, p, W, H) if d.layer == NEW]
    if not new:
        return 0.0
    d = new[-1]
    if name in (CROSSFADE, FADE_BLACK, ZOOM, ROTATE):
        return d.opacity
    if name in (SLIDE_IN, PUSH):
        return 1.0 - d.dx / W
    if name == WIPE:
        return d.clip[2] / W
    if name == CIRCLE:
        return d.circle[2] / (math.hypot(W, H) / 2)
    if name == KEN_BURNS:
        return d.opacity
    if name == BLUR:
        return 1.0
    raise AssertionError(name)


@pytest.mark.parametrize("name", [n for n in ALL_TRANSITIONS if n != BLUR])
def test_the_new_picture_never_goes_back(name):
    values = [_coverage(name, i / 100.0) for i in range(101)]
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:])), name
    assert values[0] == 0.0 and values[-1] == pytest.approx(1.0)


def test_crossfade_is_the_new_picture_over_the_old_at_the_eased_opacity():
    old, new = td.compose(CROSSFADE, 0.25, W, H)
    assert old == td.Draw(OLD)
    assert new == td.Draw(NEW, opacity=td.ease(0.25))


def test_fade_black_dips_to_black_in_the_middle_and_never_shows_both():
    assert td.compose(FADE_BLACK, 0.5, W, H)[0].opacity == pytest.approx(0.0, abs=1e-9)
    for step in range(101):
        draws = td.compose(FADE_BLACK, step / 100.0, W, H)
        assert len(draws) == 1
    assert td.compose(FADE_BLACK, 0.2, W, H)[0].layer == OLD
    assert td.compose(FADE_BLACK, 0.8, W, H)[0].layer == NEW


def test_slide_in_moves_the_new_picture_over_a_still_old_one_inside_the_window():
    old, new = td.compose(SLIDE_IN, 0.5, W, H)
    assert old == td.Draw(OLD)
    assert new.dx == pytest.approx(W * 0.5) and new.clip == (0.0, 0.0, W, H)


def test_push_moves_both_pictures_together():
    for p in (0.1, 0.5, 0.9):
        old, new = td.compose(PUSH, p, W, H)
        assert new.dx - old.dx == pytest.approx(W)  # always one window apart
        assert old.clip == new.clip == (0.0, 0.0, W, H)


def test_ken_burns_fades_in_during_its_share_and_then_keeps_moving():
    early = td.compose(KEN_BURNS, 0.05, W, H, fade_share=0.1)[-1]
    late = td.compose(KEN_BURNS, 0.5, W, H, fade_share=0.1)[-1]
    end = td.compose(KEN_BURNS, 1.0, W, H, fade_share=0.1)[-1]
    assert 0.0 < early.opacity < 1.0
    assert late.opacity == 1.0 and end.opacity == 1.0
    start = td.compose(KEN_BURNS, 0.0, W, H, fade_share=0.1)[-1]
    assert start.scale == pytest.approx(1.0 + td.KEN_BURNS_ZOOM) and start.dx < 0.0
    assert start.scale > early.scale > late.scale > end.scale == 1.0
    assert start.dx < early.dx < late.dx < end.dx == 0.0


def test_ken_burns_last_frame_is_the_picture_the_plain_drawing_shows_after_it():
    """The window draws the new picture plainly (scale 1, no shift, whole opacity) once the run is
    over, so the last animated frame must be that: no jump when the run ends."""
    plain = td.Draw(NEW)
    for share in (0.0, 0.05, 0.1, 1.0):
        assert td.compose(KEN_BURNS, 1.0, W, H, fade_share=share)[-1] == plain, share
    run = td.TransitionRun(KEN_BURNS, 9.0, fade_share=0.1)
    run.tick(0)  # the first frame
    run.tick(1_000_000)  # the clock starts
    assert run.tick(1_000_000 + 8_999_999)  # the last tick before the end still draws
    almost = run.draws(W, H)[-1]
    assert abs(almost.scale - 1.0) < 1e-5 and abs(almost.dx) < 1e-4 * W
    assert run.tick(1_000_000 + 9_000_000) is False  # over: the window draws plainly
    assert run.draws(W, H)[-1] == plain


def test_ken_burns_with_a_zero_share_does_not_divide_by_zero():
    assert td.compose(KEN_BURNS, 0.5, W, H, fade_share=0.0)[-1].opacity == 1.0


def test_zoom_grows_the_old_picture_and_brings_the_new_one_in_from_smaller():
    old, new = td.compose(ZOOM, 0.0, W, H)
    assert old.scale == 1.0 and new.scale == pytest.approx(1.0 - td.ZOOM_IN)
    old, new = td.compose(ZOOM, 1.0, W, H)
    assert old.scale == pytest.approx(1.0 + td.ZOOM_OUT) and new.scale == 1.0


def test_wipe_runs_left_to_right_and_the_circle_grows_from_the_centre():
    assert td.compose(WIPE, 0.5, W, H)[-1].clip == (0.0, 0.0, W * 0.5, H)
    cx, cy, r = td.compose(CIRCLE, 0.5, W, H)[-1].circle
    assert (cx, cy) == (W / 2, H / 2)
    assert r == pytest.approx(0.5 * math.hypot(W, H) / 2)
    # the full circle covers the corners: the farthest point from the centre is half the diagonal
    assert td.compose(CIRCLE, 1.0, W, H)[-1].circle[2] == pytest.approx(math.hypot(W, H) / 2)


def test_blur_peaks_at_the_cut_and_shows_the_old_picture_first_then_the_new_one():
    mid_before = td.compose(BLUR, 0.499, W, H)[0]
    mid_after = td.compose(BLUR, 0.501, W, H)[0]
    assert mid_before.layer == OLD and mid_after.layer == NEW
    assert mid_before.blur == pytest.approx(td.BLUR_RADIUS, rel=1e-3)
    assert td.compose(BLUR, 0.0, W, H)[0].blur == 0.0
    assert td.compose(BLUR, 1.0, W, H)[0].blur == pytest.approx(0.0, abs=1e-9)


def test_rotate_turns_the_new_picture_in_from_the_left_and_enlarged():
    new = td.compose(ROTATE, 0.0, W, H)[-1]
    assert new.angle == pytest.approx(-td.ROTATE_DEGREES)
    assert new.scale == pytest.approx(1.0 + td.ROTATE_SCALE)
    new = td.compose(ROTATE, 1.0, W, H)[-1]
    assert new.angle == 0.0 and new.scale == 1.0


def test_an_unknown_name_composes_nothing():
    assert td.compose("spin", 0.5, W, H) == []
    assert td.compose(None, 0.5, W, H) == []


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_first_frame_draws_the_new_picture_where_it_cannot_be_seen(name):
    draws = td.first_frame(name, W, H)
    assert draws[: len(draws) - 1] == td.compose(name, 0.0, W, H)
    upload = draws[-1]
    assert upload.layer == NEW and upload.clip == (0.0, 0.0, 1.0, 1.0)
    assert 0.0 < upload.opacity < 0.01


def test_first_frame_of_an_unknown_name_is_empty():
    assert td.first_frame("spin", W, H) == []


# -- which transition is really drawn --------------------------------------------------------------


def test_ken_burns_needs_a_picture_that_fills_the_window_and_does_not_scroll():
    win = (1920, 1080)
    assert td.effective_name(KEN_BURNS, (1920, 1080), win, (0, 0)) == KEN_BURNS
    assert td.effective_name(KEN_BURNS, (1920, 1000), win, (0, 0)) == CROSSFADE  # a fit bar
    assert td.effective_name(KEN_BURNS, (1600, 1080), win, (0, 0)) == CROSSFADE
    assert td.effective_name(KEN_BURNS, (1920, 2400), win, (0, 1320)) == CROSSFADE  # it scrolls
    assert td.effective_name(KEN_BURNS, (1920, 1080), win, (0, 0), software_gl=True) == KEN_BURNS


def test_blur_becomes_a_cross_fade_on_software_rendering_only():
    args = ((1920, 1080), (1920, 1080), (0, 0))
    assert td.effective_name(BLUR, *args) == BLUR
    assert td.effective_name(BLUR, *args, software_gl=True) == CROSSFADE


@pytest.mark.parametrize("name", [n for n in ALL_TRANSITIONS if n not in (BLUR, KEN_BURNS)])
def test_the_other_transitions_are_never_changed(name):
    args = ((100, 100), (1920, 1080), (0, 0))
    assert td.effective_name(name, *args, software_gl=True) == name


def test_software_renderer_is_told_from_the_renderer_and_the_environment_only():
    assert td.software_renderer("GskCairoRenderer")
    assert td.software_renderer("GskNglRenderer", {"GSK_RENDERER": "cairo"})
    assert td.software_renderer("GskNglRenderer", {"LIBGL_ALWAYS_SOFTWARE": "1"})
    assert td.software_renderer("GskGLRenderer", {"GALLIUM_DRIVER": "llvmpipe"})
    assert td.software_renderer("GskGLRenderer", {"GALLIUM_DRIVER": "softpipe"})
    assert not td.software_renderer("GskNglRenderer", {"LIBGL_ALWAYS_SOFTWARE": "0"})
    assert not td.software_renderer("GskNglRenderer", {"GALLIUM_DRIVER": "iris"})
    assert not td.software_renderer("GskNglRenderer", {})
    assert not td.software_renderer("GskVulkanRenderer")
    assert not td.software_renderer("SomethingNew", None)


# -- the choice -----------------------------------------------------------------------------------

EIGHT = [n for n in ALL_TRANSITIONS if n not in (BLUR, KEN_BURNS)]


def test_random_never_repeats_the_one_before_and_uses_every_chosen_name():
    chooser = td.TransitionChooser(random.Random(7))
    seen = []
    for _ in range(2000):
        seen.append(chooser.next(EIGHT, ORDER_RANDOM))
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert set(seen) == set(EIGHT)


def test_random_between_two_names_alternates():
    chooser = td.TransitionChooser(random.Random(1))
    seen = [chooser.next([CROSSFADE, ZOOM], ORDER_RANDOM) for _ in range(20)]
    assert seen[0::2] == [seen[0]] * 10 and seen[1::2] == [seen[1]] * 10
    assert seen[0] != seen[1]


def test_one_name_is_always_that_name_and_none_is_none():
    chooser = td.TransitionChooser(random.Random(3))
    assert [chooser.next([ROTATE], ORDER_RANDOM) for _ in range(5)] == [ROTATE] * 5
    assert chooser.next([], ORDER_RANDOM) is None
    assert chooser.next([], ORDER_SEQUENCE) is None


def test_sequence_goes_round_in_the_canonical_order_not_the_stored_one():
    chooser = td.TransitionChooser()
    names = [ROTATE, CROSSFADE, PUSH]  # stored out of order
    got = [chooser.next(names, ORDER_SEQUENCE) for _ in range(7)]
    assert got == [CROSSFADE, PUSH, ROTATE, CROSSFADE, PUSH, ROTATE, CROSSFADE]


def test_sequence_after_the_list_changed_continues_after_the_last_one_shown():
    chooser = td.TransitionChooser()
    assert chooser.next([CROSSFADE, PUSH, ZOOM], ORDER_SEQUENCE) == CROSSFADE
    assert chooser.next([CROSSFADE, PUSH, ZOOM], ORDER_SEQUENCE) == PUSH
    # PUSH was shown, and is no longer in the list: the next one after it, canonically, is ZOOM
    assert chooser.next([CROSSFADE, ZOOM, ROTATE], ORDER_SEQUENCE) == ZOOM


def test_random_after_the_list_changed_may_use_the_whole_new_list():
    chooser = td.TransitionChooser(random.Random(5))
    assert chooser.next([CROSSFADE], ORDER_RANDOM) == CROSSFADE
    assert chooser.next([PUSH, ZOOM], ORDER_RANDOM) in (PUSH, ZOOM)


def test_unknown_names_are_left_out_and_an_unknown_order_is_random():
    chooser = td.TransitionChooser(random.Random(2))
    got = {chooser.next(["spin", CROSSFADE, "other", ZOOM], "shuffle") for _ in range(30)}
    assert got == {CROSSFADE, ZOOM}
    assert chooser.next(["spin", "other"], ORDER_RANDOM) is None


def test_the_choice_is_deterministic_for_a_seed():
    first = td.TransitionChooser(random.Random(11))
    second = td.TransitionChooser(random.Random(11))
    assert [first.next(EIGHT, ORDER_RANDOM) for _ in range(50)] == [
        second.next(EIGHT, ORDER_RANDOM) for _ in range(50)
    ]


# -- the clock of a running transition -----------------------------------------------------------


def test_the_first_tick_draws_the_first_frame_and_starts_no_clock():
    run = td.TransitionRun(CROSSFADE, 1.0)
    assert run.first and run.draws(W, H) == td.first_frame(CROSSFADE, W, H)
    assert run.tick(5_000_000) is True
    assert run.first and run.progress == 0.0
    assert run.draws(W, H) == td.first_frame(CROSSFADE, W, H)  # still the first frame


def test_the_clock_starts_at_the_second_tick_and_the_end_is_a_moment_not_a_count():
    run = td.TransitionRun(WIPE, 0.8)
    run.tick(1_000_000)  # the first frame
    assert run.tick(2_000_000) is True  # t0
    assert run.progress == 0.0 and not run.first
    assert run.tick(2_400_000) is True and run.progress == pytest.approx(0.5)
    assert run.tick(2_799_999) is True and run.progress < 1.0
    assert run.tick(2_800_000) is False and run.progress == 1.0


def test_a_slow_machine_draws_fewer_frames_not_a_longer_transition():
    def frames(step_microseconds):
        run = td.TransitionRun(PUSH, 1.0)
        now, count = 0, 1
        run.tick(now)
        now += step_microseconds
        while run.tick(now):
            count += 1
            now += step_microseconds
        return count, now

    fast, fast_end = frames(16_667)
    slow, slow_end = frames(250_000)
    assert fast > slow >= 2
    # the last tick comes within one frame after the second tick plus the time
    assert fast_end - 16_667 <= 1_000_000 + 16_667 and slow_end - 250_000 <= 1_000_000 + 250_000


def test_a_clock_that_goes_backwards_does_not_give_a_negative_progress():
    run = td.TransitionRun(ZOOM, 1.0)
    run.tick(10)
    run.tick(1_000_000)
    run.tick(500_000)
    assert run.progress == 0.0


def test_draws_follow_the_progress_after_the_first_frame():
    run = td.TransitionRun(CIRCLE, 1.0)
    run.tick(0)
    run.tick(100)
    run.tick(100 + 500_000)
    assert run.draws(W, H) == td.compose(CIRCLE, 0.5, W, H)


def test_ken_burns_run_keeps_the_fade_share_it_was_given():
    run = td.TransitionRun(KEN_BURNS, 10.0, fade_share=0.08)
    run.tick(0)
    run.tick(1)
    run.tick(1 + 400_000)  # 4 % of the run: half way through the fade
    assert run.draws(W, H) == td.compose(KEN_BURNS, 0.04, W, H, 0.08)


@pytest.mark.parametrize(
    "names, expected",
    [
        ([KEN_BURNS], KEN_BURNS),
        ([KEN_BURNS, KEN_BURNS], KEN_BURNS),
        (["sparkle", KEN_BURNS], KEN_BURNS),  # the unknown name is dropped, as the chooser does
        ([], None),
        ([CROSSFADE], None),
        ([KEN_BURNS, CROSSFADE], None),
        (["sparkle"], None),
    ],
)
def test_the_first_picture_has_ken_burns_only_when_that_is_the_one_transition_chosen(
    names, expected
):
    assert td.first_picture_name(names) == expected


def test_asking_for_the_first_picture_does_not_move_the_chooser():
    chooser = td.TransitionChooser(random.Random(1))
    td.first_picture_name([CROSSFADE, KEN_BURNS])
    assert chooser.next([CROSSFADE, KEN_BURNS], ORDER_SEQUENCE) == CROSSFADE
