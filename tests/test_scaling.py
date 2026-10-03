"""Tests for the CORE-2 scaling geometry and the file checks (no decoder needed).

``plan_render`` is plain arithmetic; ``jpeg_is_complete`` and ``read_image_file`` work on
bytes and files. The decoding and the scaling themselves are in ``test_scaling_gdk.py``.
"""

from __future__ import annotations

import os
import struct
import time
import zlib

import pytest

from slideshow_lock import scaling
from slideshow_lock.scaling import (
    MAX_PAN_PIXEL_FACTOR,
    MAX_PIXELS,
    MAX_TEXTURE_SIDE,
    ImageSkipped,
    bmp_is_complete,
    device_size,
    incomplete_reason,
    jpeg_is_complete,
    plan_render,
    png_is_complete,
    read_image_file,
)
from tests.jpeg_fixtures import fake_jpeg
from tests.timeout_guard import (
    hard_timeout,
    per_test_deadline,  # noqa: F401  (autouse fixture)
)

# -- the device pixel size of a window -----------------------------------------------------------


@pytest.mark.parametrize(
    "logical, scale, expected",
    [
        ((1920, 1080), 1.0, (1920, 1080)),
        ((1920, 1080), 2.0, (3840, 2160)),
        ((1280, 720), 1.5, (1920, 1080)),
        ((1536, 864), 1.25, (1920, 1080)),
        ((1707, 960), 1.5, (2560, 1440)),  # 2560.5 rounds to the even neighbour: round(), as agreed
        ((1366, 768), 1.25, (1708, 960)),  # 1707.5 rounds up, it is not cut off
        ((1, 1), 0.1, (1, 1)),  # never 0
    ],
)
def test_ac2_the_device_size_is_the_logical_size_times_the_surface_scale_rounded(
    logical, scale, expected
):
    assert device_size(*logical, scale) == expected


# -- the crop and output size --------------------------------------------------------------------


def aspect_error_pixels(w, h, target_w, target_h):
    """How many pixels of the longer side the aspect ratio of (w, h) is off from the target's."""
    return abs(w / h - target_w / target_h) * h


MONITORS = [(1920, 1080), (2560, 1440), (3840, 2160), (1080, 1920), (1366, 768), (1600, 1200)]
PICTURES = [
    (4000, 3000),
    (3000, 4000),
    (6000, 4000),
    (4000, 4000),
    (800, 600),
    (600, 800),
    (16, 9),
    (9, 16),
    (5000, 1000),
    (1000, 5000),
    (1, 1),
    (1920, 1080),
]


@pytest.mark.parametrize("monitor", MONITORS)
@pytest.mark.parametrize("picture", PICTURES)
def test_ac2_fit_shows_the_whole_picture_undistorted_inside_the_monitor(picture, monitor):
    plan = plan_render(*picture, *monitor, "fit")
    assert plan.crop == (0, 0, *picture)
    assert plan.out[0] <= monitor[0] and plan.out[1] <= monitor[1]
    assert plan.out[0] == monitor[0] or plan.out[1] == monitor[1]  # fills one side exactly
    assert aspect_error_pixels(*plan.out, *picture) <= 1.0  # rounding only, no stretch
    assert plan.pan_range == (0, 0)


@pytest.mark.parametrize("monitor", MONITORS)
@pytest.mark.parametrize("picture", PICTURES)
def test_ac2_fill_covers_the_monitor_exactly_with_a_centred_undistorted_crop(picture, monitor):
    plan = plan_render(*picture, *monitor, "fill")
    x, y, w, h = plan.crop
    assert plan.out == monitor
    assert 0 <= x and 0 <= y and x + w <= picture[0] and y + h <= picture[1]
    assert abs((x + w / 2) - picture[0] / 2) <= 1 and abs((y + h / 2) - picture[1] / 2) <= 1
    # the crop has the monitor's aspect ratio to within one source pixel: no distortion
    assert aspect_error_pixels(w, h, *monitor) <= 1.0 + 1e-9
    # and it is as large as it can be: one of its sides is the whole picture side
    assert w == picture[0] or h == picture[1]


def test_ac3_a_portrait_picture_on_a_landscape_monitor_fills_the_width_and_loses_top_and_bottom():
    plan = plan_render(3000, 4000, 1920, 1080, "fill")
    assert plan.crop[0] == 0 and plan.crop[2] == 3000  # the whole width of the picture
    assert plan.crop[3] == round(1080 / (1920 / 3000))  # as much height as fits the monitor
    assert plan.crop[1] == (4000 - plan.crop[3]) // 2  # centred vertically
    assert plan.out == (1920, 1080)


