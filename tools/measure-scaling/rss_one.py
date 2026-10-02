"""Peak extra resident memory of one scaling path on the 24 MP source (one process per path)."""

import gc
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402
from bench import GdkPixbuf, GLib  # noqa: E402


def status(key):
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(key):
                return int(line.split()[1]) / 1024.0


name = sys.argv[1]
raw = open(os.path.join(os.environ.get("SCALING_DATA", "data"), "c.rgb"), "rb").read()
pb = GdkPixbuf.Pixbuf.new_from_bytes(
    GLib.Bytes.new(raw), GdkPixbuf.Colorspace.RGB, False, 8, 6000, 4000, 18000
)
del raw
gc.collect()
try:
    with open("/proc/self/clear_refs", "w") as fh:
        fh.write("5")
except OSError as exc:
    print("clear_refs failed", exc, file=sys.stderr)
base = status("VmRSS")
fn = dict(bench.SCALERS)[name]
out = fn(pb, 2160, 1440)
peak = status("VmHWM")
print(json.dumps({"path": name, "baseline_mb": round(base), "peak_extra_mb": round(peak - base)}))
