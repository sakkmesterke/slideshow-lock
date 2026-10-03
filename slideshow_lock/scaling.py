"""Decode one picture and scale it to a monitor's exact pixel size (CORE-2).

No window and no GTK here: this module only turns a file into a ``Frame``, a block of
RGB pixels that is shown 1:1 on the monitor. It runs on a worker thread (see
``slideshow_lock.preview``), never on the GLib main loop.

What the MEAS-1 report (``docs/measurements/scaling.md``, section 7) recommends is what
this does: pre-scale on the CPU to the exact device-pixel size and show the result 1:1.
The scaler is GStreamer's ``videoscale`` with ``method=lanczos envelope=3``. That is the
only path allowed to be called Lanczos-3, and only as "Lanczos as implemented by
videoscale" (it quantises its taps, so it is not bit-exact Lanczos). If GStreamer cannot
be used the fallback is ``GdkPixbuf`` ``BILINEAR``, logged once at WARNING level. Nothing
here claims anything "better" than that.

Geometry (``plan_render``):

* ``fit``: the whole picture, aspect ratio kept, centred, bars on the short side.
* ``fill``: the picture covers the monitor, aspect ratio kept, the overflow is cropped
  from the centre. A portrait picture on a landscape monitor therefore fills the
  monitor's width. The crop is made in source pixels before scaling, so the scaler never
  produces pixels that are thrown away.
* ``fill`` with ``pan``: a portrait picture that overflows vertically is scaled to the
  full overflow height, and the window scrolls over it. Not done when that frame would
  be huge (``MAX_PAN_PIXEL_FACTOR``, ``MAX_TEXTURE_SIDE``): the centre crop is used.

A picture that cannot be shown raises ``ImageSkipped`` with a one-line reason. That is
the contract with the controller: skip, log, go on. The header sniffing of the image
source (CORE-4) cannot see these cases, so they are handled here:

* a decoder error (deeply damaged PNG, JPEG with a valid header and no image data),
* a file still being copied: it changed while it was read, it was modified a moment ago,
  or its structure ends early (``incomplete_reason``: JPEG without end-of-image marker, PNG
  without IEND, BMP shorter than its header says). Those checks exist because the
  gdk-pixbuf loader decodes a truncated JPEG, PNG or BMP without any error and returns a
  partly empty picture (measured, gdk-pixbuf 2.42.10). GIF and TIFF report truncation
  themselves (measured), so they are left to the decoder.
* a picture larger than ``MAX_PIXELS``: refused from the size announced in the header,
  before any pixel buffer is allocated. The loader is told to produce a 0 x 0 picture,
  which stops it at once: measured with a 30000 x 30000 PNG, that costs 0 ms and 17 MB,
  while abandoning the decode after the size was announced still cost 3.3 s.

Not detected: a JPEG that is damaged inside the entropy-coded data but structurally
complete. libjpeg conceals that, and gdk-pixbuf reports no error, so the damaged picture
is shown as decoded.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

_LOG = logging.getLogger(__name__)

SCALING_FIT = "fit"
SCALING_FILL = "fill"

#: Pictures above this many pixels are refused (a decompression bomb in the picture folder
#: must not take the session down). Derived, not guessed (``docs/preview.md``, section 4):
#: preparing one picture peaked at 9.0 to 9.4 bytes per source pixel, whatever the width (36
#: and 50 MP, ``fit``, the mode that copies the whole picture; a panning frame is worse, about
#: 11 bytes per pixel, see ``docs/preview.md``, section 4), so a frame of 512 MiB holds 512 MiB /
#: 9.4 B = 57 MP, rounded down to 50 MP (about 450 MiB measured at 50 MP on two 4K-class
#: monitors). This bounds the memory of the decode step; it does not protect against an
#: out-of-memory kill of the whole process.
MAX_PIXELS = 50_000_000

#: The structure checks below stop after this many steps and say "complete" (the decoder
#: then decides): a crafted file must not keep the worker busy for tens of seconds. A real
#: 250 MB JPEG has about 1 million stuffed bytes, a real PNG some thousand chunks.
MAX_STRUCTURE_STEPS = 1_000_000

#: Files above this size are refused before they are read into memory.
MAX_FILE_BYTES = 256 * 1024 * 1024

#: A file modified less than this long ago is treated as still being written.
SETTLE_SECONDS = 2.0

#: A panning frame may hold at most this many times the monitor's pixels.
MAX_PAN_PIXEL_FACTOR = 6

#: Largest side of any frame handed to the GPU (the usual texture limit).
MAX_TEXTURE_SIDE = 16384

METHOD_LANCZOS3 = "lanczos3-videoscale"
METHOD_BILINEAR = "bilinear-gdkpixbuf"
METHOD_NONE = "none"  # the picture already had the target size

_FEED_CHUNK = 1 << 16
_ROW_BLOCK_BYTES = 4 << 20  # pixbuf rows are read in blocks of about this many bytes
_PULL_TIMEOUT_NS = 15 * 1_000_000_000


class ImageSkipped(Exception):
    """The picture cannot be shown. The message is a one-line reason for the log."""


@dataclass(frozen=True)
class Frame:
    """Pixels ready to be shown 1:1: tightly typed RGB, 8 bits per channel, 3 bytes per pixel."""

    path: str
    width: int
    height: int
    stride: int
    pixels: Any  # a GLib.Bytes made on the worker thread (any buffer is accepted by the window)
    method: str
    pan_range: Tuple[int, int] = (0, 0)  # how far the window may scroll in x and y, in pixels


@dataclass(frozen=True)
class RenderPlan:
    """What to cut from the source and how large to make it."""

    crop: Tuple[int, int, int, int]  # x, y, w, h in source pixels
    out: Tuple[int, int]  # size of the scaled result in pixels
    pan_range: Tuple[int, int]  # (0, 0) unless the result is meant to be scrolled


def device_size(logical_width: int, logical_height: int, scale: float) -> Tuple[int, int]:
    """Pixel size of a monitor window: ``round(logical size * surface scale)``, never 0."""
    return max(1, round(logical_width * scale)), max(1, round(logical_height * scale))


def plan_render(
    src_w: int, src_h: int, target_w: int, target_h: int, mode: str, pan: bool = False
) -> RenderPlan:
    """Work out the crop and the output size for one monitor. Pure arithmetic, no I/O."""
    if min(src_w, src_h, target_w, target_h) < 1:
        raise ValueError("sizes must be positive")
    if mode not in (SCALING_FIT, SCALING_FILL):
        raise ValueError(f"scaling must be {SCALING_FIT!r} or {SCALING_FILL!r}, got {mode!r}")

    if mode == SCALING_FIT:
        k = min(target_w / src_w, target_h / src_h)
        out = (min(target_w, max(1, round(src_w * k))), min(target_h, max(1, round(src_h * k))))
        return RenderPlan((0, 0, src_w, src_h), out, (0, 0))

    k = max(target_w / src_w, target_h / src_h)
    if pan and src_h > src_w and target_w / src_w >= target_h / src_h:
        # Portrait picture, width is the limiting side: keep the whole height and scroll.
        out_w, out_h = target_w, max(target_h, round(src_h * k))
        if (
            out_h > target_h
            and out_h <= MAX_TEXTURE_SIDE
            and out_w * out_h <= MAX_PAN_PIXEL_FACTOR * target_w * target_h
        ):
            return RenderPlan((0, 0, src_w, src_h), (out_w, out_h), (0, out_h - target_h))
    crop_w = min(src_w, max(1, round(target_w / k)))
    crop_h = min(src_h, max(1, round(target_h / k)))
    crop = ((src_w - crop_w) // 2, (src_h - crop_h) // 2, crop_w, crop_h)
    return RenderPlan(crop, (target_w, target_h), (0, 0))


def jpeg_is_complete(data: bytes) -> bool:
    """True if the JPEG marker structure of *data* reaches its end-of-image marker.

    Walks the segments (skipping by their length, so a thumbnail inside an APP segment
    does not count) and the entropy-coded data (skipping stuffed FF00 and restart
    markers). Anything after the first end-of-image marker (the video appended to a
    "motion photo", for example) is ignored. Returns True for data that is not a JPEG, and
    after ``MAX_STRUCTURE_STEPS`` steps (a truncation beyond that point is left to the decoder).
    """
    if data[:2] != b"\xff\xd8":
        return True
    n = len(data)
    i = 2
    for _step in range(MAX_STRUCTURE_STEPS):
        j = data.find(b"\xff", i)
        if j < 0 or j + 1 >= n:
            return False
        marker = data[j + 1]
        if marker == 0xFF:  # fill byte
            i = j + 1
        elif marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i = j + 2  # stuffed byte, TEM, restart, nested SOI: no length
        elif marker == 0xD9:
            return True
        else:
            if j + 4 > n:
                return False
            i = j + 2 + ((data[j + 2] << 8) | data[j + 3])
            if i > n:
                return False
    return True  # too many steps to follow: the decoder decides


def png_is_complete(data: bytes) -> bool:
    """True if the PNG chunk structure of *data* reaches its IEND chunk (not a PNG: True;
    also True after ``MAX_STRUCTURE_STEPS`` chunks)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return True
    n = len(data)
    i = 8
    for _step in range(MAX_STRUCTURE_STEPS):
        if i + 12 > n:
            return False
        length = int.from_bytes(data[i : i + 4], "big")
        end = i + 12 + length
        if end > n:
            return False
        if data[i + 4 : i + 8] == b"IEND":
            return True
        i = end
    return True  # too many chunks to follow: the decoder decides