def test_ac3_a_landscape_picture_on_a_portrait_monitor_fills_the_height_and_loses_the_sides():
    plan = plan_render(4000, 3000, 1080, 1920, "fill")
    assert plan.crop[1] == 0 and plan.crop[3] == 3000
    assert plan.crop[2] == round(1080 / (1920 / 3000))
    assert plan.out == (1080, 1920)


def test_ac3_fit_puts_a_portrait_picture_on_a_landscape_monitor_with_bars_at_the_sides():
    plan = plan_render(3000, 4000, 1920, 1080, "fit")
    assert plan.out == (810, 1080)


def test_ac4_pan_keeps_the_whole_height_of_a_portrait_picture_and_gives_the_scroll_range():
    plan = plan_render(3000, 4000, 1920, 1080, "fill", pan=True)
    assert plan.crop == (0, 0, 3000, 4000)
    assert plan.out == (1920, 2560)  # the width fills the monitor
    assert plan.pan_range == (0, 2560 - 1080)


@pytest.mark.parametrize(
    "picture, monitor, mode",
    [
        ((4000, 3000), (1920, 1080), "fill"),  # landscape picture: nothing to scroll
        ((3000, 4000), (1920, 1080), "fit"),  # fit shows everything
        ((3000, 4000), (1080, 1920), "fill"),  # portrait monitor, height is the limit
        ((1920, 1080), (1920, 1080), "fill"),
    ],
)
def test_ac4_pan_is_only_for_a_portrait_picture_that_overflows_vertically(picture, monitor, mode):
    assert plan_render(*picture, *monitor, mode, pan=True).pan_range == (0, 0)


def test_ac4_pan_off_means_no_scroll_range_at_all():
    assert plan_render(3000, 4000, 1920, 1080, "fill", pan=False).pan_range == (0, 0)


def test_ac4_an_extremely_tall_picture_is_not_panned_in_one_giant_frame():
    # 1:20 aspect: the scaled height would be 38400 px, far past the texture limit
    plan = plan_render(1000, 20000, 1920, 1080, "fill", pan=True)
    assert plan.pan_range == (0, 0)
    assert plan.out == (1920, 1080)
    assert plan.out[1] <= MAX_TEXTURE_SIDE
    # a tall picture just below the limits still pans, and stays inside both of them
    plan = plan_render(1000, 3000, 1920, 1080, "fill", pan=True)
    assert plan.pan_range[1] > 0
    assert plan.out[0] * plan.out[1] <= MAX_PAN_PIXEL_FACTOR * 1920 * 1080


def test_ac4_a_portrait_picture_on_a_portrait_monitor_is_panned_when_it_overflows_vertically():
    # 1:3 picture on a 9:16 monitor: the picture is narrower than the monitor, so its width
    # is the limiting side and its height overflows. "Portrait on portrait" is not the rule.
    plan = plan_render(1000, 3000, 1080, 1920, "fill", pan=True)
    assert plan.pan_range == (0, 1320)
    assert plan.out == (1080, 3240)
    # 3:4 picture on the same monitor is wider than the monitor: the height limits, no pan
    assert plan_render(3000, 4000, 1080, 1920, "fill", pan=True).pan_range == (0, 0)


def test_ac4_a_panning_frame_of_more_than_six_monitors_is_cropped_instead():
    """The limit that binds on a 1080p monitor is the pixel factor, not the texture side."""
    just_inside = plan_render(1000, 3000, 1920, 1080, "fill", pan=True)  # 1920 x 5760
    assert just_inside.pan_range == (0, 5760 - 1080)
    assert just_inside.out[0] * just_inside.out[1] <= MAX_PAN_PIXEL_FACTOR * 1920 * 1080
    just_over = plan_render(1000, 4000, 1920, 1080, "fill", pan=True)  # would be 1920 x 7680
    assert 7680 <= MAX_TEXTURE_SIDE  # the texture side limit is not what stops it
    assert 1920 * 7680 > MAX_PAN_PIXEL_FACTOR * 1920 * 1080
    assert just_over.pan_range == (0, 0)
    assert just_over.out == (1920, 1080)  # the middle crop


