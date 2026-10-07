"""Whether the effects are drawn: the decision from the renderer and the guard of the drawing time.

No GTK and no display: ``decide`` and ``Effects`` are plain functions of what they are given.
"""

from __future__ import annotations

import logging

import pytest

from slideshow_lock import effects as fx
from slideshow_lock.effects import Decision, Effects, FrameTimer, decide

NGL, GL, VULKAN, CAIRO = "GskNglRenderer", "GskGLRenderer", "GskVulkanRenderer", "GskCairoRenderer"


# -- decide: which machine is known to have a GPU ------------------------------------------------


@pytest.mark.parametrize(
    "renderer_class, gl_renderer",
    [
        (NGL, "Mesa Intel(R) UHD Graphics 620 (KBL GT2)"),
        (NGL, "AMD Radeon RX 6700 XT (radeonsi, navi22, LLVM 17.0.6, DRM 3.57, 6.8.0)"),
        (NGL, "NVIDIA GeForce RTX 3060/PCIe/SSE2"),
        (GL, "Mesa Intel(R) HD Graphics 4000 (IVB GT2)"),
        (VULKAN, "Mesa Intel(R) Graphics (ADL GT2)"),
        (NGL, "Apple M1"),
        (NGL, "Mali-G610"),
    ],
)
def test_a_gpu_renderer_with_a_gpu_string_gets_the_effects(renderer_class, gl_renderer):
    decision = decide(renderer_class, gl_renderer, {})
    assert decision.full is True
    assert gl_renderer in decision.reason


@pytest.mark.parametrize(
    "gl_renderer",
    [
        "llvmpipe (LLVM 15.0.6, 256 bits)",
        "LLVMPIPE (LLVM 19.1.7, 128 bits)",
        "softpipe",
        "Mesa Software Rasterizer",
        "Software Rasterizer",
        "llvmpipe",
        "Mesa/X.org swrast",
        "llvmpipe (LLVM 17.0.6, 256 bits) lavapipe",
        "SwiftShader Device (Subzero)",
        "Mesa lavapipe",
    ],
)
def test_a_string_that_names_the_cpu_is_no(gl_renderer):
    """The case the renderer class cannot show: GTK says ``GskNglRenderer`` and Mesa has fallen
    back to the CPU by itself."""
    decision = decide(NGL, gl_renderer, {})
    assert decision.full is False
    assert "CPU" in decision.reason


@pytest.mark.parametrize(
    "gl_renderer",
    [
        "SVGA3D; build: RELEASE; LLVM;",
        "SVGA3D; build: RELEASE;  LLVM;",
        "virgl (NVIDIA GeForce RTX 3060)",
        "virgl",
        "VMware, Inc. SVGA II",
        "Virtio GPU",
        "VirtualBox Graphics Adapter",
        "llvmpipe on QXL",
        "Parallels Display Adapter",
        "Bochs Display",
    ],
)
def test_a_virtual_gpu_is_no(gl_renderer):
    decision = decide(NGL, gl_renderer, {})
    assert decision.full is False


@pytest.mark.parametrize("gl_renderer", [None, "", "   "])
def test_a_string_that_could_not_be_read_is_no(gl_renderer):
    decision = decide(NGL, gl_renderer, {})
    assert decision == Decision(False, "the OpenGL renderer string could not be read")


@pytest.mark.parametrize("renderer_class", [None, "", "  "])
def test_a_renderer_that_is_not_known_is_no(renderer_class):
    decision = decide(renderer_class, "Mesa Intel(R) UHD Graphics", {})
    assert decision == Decision(False, "the renderer is not known")


@pytest.mark.parametrize("renderer_class", [CAIRO, "GskBroadwayRenderer", "Whatever", "gl"])
def test_a_renderer_class_that_is_not_a_gpu_one_is_no_whatever_the_string_says(renderer_class):
    assert decide(renderer_class, "Mesa Intel(R) UHD Graphics 620", {}).full is False


def test_the_cairo_renderer_is_no():
    decision = decide(CAIRO, None, {})
    assert decision == Decision(False, "GskCairoRenderer draws with the CPU")


@pytest.mark.parametrize(
    "environ",
    [
        {"GSK_RENDERER": "cairo"},
        {"LIBGL_ALWAYS_SOFTWARE": "1"},
        {"GALLIUM_DRIVER": "llvmpipe"},
        {"GALLIUM_DRIVER": "softpipe"},
    ],
)
def test_the_environment_asking_for_software_drawing_is_no_even_for_a_gpu_string(environ):
    decision = decide(NGL, "Mesa Intel(R) UHD Graphics 620", environ)
    assert decision == Decision(False, "the environment asks for software drawing")


def test_the_environment_not_asking_for_it_does_not_matter():
    environ = {"LIBGL_ALWAYS_SOFTWARE": "0", "GALLIUM_DRIVER": "iris", "GSK_RENDERER": "ngl"}
    assert decide(NGL, "Mesa Intel(R) UHD Graphics 620", environ).full is True
    assert decide(NGL, "Mesa Intel(R) UHD Graphics 620").full is True  # no environment at all


