"""Scaling-path measurement for the base-repository GTK4 stack (MEAS-1).

Every path gets the same decoded RGB GdkPixbuf as input and must hand back an RGB image of the
requested size. Timing covers everything between "decoded pixbuf" and "pixels ready to upload";
setup of the source image and conversion of the result for the metrics are outside the timer.

Usage: python3 bench.py CASE [--out DIR]    (CASE: A, B, C, D; see CASES)
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gst", "1.0")

import cairo  # noqa: E402
import numpy as np  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GLib, Gst  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402

Gst.init(None)

# name: (source width, fine scene?, source height or None for 16:9, output size)
CASES = {
    "A": ("downscale 23 MP 16:9 -> 2560x1440", 6400, True, None, (2560, 1440), True),
    "B": ("upscale 0.36 MP 16:9 -> 2560x1440", 800, False, None, (2560, 1440), True),
    "C": ("downscale 24 MP 3:2 -> 2160x1440", 6000, True, 4000, (2160, 1440), False),
    "D": ("upscale 0.48 MP 4:3 -> 1920x1440", 800, False, 600, (1920, 1440), False),
}


def pixbuf_from_rgb(arr):
    h, w = arr.shape[:2]
    data = GLib.Bytes.new(np.ascontiguousarray(arr).tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(data, GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)


def rgb_from_pixbuf(pb):
    w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
    raw = np.frombuffer(pb.read_pixel_bytes().get_data(), np.uint8)
    return raw.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)


# --- scalers: fn(pixbuf, out_w, out_h) -> something rgb_of() can turn into an RGB array ----------


def pixbuf_scaler(interp):
    def run(pb, w, h):
        return pb.scale_simple(w, h, interp)

    return run


def pixbuf_to_cairo(pb):
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, pb.get_width(), pb.get_height())
    cr = cairo.Context(surf)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    return surf


def cairo_scaler(filt):
    def run(pb, w, h):
        src = pixbuf_to_cairo(pb)
        dst = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
        cr = cairo.Context(dst)
        cr.scale(w / pb.get_width(), h / pb.get_height())
        cr.set_source_surface(src, 0, 0)
        pattern = cr.get_source()
        pattern.set_filter(filt)
        pattern.set_extend(cairo.EXTEND_PAD)
        cr.paint()
        dst.flush()
        return dst

    return run


GST_WARM = {}


def gst_pipeline(pb, w, h, props):
    pipe = Gst.parse_launch(
        "appsrc name=src format=time ! videoscale %s ! video/x-raw,format=RGB,width=%d,height=%d"
        " ! appsink name=sink sync=false" % (props, w, h)
    )
    src = pipe.get_by_name("src")
    src.set_property(
        "caps",
        Gst.Caps.from_string(
            "video/x-raw,format=RGB,width=%d,height=%d,framerate=1/1"
            % (pb.get_width(), pb.get_height())
        ),
    )
    pipe.set_state(Gst.State.PLAYING)
    return pipe, src, pipe.get_by_name("sink")


def gst_push_pull(pb, src, sink):
    src.emit("push-buffer", Gst.Buffer.new_wrapped_bytes(pb.read_pixel_bytes()))
    sample = sink.emit("pull-sample")
    buf = sample.get_buffer()
    return buf.extract_dup(0, buf.get_size())


def gst_scaler(props, warm=False):
    def run(pb, w, h):
        if warm:
            key = (props, pb.get_width(), pb.get_height(), w, h)
            if key not in GST_WARM:
                GST_WARM[key] = gst_pipeline(pb, w, h, props)
            _pipe, src, sink = GST_WARM[key]
            return (gst_push_pull(pb, src, sink), w, h)
        pipe, src, sink = gst_pipeline(pb, w, h, props)
        data = gst_push_pull(pb, src, sink)
        pipe.set_state(Gst.State.NULL)
        return (data, w, h)

    return run


def lanczos_taps(in_size, out_size, a=3):
    """Taps and weights of a separable Lanczos-a resampler (support widened when downscaling)."""
    scale = in_size / out_size
    filter_scale = max(scale, 1.0)
    support = a * filter_scale
    centers = (np.arange(out_size) + 0.5) * scale
    n = int(math.ceil(2 * support)) + 1
    first = np.floor(centers - support + 0.5).astype(np.int64)
    idx = first[:, None] + np.arange(n)[None, :]
    x = (idx + 0.5 - centers[:, None]) / filter_scale
    w = np.sinc(x) * np.sinc(x / a) * (np.abs(x) < a)
    w /= w.sum(axis=1, keepdims=True)
    return np.clip(idx, 0, in_size - 1), w.astype(np.float32)


def numpy_lanczos(pb, out_w, out_h, a=3):
    src = rgb_from_pixbuf(pb)
    in_h, in_w = src.shape[:2]
    idx_y, wy = lanczos_taps(in_h, out_h, a)
    idx_x, wx = lanczos_taps(in_w, out_w, a)
    mid = np.empty((out_h, in_w, 3), np.float32)
    step = 48
    for r0 in range(0, out_h, step):
        r1 = min(out_h, r0 + step)
        acc = np.zeros((r1 - r0, in_w, 3), np.float32)
        for k in range(idx_y.shape[1]):
            acc += wy[r0:r1, k, None, None] * src[idx_y[r0:r1, k]]
        mid[r0:r1] = acc
    out = np.empty((out_h, out_w, 3), np.uint8)
    for r0 in range(0, out_h, step):
        r1 = min(out_h, r0 + step)
        block = mid[r0:r1]
        acc = np.zeros((r1 - r0, out_w, 3), np.float32)
        for k in range(idx_x.shape[1]):
            acc += wx[None, :, k, None] * block[:, idx_x[:, k]]
        out[r0:r1] = np.clip(acc + 0.5, 0, 255).astype(np.uint8)
    return out


def rgb_of(result):
    if isinstance(result, GdkPixbuf.Pixbuf):
        return rgb_from_pixbuf(result)
    if isinstance(result, cairo.ImageSurface):
        h, w = result.get_height(), result.get_width()
        raw = np.frombuffer(result.get_data(), np.uint8).reshape(h, result.get_stride() // 4, 4)
        return raw[:, :w, 2::-1]
    if isinstance(result, tuple):
        data, w, h = result
        return np.frombuffer(data, np.uint8).reshape(h, w, 3)
    return result


P = GdkPixbuf.InterpType
F = cairo.Filter
SCALERS = [
    ("pixbuf nearest", pixbuf_scaler(P.NEAREST)),
    ("pixbuf tiles", pixbuf_scaler(P.TILES)),
    ("pixbuf bilinear", pixbuf_scaler(P.BILINEAR)),
    ("pixbuf hyper", pixbuf_scaler(P.HYPER)),
    ("cairo nearest", cairo_scaler(F.NEAREST)),
    ("cairo fast", cairo_scaler(F.FAST)),
    ("cairo bilinear", cairo_scaler(F.BILINEAR)),
    ("cairo good", cairo_scaler(F.GOOD)),
    ("cairo best", cairo_scaler(F.BEST)),
    ("gst nearest-neighbour", gst_scaler("method=nearest-neighbour")),
    ("gst bilinear", gst_scaler("method=bilinear")),
    ("gst 4-tap", gst_scaler("method=4-tap")),
    ("gst catrom", gst_scaler("method=catrom")),
    ("gst mitchell", gst_scaler("method=mitchell")),
    ("gst lanczos env=2 (default)", gst_scaler("method=lanczos")),
    ("gst lanczos env=3", gst_scaler("method=lanczos envelope=3")),
    ("gst lanczos env=3 n-threads=2", gst_scaler("method=lanczos envelope=3 n-threads=2")),
    ("gst lanczos env=3 warm", gst_scaler("method=lanczos envelope=3", warm=True)),
    ("numpy lanczos3 (own code)", numpy_lanczos),
]


def timeit(fn, reps):
    fn()  # warm-up (also fills library caches)
    walls, cpus = [], []
    for _ in range(reps):
        t0, c0 = time.perf_counter(), time.process_time()
        fn()
        walls.append((time.perf_counter() - t0) * 1000)
        cpus.append((time.process_time() - c0) * 1000)
    return {
        "min_ms": round(min(walls), 1),
        "median_ms": round(statistics.median(walls), 1),
        "cpu_median_ms": round(statistics.median(cpus), 1),
        "reps": reps,
    }


def psnr(a, b):
    mse = np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2)
    return round(99.0 if mse == 0 else 10 * math.log10(255.0**2 / mse), 2)


def edge_metrics(out):
    """Overshoot (levels beyond the 64/192 plateaus) and 10-90% rise width of the step edge."""
    out_w = out.shape[1]
    x, y, w, h = scene.STEP_RECT
    x0, y0 = int(round(x * out_w)), int(round(y * out_w))
    ew, eh = int(round(w * out_w)), int(round(h * out_w))
    block = out[y0 + 4 : y0 + eh - 4, x0 + 4 : x0 + ew - 4, 1].astype(np.float32)
    profile = block.mean(axis=0)
    mid = profile.size // 2
    near = block[:, max(0, mid - 12) : mid + 12]
    lo, hi = 64 + 0.1 * 128, 64 + 0.9 * 128
    seg = profile[max(0, mid - 12) : mid + 12]
    rise = None
    up = np.where(seg >= lo)[0]
    top = np.where(seg >= hi)[0]
    if up.size and top.size and up[0] > 0 and top[0] > 0:
        # linear interpolation of the first crossing of each threshold
        x_lo = up[0] - 1 + (lo - seg[up[0] - 1]) / (seg[up[0]] - seg[up[0] - 1])
        x_hi = top[0] - 1 + (hi - seg[top[0] - 1]) / (seg[top[0]] - seg[top[0] - 1])
        rise = round(float(x_hi - x_lo), 2)
    return {
        "overshoot_high": round(float(max(0.0, near.max() - 192)), 1),
        "undershoot_low": round(float(max(0.0, 64 - near.min())), 1),
        "rise_10_90_px": rise,
    }


def region_psnr(out, ref):
    res = {"full": psnr(out, ref)}
    for name in ("star", "gratings", "text", "noise"):
        res[name] = psnr(
            scene.crop(out, scene.REGIONS[name], out.shape[1]),
            scene.crop(ref, scene.REGIONS[name], out.shape[1]),
        )
    return res


def build_source(width, fine, height):
    arr = scene.render(width, fine)
    if height and height != arr.shape[0]:
        if height > arr.shape[0]:
            extra = height - arr.shape[0]
            arr = np.concatenate([arr, arr[:extra][::-1]], axis=0)
        else:
            arr = arr[:height]
    return np.ascontiguousarray(arr)


def save_png(arr, path):
    pixbuf_from_rgb(arr).savev(path, "png", [], [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    title, src_w, fine, src_h, (out_w, out_h), with_quality = CASES[args.case]
    os.makedirs(args.out, exist_ok=True)

    src = build_source(src_w, fine, src_h)
    pb = pixbuf_from_rgb(src)
    del src
    reps = 5 if pb.get_width() * pb.get_height() > 4_000_000 else 15
    print(
        "case",
        args.case,
        title,
        "source",
        pb.get_width(),
        pb.get_height(),
        "reps",
        reps,
        "load",
        os.getloadavg(),
        flush=True,
    )

    ref = scene.render(out_w, fine) if with_quality else None
    results = {"case": args.case, "title": title, "load": os.getloadavg(), "paths": {}}
    conv = timeit(lambda: pixbuf_to_cairo(pb), reps)
    results["pixbuf_to_cairo_surface"] = conv
    print("pixbuf->cairo surface", conv, flush=True)

    for name, fn in SCALERS:
        if args.only and args.only not in name:
            continue
        entry = {}
        try:
            entry.update(timeit(lambda fn=fn: fn(pb, out_w, out_h), 2 if "numpy" in name else reps))
            out = np.array(rgb_of(fn(pb, out_w, out_h)))
            if with_quality:
                entry["psnr_db"] = region_psnr(out, ref)
                entry["edge"] = edge_metrics(out)
                np.save(
                    os.path.join(args.out, "%s_%s.npy" % (args.case, name.replace(" ", "_"))), out
                )
        except Exception as exc:  # report, keep going
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        results["paths"][name] = entry
        print(name, json.dumps(entry), flush=True)
    if with_quality:
        results["reference_edge"] = edge_metrics(ref)
        np.save(os.path.join(args.out, "%s_reference.npy" % args.case), ref)
    with open(os.path.join(args.out, "results_%s.json" % args.case), "w") as fh:
        json.dump(results, fh, indent=1)


if __name__ == "__main__":
    main()
