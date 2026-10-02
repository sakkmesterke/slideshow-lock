"""Quality numbers of the GL renderer output saved by capture_ngl.py.

Needs out/A_reference.npy and out/B_reference.npy (written by bench.py A and bench.py B) and the
out/*_gsk-*.npy files of capture_ngl.py. Usage: python3 gl_metrics.py [OUT_DIR]
"""

import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

out_dir = sys.argv[1] if len(sys.argv) > 1 else "out"
for case in ("A", "B"):
    ref = np.load(os.path.join(out_dir, "%s_reference.npy" % case))
    for path in sorted(glob.glob(os.path.join(out_dir, "%s_gsk-*.npy" % case))):
        name = os.path.basename(path)[2:-4]
        out = np.load(path)
        print(
            case,
            name,
            json.dumps({"psnr": bench.region_psnr(out, ref), "edge": bench.edge_metrics(out)}),
        )
