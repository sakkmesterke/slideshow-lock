"""Tests for decoding and scaling with the real GdkPixbuf (and GStreamer where it is installed).

Everything here goes through the real ``ImageScaler`` on real files. Which scaler produced a
frame is read from ``Frame.method``. The ``scaler`` fixture runs every test twice when
GStreamer's ``videoscale`` is installed: once on the Lanczos-3 path and once with GStreamer
switched off, on the bilinear fallback. So "the tests hold for both paths" is something the
test run does, not something this docstring claims. ``Frame.method`` only says which path a
frame went through; what Lanczos-3 does to pixels is measured in
``test_the_lanczos3_path_rings_at_a_sharp_edge_...``.
"""

from __future__ import annotations

import logging
import os
import random
import struct
import subprocess
import sys
import time
import tracemalloc
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
    hard_timeout,
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
    return tuple(frame.pixels.get_data()[offset : offset + 3])


def close(color, expected, tolerance=6):
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 200, 0)


@pytest.fixture(params=["videoscale", "bilinear"] if HAVE_GST else ["bilinear"])
def scaler(request):
    scaler = ImageScaler(settle_seconds=0)
    if request.param == "bilinear":
        scaler._gst_failure = "switched off for this test"
    scaler.scaled_method = METHOD_LANCZOS3 if request.param == "videoscale" else METHOD_BILINEAR
    return scaler


# -- scaling to the exact size -------------------------------------------------------------------


def test_ac2_fit_gives_the_exact_scaled_size_and_the_right_pixels(tmp_path, scaler):
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(100, 100)], "fit")
    assert (frame.width, frame.height) == (100, 50)
    assert frame.method == scaler.scaled_method
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
    assert frame.pixels.get_size() == frame.stride * frame.height
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


def test_every_frame_holds_its_pixels_as_a_glib_bytes_made_on_the_worker(tmp_path, scaler):
    """The window wraps the pixels into a texture on the main loop: a GLib.Bytes costs it no
    copy, a bytes object costs a copy of up to 150 MB for a panning 4K frame."""
    path = save(tmp_path, "h.png", halves(400, 200, RED, BLUE), "png")
    same = save(tmp_path, "same.png", halves(120, 80, RED, BLUE), "png")
    for source, size in ((path, (100, 50)), (same, (120, 80))):
        (frame,) = scaler.prepare(source, [size], "fit")
        assert isinstance(frame.pixels, GLib.Bytes)
        assert frame.pixels.get_size() == frame.stride * frame.height
    kinds = {
        scaler.prepare(path, [(100, 50)], "fit")[0].method,
        scaler.prepare(same, [(120, 80)], "fit")[0].method,
    }
    assert kinds == {scaler.scaled_method, METHOD_NONE}  # both of the two code paths were seen


@pytest.mark.parametrize("width", [97, 98, 99, 100, 101, 102, 103])
def test_a_frame_is_tightly_laid_out_for_every_row_padding_on_either_scaler(
    tmp_path, scaler, width
):
    """stride is GStreamer's (rows padded to 4 bytes) and the pixel buffer is exactly
    stride * height, on the Lanczos path and on the bilinear fallback alike."""
    path = save(tmp_path, "w.png", halves(300, 200, RED, BLUE), "png")
    (frame,) = scaler.prepare(path, [(width, 67)], "fill")
    assert frame.stride == scaling._gst_row_stride(width) == (width * 3 + 3) // 4 * 4
    assert frame.pixels.get_size() == frame.stride * frame.height
    assert close(pixel(frame, 0, 66), RED) and close(pixel(frame, width - 1, 66), BLUE)


def _striped(width, height, *, x0=0, y0=0):
    """A pixbuf whose every pixel has its own value, built like a decoder's: rows padded to 4
    bytes except the last one. ``x0``/``y0`` shift the pattern (the expected rows of a crop)."""
    stride = (width * 3 + 3) // 4 * 4
    data = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            data[y * stride + 3 * x : y * stride + 3 * x + 3] = _striped_pixel(x + x0, y + y0)
    del data[stride * (height - 1) + 3 * width :]
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, False, 8, width, height, stride
    )


def _striped_pixel(x, y):
    return bytes([(y * 7 + x * 3) % 256, (y * 5 + x) % 256, (y + x * 11) % 256])


