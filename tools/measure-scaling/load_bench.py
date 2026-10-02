"""Decode + scale cost for a 24 MP JPEG/PNG, and the pamscale subprocess path."""

import json
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402
from bench import GdkPixbuf, rgb_from_pixbuf  # noqa: E402

D = sys.argv[1] if len(sys.argv) > 1 else "data"
OW, OH = 2160, 1440
res = {}


def t(fn, reps=3):
    fn()
    return bench.timeit(fn, reps)


jpg, png = os.path.join(D, "c.jpg"), os.path.join(D, "c.png")
res["files_bytes"] = {"jpg_q90": os.path.getsize(jpg), "png": os.path.getsize(png)}
res["jpeg_decode_full"] = t(lambda: GdkPixbuf.Pixbuf.new_from_file(jpg))
res["jpeg_decode_full_plus_bilinear"] = t(
    lambda: GdkPixbuf.Pixbuf.new_from_file(jpg).scale_simple(OW, OH, GdkPixbuf.InterpType.BILINEAR)
)
res["jpeg_at_scale"] = t(lambda: GdkPixbuf.Pixbuf.new_from_file_at_scale(jpg, OW, OH, True))
lan = bench.gst_scaler("method=lanczos envelope=3")
res["jpeg_decode_full_plus_gst_lanczos3"] = t(
    lambda: lan(GdkPixbuf.Pixbuf.new_from_file(jpg), OW, OH)
)
res["png_decode_full"] = t(lambda: GdkPixbuf.Pixbuf.new_from_file(png), 2)
res["png_decode_full_plus_bilinear"] = t(
    lambda: GdkPixbuf.Pixbuf.new_from_file(png).scale_simple(OW, OH, GdkPixbuf.InterpType.BILINEAR),
    2,
)
full = GdkPixbuf.Pixbuf.new_from_file(jpg)
a = np.array(rgb_from_pixbuf(full.scale_simple(OW, OH, GdkPixbuf.InterpType.BILINEAR)))
b_pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(jpg, OW, OH, True)
b = np.array(rgb_from_pixbuf(b_pb))
res["at_scale_size"] = [b_pb.get_width(), b_pb.get_height()]
res["psnr_at_scale_vs_decode_then_bilinear"] = bench.psnr(a, b)

# pamscale subprocess path (PPM in, PPM out through files)
ppm = os.path.join(D, "c.ppm")


def pam(filter_args):
    def run():
        with open(ppm, "rb") as fin, open(os.path.join(D, "pam_out.ppm"), "wb") as fout:
            subprocess.run(
                ["pamscale", "-xsize", str(OW), "-ysize", str(OH)] + filter_args,
                stdin=fin,
                stdout=fout,
                stderr=subprocess.DEVNULL,
                check=True,
            )

    return run


for name, args in (
    ("pamscale default (pixel mixing)", []),
    ("pamscale -filter=lanczos", ["-filter=lanczos"]),
):
    t0 = time.perf_counter()
    pam(args)()
    res[name] = {"single_run_ms": round((time.perf_counter() - t0) * 1000, 1)}
json.dump(res, open(os.path.join(D, "load_results.json"), "w"), indent=1)
print(json.dumps(res, indent=1))
