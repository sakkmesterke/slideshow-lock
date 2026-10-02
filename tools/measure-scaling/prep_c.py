"""Create the 24 MP (6000x4000) source files used by the load-time and memory measurements."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "data"
os.makedirs(OUT, exist_ok=True)
src = bench.build_source(6000, True, 4000).copy()
rng = np.random.RandomState(7)
for r0 in range(0, src.shape[0], 500):  # sensor-like grain so that the JPEG is photo-sized
    block = src[r0 : r0 + 500].astype(np.int16)
    block += rng.normal(0, 3, block.shape[:2]).astype(np.int16)[..., None]
    src[r0 : r0 + 500] = np.clip(block, 0, 255).astype(np.uint8)
with open(os.path.join(OUT, "c.rgb"), "wb") as fh:
    fh.write(src.tobytes())
with open(os.path.join(OUT, "c.ppm"), "wb") as fh:
    fh.write(b"P6\n6000 4000\n255\n")
    fh.write(src.tobytes())
bench.pixbuf_from_rgb(src).savev(os.path.join(OUT, "c.jpg"), "jpeg", ["quality"], ["90"])
bench.pixbuf_from_rgb(src).savev(os.path.join(OUT, "c.png"), "png", [], [])
print({f: os.path.getsize(os.path.join(OUT, f)) for f in sorted(os.listdir(OUT))})