@pytest.mark.parametrize("block_bytes", [1, 5000, scaling._ROW_BLOCK_BYTES])
@pytest.mark.parametrize("width", [3, 20, 21])
@pytest.mark.parametrize("crop", [False, True], ids=["whole", "crop"])
def test_rows_repacks_every_row_whatever_the_block_size_and_the_row_padding(
    monkeypatch, block_bytes, width, crop
):
    """``_rows`` reads the pixbuf a block of rows at a time: a block boundary in the wrong place
    would drop, repeat or shift a row. Width 21 has 63 bytes per row (not a multiple of 4),
    width 20 has 60; a crop has a stride that is not the frame's."""
    monkeypatch.setattr(scaling, "_ROW_BLOCK_BYTES", block_bytes)
    height = 37
    if crop:
        pixbuf = _striped(width + 4, height + 3).new_subpixbuf(1, 2, width, height)
    else:
        pixbuf = _striped(width, height)
    x0, y0 = (1, 2) if crop else (0, 0)
    want = (width * 3 + 3) // 4 * 4
    expected = b"".join(
        b"".join(_striped_pixel(x + x0, y + y0) for x in range(width)).ljust(want, b"\0")
        for y in range(height)
    )
    rows = ImageScaler._rows(pixbuf)
    assert type(rows) is bytes  # PyGObject converts a bytearray item by item: 3 s per 150 MB
    assert rows == expected


@pytest.mark.parametrize("crop", [False, True], ids=["whole", "crop"])
def test_rows_peaks_at_two_copies_of_the_frame_for_a_width_that_needs_row_padding(
    monkeypatch, crop
):
    """The old code joined one string per row: a list of the rows, the joined frame and the
    pixbuf's own bytes, three copies at the peak. The Python-side peak is now two: the pixbuf's
    bytes and the padded frame (whole picture), or the frame buffer and the ``bytes`` made from
    it (a crop). ``tracemalloc`` sees those (the pixbuf itself is native memory)."""
    monkeypatch.setattr(scaling, "_ROW_BLOCK_BYTES", 64 * 1024)
    width, height = 1501, 1000  # 4503 bytes per row, padded to 4504
    extra = 8 if crop else 0  # a crop shares its parent's wider rows
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width + extra, height + extra)
    pixbuf.fill(0x336699FF)
    if crop:
        pixbuf = pixbuf.new_subpixbuf(3, 1, width, height)
    frame_bytes = scaling._gst_row_stride(width) * height
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        rows = ImageScaler._rows(pixbuf)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == frame_bytes
    assert peak <= 2.2 * frame_bytes, f"peak {peak / frame_bytes:.2f} frames"


_HWM_SCRIPT = """
import sys
from slideshow_lock.scaling import ImageScaler

def hwm():
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) * 1024 for line in status if line.startswith("VmHWM"))

scaler = ImageScaler(settle_seconds=0)
scaler.prepare(sys.argv[1], [(64, 64)], "fit")  # warm up GStreamer, the loaders and the allocator
base = hwm()
(frame,) = scaler.prepare(sys.argv[2], [(1920, 1080)], "fit")
assert frame.method == "lanczos3-videoscale", frame.method
print((hwm() - base) / (int(sys.argv[3]) ** 2))
"""