def test_ac4_a_panning_frame_taller_than_the_texture_limit_is_cropped_instead():
    """On a large monitor the texture side limit binds before the pixel factor does: a 3000 x
    3000 monitor allows a frame of 54 MP, but not a side of 18000 px."""
    inside = plan_render(1000, 5000, 3000, 3000, "fill", pan=True)  # 3000 x 15000
    assert inside.pan_range == (0, 15000 - 3000)
    outside = plan_render(1000, 6000, 3000, 3000, "fill", pan=True)  # 3000 x 18000, exactly 6x
    assert 3000 * 18000 <= MAX_PAN_PIXEL_FACTOR * 3000 * 3000  # the pixel factor lets it through
    assert outside.pan_range == (0, 0)  # the texture limit does not
    assert outside.out == (3000, 3000)


def test_ac4_the_panning_frame_limit_is_exactly_six_monitors_and_one_texture_side():
    """The documented limits, pinned from both sides with literal numbers (a 1000 x 1000 monitor
    and a picture of its width, so the scale is 1 and the frame height is the picture's)."""
    assert plan_render(1000, 6000, 1000, 1000, "fill", pan=True).pan_range == (0, 5000)  # 6.000x
    assert plan_render(1000, 6001, 1000, 1000, "fill", pan=True).pan_range == (0, 0)  # 6.001x
    assert plan_render(1000, 6500, 1000, 1000, "fill", pan=True).pan_range == (0, 0)  # 6.5x
    assert plan_render(1000, 7000, 1000, 1000, "fill", pan=True).pan_range == (0, 0)  # 7x
    # the side limit: 3000 x 16384 is 49 MP, under 6 x a 3000 x 3000 monitor (54 MP)
    assert plan_render(3000, 16384, 3000, 3000, "fill", pan=True).pan_range == (0, 13384)
    assert plan_render(3000, 16385, 3000, 3000, "fill", pan=True).pan_range == (0, 0)


def test_the_structure_walk_limit_is_a_million_steps_by_default():
    """The other tests set the cap themselves, so this is the only one that runs the real one:
    a run of fill bytes costs exactly ``MAX_STRUCTURE_STEPS`` finds, and a million is the
    number the docs justify (a real 250 MB JPEG has below that many marker bytes)."""
    assert scaling.MAX_STRUCTURE_STEPS <= 1_000_000
    steps = []
    real_find = bytes.find

    class Counting(bytes):
        def find(self, *args):
            steps.append(1)
            return real_find(self, *args)

    data = Counting(b"\xff\xd8" + b"\xff" * 1_500_000)  # no end-of-image marker anywhere
    assert jpeg_is_complete(data)  # the walk stopped at its cap: "complete", the decoder decides
    assert len(steps) == 1_000_000


def test_the_plan_refuses_nonsense():
    with pytest.raises(ValueError):
        plan_render(0, 100, 1920, 1080, "fit")
    with pytest.raises(ValueError):
        plan_render(100, 100, 1920, 0, "fit")
    with pytest.raises(ValueError):
        plan_render(100, 100, 1920, 1080, "stretch")


# -- JPEG completeness ---------------------------------------------------------------------------


def test_ac5_a_whole_jpeg_structure_is_complete_and_so_is_one_with_data_after_the_end():
    whole = fake_jpeg()
    assert jpeg_is_complete(whole)
    assert jpeg_is_complete(whole + b"\x00\x00\x00")  # padding
    assert jpeg_is_complete(whole + b"ftypmp42" + bytes(5000))  # a "motion photo" video after EOI


def test_ac5_every_truncation_of_a_jpeg_structure_is_incomplete():
    whole = fake_jpeg()
    for cut in list(range(2, 120)) + list(range(120, len(whole) - 2, 17)) + [len(whole) - 2]:
        assert not jpeg_is_complete(whole[:cut]), f"cut at {cut} of {len(whole)} looked complete"
    assert not jpeg_is_complete(whole[:-1])


def test_ac5_the_end_marker_of_an_embedded_thumbnail_does_not_make_a_truncated_jpeg_complete():
    whole = fake_jpeg()
    thumbnail_end = whole.index(b"\xff\xd9") + 2  # the EOI inside the APP1 segment
    assert thumbnail_end < whole.rindex(b"\xff\xd9")
    assert not jpeg_is_complete(whole[: thumbnail_end + 30])


def test_ac5_a_segment_that_runs_past_the_end_is_incomplete():
    assert not jpeg_is_complete(b"\xff\xd8\xff\xe0\x10\x00JFIF")


