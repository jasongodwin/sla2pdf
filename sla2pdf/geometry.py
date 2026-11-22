"""Frame outlines, text-flow obstacles and the free space on one text line.

All coordinates are canvas pt (x right, y down), the same as `Item.x/y`.
"""
from __future__ import annotations

import logging
import math
import re

from .sla import Document, Item

log = logging.getLogger(__name__)

Point = tuple[float, float]
Polygon = list[Point]
Interval = tuple[float, float]

CURVE_SEGMENTS = 16
MIN_SEGMENT_WIDTH = 1.0
_EDGE_NUDGE = 1e-6

# Scribus TEXTFLOWMODE values (PageItem::TextFlowMode).
FLOW_NONE, FLOW_FRAME_SHAPE, FLOW_BOUNDING_BOX, FLOW_CONTOUR_LINE, FLOW_IMAGE_CLIP = range(5)

_PATH_TOKEN = re.compile(r"[MLCZmlcz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


# --- path data -------------------------------------------------------------

def _cubic(p0: Point, p1: Point, p2: Point, p3: Point) -> list[Point]:
    """Points along a cubic Bezier, excluding p0, ending at p3."""
    out = []
    for i in range(1, CURVE_SEGMENTS + 1):
        t = i / CURVE_SEGMENTS
        u = 1 - t
        out.append((
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        ))
    return out


def _flatten_path(path: str) -> list[Polygon]:
    """Scribus path data (absolute M/L/C/Z) -> one point list per subpath."""
    tokens = _PATH_TOKEN.findall(path)
    subpaths: list[Polygon] = []
    current: Polygon = []
    cmd = None
    i = 0

    def nums(n: int) -> list[float]:
        nonlocal i
        vals = [float(t) for t in tokens[i:i + n]]
        if len(vals) != n or any(t.isalpha() for t in tokens[i:i + n]):
            raise ValueError(f"bad path data near token {i}: {path[:80]!r}")
        i += n
        return vals

    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Zz":
                if current:
                    subpaths.append(current)
                current = []
                continue
        if cmd is None or cmd not in "MLC":
            raise ValueError(f"unsupported path command {cmd!r} in {path[:80]!r}")
        if cmd == "M":
            if current:
                subpaths.append(current)
            current = [tuple(nums(2))]  # type: ignore[list-item]
            cmd = "L"  # further pairs after M are line-tos
        elif cmd == "L":
            current.append(tuple(nums(2)))  # type: ignore[arg-type]
        else:  # C
            x1, y1, x2, y2, x, y = nums(6)
            start = current[-1] if current else (0.0, 0.0)
            current.extend(_cubic(start, (x1, y1), (x2, y2), (x, y)))
    if current:
        subpaths.append(current)
    return [_dedupe(sp) for sp in subpaths if len(sp) >= 3]


def _dedupe(points: Polygon) -> Polygon:
    """Drop repeated consecutive points and a closing point equal to the start."""
    out: Polygon = []
    for p in points:
        if not out or not _same(out[-1], p):
            out.append(p)
    if len(out) > 1 and _same(out[0], out[-1]):
        out.pop()
    return out


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


# --- item outlines ---------------------------------------------------------

def to_canvas(points: Polygon, item: Item) -> Polygon:
    """Frame-local points -> canvas: rotate about the top-left (clockwise, y down), then offset."""
    a = math.radians(item.rotation)
    c, s = math.cos(a), math.sin(a)
    return [(item.x + px * c - py * s, item.y + px * s + py * c) for px, py in points]


def to_frame_local(item: Item, polygon: Polygon) -> Polygon:
    """Canvas points -> the item's own unrotated coordinates, origin at its top-left
    (the inverse of the transform in `item_polygon`)."""
    a = math.radians(item.rotation)
    c, s = math.cos(a), math.sin(a)
    out = []
    for px, py in polygon:
        dx, dy = px - item.x, py - item.y
        out.append((dx * c + dy * s, -dx * s + dy * c))
    return out


def _local_rectangle(item: Item) -> Polygon:
    return [(0.0, 0.0), (item.width, 0.0), (item.width, item.height), (0.0, item.height)]


def item_polygon(item: Item, path: str | None = None) -> Polygon:
    """The item's outline in canvas pt: its `path` (or plain rectangle), rotated and placed.

    `path` overrides `item.path` (used for the contour line)."""
    path = item.path if path is None else path
    local = _local_rectangle(item)
    if path.strip():
        subpaths = _flatten_path(path)
        if len(subpaths) > 1:
            log.warning("item %s: path has %d subpaths, using the first", item.item_id, len(subpaths))
        if subpaths:
            local = subpaths[0]
    return to_canvas(local, item)


def _bounding_box(points: Polygon) -> Polygon:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def obstacle_polygon(item: Item) -> Polygon | None:
    """The area text must avoid around `item`, per its TEXTFLOWMODE; None when text ignores it."""
    mode = item.text_flow
    if mode == FLOW_NONE:
        return None
    if mode == FLOW_FRAME_SHAPE:
        return item_polygon(item)
    if mode == FLOW_CONTOUR_LINE:
        # Scribus keeps the contour line separately (copath); it starts as a copy of the path.
        return item_polygon(item, item.contour_path or item.path)
    if mode == FLOW_IMAGE_CLIP:
        log.info("item %s: text flow around image clip path not supported, using bounding box",
                 item.item_id)
    elif mode != FLOW_BOUNDING_BOX:
        log.warning("item %s: unknown text flow mode %s, using bounding box", item.item_id, mode)
    # Scribus uses the visual bounding rect of the (rotated) frame.
    return _bounding_box(to_canvas(_local_rectangle(item), item))


def frame_obstacles(doc: Document, frame: Item) -> list[Polygon]:
    """Obstacle polygons for `frame`: text-flow items on its page drawn above it.

    Scribus's rule (PageItem_TextFrame::calcAvailableRegion): an item is above the frame
    when it is on a higher layer level, or on the same layer and later in the document.
    """
    def level(layer: int) -> int:
        lay = doc.layers.get(layer)
        return lay.level if lay else layer

    frame_index = next(i for i, it in enumerate(doc.items) if it is frame)
    frame_level = level(frame.layer)
    out: list[Polygon] = []
    for i, it in enumerate(doc.items):
        if it is frame or it.page != frame.page or it.text_flow == FLOW_NONE:
            continue
        above = level(it.layer) > frame_level or (it.layer == frame.layer and i > frame_index)
        if above:
            poly = obstacle_polygon(it)
            if poly:
                out.append(poly)
    return out


# --- horizontal intervals --------------------------------------------------

def _scanline(poly: Polygon, y: float) -> list[Interval]:
    """Inside x-intervals of `poly` on the line y (even-odd, half-open edges)."""
    xs = []
    n = len(poly)
    for k in range(n):
        (x0, y0), (x1, y1) = poly[k], poly[(k + 1) % n]
        if y0 == y1:
            continue
        if min(y0, y1) <= y < max(y0, y1):
            xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    return [(xs[j], xs[j + 1]) for j in range(0, len(xs) - 1, 2) if xs[j + 1] > xs[j]]


def _intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _union(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _subtract(a: list[Interval], blocked: list[Interval]) -> list[Interval]:
    out = []
    for lo, hi in a:
        cur = lo
        for b0, b1 in blocked:
            if b1 <= cur or b0 >= hi:
                continue
            if b0 > cur:
                out.append((cur, b0))
            cur = max(cur, b1)
        if cur < hi:
            out.append((cur, hi))
    return out


def _sample_ys(y_top: float, y_bottom: float, polygons: list[Polygon]) -> list[float]:
    """Band top, middle and bottom (nudged inside so touching edges count as inside),
    plus every polygon corner lying within the band."""
    nudge = min(_EDGE_NUDGE, (y_bottom - y_top) / 4)
    lo, hi = y_top + nudge, y_bottom - nudge
    ys = {lo, hi, (y_top + y_bottom) / 2}
    for poly in polygons:
        ys.update(py for _, py in poly if lo < py < hi)
    return sorted(ys)


def _swept_intervals(poly: Polygon, ys: list[float]) -> list[Interval]:
    """Every x the polygon covers anywhere between the first and last of `ys`.

    `ys` must include every corner height inside that range, so between two consecutive
    heights each edge is straight and every inside interval slides without splitting:
    the k-th interval just below one height matches the k-th just above the next, and the
    polygon covers everything from the smaller start to the larger end."""
    out = [iv for y in ys for iv in _scanline(poly, y)]
    for ya, yb in zip(ys, ys[1:]):
        nudge = min(_EDGE_NUDGE, (yb - ya) / 4)
        upper, lower = _scanline(poly, ya + nudge), _scanline(poly, yb - nudge)
        if len(upper) == len(lower):
            out.extend((min(a[0], b[0]), max(a[1], b[1])) for a, b in zip(upper, lower))
        elif upper or lower:  # should not happen; block the whole span to stay safe
            both = upper + lower
            out.append((min(iv[0] for iv in both), max(iv[1] for iv in both)))
    return out


def free_segments(frame: Polygon, obstacles: list[Polygon], y_top: float, y_bottom: float,
                  left_pad: float, right_pad: float) -> list[Interval]:
    """Where text may go on the band [y_top, y_bottom]: inside `frame` and outside every
    obstacle over the whole band, sorted left to right, pieces under 1 pt dropped.

    Padding works like Scribus's column insets: text keeps `left_pad` from the frame's
    leftmost point and `right_pad` from its rightmost; a slanted edge or an obstacle
    is followed exactly, without extra distance.
    """
    if not frame:
        return []
    ys = _sample_ys(y_top, y_bottom, [frame, *obstacles])
    free: list[Interval] | None = None
    for y in ys:
        row = _scanline(frame, y)
        free = row if free is None else _intersect(free, row)
        if not free:
            return []
    blocked = _union([iv for obs in obstacles for iv in _swept_intervals(obs, ys)])
    xs = [p[0] for p in frame]
    column = [(min(xs) + left_pad, max(xs) - right_pad)]
    result = _intersect(_subtract(free or [], blocked), column)
    return [(lo, hi) for lo, hi in result if hi - lo >= MIN_SEGMENT_WIDTH]