@pytest.mark.spawns_processes  # a fresh interpreter measures the peak memory
@pytest.mark.skipif(not HAVE_GST, reason="needs GStreamer's videoscale")
@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs /proc")
@pytest.mark.parametrize("side", [3000, 3001], ids=["row-4-aligned", "row-not-4-aligned"])
def test_preparing_a_picture_costs_about_the_same_bytes_per_pixel_for_every_width(tmp_path, side):
    """docs/preview.md, section 4 ("Memory"): the pixel limit rests on a measured cost per source
    pixel. A width of 3001 has 9003 bytes per row (not a multiple of 4) and used to cost 12.1
    bytes per pixel against 9.1 for 3000. Measured in a fresh process: the peak resident size
    of the worker is a process-wide number and the test run has had larger peaks before."""
    tiny = save(tmp_path, "tiny.png", solid(64, 64, RED), "png")
    big = save(tmp_path, "big.png", solid(side, side, BLUE), "png", compression=1)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(filter(None, [repo_root, os.environ.get("PYTHONPATH")])),
    )
    done = subprocess.run(
        [sys.executable, "-c", _HWM_SCRIPT, tiny, big, str(side)],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    bytes_per_pixel = float(done.stdout.strip().splitlines()[-1])
    assert bytes_per_pixel <= 10.5, f"{bytes_per_pixel:.2f} bytes per source pixel"


@pytest.mark.skipif(not HAVE_GST, reason="needs GStreamer's videoscale")
def test_the_lanczos3_path_rings_at_a_sharp_edge_and_bilinear_does_not(tmp_path):
    """``Frame.method`` is only a label. What a Lanczos-3 scaler does to a hard edge is that it
    rings: a dip below the dark side and a peak above the bright side. Bilinear does not, and
    neither does a smaller Lanczos envelope (envelope=1: no dip; 3 levels of peak at most)."""
    path = save(tmp_path, "edge.png", halves(600, 300, (40, 40, 40), (220, 220, 220)), "png")

    def edge_row(scaler):
        (frame,) = scaler.prepare(path, [(100, 100)], "fit")
        y = frame.height // 2
        return [pixel(frame, x, y)[0] for x in range(frame.width)], frame.method

    row, method = edge_row(ImageScaler(settle_seconds=0))
    assert method == METHOD_LANCZOS3
    dark, bright = row[15], row[-16]  # the flat parts, away from the edge
    assert dark - min(row) >= 4, f"no dip below the dark side: {dark} / {min(row)}"
    assert max(row) - bright >= 4, f"no peak above the bright side: {bright} / {max(row)}"

    fallback = ImageScaler(settle_seconds=0)
    fallback._gst_failure = "switched off for this test"
    row, method = edge_row(fallback)
    assert method == METHOD_BILINEAR
    assert min(row) == row[15] and max(row) == row[-16]  # monotonic, no ringing


def test_in_ci_gstreamer_must_be_there_so_the_lanczos_path_is_the_one_tested():
    """Without GStreamer every test above runs on the bilinear path and stays green, so only this
    test and the verify step of the workflow keep the Lanczos path from silently dropping out."""
    assert HAVE_GST or os.environ.get("CI") != "true", "CI must have GStreamer videoscale"


# -- the picture probe of the image source --------------------------------------------------------


def _without_loader(monkeypatch, name):
    """gdk-pixbuf lists no loader for the format called *name* (a missing WebP loader)."""
    real = GdkPixbuf.Pixbuf.get_formats
    monkeypatch.setattr(
        GdkPixbuf.Pixbuf,
        "get_formats",
        staticmethod(lambda: [f for f in real() if f.get_name() != name]),
    )


def test_the_probe_refuses_a_picture_no_installed_loader_reads(tmp_path, monkeypatch):
    from slideshow_lock.scaling import probe_loadable

    good = save(tmp_path, "ok.png", solid(20, 10, RED), "png")
    webp = tmp_path / "x.webp"
    webp.write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(40))
    probe_loadable(good)  # a loadable picture passes
    _without_loader(monkeypatch, "webp")  # whether this machine has a WebP loader is not assumed
    with pytest.raises(ValueError, match="no installed gdk-pixbuf loader"):
        probe_loadable(str(webp))
    probe_loadable(good)
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not a picture at all")
    with pytest.raises(ValueError, match="not a recognised image header"):
        probe_loadable(str(junk))  # the header sniff of the image source still runs first


@pytest.mark.parametrize("fmt", ["png", "jpeg", "tiff", "bmp"])
def test_the_probe_agrees_with_the_loaders_on_real_pictures_of_every_format_it_can_write(
    tmp_path, fmt
):
    """Same answer as asking ``get_file_info`` by name (what the probe used to do), per format."""
    from slideshow_lock.scaling import probe_loadable

    path = save(tmp_path, f"p.{fmt}", solid(40, 30, RED), fmt)
    assert GdkPixbuf.Pixbuf.get_file_info(path)[0] is not None
    probe_loadable(path)  # no exception: a loader exists for it


def test_the_probe_does_not_count_a_disabled_loader(tmp_path, monkeypatch):
    """A format that is listed but switched off cannot decode: it is as good as missing."""
    from slideshow_lock.scaling import probe_loadable

    path = save(tmp_path, "ok.png", solid(20, 10, RED), "png")

    class Format:
        def __init__(self, name, disabled):
            self.name, self.disabled = name, disabled

        def get_name(self):
            return self.name

        def is_disabled(self):
            return self.disabled

    monkeypatch.setattr(
        GdkPixbuf.Pixbuf, "get_formats", staticmethod(lambda: [Format("png", True)])
    )
    with pytest.raises(ValueError, match="no installed gdk-pixbuf loader"):
        probe_loadable(path)
    monkeypatch.setattr(
        GdkPixbuf.Pixbuf,
        "get_formats",
        staticmethod(lambda: [Format("png", False), Format("jpeg", True)]),
    )
    probe_loadable(path)


