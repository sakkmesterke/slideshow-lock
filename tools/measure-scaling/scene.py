"""Deterministic synthetic test scene for the scaling measurement.

The scene is defined in units of the image width, so it can be rendered natively at any
resolution. Rendering the same scene at the target resolution gives a reference image that
the scaled-down (or scaled-up) version can be compared against.
"""

import cairo
import numpy as np

# Rectangles (x, y, w, h) in units of image width, used both for drawing and for metrics.
REGIONS = {
    "star": (0.03, 0.10, 0.34, 0.34),
    "gratings": (0.40, 0.04, 0.55, 0.12),
    "edges": (0.40, 0.17, 0.55, 0.09),
    "text": (0.03, 0.46, 0.34, 0.09),
    "noise": (0.50, 0.30, 0.40, 0.20),
}
STEP_RECT = (0.40, 0.17, 0.18, 0.09)  # vertical 64 -> 192 step edge lives in here
DARK, LIGHT = 40, 215


def _noise_patch(width_px, x0, y0, w, h, octaves, seed=1234):
    """Band-limited value noise, a continuous function of canvas position (units of width)."""
    rs = np.random.RandomState(seed)
    xs = (np.arange(int(round(w * width_px))) + 0.5) / width_px + x0
    ys = (np.arange(int(round(h * width_px))) + 0.5) / width_px + y0
    acc = np.zeros((ys.size, xs.size), np.float32)
    amp = 1.0
    total = 0.0
    for k in range(octaves):
        cell = 0.06 / (2**k)
        gx = int(w / cell) + 3
        gy = int(h / cell) + 3
        lattice = rs.rand(gy, gx).astype(np.float32)
        fx = (xs - x0) / cell
        fy = (ys - y0) / cell
        ix = fx.astype(np.int64)
        iy = fy.astype(np.int64)
        tx = (fx - ix).astype(np.float32)
        ty = (fy - iy).astype(np.float32)
        # smoothstep keeps the lattice from being visible
        tx = tx * tx * (3 - 2 * tx)
        ty = ty * ty * (3 - 2 * ty)
        top = lattice[iy][:, ix] * (1 - tx) + lattice[iy][:, ix + 1] * tx
        bot = lattice[iy + 1][:, ix] * (1 - tx) + lattice[iy + 1][:, ix + 1] * tx
        acc += amp * (top * (1 - ty[:, None]) + bot * ty[:, None])
        total += amp
        amp *= 0.55
    acc /= total
    acc = (acc - acc.min()) / (acc.max() - acc.min() + 1e-9)
    lo = np.array([30, 70, 35], np.float32)
    mid = np.array([120, 150, 60], np.float32)
    hi = np.array([225, 205, 150], np.float32)
    t = acc[..., None]
    rgb = np.where(t < 0.5, lo + (mid - lo) * (t / 0.5), mid + (hi - mid) * ((t - 0.5) / 0.5))
    return rgb.astype(np.uint8)


