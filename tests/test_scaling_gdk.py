"""Tests for decoding and scaling with the real GdkPixbuf (and GStreamer where it is installed).

Everything here goes through the real ``ImageScaler`` on real files. Which scaler produced a
frame is read from ``Frame.method``: with GStreamer's ``videoscale`` present it must be the
Lanczos-3 path, without it the bilinear fallback, and the tests below hold for both.
"""

from __future__ import annotations

import logging
import os
import struct
import time
import zlib

import gi
import pytest

try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
except (ValueError, ImportError):  # pragma: no cover - depends on the machine
    pytest.skip("the GdkPixbuf typelib is not installed", allow_module_level=True)

from slideshow_lock import scaling
from slideshow_lock.scaling import (
    METHOD_BILINEAR,
    METHOD_LANCZOS3,
    METHOD_NONE,
    ImageScaler,
    ImageSkipped,
)
from tests.timeout_guard import (
    per_test_deadline,  # noqa: F401  (autouse fixture)
)


def _gst_available() -> bool:
    try:
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        return Gst.ElementFactory.find("videoscale") is not None
    except Exception:
        return False


HAVE_GST = _gst_available()
SCALED = METHOD_LANCZOS3 if HAVE_GST else METHOD_BILINEAR


# -- building pictures ---------------------------------------------------------------------------


def solid(width, height, rgb, alpha=False):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, alpha, 8, width, height)
    pixbuf.fill((rgb[0] << 24) | (rgb[1] << 16) | (rgb[2] << 8) | 0xFF)
    return pixbuf


def halves(width, height, left, right):
    """A picture whose left half is *left* and whose right half is *right*."""
    pixbuf = solid(width, height, left)
    other = solid(width - width // 2, height, right)
    other.copy_area(0, 0, other.get_width(), height, pixbuf, width // 2, 0)
    return pixbuf


def old(path):
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))
    return str(path)


def save(tmp_path, name, pixbuf, fmt, **options):
    path = tmp_path / name
    keys, values = list(options), [str(v) for v in options.values()]
    pixbuf.savev(str(path), fmt, keys, values)
    return old(path)


def pixel(frame, x, y):
    """(r, g, b) of one pixel of a frame."""
    offset = y * frame.stride + 3 * x
    return tuple(frame.pixels[offset : offset + 3])


def close(color, expected, tolerance=6):
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 200, 0)


@pytest.fixture
def scaler():
    return ImageScaler(settle_seconds=0)


# -- scaling to the exact size -------------------------------------------------------------------


def test_ac2_fit_gives_the_exact_scaled_size_and_the_right_pixels(tmp_path, scaler):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(100, 100)], "fit")
    assert (frame.width, frame.height) == (100, 50)
    assert frame.method == SCALED
    assert close(pixel(frame, 10, 25), RED) and close(pixel(frame, 90, 25), BLUE)


def test_ac3_fill_crops_the_middle_so_a_landscape_picture_on_a_portrait_monitor_is_not_stretched(
    tmp_path, scaler
):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(100, 200)], "fill")
    assert (frame.width, frame.height) == (100, 200)
    # the crop is the middle 100 x 200 of the picture: the red/blue edge stays in its middle
    assert close(pixel(frame, 20, 100), RED) and close(pixel(frame, 80, 100), BLUE)
    assert close(pixel(frame, 45, 100), RED) and close(pixel(frame, 55, 100), BLUE)


def test_ac3_a_portrait_picture_on_a_landscape_monitor_fills_the_width_without_distortion(
    tmp_path, scaler
):
    # top third red, middle third green, bottom third blue, 300 x 900
    pixbuf = solid(300, 900, RED)
    solid(300, 300, GREEN).copy_area(0, 0, 300, 300, pixbuf, 0, 300)
    solid(300, 300, BLUE).copy_area(0, 0, 300, 300, pixbuf, 0, 600)
    path = save(tmp_path, "tall.png", pixbuf, "png")
    (frame,) = scaler.prepare(path, [(300, 150)], "fill")
    assert (frame.width, frame.height) == (300, 150)
    # a 300 x 150 window on a 300 x 900 picture shows its middle: all green, no red or blue
    assert all(close(pixel(frame, x, y), GREEN) for x in (0, 150, 299) for y in (0, 75, 149))


