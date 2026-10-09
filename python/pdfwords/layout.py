# SPDX-License-Identifier: Apache-2.0
"""Stage 2: group glyphs into lines and blocks with a pen-tracking state machine.

Follow the pen through the content stream and for each glyph measure, in units of
font size,
  * spacing     - distance along the writing direction from where the previous glyph ended
  * baseline shift - perpendicular distance from the previous glyph's baseline.
Small spacing -> same word; moderate forward spacing -> same line with a synthetic space;
big jump -> new line; baseline moved a little -> new line in the same block;
baseline moved a lot (or direction changed) -> new block.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from .chars import TEXT_FONT_SUPERSCRIPT



@dataclass(frozen=True)
class LayoutParams:
    """Grouping thresholds. Distances are in multiples of the font size unless noted.
    Defaults were derived by tools/tune_thresholds.py (see docs/THRESHOLDS.md)."""
    word_gap: float = 0.14        # forward gap >= this inserts a word space (below: kerning)
    column_gap: float = 0.85       # forward gap >= this ends the line (table cells, columns)
    baseline_tol: float = 0.85     # baseline shift < this keeps the glyph on the same line
    block_gap: float = 1.5        # baseline shift > this starts a new block
    dup_dist: float = 0.1          # same char drawn again closer than this -> overprint, drop
    gen_space_min: float = -0.46   # lowest gap accepted as a word break when PDFium reports a space
    indent_pt: float = 4.1        # line start right of the previous line start by > this (points) -> new block
    superscript_rise: float = 0.21  # baseline raised by > this -> superscript flag
    xycut_gap_x: float = 12.5      # XY-cut: min vertical gutter (points)
    xycut_gap_y: float = 2.0      # XY-cut: min horizontal gap (points)


DEFAULT_PARAMS = LayoutParams()

_BULLETS = set("*\u00b7\u2022\u2023\u2043\u204c\u204d\u2219\u25c9\u25cb\u25cf\u25d8\u25e6\u2619\u261a\u261b\u261c\u261d\u261e\u261f\u2765\u2767\u29be\u29bf\u25aa\u25ab\u25a0\u25a1\u2013\u2014-")


def _space_allowed_after(last):
    if last is None or last == " ":
        return False
    o = ord(last)
    return o < 0x700 or 0x2000 <= o <= 0x20CF


def _is_rtl(ch):
    return ch >= "\u0590" and unicodedata.bidirectional(ch) in ("R", "AL")


class Char:
    __slots__ = ("c", "origin", "bbox", "size", "font", "color", "synthetic", "bidi", "idx")

    def __init__(self, c, origin, bbox, size, font, color, synthetic=False, bidi=0, idx=-1):
        self.c, self.origin, self.bbox = c, origin, bbox
        self.size, self.font, self.color = size, font, color
        self.synthetic, self.bidi, self.idx = synthetic, bidi, idx


class Line:
    __slots__ = ("chars", "dir", "wmode", "bbox", "visual_rtl")

    def __init__(self, d):
        self.chars, self.dir, self.wmode, self.bbox, self.visual_rtl = [], d, 0, None, False


class Block:
    __slots__ = ("lines", "bbox", "number")

    def __init__(self):
        self.lines, self.bbox, self.number = [], None, 0


def _norm_asc_desc(font):
    asc, dsc = font.ascender, font.descender
    if asc <= 0 and dsc >= 0:
        asc, dsc = 0.8, -0.2
    h = asc - dsc
    if 0 < h < 1:  # boxes are at least one font-size tall (PyMuPDF-compatible output)
        asc, dsc = asc / h, dsc / h
    return asc, dsc


def _char_box(px, py, qx, qy, dx, dy, size, font, synthetic=False):
    asc, dsc = font.norm
    if dy == 0.0 and dx > 0:  # fast path: ordinary horizontal text
        return (px, py - asc * size, qx, py - dsc * size)
    ux, uy = dy, -dx  # "up" vector in y-down space
    ax, ay = ux * asc * size, uy * asc * size
    bx, by = ux * dsc * size, uy * dsc * size
    xs = (px + ax, px + bx, qx + ax, qx + bx)
    ys = (py + ay, py + by, qy + ay, qy + by)
    return (min(xs), min(ys), max(xs), max(ys))


def _union(a, b):
    if a is None:
        return b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def build_blocks(glyphs, clip=None, params=None):
    P = params or DEFAULT_PARAMS
    word_gap, column_gap, baseline_tol = P.word_gap, P.column_gap, P.baseline_tol
    block_gap, dup_dist, gen_min, indent = P.block_gap, P.dup_dist, P.gen_space_min, P.indent_pt
    blocks = []
    blk = None
    line = None
    pen = prev_origin = None
    last_c = None
    last_bidi = 0
    start_x = 0.0
    starts_with_bullet = False

    for g in glyphs:
        if g.font.norm is None:
            g.font.norm = _norm_asc_desc(g.font)
        box = _char_box(g.px, g.py, g.qx, g.qy, g.dx, g.dy, g.size, g.font)
        if clip is not None and (box[2] < clip[0] or box[0] > clip[2] or box[3] < clip[1] or box[1] > clip[3]):
            continue
        size = g.size or 1.0
        bidi = 1 if _is_rtl(g.c) else 0
        # combining marks: attach without moving the pen
        if line is not None and (g.cont or unicodedata.category(g.c) == "Mn"):
            # extra code points of one glyph ("fi" from a ligature) / combining marks:
            # zero-width box at the current pen, pen does not move
            if g.cont and pen is not None:
                box = _char_box(pen[0], pen[1], pen[0], pen[1], g.dx, g.dy, g.size, g.font)
            line.chars.append(Char(g.c, pen if g.cont and pen else (g.px, g.py), box, g.size, g.font, g.color, False, bidi, g.idx))
            last_c = g.c
            continue

        start_block = start_line = True
        insert_space = 0
        if line is not None and line.dir[0] * g.dx + line.dir[1] * g.dy >= 0.999:
            # fake bold / duplicate overprint
            if last_c == g.c and math.hypot(g.px - prev_origin[0], g.py - prev_origin[1]) / size < dup_dist:
                continue
            # baseline shift is measured in units of the current glyph size
            ls = size
            ddx, ddy = g.px - pen[0], g.py - pen[1]
            spacing = (g.dx * ddx + g.dy * ddy) / size
            shift = (-g.dy * ddx + g.dx * ddy) / ls
            start_block = False
            if abs(shift) < baseline_tol:
                if bidi != last_bidi:
                    start_line = False
                elif bidi:  # RTL: accept logical order (pen moving backwards) or visual order
                    adv = math.hypot(g.qx - g.px, g.qy - g.py) / size
                    lsp = (g.dx * (g.px - prev_origin[0]) + g.dy * (g.py - prev_origin[1])) / size + adv
                    if abs(lsp) < word_gap:
                        start_line = False
                    elif abs(spacing) < word_gap:
                        start_line, line.visual_rtl = False, True
                    elif -column_gap < lsp < 0:
                        insert_space, start_line = int(_space_allowed_after(last_c)), False
                    elif -column_gap < spacing < 0:
                        start_line = False
                    elif 0 < spacing < column_gap:
                        line.visual_rtl = True
                        insert_space, start_line = int(_space_allowed_after(last_c)), False
                else:
                    if g.gen_space and gen_min < spacing < column_gap:
                        # PDFium knows the true glyph advance (we only see its loose box, which
                        # includes italic overhang), so trust its word-gap detection here
                        insert_space, start_line = int(_space_allowed_after(last_c)), False
                    elif abs(spacing) < word_gap or -column_gap < spacing < 0:
                        start_line = False
                    elif 0 < spacing < column_gap:
                        insert_space, start_line = int(_space_allowed_after(last_c)), False
            elif abs(shift) <= block_gap:
                # next line of the same paragraph -- unless it is indented (new paragraph)
                if (g.px - start_x) > indent and not starts_with_bullet:
                    start_block = True
            else:
                start_block = True

        if start_block or blk is None:
            blk = Block()
            blocks.append(blk)
            line = None
        if start_line or line is None:
            line = Line((g.dx, g.dy))
            blk.lines.append(line)
            start_x = g.px
            starts_with_bullet = g.c in _BULLETS
        if insert_space and g.c != " ":
            sb = _char_box(pen[0], pen[1], g.px, g.py, g.dx, g.dy, g.size, g.font)
            line.chars.append(Char(" ", pen, sb, g.size, g.font, g.color, True, bidi))
        line.chars.append(Char(g.c, (g.px, g.py), box, g.size, g.font, g.color, False, bidi, g.idx))
        last_c, last_bidi = g.c, bidi
        prev_origin = (g.px, g.py)
        pen = (g.qx, g.qy)

    # finalise bboxes, drop empty lines, reorder visual RTL lines into logical order
    out = []
    for b in blocks:
        b.lines = [ln for ln in b.lines if ln.chars]
        for ln in b.lines:
            if ln.visual_rtl or (any(ch.bidi for ch in ln.chars) and _looks_visual(ln)):
                ln.chars = _visual_to_logical(ln.chars)
            x0 = y0 = float("inf")
            x1 = y1 = float("-inf")
            for ch in ln.chars:
                a0, a1, a2, a3 = ch.bbox
                if a0 < x0: x0 = a0  # noqa: E701
                if a1 < y0: y0 = a1  # noqa: E701
                if a2 > x1: x1 = a2  # noqa: E701
                if a3 > y1: y1 = a3  # noqa: E701
            bb = ln.bbox = (x0, y0, x1, y1)
            b.bbox = _union(b.bbox, bb)
        if b.lines:
            out.append(b)
    for i, b in enumerate(out):
        b.number = i
    return out


def _looks_visual(line):
    """RTL chars emitted left-to-right (PDFium never reorders): visual order."""
    rtl = [c for c in line.chars if c.bidi]
    return len(rtl) > 1 and rtl[-1].origin[0] > rtl[0].origin[0]


def _visual_to_logical(chars):
    """Minimal bidi: chars arrive in visual (left->right) order; produce logical order
    for an RTL paragraph: reverse the run order, keep LTR runs (digits/latin) intact."""
    runs, cur, cur_rtl = [], [], None
    for c in sorted(chars, key=lambda c: c.origin[0]):
        r = bool(c.bidi) if not c.c.isspace() else cur_rtl
        if cur and r != cur_rtl:
            runs.append((cur_rtl, cur))
            cur = []
        cur.append(c)
        cur_rtl = r
    if cur:
        runs.append((cur_rtl, cur))
    res = []
    for rtl, run in reversed(runs):
        res.extend(reversed(run) if rtl else run)
    return res


def is_superscript(line, ch, rise=None):
    """Horizontal line and the char's baseline noticeably above the line's first baseline."""
    if line.dir[0] < 0.999:
        return False
    if rise is None:
        rise = DEFAULT_PARAMS.superscript_rise
    return ch.origin[1] < line.chars[0].origin[1] - ch.size * rise


def make_spans(line, params=None):
    """Split a line into style runs (font, size, flags, colour)."""
    rise = (params or DEFAULT_PARAMS).superscript_rise
    spans = []
    cur = None
    key = None
    for ch in line.chars:
        flags = ch.font.flags | (TEXT_FONT_SUPERSCRIPT if is_superscript(line, ch, rise) else 0)
        k = (ch.font.name, round(ch.size, 3), flags, ch.color)
        if cur is None or k != key:
            cur = {"chars": [], "font": ch.font, "size": ch.size, "flags": flags, "color": ch.color, "bbox": None}
            spans.append(cur)
            key = k
        cur["chars"].append(ch)
        cur["bbox"] = _union(cur["bbox"], ch.bbox)
    return spans