def render(width, fine=True):
    """Return the scene as an (height, width, 3) uint8 RGB array; height = width * 9 / 16."""
    height = width * 9 // 16
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surf)
    cr.scale(width, width)

    grad = cairo.LinearGradient(0, 0, 1, 0.5625)
    grad.add_color_stop_rgb(0.0, 0.10, 0.12, 0.30)
    grad.add_color_stop_rgb(0.5, 0.20, 0.50, 0.55)
    grad.add_color_stop_rgb(1.0, 0.85, 0.55, 0.25)
    cr.set_source(grad)
    cr.paint()

    # Siemens star
    sx, sy, sr, spokes = 0.20, 0.27, 0.165, 72
    cr.set_source_rgb(DARK / 255, DARK / 255, DARK / 255)
    cr.arc(sx, sy, sr, 0, 6.283185307179586)
    cr.set_source_rgb(LIGHT / 255, LIGHT / 255, LIGHT / 255)
    cr.fill()
    cr.set_source_rgb(DARK / 255, DARK / 255, DARK / 255)
    for i in range(spokes):
        a0 = 2 * 3.141592653589793 * i / spokes
        a1 = a0 + 3.141592653589793 / spokes
        cr.move_to(sx, sy)
        cr.arc(sx, sy, sr, a0, a1)
        cr.close_path()
        cr.fill()

    # Line gratings (period given in target pixels for the fine scene, source pixels otherwise)
    if fine:
        periods = [p / 2560.0 for p in (2.0, 2.5, 3.0, 4.0, 6.0, 8.0)]
    else:
        periods = [p / 800.0 for p in (3.0, 4.0, 5.0, 6.0, 8.0, 12.0)]
    gx0, gy0, gw, gh = REGIONS["gratings"]
    cell_w = gw / len(periods)
    for k, period in enumerate(periods):
        x0 = gx0 + k * cell_w
        cr.set_source_rgb(LIGHT / 255, LIGHT / 255, LIGHT / 255)
        cr.rectangle(x0, gy0, cell_w * 0.9, gh)
        cr.fill()
        cr.set_source_rgb(DARK / 255, DARK / 255, DARK / 255)
        n = int(cell_w * 0.9 / period)
        for j in range(n):
            cr.rectangle(x0 + j * period, gy0, period / 2, gh)
        cr.fill()

    # Step edges between two grey plateaus (room for overshoot on both sides)
    ex, ey, ew, eh = STEP_RECT
    cr.set_source_rgb(64 / 255, 64 / 255, 64 / 255)
    cr.rectangle(ex, ey, ew, eh)
    cr.fill()
    cr.set_source_rgb(192 / 255, 192 / 255, 192 / 255)
    cr.rectangle(ex + ew * 0.5 + 0.00013, ey, ew * 0.5, eh)  # edge at a fractional pixel
    cr.fill()
    # slanted edge (~5.7 degrees) and 45 degree edge
    sx0 = ex + ew + 0.03
    cr.set_source_rgb(64 / 255, 64 / 255, 64 / 255)
    cr.rectangle(sx0, ey, 0.16, eh)
    cr.fill()
    cr.set_source_rgb(192 / 255, 192 / 255, 192 / 255)
    cr.move_to(sx0 + 0.04, ey)
    cr.line_to(sx0 + 0.16, ey)
    cr.line_to(sx0 + 0.16, ey + eh)
    cr.line_to(sx0 + 0.04 + 0.1 * eh, ey + eh)
    cr.close_path()
    cr.fill()

    # Text
    tx, ty, tw, th = REGIONS["text"]
    cr.set_source_rgb(0.95, 0.95, 0.9)
    cr.rectangle(tx, ty, tw, th)
    cr.fill()
    cr.set_source_rgb(0.05, 0.05, 0.05)
    cr.select_font_face("sans-serif")
    sizes = (0.008, 0.0055, 0.004) if fine else (0.02, 0.014, 0.011)
    y = ty
    for size in sizes:
        cr.set_font_size(size)
        y += size * 1.35
        cr.move_to(tx + 0.004, y)
        cr.show_text("The quick brown fox jumps over the lazy dog 0123456789")
    surf.flush()

    buf = np.frombuffer(surf.get_data(), np.uint8).reshape(height, surf.get_stride() // 4, 4)
    rgb = buf[:, :width, 2::-1].copy()  # BGRA -> RGB
    nx, ny, nw, nh = REGIONS["noise"]
    px, py = int(round(nx * width)), int(round(ny * width))
    patch = _noise_patch(width, nx, ny, nw, nh, 5 if fine else 4)
    rgb[py : py + patch.shape[0], px : px + patch.shape[1]] = patch
    return rgb


def crop(rgb, region, out_width):
    """Crop a REGIONS-style rectangle (units of width) out of an image of width out_width."""
    x, y, w, h = region
    x0, y0 = int(round(x * out_width)), int(round(y * out_width))
    return rgb[y0 : y0 + int(round(h * out_width)), x0 : x0 + int(round(w * out_width))]