def bmp_is_complete(data: bytes) -> bool:
    """True if a BMP holds as many bytes as its own header announces (not a BMP: True)."""
    if data[:2] != b"BM" or len(data) < 30:
        return True
    declared = int.from_bytes(data[2:6], "little")
    if declared and len(data) < declared:
        return False
    offset = int.from_bytes(data[10:14], "little")
    dib_size = int.from_bytes(data[14:18], "little")
    if dib_size == 12:  # the old OS/2 header
        width = int.from_bytes(data[18:20], "little")
        height = int.from_bytes(data[20:22], "little")
        bits = int.from_bytes(data[24:26], "little")
        compression = 0
    elif dib_size >= 40 and len(data) >= 34:
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        bits = int.from_bytes(data[28:30], "little")
        compression = int.from_bytes(data[30:34], "little")
    else:
        return True
    if compression not in (0, 3) or width <= 0 or height == 0 or bits == 0:
        return True  # compressed or unusual: the size cannot be worked out from the header
    row = ((width * bits + 31) // 32) * 4
    return len(data) >= offset + row * abs(height)


def incomplete_reason(data: bytes) -> Optional[str]:
    """Why *data* looks like a picture that was cut off, or None."""
    if not jpeg_is_complete(data):
        return "JPEG data ends before the end-of-image marker, probably incomplete"
    if not png_is_complete(data):
        return "PNG data ends before the IEND chunk, probably incomplete"
    if not bmp_is_complete(data):
        return "BMP file is shorter than its header says, probably incomplete"
    return None


def probe_loadable(path: str) -> None:
    """``ImageSource`` probe: the header sniff of the image source, then *a loader exists*.

    The header sniff names the format, and ``GdkPixbuf.Pixbuf.get_formats`` says which formats
    have an installed loader (not one for a ``.webp`` where the WebP loader is missing, for
    example). No loader raises ``ValueError``, which the image source logs as a skipped picture
    once, instead of the preview finding out again on every pass. The extension list of the
    image source is not narrowed: where a loader exists, the picture is shown.

    The check is wider than the ``get_file_info`` call it replaced: it does not parse the
    header, so a file whose first bytes are right and whose header is broken (``BM`` and then
    noise, a 3-byte JPEG, a PNG cut after its signature) passes here and is skipped, and logged
    once, when it is decoded. Such a file stays in the queue and fails at every pass over it;
    the controller's loop is bounded by the number of pictures in the queue, and a failing
    pass costs about 0.06 ms per small file. Reading the header here would be code of our own
    that parses untrusted bytes, and the FIFO safety would have to be measured again.

    The file is opened once, by ``probe_image``, with ``O_NONBLOCK`` and ``fstat``. Asking
    ``Pixbuf.get_file_info(path)`` instead would open it again by name with a blocking
    ``open``, which hangs on a file swapped for a FIFO in between (measured). This runs on the
    main loop, like the image source's own header read.
    """
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    from slideshow_lock.image_source import probe_image

    kind = probe_image(path)
    if kind not in {f.get_name() for f in GdkPixbuf.Pixbuf.get_formats() if not f.is_disabled()}:
        raise ValueError("no installed gdk-pixbuf loader reads this format")


def read_image_file(path: str, *, settle_seconds: float = SETTLE_SECONDS) -> bytearray:
    """Read a picture file for decoding, or raise ``ImageSkipped``.

    Opened ``O_NONBLOCK`` and checked with ``fstat`` on the descriptor, like the image
    source does, so a file swapped for a FIFO cannot hang the worker. The file is read into
    one buffer of the size ``fstat`` reported, so a large file is held in memory once.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError as exc:
        raise ImageSkipped(f"cannot be opened: {exc.strerror or exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ImageSkipped("not a regular file")
        if before.st_size > MAX_FILE_BYTES:
            raise ImageSkipped(f"file is larger than {MAX_FILE_BYTES >> 20} MiB")
        data = bytearray(before.st_size)
        view = memoryview(data)
        filled = 0
        while filled < len(data):
            count = os.readv(fd, [view[filled:]])
            if count == 0:
                break
            filled += count
        grew = filled == len(data) and bool(os.read(fd, 1))
        after = os.fstat(fd)
    except OSError as exc:
        raise ImageSkipped(f"cannot be read: {exc.strerror or exc}") from exc
    finally:
        os.close(fd)
    if (
        grew
        or filled != len(data)
        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    ):
        raise ImageSkipped("changed while it was read, probably still being copied")
    age = time.time() - before.st_mtime
    if 0 <= age < settle_seconds:
        raise ImageSkipped(f"modified {age:.1f} s ago, probably still being copied")
    if not data:
        raise ImageSkipped("empty file")
    reason = incomplete_reason(data)
    if reason is not None:
        raise ImageSkipped(reason)
    return data


def _to_gbytes(data):
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return GLib.Bytes.new(data)


def _gst_row_stride(width: int) -> int:
    """GStreamer's stride for 8-bit RGB: 3 bytes per pixel, rows padded to 4 bytes."""
    return (width * 3 + 3) & ~3


class ImageScaler:
    """Decode with GdkPixbuf and scale with videoscale Lanczos-3 (bilinear fallback).

    One instance per preview, used from one worker thread at a time (the controller's).
    """

    def __init__(self, *, settle_seconds: float = SETTLE_SECONDS) -> None:
        self._settle = settle_seconds
        self._gst: Optional[Any] = None  # the Gst module once initialised
        self._gst_failure: Optional[str] = None  # why GStreamer cannot be used, if it cannot
        self._fallback_logged = False

    def prepare(
        self, path: str, sizes: Sequence[Tuple[int, int]], mode: str, pan: bool = False
    ) -> List[Frame]:
        """Decode *path* once and render one frame per ``(width, height)`` in *sizes*."""
        data = read_image_file(path, settle_seconds=self._settle)
        pixbuf = self._decode(data)
        frames: List[Frame] = []
        cache = {}
        for target_w, target_h in sizes:
            plan = plan_render(
                pixbuf.get_width(), pixbuf.get_height(), target_w, target_h, mode, pan
            )
            if plan not in cache:
                cache[plan] = self._render(path, pixbuf, plan)
            frames.append(cache[plan])
        return frames

    # -- decoding --------------------------------------------------------------

    @staticmethod
    def _decode(data):
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        loader = GdkPixbuf.PixbufLoader()
        too_big: List[Tuple[int, int]] = []

        def on_size(_loader, width: int, height: int) -> None:
            if width * height > MAX_PIXELS:
                too_big.append((width, height))
                _loader.set_size(0, 0)  # the loader gives up at once, nothing is allocated

        loader.connect("size-prepared", on_size)

        def close_quietly() -> None:
            try:
                loader.close()
            except GLib.Error:
                pass

        def too_big_error() -> ImageSkipped:
            w, h = too_big[0]
            return ImageSkipped(f"{w}x{h} pixels is more than the {MAX_PIXELS} pixel limit")

        try:
            view = memoryview(data)
            for offset in range(0, len(data), _FEED_CHUNK):
                # ``bytes``, not the bytearray slice: PyGObject converts a bytearray item by
                # item (57 MiB BMP: 1.2 s CPU with the GIL held, 0.04 s with ``bytes``).
                loader.write(bytes(view[offset : offset + _FEED_CHUNK]))
                if too_big:
                    raise too_big_error()
            loader.close()
        except GLib.Error as exc:
            close_quietly()
            if too_big:
                raise too_big_error() from exc
            raise ImageSkipped(f"cannot be decoded: {exc.message}") from exc
        except ImageSkipped:
            close_quietly()
            raise
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            raise ImageSkipped("the decoder returned no picture")
        rotated = pixbuf.apply_embedded_orientation()  # EXIF: phone photos are stored sideways
        if rotated is not None:
            pixbuf = rotated
        if pixbuf.get_has_alpha():
            flat = GdkPixbuf.Pixbuf.new(
                GdkPixbuf.Colorspace.RGB, False, 8, pixbuf.get_width(), pixbuf.get_height()
            )
            flat.fill(0x000000FF)  # transparent areas are shown black, like the letterbox
            pixbuf.composite(
                flat,
                0,
                0,
                pixbuf.get_width(),
                pixbuf.get_height(),
                0,
                0,
                1.0,
                1.0,
                GdkPixbuf.InterpType.NEAREST,
                255,
            )
            pixbuf = flat
        return pixbuf

    # -- scaling -----------------------------------------------------------------

    def _render(self, path: str, pixbuf, plan: RenderPlan) -> Frame:
        x, y, w, h = plan.crop
        full = (x, y, w, h) == (0, 0, pixbuf.get_width(), pixbuf.get_height())
        part = pixbuf if full else pixbuf.new_subpixbuf(x, y, w, h)
        out_w, out_h = plan.out
        stride = _gst_row_stride(out_w)
        if (w, h) == (out_w, out_h):
            data, method = self._rows(part), METHOD_NONE
        else:
            data, method = self._lanczos(part, out_w, out_h), METHOD_LANCZOS3
            if data is None:
                from gi.repository import GdkPixbuf

                bilinear = part.scale_simple(out_w, out_h, GdkPixbuf.InterpType.BILINEAR)
                data, method = self._rows(bilinear), METHOD_BILINEAR
        # The copy into a GLib.Bytes is made here, on the worker thread, so the main loop does
        # not copy a frame (150 MB for a panning 4K frame) when it wraps it into a texture.
        return Frame(path, out_w, out_h, stride, _to_gbytes(data), method, plan.pan_range)

    @staticmethod
    def _rows(pixbuf) -> bytes:
        """The pixels of *pixbuf* with rows padded to GStreamer's 4-byte stride.

        Result and peak, per source pixel of 3 bytes: when the pixbuf already has that stride the
        pixels are used as they are, plus the padding its last row lacks (a pixbuf has none
        there, so a width whose three bytes per pixel are not a multiple of 4 needs one more
        copy). Otherwise (a crop shares its parent's wider rows) the frame is built in one
        buffer allocated up front and read from the pixbuf a block of rows at a time. Never a
        string per row joined at the end: that held a second full copy of the frame. The result
        is ``bytes`` and not ``bytearray`` because PyGObject hands only ``bytes`` to GLib and
        GStreamer in one piece (a ``bytearray`` is converted item by item: about 3.3 s of CPU
        for 150 MiB, 30 to 60 times a ``bytes``; the wall time follows the load of the machine).

        The padding bytes of a row are the pixbuf's own, not zeros, when the pixbuf already has
        the stride (only the tail padding of the last row is zero): the pixels are the same, and
        the pixels are the same.
        """
        width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
        want = _gst_row_stride(width)
        size = want * height
        if stride == want:
            data = pixbuf.read_pixel_bytes().get_data()
            if len(data) >= size:
                return data[:size]
            return data + bytes(size - len(data))
        row_bytes = width * 3
        out = bytearray(size)  # the padding bytes stay zero
        step = max(1, _ROW_BLOCK_BYTES // stride)
        for top in range(0, height, step):
            count = min(step, height - top)
            block = pixbuf.new_subpixbuf(0, top, width, count).read_pixel_bytes().get_data()
            view = memoryview(block)
            for row in range(count):
                start = (top + row) * want
                out[start : start + row_bytes] = view[row * stride : row * stride + row_bytes]
        return bytes(out)

    def _init_gst(self) -> Optional[Any]:
        if self._gst is not None or self._gst_failure is not None:
            return self._gst
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
            if Gst.ElementFactory.find("videoscale") is None:
                raise RuntimeError("the videoscale element is not installed")
            self._gst = Gst
        except Exception as exc:  # ImportError, ValueError (typelib), GLib.Error, RuntimeError
            self._gst_failure = f"{type(exc).__name__}: {exc}"
        return self._gst

    def _lanczos(self, pixbuf, out_w: int, out_h: int) -> Optional[bytes]:
        """Scale with videoscale Lanczos-3, or return None (and log once) if that is impossible."""
        gst = self._init_gst()
        reason = self._gst_failure
        if gst is not None:
            try:
                return self._run_pipeline(gst, pixbuf, out_w, out_h)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
        if not self._fallback_logged:
            self._fallback_logged = True
            _LOG.warning(
                "[slideshow] Lanczos scaling through GStreamer is unavailable (%s), "
                "using GdkPixbuf bilinear scaling instead",
                reason,
            )
        return None

    def _run_pipeline(self, gst, pixbuf, out_w: int, out_h: int) -> bytes:
        in_w, in_h = pixbuf.get_width(), pixbuf.get_height()
        pipeline = gst.parse_launch(
            "appsrc name=src format=time ! videoscale method=lanczos envelope=3 "
            f"! video/x-raw,format=RGB,width={out_w},height={out_h} "
            "! appsink name=sink sync=false"
        )
        try:
            src = pipeline.get_by_name("src")
            src.set_property(
                "caps",
                gst.Caps.from_string(
                    f"video/x-raw,format=RGB,width={in_w},height={in_h},framerate=1/1"
                ),
            )
            pipeline.set_state(gst.State.PLAYING)
            src.emit("push-buffer", gst.Buffer.new_wrapped(self._rows(pixbuf)))
            src.emit("end-of-stream")
            sample = pipeline.get_by_name("sink").emit("try-pull-sample", _PULL_TIMEOUT_NS)
            if sample is None:
                raise RuntimeError("videoscale produced no picture")
            buf = sample.get_buffer()
            size = buf.get_size()
            if size != _gst_row_stride(out_w) * out_h:
                raise RuntimeError(f"videoscale returned {size} bytes, not a tightly packed frame")
            return buf.extract_dup(0, size)
        finally:
            pipeline.set_state(gst.State.NULL)