def test_ac4_pan_gives_the_full_height_and_the_scroll_range(tmp_path, scaler):
    pixbuf = solid(300, 900, RED)
    solid(300, 450, BLUE).copy_area(0, 0, 300, 450, pixbuf, 0, 450)
    path = save(tmp_path, "tall.png", pixbuf, "png")
    (frame,) = scaler.prepare(path, [(300, 150)], "fill", pan=True)
    assert (frame.width, frame.height) == (300, 900)
    assert frame.pan_range == (0, 750)
    assert close(pixel(frame, 100, 10), RED) and close(pixel(frame, 100, 890), BLUE)


def test_a_width_that_is_not_a_multiple_of_four_gets_properly_padded_rows(tmp_path, scaler):
    path = save(tmp_path, "odd.png", halves(300, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(101, 67)], "fill")
    assert (frame.width, frame.height) == (101, 67)
    assert frame.stride == 304  # 303 bytes of pixels, rows padded to 4
    assert len(frame.pixels) >= frame.stride * frame.height
    assert close(pixel(frame, 0, 66), RED) and close(pixel(frame, 100, 66), BLUE)


def test_a_cropped_region_with_an_odd_row_stride_is_repacked_correctly(tmp_path, scaler):
    # fill with a crop: the sub-picture shares the parent's rows, whose stride is not the
    # target's. Red up to x=500, green from 500 to 550, blue after: the crop is the middle
    # 100 columns (450 to 550), so its left half is red and its right half green, in every row.
    pixbuf = solid(1001, 300, BLUE)
    solid(500, 300, RED).copy_area(0, 0, 500, 300, pixbuf, 0, 0)
    solid(50, 300, GREEN).copy_area(0, 0, 50, 300, pixbuf, 500, 0)
    path = save(tmp_path, "w.png", pixbuf, "png")
    (frame,) = scaler.prepare(path, [(100, 300)], "fill")
    assert (frame.width, frame.height) == (100, 300)
    for y in (0, 1, 150, 298, 299):
        assert pixel(frame, 5, y) == RED and pixel(frame, 94, y) == GREEN, f"row {y}"


def test_a_picture_that_already_has_the_target_size_is_not_resampled(tmp_path, scaler):
    path = save(tmp_path, "same.png", halves(120, 80, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(120, 80)], "fit")
    assert frame.method == METHOD_NONE
    assert pixel(frame, 0, 0) == RED and pixel(frame, 119, 79) == BLUE  # exact, not filtered


def test_ac1_one_decode_serves_every_monitor_and_each_gets_its_own_size(
    tmp_path, scaler, monkeypatch
):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    decodes = []
    real = ImageScaler._decode
    monkeypatch.setattr(
        ImageScaler, "_decode", staticmethod(lambda data: decodes.append(1) or real(data))
    )
    frames = scaler.prepare(path, [(200, 100), (100, 200), (200, 100)], "fill")
    assert [(f.width, f.height) for f in frames] == [(200, 100), (100, 200), (200, 100)]
    assert len(decodes) == 1
    assert frames[0] is frames[2]  # the same size is rendered once


def test_a_horizontal_gradient_stays_a_clean_monotonic_ramp_after_downscaling(tmp_path, scaler):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 800, 100)
    stride = pixbuf.get_rowstride()
    row = bytearray(stride)
    for x in range(800):
        row[3 * x : 3 * x + 3] = bytes([x * 255 // 799] * 3)
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(row) * 100), GdkPixbuf.Colorspace.RGB, False, 8, 800, 100, stride
    )
    path = save(tmp_path, "ramp.png", pixbuf, "png")
    (frame,) = scaler.prepare(path, [(200, 25)], "fit")
    values = [pixel(frame, x, 12)[0] for x in range(200)]
    inner = values[4:-4]  # the last pixels ring a little against the edge (Lanczos: 2 levels)
    assert all(b >= a for a, b in zip(inner, inner[1:]))
    assert all(abs(v - round(x * 255 / 199)) <= 6 for x, v in enumerate(values))


# -- what the decoder and the file checks reject -------------------------------------------------


