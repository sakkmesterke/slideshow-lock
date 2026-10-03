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
