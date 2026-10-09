# SPDX-License-Identifier: Apache-2.0
"""Text insertion: line layout (wrapping / alignment / rotation) and content-stream snippets."""
from __future__ import annotations

import math

from ._fonts import Base14Font, EmbeddedFont
from ._lexer import PdfStr, fmt

# writing direction u and line-advance direction v (top-left page coordinates) per rotation
_DIRS = {0: ((1, 0), (0, 1)), 90: ((0, -1), (1, 0)), 180: ((-1, 0), (0, -1)), 270: ((0, 1), (-1, 0))}


def check_rotate(rotate):
    r = int(rotate) % 360
    if r not in _DIRS:
        raise ValueError("rotate must be a multiple of 90")
    return r


def _color(c):
    if c is None:
        return None
    if isinstance(c, (int, float)):
        c = (c,)
    c = tuple(float(x) for x in c)
    if len(c) not in (1, 3, 4):
        raise ValueError("color must have 1 (gray), 3 (RGB) or 4 (CMYK) components in 0..1")
    return c


def color_op(c, stroke=False):
    c = _color(c)
    if c is None:
        return b""
    op = {1: b"g", 3: b"rg", 4: b"k"}[len(c)]
    if stroke:
        op = op.upper()
    return b" ".join(fmt(x) for x in c) + b" " + op + b"\n"


def _break_word(font, word, width, size):
    pieces, cur = [], ""
    for ch in word:
        if cur and font.text_width(cur + ch, size) > width + 1e-6:
            pieces.append(cur)
            cur = ""
        cur += ch
    pieces.append(cur)
    return pieces


def wrap(font, text, width, size):
    """Greedy word wrap on spaces (over-long words are broken by characters).
    Returns [(line_text, line_width, ends_paragraph)]."""
    out = []
    sw = font.text_width(" ", size) if _has(font, " ") else 0.0
    for para in text.split("\n"):
        line, lw = None, 0.0
        for w in para.split(" "):
            ww = font.text_width(w, size) if w else 0.0
            if ww > width + 1e-6:
                pieces = _break_word(font, w, width, size)
                if line is not None:
                    out.append((line, lw, False))
                for pc in pieces[:-1]:
                    out.append((pc, font.text_width(pc, size), False))
                line, lw = pieces[-1], font.text_width(pieces[-1], size)
                continue
            if line is None:
                line, lw = w, ww
            elif lw + sw + ww <= width + 1e-6:
                line, lw = line + " " + w, lw + sw + ww
            else:
                out.append((line, lw, False))
                line, lw = w, ww
        out.append((line or "", lw, True))
    return out


def _has(font, ch):
    try:
        font.encode(ch)
        return True
    except ValueError:
        return False


class TextRun:
    __slots__ = ("text", "x", "y", "extra_space")

    def __init__(self, text, x, y, extra_space=0.0):
        self.text, self.x, self.y, self.extra_space = text, x, y, extra_space


def build_snippet(font, res_name, runs, size, color, rotate, to_user, render_mode=0, opacity_gs=None):
    """runs: TextRun with (x, y) = baseline start in top-left page coords.
    Returns (content bytes, used glyphs for embedded fonts)."""
    r = check_rotate(rotate)
    ang = math.radians(r)
    cs, sn = round(math.cos(ang), 12), round(math.sin(ang), 12)
    parts = [b"q\n"]
    if opacity_gs:
        parts.append(fmt(opacity_gs) + b" gs\n")
    parts.append(color_op(color))
    parts.append(color_op(color, stroke=True))
    parts.append(b"BT\n" + fmt(res_name) + b" " + fmt(float(size)) + b" Tf\n")
    if render_mode:
        parts.append(fmt(int(render_mode)) + b" Tr\n")
    used = []
    for run in runs:
        ux, uy = to_user(run.x, run.y)
        parts.append(b" ".join(fmt(v) for v in (cs, sn, -sn, cs, ux, uy)) + b" Tm\n")
        if isinstance(font, EmbeddedFont):
            pairs = font.encode(run.text)
            used.extend(pairs)
            segs = _segments([g.to_bytes(2, "big") for g, _ in pairs], [ch == " " for _, ch in pairs])
        else:
            enc = font.encode(run.text)
            segs = _segments([bytes([b]) for b in enc], [b == 32 for b in enc])
        if run.extra_space and size:
            adj = -run.extra_space / size * 1000.0
            arr = []
            for k, seg in enumerate(segs):
                arr.append(PdfStr(seg, hex=isinstance(font, EmbeddedFont)))
                if k < len(segs) - 1:
                    arr.append(adj)
            parts.append(fmt(arr) + b" TJ\n")
        else:
            parts.append(fmt(PdfStr(b"".join(segs), hex=isinstance(font, EmbeddedFont))) + b" Tj\n")
    parts.append(b"ET\nQ\n")
    return b"".join(parts), used


def _segments(codes, is_space):
    """Split encoded text after each space (justification points)."""
    segs, cur = [], b""
    for c, sp in zip(codes, is_space):
        cur += c
        if sp:
            segs.append(cur)
            cur = b""
    segs.append(cur)
    return segs


def frame(rect, rotate):
    """Text frame of a box for a rotation: (origin, u, v, width_along_u, height_along_v)."""
    x0, y0, x1, y1 = rect
    r = check_rotate(rotate)
    u, v = _DIRS[r]
    origin = {0: (x0, y0), 90: (x0, y1), 180: (x1, y1), 270: (x1, y0)}[r]
    w, h = (x1 - x0, y1 - y0) if r in (0, 180) else (y1 - y0, x1 - x0)
    return origin, u, v, w, h


def layout_textbox(font, text, rect, size, align=0, rotate=0, lineheight=None):
    """Returns (runs, rc) like PyMuPDF: rc >= 0 is unused height; rc < 0 -> does not fit."""
    origin, u, v, w, h = frame(rect, rotate)
    lh = size * (lineheight if lineheight else (font.asc - font.desc))
    lines = wrap(font, text, w, size)
    needed = font.asc * size + (len(lines) - 1) * lh - font.desc * size
    rc = h - needed
    if rc < -1e-6:
        return [], rc
    runs = []
    nsp = None
    for i, (t, lw, last) in enumerate(lines):
        off = 0.0
        extra = 0.0
        if align == 1:
            off = (w - lw) / 2
        elif align == 2:
            off = w - lw
        elif align == 3 and not last:
            nsp = t.count(" ")
            if nsp:
                extra = (w - lw) / nsp
        dy = font.asc * size + i * lh
        x = origin[0] + off * u[0] + dy * v[0]
        y = origin[1] + off * u[1] + dy * v[1]
        runs.append(TextRun(t, x, y, extra))
    return runs, rc


def layout_point(font, text, point, size, rotate=0, lineheight=None):
    _, u, v, _, _ = frame((0, 0, 1, 1), rotate)
    lh = size * (lineheight if lineheight else (font.asc - font.desc))
    runs = []
    for i, t in enumerate(text.split("\n")):
        runs.append(TextRun(t, point[0] + i * lh * v[0], point[1] + i * lh * v[1]))
    return runs


__all__ = ["Base14Font", "EmbeddedFont", "layout_textbox", "layout_point", "build_snippet", "color_op"]