@pytest.mark.parametrize("renderer_class", [GL, NGL, VULKAN, " " + NGL + " ", NGL.upper()])
def test_the_gpu_renderer_classes(renderer_class):
    assert fx.renderer_is_gpu(renderer_class) is True


@pytest.mark.parametrize(
    "renderer_class", [CAIRO, "", None, "GskRenderer", "ngl", "GskNglRenderer2"]
)
def test_the_others_are_not(renderer_class):
    assert fx.renderer_is_gpu(renderer_class) is False


# -- the budget ---------------------------------------------------------------------------------


def test_the_budget_has_a_provisional_default():
    assert fx.frame_budget_ms({}) == fx.FRAME_BUDGET_MS == 25.0
    assert fx.frame_budget_ms(None) == fx.FRAME_BUDGET_MS


def test_the_budget_can_be_set_without_a_new_build():
    assert fx.frame_budget_ms({fx.FRAME_BUDGET_ENV: "12.5"}) == 12.5
    assert fx.frame_budget_ms({fx.FRAME_BUDGET_ENV: " 40 "}) == 40.0


@pytest.mark.parametrize("text", ["abc", "0", "-5", "nan", "inf", "-inf", "1e999", "5 ms"])
def test_a_budget_that_is_not_a_positive_number_is_ignored_and_logged(text, caplog):
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.effects"):
        assert fx.frame_budget_ms({fx.FRAME_BUDGET_ENV: text}) == fx.FRAME_BUDGET_MS
    assert fx.FRAME_BUDGET_ENV in caplog.text


def test_an_empty_budget_is_the_default_without_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="slideshow_lock.effects"):
        assert fx.frame_budget_ms({fx.FRAME_BUDGET_ENV: ""}) == fx.FRAME_BUDGET_MS
    assert caplog.text == ""


# -- FrameTimer: the median of a window of frame intervals ---------------------------------------


def feed(timer, times_ms):
    """The answers of ``timer.add`` for frame times given in milliseconds."""
    return [timer.add(int(t * 1000)) for t in times_ms]


def test_the_timer_answers_once_per_full_window_with_the_median():
    timer = FrameTimer(frames=4)
    answers = feed(timer, [0, 16, 32, 48, 64, 80, 96, 112, 128])
    assert answers == [None, None, None, None, 16.0, None, None, None, 16.0]


def test_the_first_frame_is_no_interval():
    timer = FrameTimer(frames=1)
    assert feed(timer, [100]) == [None]
    assert feed(timer, [133]) == [33.0]


def test_the_median_ignores_a_few_slow_frames():
    timer = FrameTimer(frames=5)
    answers = feed(timer, [0, 16, 32, 200, 216, 232])  # intervals 16 16 168 16 16
    assert answers[-1] == 16.0


def test_the_median_sees_a_window_that_is_mostly_slow():
    timer = FrameTimer(frames=5)
    answers = feed(timer, [0, 60, 120, 180, 196, 212])  # intervals 60 60 60 16 16
    assert answers[-1] == 60.0


def test_two_callbacks_of_the_same_frame_count_once():
    timer = FrameTimer(frames=3)
    answers = feed(timer, [0, 0, 16, 16, 32, 32, 48, 48])
    assert answers == [None, None, None, None, None, None, 16.0, None]


def test_a_pause_is_not_slow_drawing_and_starts_the_window_again():
    timer = FrameTimer(frames=3, gap_ms=1000.0)
    answers = feed(timer, [0, 16, 32, 5000, 5016, 5032, 5048])
    # 16 16 | gap | 16 16 16 -> the window is only complete after the pause
    assert answers[:3] == [None, None, None] and answers[3] is None
    assert answers[-1] == 16.0 and answers[-2] is None


def test_an_interval_just_under_the_gap_counts():
    timer = FrameTimer(frames=1, gap_ms=1000.0)
    feed(timer, [0])
    assert feed(timer, [999.0]) == [999.0]
    assert feed(timer, [999.0 + 1000.0]) == [None]


def test_a_clock_that_goes_back_starts_the_window_again():
    timer = FrameTimer(frames=3)
    answers = feed(timer, [100, 116, 132, 10, 26, 42, 58])
    assert answers[3] is None  # the two intervals before the jump back are dropped, not completed
    assert answers[-2] is None and answers[-1] == 16.0


# -- Effects: the decision, made once, and the guard --------------------------------------------


def reader(*readings):
    """A ``read`` that gives *readings* one after the other and counts its calls."""
    calls = []
    answers = list(readings)

    def read():
        calls.append(1)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    read.calls = calls
    return read


GPU = (NGL, "Mesa Intel(R) UHD Graphics 620 (KBL GT2)")
CPU = (NGL, "llvmpipe (LLVM 15.0.6, 256 bits)")