def test_ac5_a_truncated_jpeg_is_skipped_although_the_decoder_alone_would_accept_it(
    tmp_path, scaler
):
    whole = save(tmp_path, "whole.jpg", halves(640, 480, RED, BLUE), "jpeg", quality=90)
    data = open(whole, "rb").read()
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(data[: len(data) * 2 // 3])
    old(cut)
    with pytest.raises(ImageSkipped, match="end-of-image"):
        scaler.prepare(str(cut), [(100, 100)], "fit")
    (frame,) = scaler.prepare(whole, [(100, 100)], "fit")  # negative control: the whole file
    assert frame.width > 0


def test_ac5_a_truncated_png_is_skipped(tmp_path, scaler):
    whole = save(tmp_path, "whole.png", halves(640, 480, RED, BLUE), "png")
    data = open(whole, "rb").read()
    cut = tmp_path / "cut.png"
    cut.write_bytes(data[: len(data) * 2 // 3])
    old(cut)
    with pytest.raises(ImageSkipped, match="IEND"):
        scaler.prepare(str(cut), [(100, 100)], "fit")
    (frame,) = scaler.prepare(whole, [(100, 100)], "fit")  # negative control: the whole file
    assert frame.width > 0


def test_ac5_a_truncated_tiff_is_skipped_by_the_decoder_itself(tmp_path, scaler):
    whole = save(tmp_path, "whole.tif", halves(300, 200, RED, BLUE), "tiff")
    data = open(whole, "rb").read()
    cut = tmp_path / "cut.tif"
    cut.write_bytes(data[: len(data) // 2])
    old(cut)
    with pytest.raises(ImageSkipped, match="cannot be decoded"):
        scaler.prepare(str(cut), [(100, 100)], "fit")
    (frame,) = scaler.prepare(whole, [(100, 100)], "fit")
    assert frame.width > 0


def test_ac5_a_truncated_gif_is_skipped_by_the_decoder_itself(tmp_path, scaler):
    gif = bytes.fromhex(  # the well-known 1 x 1 pixel GIF
        "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
    )
    whole = tmp_path / "whole.gif"
    whole.write_bytes(gif)
    (frame,) = scaler.prepare(old(whole), [(10, 10)], "fit")
    assert (frame.width, frame.height) == (10, 10)
    for cut in (13, 30, 38, len(gif) - 1):
        path = tmp_path / f"cut{cut}.gif"
        path.write_bytes(gif[:cut])
        with pytest.raises(ImageSkipped, match="cannot be decoded"):
            scaler.prepare(old(path), [(10, 10)], "fit")


def test_ac5_a_truncated_bmp_is_skipped(tmp_path, scaler):
    from tests.test_scaling import bmp_bytes

    whole = tmp_path / "whole.bmp"
    whole.write_bytes(bmp_bytes(60, 40))
    cut = tmp_path / "cut.bmp"
    cut.write_bytes(bmp_bytes(60, 40)[:-300])
    with pytest.raises(ImageSkipped, match="BMP"):
        scaler.prepare(old(cut), [(30, 30)], "fit")
    (frame,) = scaler.prepare(old(whole), [(30, 30)], "fit")
    assert frame.width > 0


def test_ac5_a_jpeg_header_followed_by_garbage_is_skipped(tmp_path, scaler):
    path = tmp_path / "bad.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(600) + b"\xff\xd9")
    old(path)
    with pytest.raises(ImageSkipped):
        scaler.prepare(str(path), [(100, 100)], "fit")


def test_ac5_a_png_claiming_a_gigantic_size_is_refused_before_it_is_decoded(tmp_path, scaler):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)  # 900 megapixels, 2.7 GB of RGB
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(100)))
        + chunk(b"IEND", b"")
    )
    path = tmp_path / "bomb.png"
    path.write_bytes(data)
    old(path)
    started = time.monotonic()
    with pytest.raises(ImageSkipped) as caught:
        scaler.prepare(str(path), [(100, 100)], "fit")
    assert time.monotonic() - started < 1.0  # not the 3 s of allocating the buffer first
    assert "pixel limit" in str(caught.value)


def test_ac5_a_picture_over_the_pixel_limit_is_refused_by_the_size_the_header_announces(
    tmp_path, scaler, monkeypatch
):
    monkeypatch.setattr(
        scaling, "MAX_PIXELS", 10_000
    )  # the pictures below are 200 x 100 = 20000 px
    for name, fmt, options in (("ok.png", "png", {}), ("ok.jpg", "jpeg", {"quality": 90})):
        path = save(tmp_path, name, solid(200, 100, RED), fmt, **options)
        with pytest.raises(ImageSkipped, match="pixel limit"):
            scaler.prepare(path, [(100, 100)], "fit")
    monkeypatch.setattr(scaling, "MAX_PIXELS", 20_000)  # negative control: exactly at the limit
    (frame,) = scaler.prepare(path, [(100, 100)], "fit")
    assert frame.width > 0


# -- orientation and transparency ----------------------------------------------------------------


def exif_orientation(orientation):
    tiff = (
        b"MM\x00\x2a\x00\x00\x00\x08"
        + b"\x00\x01"
        + b"\x01\x12\x00\x03\x00\x00\x00\x01"
        + struct.pack(">H", orientation)
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def test_ac2_a_phone_photo_stored_sideways_is_turned_upright_before_it_is_scaled(tmp_path, scaler):
    plain = save(tmp_path, "plain.jpg", halves(300, 200, RED, BLUE), "jpeg", quality=95)
    data = open(plain, "rb").read()
    tagged = tmp_path / "tagged.jpg"
    tagged.write_bytes(data[:2] + exif_orientation(6) + data[2:])  # "rotate 90 clockwise"
    old(tagged)
    (frame,) = scaler.prepare(str(tagged), [(400, 400)], "fit")
    assert frame.height > frame.width  # 200 x 300 after the turn, not 300 x 200
    assert (frame.width, frame.height) == (267, 400)
    (untagged,) = scaler.prepare(plain, [(400, 400)], "fit")
    assert untagged.width > untagged.height  # negative control: no tag, no turn


def test_ac2_transparent_areas_are_shown_black_like_the_letterbox(tmp_path, scaler):
    pixbuf = solid(100, 100, RED, alpha=True)
    pixbuf.fill(0xFF000000)  # opaque red ... then make the right half fully transparent
    pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    stride, n = pixbuf.get_rowstride(), pixbuf.get_n_channels()
    data = bytearray(pixbuf.get_pixels())
    for y in range(100):
        for x in range(50, 100):
            data[y * stride + x * n : y * stride + x * n + 4] = bytes([255, 255, 255, 0])
        for x in range(50):
            data[y * stride + x * n : y * stride + x * n + 4] = bytes([255, 0, 0, 255])
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, True, 8, 100, 100, stride
    )
    path = save(tmp_path, "alpha.png", pixbuf, "png")
    (frame,) = scaler.prepare(path, [(100, 100)], "fit")
    assert close(pixel(frame, 10, 50), RED)
    assert close(pixel(frame, 90, 50), (0, 0, 0))


# -- which scaler --------------------------------------------------------------------------------


def test_ac2_the_scaler_is_lanczos3_through_videoscale_when_gstreamer_is_there(tmp_path, scaler):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(100, 50)], "fit")
    assert frame.method == (METHOD_LANCZOS3 if HAVE_GST else METHOD_BILINEAR)