def test_ac5_the_structure_walk_gives_up_after_a_bounded_number_of_steps(monkeypatch):
    """A crafted file must not keep the worker busy: past the cap the answer is "complete"
    and the decoder decides. Below the cap a truncation is still found."""
    cut = fake_jpeg()[:-3]  # no end-of-image marker, so incomplete ...
    assert not jpeg_is_complete(cut)
    monkeypatch.setattr(scaling, "MAX_STRUCTURE_STEPS", 5)  # ... unless the walk stops early
    assert jpeg_is_complete(cut)
    monkeypatch.setattr(scaling, "MAX_STRUCTURE_STEPS", 1_000_000)
    assert not jpeg_is_complete(cut)


def test_ac5_a_run_of_fill_bytes_costs_a_bounded_number_of_steps(monkeypatch):
    """Every 0xFF is one step, so a file of fill bytes is the worst case for the walk."""
    steps = []
    real_find = bytes.find

    class Counting(bytes):
        def find(self, *args):
            steps.append(1)
            return real_find(self, *args)

    monkeypatch.setattr(scaling, "MAX_STRUCTURE_STEPS", 1000)
    data = Counting(b"\xff\xd8" + b"\xff" * 50_000)  # no end-of-image marker anywhere
    assert jpeg_is_complete(data)  # "complete": the walk stopped, the decoder decides
    assert len(steps) == 1000


def test_ac5_a_png_with_endless_tiny_chunks_is_not_walked_to_the_end(monkeypatch):
    chunk = b"\x00\x00\x00\x00tEXt\x00\x00\x00\x00"
    data = b"\x89PNG\r\n\x1a\n" + chunk * 50  # no IEND
    assert not png_is_complete(data)
    monkeypatch.setattr(scaling, "MAX_STRUCTURE_STEPS", 10)
    assert png_is_complete(data)


def test_the_pixel_limit_is_the_documented_one():
    """docs/preview.md, section 4 ("Memory"): 50 MP, derived from the measured cost per source
    pixel. This only pins the number so that a change is a decision; that the cost per pixel
    holds for every width is measured in ``test_scaling_gdk.py`` (a fresh process, real
    peak resident size)."""
    assert MAX_PIXELS == 50_000_000


def test_things_that_are_not_jpeg_are_not_judged():
    assert jpeg_is_complete(b"\x89PNG\r\n\x1a\n" + bytes(100))
    assert jpeg_is_complete(b"")
    assert jpeg_is_complete(b"GIF89a")


