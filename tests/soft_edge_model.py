"""What the pictures of a transition amount to at a window pixel, read the way the window reads
them: cut, move, turn, scale, then the soft edge's gradient. No GTK: the profile tests of the
``Draw`` values (``test_transition_soft_edges.py``) and the comparison with what GTK really renders
(``test_transition_edges_gdk.py``) use the same reading, so the two cannot drift apart unseen.

Only the fields of ``Draw`` and ``compose`` are used (``soft`` through ``getattr``), so that a
``Draw`` without a soft edge reads as the hard edge it is. The picture's own rectangle is taken to
be the window's size, which is what the tests' pictures are.
"""

from __future__ import annotations

import math

from slideshow_lock import transition_draw as td
from slideshow_lock.transitions import NEW


def soft_factor(draw, local_x, local_y, x, y, width, height):
    """The factor the soft edge of *draw* makes at a point (``local_*``: in the picture, ``x, y``:
    in the window): 1 where it has none."""
    soft = getattr(draw, "soft", None)
    if soft is None or soft.width <= 0.0:
        return 1.0
    if soft.kind == td.SOFT_CIRCLE:
        cx, cy, radius = draw.circle
        offset = math.hypot(x - cx, y - cy) / radius
        return td.stops_alpha(td.rim_stops(radius, soft.width), offset)
    if soft.kind == td.SOFT_CLIP:
        left, top, w, h = draw.clip
        px, py = x, y
    else:
        left, top, w, h = 0.0, 0.0, width, height
        px, py = local_x, local_y
    alpha = 1.0
    if "l" in soft.sides or "r" in soft.sides:
        stops = td.edge_stops(w, soft.width, "l" in soft.sides, "r" in soft.sides)
        alpha *= td.stops_alpha(stops, (px - left) / w)
    if "t" in soft.sides or "b" in soft.sides:
        stops = td.edge_stops(h, soft.width, "t" in soft.sides, "b" in soft.sides)
        alpha *= td.stops_alpha(stops, (py - top) / h)
    return alpha


def geometry(draw, x, y, width, height):
    """How much of the picture *draw* covers the window pixel centred at ``(x, y)``, 0 to 1, without
    its opacity: the cuts, the picture's own rectangle (moved, turned and scaled around the
    window's centre) and the soft edge."""
    if draw.clip is not None:
        cx, cy, cw, ch = draw.clip
        if not (cx <= x <= cx + cw and cy <= y <= cy + ch):
            return 0.0
    if draw.circle is not None:
        circle_x, circle_y, radius = draw.circle
        if math.hypot(x - circle_x, y - circle_y) > radius:
            return 0.0
    # the inverse of: translate(centre + d), rotate(angle), scale, translate(-centre)
    rx, ry = x - (width / 2 + draw.dx), y - (height / 2 + draw.dy)
    turn = math.radians(-draw.angle)
    ux = (rx * math.cos(turn) - ry * math.sin(turn)) / draw.scale + width / 2
    uy = (rx * math.sin(turn) + ry * math.cos(turn)) / draw.scale + height / 2
    if not (0.0 <= ux <= width and 0.0 <= uy <= height):
        return 0.0
    return soft_factor(draw, ux, uy, x, y, width, height)


def new_alpha(draws, x, y, width, height):
    """The opacity of the new picture at a window pixel: what it adds over the old one."""
    total = 0.0
    for draw in draws:
        if draw.layer == NEW:
            total = max(total, draw.opacity * geometry(draw, x, y, width, height))
    return total