def test_ac2_without_gstreamer_it_falls_back_to_bilinear_and_says_so_once(
    tmp_path, caplog, monkeypatch
):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    scaler = ImageScaler(settle_seconds=0)
    monkeypatch.setattr(scaler, "_gst_failure", "ImportError: simulated")
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        for _ in range(3):
            (frame,) = scaler.prepare(path, [(100, 50)], "fit")
            assert frame.method == METHOD_BILINEAR
    warnings = [r for r in caplog.records if "Lanczos scaling through GStreamer" in r.message]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "simulated" in warnings[0].message
    assert close(pixel(frame, 5, 25), RED) and close(pixel(frame, 95, 25), BLUE)


def test_ac2_a_failing_pipeline_falls_back_for_that_picture_instead_of_failing_the_slide(
    tmp_path, caplog, monkeypatch
):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    scaler = ImageScaler(settle_seconds=0)

    def broken(*_args, **_kwargs):
        raise RuntimeError("caps negotiation failed")

    monkeypatch.setattr(scaler, "_init_gst", lambda: object())
    monkeypatch.setattr(scaler, "_run_pipeline", broken)
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        (frame,) = scaler.prepare(path, [(100, 50)], "fit")
    assert frame.method == METHOD_BILINEAR
    assert any("caps negotiation failed" in r.message for r in caplog.records)
