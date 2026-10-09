# SPDX-License-Identifier: Apache-2.0
"""Stage 3 (optional): reading order.

`sort_simple`  - PyMuPDF's sort=True behaviour: blocks by (y1, x0), lines by (y1, x0).
`sort_xycut`   - recursive XY-cut on block bboxes, column-aware:
   1. try a vertical cut (whitespace gap in the x-projection spanning the whole region)
      -> columns, processed left-to-right (right-to-left with rtl=True);
   2. otherwise cut horizontally at every y-gap, but first MERGE consecutive strips
      that share a column gutter, so paragraph gaps that happen to line up across two
      columns don't interleave the columns (the classic XY-cut failure);
   3. recurse; regions that can't be cut are ordered top-to-bottom, left-to-right.
"""
from __future__ import annotations


def _gaps(intervals, lo, hi, min_gap):
    """Return list of (gap_start, gap_end) in [lo,hi] not covered by intervals."""
    iv = sorted(intervals)
    gaps = []
    cur = lo
    for a, b in iv:
        if a - cur >= min_gap and cur > lo:
            gaps.append((cur, a))
        cur = max(cur, b)
    return gaps


def _bounds(items):
    return (min(i[1][0] for i in items), min(i[1][1] for i in items),
            max(i[1][2] for i in items), max(i[1][3] for i in items))


def _xcut(items, min_gap):
    x0, y0, x1, y1 = _bounds(items)
    gaps = _gaps([(b[0], b[2]) for _, b in items], x0, x1, min_gap)
    if not gaps:
        return None
    return gaps


def _split_by(items, gaps, axis):
    lo_i, hi_i = (0, 2) if axis == "x" else (1, 3)
    groups = [[] for _ in range(len(gaps) + 1)]
    for it in items:
        c = (it[1][lo_i] + it[1][hi_i]) / 2
        k = sum(1 for g in gaps if c > g[0])
        groups[k].append(it)
    return [g for g in groups if g]


def _xycut(items, min_gap_x, min_gap_y, rtl, depth=0):
    if len(items) <= 1 or depth > 50:
        return sorted(items, key=lambda it: (it[1][1], it[1][0]))
    xg = _xcut(items, min_gap_x)
    if xg:
        cols = _split_by(items, xg, "x")
        if rtl:
            cols = cols[::-1]
        out = []
        for col in cols:
            out.extend(_xycut(col, min_gap_x, min_gap_y, rtl, depth + 1))
        return out
    x0, y0, x1, y1 = _bounds(items)
    yg = _gaps([(b[1], b[3]) for _, b in items], y0, y1, min_gap_y)
    if not yg:
        return sorted(items, key=lambda it: (round(it[1][1], 0), it[1][0]))
    strips = _split_by(items, yg, "y")
    # merge consecutive strips that share a column gutter
    merged = [strips[0]]
    for s in strips[1:]:
        prev = merged[-1]
        if _xcut(prev, min_gap_x) and _xcut(s, min_gap_x) and _xcut(prev + s, min_gap_x):
            merged[-1] = prev + s
        else:
            merged.append(s)
    if len(merged) == 1 and len(merged[0]) == len(items) and not _xcut(items, min_gap_x):
        return sorted(items, key=lambda it: (it[1][1], it[1][0]))
    out = []
    for s in merged:
        if len(s) == len(items):  # no progress
            return sorted(items, key=lambda it: (it[1][1], it[1][0]))
        out.extend(_xycut(s, min_gap_x, min_gap_y, rtl, depth + 1))
    return out


def sort_xycut(blocks, min_gap_x=6.0, min_gap_y=1.0, rtl=False):
    """Column-aware order for horizontal blocks; rotated/vertical blocks (margin stamps,
    rotated table headers) are appended afterwards, ordered top-to-bottom."""
    horiz = [b for b in blocks if b.lines and b.lines[0].dir[0] > 0.99]
    other = [b for b in blocks if not (b.lines and b.lines[0].dir[0] > 0.99)]
    items = [(b, b.bbox) for b in horiz]
    ordered = [b for b, _ in _xycut(items, min_gap_x, min_gap_y, rtl)] if items else []
    return ordered + sorted(other, key=lambda b: (b.bbox[0], b.bbox[1]))


def sort_simple(blocks):
    for b in blocks:
        b.lines.sort(key=lambda ln: (ln.bbox[3], ln.bbox[0]))
    return sorted(blocks, key=lambda b: (b.bbox[3], b.bbox[0]))