def png_bytes(width=64, height=48, idat_chunks=5):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    raw = zlib.compress(bytes(width * height * 3 + height))
    step = max(1, len(raw) // idat_chunks + 1)
    idats = b"".join(chunk(b"IDAT", raw[i : i + step]) for i in range(0, len(raw), step))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + idats + chunk(b"IEND", b"")


def bmp_bytes(width=30, height=20, declared=True):
    row = ((width * 24 + 31) // 32) * 4
    pixels = bytes(row * height)
    size = 54 + len(pixels)
    header = b"BM" + struct.pack("<IHHI", size if declared else 0, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + dib + pixels


def test_ac5_a_png_is_complete_only_when_its_chunks_reach_iend():
    whole = png_bytes()
    assert png_is_complete(whole)
    assert png_is_complete(whole + b"trailing bytes")
    for cut in list(range(8, len(whole) - 12, 23)) + [len(whole) - 1, len(whole) - 12]:
        assert not png_is_complete(whole[:cut]), f"cut at {cut} of {len(whole)}"
    assert incomplete_reason(whole[: len(whole) // 2]) == (
        "PNG data ends before the IEND chunk, probably incomplete"
    )


def test_ac5_a_bmp_is_complete_only_when_it_holds_all_the_bytes_its_header_announces():
    whole = bmp_bytes()
    assert bmp_is_complete(whole)
    assert not bmp_is_complete(whole[:-1])
    assert not bmp_is_complete(whole[: len(whole) // 2])
    # a writer that left the file-size field at 0: the pixel rows still say how much is due
    undeclared = bmp_bytes(declared=False)
    assert bmp_is_complete(undeclared) and not bmp_is_complete(undeclared[:-5])
    assert (
        incomplete_reason(whole[:-1])
        == "BMP file is shorter than its header says, probably incomplete"
    )


def test_ac5_incomplete_reason_is_none_for_whole_files_of_every_known_kind():
    for data in (
        fake_jpeg(),
        png_bytes(),
        bmp_bytes(),
        b"GIF89a" + bytes(30),
        b"II*\x00" + bytes(30),
    ):
        assert incomplete_reason(data) is None


# -- reading a file for decoding -----------------------------------------------------------------


def settled(path, data):
    path.write_bytes(data)
    old = time.time() - 3600
    os.utime(path, (old, old))
    return str(path)


def test_ac5_a_settled_complete_file_is_read(tmp_path):
    data = fake_jpeg()
    assert read_image_file(settled(tmp_path / "a.jpg", data)) == data


def test_ac5_a_file_modified_a_moment_ago_is_treated_as_still_being_copied(tmp_path):
    path = tmp_path / "new.jpg"
    path.write_bytes(fake_jpeg())  # mtime is now
    with pytest.raises(ImageSkipped, match="probably still being copied"):
        read_image_file(str(path))
    assert read_image_file(str(path), settle_seconds=0) == fake_jpeg()  # a settle time of 0 passes


def test_ac5_a_truncated_jpeg_with_a_valid_header_is_skipped(tmp_path):
    whole = fake_jpeg()
    path = settled(tmp_path / "half.jpg", whole[: len(whole) // 2])
    assert whole[:3] == b"\xff\xd8\xff"  # the header the image source checks is fine
    with pytest.raises(ImageSkipped, match="end-of-image"):
        read_image_file(path)


def test_ac5_negative_control_the_whole_file_passes(tmp_path):
    assert read_image_file(settled(tmp_path / "whole.jpg", fake_jpeg()))


def test_ac5_a_file_that_grows_while_it_is_read_is_skipped(tmp_path, monkeypatch):
    path = settled(tmp_path / "growing.jpg", fake_jpeg())
    real_readv = os.readv
    state = {"done": False}

    def growing_readv(fd, buffers):
        count = real_readv(fd, buffers)
        if not state["done"]:
            state["done"] = True
            with open(path, "ab") as handle:  # the copy goes on while we read
                handle.write(b"\x00" * 10)
        return count

    monkeypatch.setattr(scaling.os, "readv", growing_readv)
    with pytest.raises(ImageSkipped, match="changed while it was read"):
        read_image_file(path)


def test_ac5_a_file_rewritten_in_place_to_the_same_size_is_caught_by_its_mtime_alone(
    tmp_path, monkeypatch
):
    path = settled(tmp_path / "rewritten.jpg", fake_jpeg())
    real_readv = os.readv
    state = {"done": False}

    def rewriting_readv(fd, buffers):
        count = real_readv(fd, buffers)
        if not state["done"]:
            state["done"] = True
            os.utime(path, (time.time() - 1800, time.time() - 1799))  # same bytes, same size
        return count

    monkeypatch.setattr(scaling.os, "readv", rewriting_readv)
    with pytest.raises(ImageSkipped, match="changed while it was read"):
        read_image_file(path)
    monkeypatch.undo()
    assert read_image_file(path)  # negative control: untouched, the same file is read


def test_ac5_a_file_that_shrinks_while_it_is_read_is_skipped(tmp_path, monkeypatch):
    path = settled(tmp_path / "shrinking.jpg", fake_jpeg())
    monkeypatch.setattr(scaling.os, "readv", lambda fd, buffers: 0)  # the data is gone
    with pytest.raises(ImageSkipped, match="changed while it was read"):
        read_image_file(path)


def test_ac5_a_fifo_cannot_hang_the_worker(tmp_path):
    fifo = str(tmp_path / "pipe.jpg")
    os.mkfifo(fifo)
    with hard_timeout(5):
        with pytest.raises(ImageSkipped, match="not a regular file"):
            read_image_file(fifo)


def test_ac5_unreadable_missing_empty_and_oversized_files_are_skips_not_crashes(
    tmp_path, monkeypatch
):
    with pytest.raises(ImageSkipped, match="cannot be opened"):
        read_image_file(str(tmp_path / "missing.jpg"))
    with pytest.raises(ImageSkipped, match="not a regular file"):
        read_image_file(str(tmp_path))
    with pytest.raises(ImageSkipped, match="empty"):
        read_image_file(settled(tmp_path / "empty.png", b""))
    big = settled(tmp_path / "big.png", b"\x89PNG\r\n\x1a\n" + bytes(2000))
    monkeypatch.setattr(scaling, "MAX_FILE_BYTES", 1000)
    with pytest.raises(ImageSkipped, match="larger than"):
        read_image_file(big)
    if os.geteuid() != 0:
        locked = settled(tmp_path / "private.png", b"\x89PNG" + bytes(50))
        os.chmod(locked, 0)
        with pytest.raises(ImageSkipped, match="cannot be opened"):
            read_image_file(locked)
