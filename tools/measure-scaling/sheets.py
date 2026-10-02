"""Build side-by-side comparison sheets (1:1 crops, PNG) from the saved bench outputs."""

import json
import os
import sys

import cairo
import numpy as np

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
W = 2560  # output width of both headline cases

TILE_W, TILE_H, CAP_H, COLS = 400, 225, 26, 4

# (label shown, npy file stem, key in results json or None)
ORDER_DOWN = [
    ("reference (native render)", "reference", None),
    ("pixbuf nearest", "pixbuf_nearest", "pixbuf nearest"),
    ("pixbuf bilinear", "pixbuf_bilinear", "pixbuf bilinear"),
    ("pixbuf hyper (deprecated)", "pixbuf_hyper", "pixbuf hyper"),
    ("cairo good", "cairo_good", "cairo good"),
    ("cairo best", "cairo_best", "cairo best"),
    ("GL default = Gtk.Picture", "gsk-NglRenderer_default", "=default"),
    ("GL linear (explicit)", "gsk-NglRenderer_linear", "=linear"),
    ("gst lanczos env=2", "gst_lanczos_env=2_(default)", "gst lanczos env=2 (default)"),
    ("gst lanczos env=3", "gst_lanczos_env=3", "gst lanczos env=3"),
    ("numpy lanczos3 (own code)", "numpy_lanczos3_(own_code)", "numpy lanczos3 (own code)"),
    ("gst 4-tap", "gst_4-tap", "gst 4-tap"),
]

CROPS = {
    # name: (x, y, w, h in output pixels, zoom)
    "star-centre": (512 - 200, 691 - 112, 400, 225, 1),
    "gratings-fine": (1024, 112, 400, 225, 1),
    "gratings-mid": (1024 + 3 * 235, 112, 400, 225, 1),
    "text": (int(0.03 * W), int(0.46 * W), 400, 125, 1),
    "step-edge-4x": (int((0.40 + 0.09) * W) - 50, int(0.17 * W) + 60, 100, 56, 4),
}


def to_surface(tile):
    h, w = tile.shape[:2]
    buf = np.zeros((h, w, 4), np.uint8)
    buf[..., 0] = tile[..., 2]
    buf[..., 1] = tile[..., 1]
    buf[..., 2] = tile[..., 0]
    buf[..., 3] = 255
    return cairo.ImageSurface.create_for_data(bytearray(buf.tobytes()), cairo.FORMAT_ARGB32, w, h)


def make_sheet(case, order, results, crop_name, path, gl_timings):
    x, y, w, h, zoom = CROPS[crop_name]
    tiles = []
    for label, stem, key in order:
        f = os.path.join(OUT, "%s_%s.npy" % (case, stem))
        if not os.path.exists(f):
            continue
        arr = np.load(f)[y : y + h, x : x + w]
        if zoom > 1:
            arr = arr.repeat(zoom, axis=0).repeat(zoom, axis=1)
        cap = label
        if key and key.startswith("="):
            ms = gl_timings.get(case, {}).get(key[1:])
            if ms:
                cap += "  %d ms (llvmpipe)" % ms
        elif key and key in results["paths"]:
            cap += "  %.0f ms" % results["paths"][key]["median_ms"]
        tiles.append((cap, arr))
    th = max(t[1].shape[0] for t in tiles) + CAP_H
    tw = max(t[1].shape[1] for t in tiles)
    rows = (len(tiles) + COLS - 1) // COLS
    sheet = cairo.ImageSurface(
        cairo.FORMAT_RGB24, tw * COLS + 4 * (COLS - 1), th * rows + 4 * (rows - 1)
    )
    cr = cairo.Context(sheet)
    cr.set_source_rgb(0.12, 0.12, 0.12)
    cr.paint()
    cr.select_font_face("sans-serif")
    cr.set_font_size(14)
    for i, (cap, arr) in enumerate(tiles):
        ox, oy = (i % COLS) * (tw + 4), (i // COLS) * (th + 4)
        cr.set_source_rgb(0.95, 0.95, 0.95)
        cr.move_to(ox + 4, oy + 18)
        cr.show_text(cap)
        cr.set_source_surface(to_surface(arr), ox, oy + CAP_H)
        cr.paint()
    sheet.write_to_png(path)


def main():
    os.makedirs(os.path.join(OUT, "sheets"), exist_ok=True)
    for case, tag in (("A", "downscale"), ("B", "upscale")):
        results = json.load(open(os.path.join(OUT, "results_%s.json" % case)))
        timing_file = os.path.join(OUT, "gl_timings.json")
        gl_timings = json.load(open(timing_file)) if os.path.exists(timing_file) else {}
        order = (
            ORDER_DOWN
            if case == "A"
            else [(a, b, c) for (a, b, c) in ORDER_DOWN if b not in ("pixbuf_hyper",)]
        )
        for crop in CROPS:
            make_sheet(
                case,
                order,
                results,
                crop,
                os.path.join(OUT, "sheets", "%s-%s.png" % (tag, crop)),
                gl_timings,
            )
            print("wrote", tag, crop)


if __name__ == "__main__":
    main()