def test_the_first_reading_that_comes_decides_for_good():
    effects = Effects({})
    read = reader(GPU)
    assert effects.full(read) is True
    assert effects.full(read) is True
    assert len(read.calls) == 1  # asked once
    assert effects.decision.full is True


def test_a_window_without_a_renderer_yet_is_asked_again_and_has_no_effects_meanwhile():
    effects = Effects({})
    read = reader(None, None, GPU)
    assert [effects.full(read) for _ in range(3)] == [False, False, True]
    assert len(read.calls) == 3
    assert effects.full(read) is True and len(read.calls) == 3


def test_a_reading_that_fails_is_a_machine_that_is_not_known_and_is_not_asked_again():
    effects = Effects({})
    read = reader(RuntimeError("no GL"))
    assert effects.full(read) is False
    assert effects.full(read) is False
    assert len(read.calls) == 1
    assert "not known" in effects.decision.reason


def test_a_software_machine_stays_plain():
    effects = Effects({})
    assert effects.full(reader(CPU)) is False
    assert effects.decision.full is False and "CPU" in effects.decision.reason


def test_the_environment_given_to_effects_is_the_one_decide_reads():
    assert Effects({"GSK_RENDERER": "cairo"}).full(reader(GPU)) is False
    assert Effects({}).full(reader(GPU)) is True


def test_the_decision_is_logged_with_its_reason(caplog):
    with caplog.at_level(logging.INFO, logger="slideshow_lock.effects"):
        Effects({}).full(reader(CPU))
    assert "plain drawing" in caplog.text and "llvmpipe" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="slideshow_lock.effects"):
        Effects({}).full(reader(GPU))
    assert "full effects" in caplog.text and "UHD Graphics" in caplog.text


def run_frames(effects, interval_ms, count, start_ms=0.0):
    """*count* frames *interval_ms* apart; returns the time of the last one."""
    now = start_ms
    effects.frame(int(now * 1000))
    for _ in range(count):
        now += interval_ms
        effects.frame(int(now * 1000))
    return now


def test_frames_that_keep_up_leave_the_effects_on():
    effects = Effects({}, budget_ms=25.0, frames=10)
    assert effects.full(reader(GPU))
    run_frames(effects, 16.7, 200)
    assert effects.full(reader(GPU)) is True and effects.tripped is None


def test_frames_that_do_not_keep_up_take_the_effects_away_for_the_rest_of_the_process(caplog):
    effects = Effects({}, budget_ms=25.0, frames=10)
    assert effects.full(reader(GPU))
    with caplog.at_level(logging.DEBUG, logger="slideshow_lock.effects"):
        end = run_frames(effects, 60.0, 10)
    assert effects.full(reader(GPU)) is False
    assert "60.0 ms" in effects.tripped and "25.0 ms" in effects.tripped
    assert "plain drawing from now on" in caplog.text
    run_frames(effects, 16.7, 500, start_ms=end)  # fast frames later do not bring them back
    assert effects.full(reader(GPU)) is False


def test_the_budget_is_a_parameter_not_a_constant():
    slow = Effects({}, budget_ms=100.0, frames=10)
    slow.full(reader(GPU))
    run_frames(slow, 60.0, 50)
    assert slow.full(reader(GPU)) is True  # 60 ms is within a 100 ms budget
    strict = Effects({}, budget_ms=10.0, frames=10)
    strict.full(reader(GPU))
    run_frames(strict, 16.7, 10)
    assert strict.full(reader(GPU)) is False


def test_the_budget_comes_from_the_environment_by_default():
    assert Effects({fx.FRAME_BUDGET_ENV: "7"}).budget_ms == 7.0
    assert Effects({}).budget_ms == fx.FRAME_BUDGET_MS


def test_a_median_exactly_at_the_budget_is_within_it():
    effects = Effects({}, budget_ms=20.0, frames=10)
    effects.full(reader(GPU))
    run_frames(effects, 20.0, 10)
    assert effects.full(reader(GPU)) is True


def test_a_single_slow_window_is_enough_and_one_slow_frame_is_not():
    effects = Effects({}, budget_ms=25.0, frames=10)
    effects.full(reader(GPU))
    end = run_frames(effects, 16.0, 8)
    end = run_frames(effects, 400.0, 1, start_ms=end)  # one frame of 400 ms in a window of 10
    run_frames(effects, 16.0, 30, start_ms=end)
    assert effects.full(reader(GPU)) is True


def test_nothing_is_measured_while_the_effects_are_off():
    for read in (reader(CPU), reader(None)):
        effects = Effects({}, budget_ms=1.0, frames=2)
        effects.full(read)
        run_frames(effects, 100.0, 20)
        assert effects.tripped is None


def test_nothing_is_measured_before_the_renderer_is_known():
    effects = Effects({}, budget_ms=1.0, frames=2)
    run_frames(effects, 100.0, 20)
    assert effects.tripped is None and effects.decision is None
    assert effects.full(reader(GPU)) is True  # the frames before the decision did not count