def test_the_probe_opens_the_picture_once_and_never_by_name_again(tmp_path, monkeypatch):
    """``Pixbuf.get_file_info(path)`` opens the file again, blocking: a file swapped for a FIFO
    after the first look would hang the main loop there. The probe must not call it."""
    from slideshow_lock.scaling import probe_loadable

    path = save(tmp_path, "ok.png", solid(20, 10, RED), "png")

    def forbidden(*_args):
        raise AssertionError("the probe reopened the file by name")

    monkeypatch.setattr(GdkPixbuf.Pixbuf, "get_file_info", staticmethod(forbidden))
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a[1]) or real_open(*a, **k))
    probe_loadable(path)
    assert len(opened) == 1
    assert opened[0] & os.O_NONBLOCK  # and that one opening cannot block on a FIFO


def test_a_fifo_never_hangs_the_probe(tmp_path, monkeypatch):
    from slideshow_lock import image_source
    from slideshow_lock.scaling import probe_loadable

    fifo = tmp_path / "pic.png"
    os.mkfifo(fifo)
    with hard_timeout(10):
        with pytest.raises(ValueError, match="not a regular file"):
            probe_loadable(str(fifo))
        # and a file that turned into a FIFO after the header look, which is the swap in the
        # window between the two openings that the old probe could not survive
        monkeypatch.setattr(image_source, "probe_image", lambda _path: "png")
        probe_loadable(str(fifo))


def test_a_source_with_the_probe_does_not_queue_pictures_without_a_loader(
    tmp_path, monkeypatch, caplog
):
    from slideshow_lock.scaling import probe_loadable
    from tests.test_image_source import FakeWatcher, ManualScheduler, started

    for name in ("a.png", "b.png"):  # real pictures: the probe asks the real loaders
        save(tmp_path, name, solid(20, 10, RED), "png")
    for n in range(300):  # a folder full of pictures nothing here can show
        (tmp_path / f"w{n:03d}.webp").write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(40))
    _without_loader(monkeypatch, "webp")
    with caplog.at_level(logging.INFO, logger="slideshow_lock"):
        source = started(tmp_path, (FakeWatcher(), ManualScheduler()), probe=probe_loadable)
    assert [os.path.basename(p) for p in source.images()] == ["a.png", "b.png"]
    skipped = [m for m in caplog.messages if "no installed gdk-pixbuf loader" in m]
    assert 1 <= len(skipped) <= 10  # logged once per picture at most, burst-limited
    plain = started(
        tmp_path, (FakeWatcher(), ManualScheduler()), probe=lambda path: None
    )  # negative control: the default sniff keeps them all, which is what the probe fixes
    assert len(plain.images()) == 302


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


def test_ac5_a_noisy_photo_cut_off_at_60_percent_is_skipped_with_the_real_step_limit(
    tmp_path, scaler
):
    """A noisy 1600 x 1200 picture has thousands of stuffed bytes: a walk limit that is too low
    (1000 steps) would call its truncated half "complete" and show half a picture. No
    monkeypatching here: the limit is the one the code ships with."""
    rnd = random.Random(1)
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rnd.randbytes(1600 * 1200 * 3)),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        1600,
        1200,
        1600 * 3,
    )
    ok, jpeg = pixbuf.save_to_bufferv("jpeg", ["quality"], ["95"])
    assert ok
    steps = []
    real_find = bytes.find

    class Counting(bytes):
        def find(self, *args):
            steps.append(1)
            return real_find(self, *args)

    assert scaling.jpeg_is_complete(Counting(jpeg))
    assert 1000 < len(steps) < scaling.MAX_STRUCTURE_STEPS  # the picture tests what it must
    cut = jpeg[: len(jpeg) * 6 // 10]
    path = tmp_path / "cut.jpg"
    path.write_bytes(cut)
    with pytest.raises(ImageSkipped, match="incomplete"):
        scaler.prepare(old(path), [(400, 300)], "fit")


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
    assert frame.method == scaler.scaled_method


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
