# SPDX-License-Identifier: Apache-2.0
"""Table detection from ruling lines and text alignment (written from scratch for pdfwords).

    tabs = page.find_tables()                 # strategy="auto": ruled grids, rule-delimited
    for t in tabs:                            #   ("booktabs") tables, optional text-only tables
        t.bbox, t.row_count, t.col_count
        t.extract()                           # [[cell text | None (covered by a span)], ...]
        t.to_markdown(); t.to_csv(); t.to_pandas(); t.cells   # cells with row/col spans + boxes

Method
1. Rules: stroked lines, thin filled rectangles and rectangle edges from get_drawings(),
   snapped and merged into maximal horizontal / vertical segments.
2. Regions: connected components of crossing/touching rules (grids), plus stacks of >= 2
   horizontal rules with matching extents and no verticals (rule-delimited tables). With
   strategy="text" (or "auto" + text=True), runs of >= 3 text lines that split into
   aligned segments become candidate regions too.
3. Rows: between horizontal rules when rules separate (almost) every text line, otherwise one
   row per text line. Columns: vertical rules plus whitespace corridors (x ranges that no word
   of the body rows covers; header rows above the first inner rule may span columns).
4. Cells: grid units; on ruled grids, units not separated by a rule are merged into spanning
   cells. Words are assigned by their centre.
5. Validation: >= min_rows x min_cols (and <= 20 columns), enough non-empty cells, short cell
   texts (prose boxes, chart grids and word-by-word figure labels are rejected).
"""
from __future__ import annotations

import csv
import io

SNAP = 1.5      # pt: rules closer than this are the same line
JOIN = 2.5      # pt: collinear pieces with gaps up to this are one rule
TOUCH = 2.5     # pt: tolerance for rules crossing / touching


# ---------------------------------------------------------------------- rules
def _merge(segs, horizontal):
    """Snap collinear segments and join overlapping/near ones. segs: (x0, y0, x1, y1)."""
    if horizontal:
        items = sorted(((s[1], s[0], s[2]) for s in segs))
    else:
        items = sorted(((s[0], s[1], s[3]) for s in segs))
    groups = []
    for pos, a, b in items:
        if groups and pos - groups[-1][0][-1] <= SNAP:
            groups[-1][0].append(pos)
            groups[-1][1].append((a, b))
        else:
            groups.append(([pos], [(a, b)]))
    out = []
    for poss, spans in groups:
        p = sum(poss) / len(poss)
        spans.sort()
        cur = list(spans[0])
        for a, b in spans[1:]:
            if a <= cur[1] + JOIN:
                cur[1] = max(cur[1], b)
            else:
                out.append((p, cur[0], cur[1]))
                cur = [a, b]
        out.append((p, cur[0], cur[1]))
    return out     # (position, start, end)


def rules(page, rotated=False, clip=None):
    from .objects import lines_and_rects
    h, v = lines_and_rects(page.get_drawings(rotated=rotated))
    H, V = _merge(h, True), _merge(v, False)
    if clip:
        H = [r for r in H if clip[1] - 1 <= r[0] <= clip[3] + 1 and r[2] >= clip[0] and r[1] <= clip[2]]
        V = [r for r in V if clip[0] - 1 <= r[0] <= clip[2] + 1 and r[2] >= clip[1] and r[1] <= clip[3]]
    return H, V


def _cross(h, v, tol=TOUCH):
    return h[1] - tol <= v[0] <= h[2] + tol and v[1] - tol <= h[0] <= v[2] + tol


def _regions(H, V, min_rule=8.0):
    """Connected components of crossing rules -> [(bbox, hs, vs)]; plus h-only stacks."""
    H = [h for h in H if h[2] - h[1] >= min_rule]
    V = [v for v in V if v[2] - v[1] >= min_rule]
    n = len(H) + len(V)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, h in enumerate(H):
        for j, v in enumerate(V):
            if _cross(h, v):
                parent[find(i)] = find(len(H) + j)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out, lone = [], []
    for idx in comps.values():
        hs = [H[i] for i in idx if i < len(H)]
        vs = [V[i - len(H)] for i in idx if i >= len(H)]
        if not vs and len(hs) == 1:
            lone.append(hs[0])
            continue
        if not hs or not vs:
            continue
        x0 = min([h[1] for h in hs] + [v[0] for v in vs])
        x1 = max([h[2] for h in hs] + [v[0] for v in vs])
        y0 = min([h[0] for h in hs] + [v[1] for v in vs])
        y1 = max([h[0] for h in hs] + [v[2] for v in vs])
        if x1 - x0 >= 20 and y1 - y0 >= 8:
            out.append(((x0, y0, x1, y1), hs, vs))
    # stacks of lone horizontal rules with (nearly) the same extent: rule-delimited tables
    lone.sort(key=lambda h: (round(h[1]), round(h[2]), h[0]))
    used = set()
    for i, h in enumerate(lone):
        if i in used or h[2] - h[1] < 40:
            continue
        stack = [h]
        for j in range(i + 1, len(lone)):
            g = lone[j]
            if j not in used and abs(g[1] - h[1]) <= 6 and abs(g[2] - h[2]) <= 6:
                stack.append(g)
                used.add(j)
        if len(stack) >= 2:
            stack.sort()
            # split the stack where consecutive rules are far apart (separate tables)
            run = [stack[0]]
            for g in stack[1:] + [None]:
                if g is not None and g[0] - run[-1][0] <= 250:
                    run.append(g)
                    continue
                if len(run) >= 2:
                    x0, x1 = min(r[1] for r in run), max(r[2] for r in run)
                    out.append(((x0, run[0][0], x1, run[-1][0]), run, []))
                run = [g] if g is not None else []
    return out


# ---------------------------------------------------------------------- text helpers
def _rows_of(words):
    """Cluster words into text rows (vertical overlap), top-down; words left-to-right."""
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        hh = w[3] - w[1]
        for r in rows[-3:]:
            top, bot = r[0]
            if min(bot, w[3]) - max(top, w[1]) >= 0.5 * min(hh, bot - top):
                r[1].append(w)
                r[0] = (min(top, w[1]), max(bot, w[3]))
                break
        else:
            rows.append([(w[1], w[3]), [w]])
    rows.sort(key=lambda r: r[0][0])
    return [(r[0], sorted(r[1], key=lambda w: w[0])) for r in rows]


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0.0


def _corridors(rows, x0, x1, min_gap, allowed=0):
    """Whitespace corridors: x ranges covered by at most `allowed` rows, >= min_gap wide,
    strictly inside [x0, x1] and between words. Returns [(separator x, gap x0, gap x1)]."""
    if not rows:
        return []
    step = 0.5
    nb = max(1, int((x1 - x0) / step) + 1)
    cov = [0] * nb
    for _, ws in rows:
        seen = [False] * nb
        for w in ws:
            a = max(0, int((w[0] - x0) / step))
            b = min(nb - 1, int((w[2] - x0) / step))
            for k in range(a, b + 1):
                if not seen[k]:
                    seen[k] = True
                    cov[k] += 1
    # extent actually used by text
    used = [k for k in range(nb) if cov[k] > 0]
    if not used:
        return []
    lo, hi = used[0], used[-1]

    def runs(pred, a, b):
        out, k = [], a
        while k <= b:
            if pred(k):
                s = k
                while k <= b and pred(k):
                    k += 1
                out.append((s, k))
            else:
                k += 1
        return out
    # valleys of the coverage profile, cleanest first: a run of coverage <= level becomes a
    # separator unless it already contains one (clean gaps on both sides of a sparse column;
    # gaps crossed only by a few spanning cells)
    chosen = []
    for level in range(0, allowed + 1):
        for a, b in runs(lambda k: cov[k] <= level, lo, hi):
            if (b - a) * step >= min_gap and not any(a <= c[0] < b for c in chosen):
                chosen.append(((a + b) // 2, a, b))
    # (separator x, gap start, gap end)
    seps = sorted((x0 + (c + 0.5) * step, x0 + a * step, x0 + b * step) for c, a, b in chosen)
    return seps


# ---------------------------------------------------------------------- table object
class Table:
    """A detected table. rows: [[text | None]] (None = covered by a spanning cell)."""

    def __init__(self, page, bbox, xs, ys, cells, strategy):
        self.page = page
        self.page_number = page.number if page is not None else None
        self.bbox = tuple(round(v, 3) for v in bbox)
        self._xs, self._ys = xs, ys
        self.cells = cells          # [{"row", "col", "rowspan", "colspan", "bbox", "text"}]
        self.strategy = strategy
        self.row_count = len(ys) - 1
        self.col_count = len(xs) - 1
        grid = [[None] * self.col_count for _ in range(self.row_count)]
        for c in cells:
            grid[c["row"]][c["col"]] = c["text"]
        self._grid = grid

    def __repr__(self):
        return f"<Table {self.row_count}x{self.col_count} bbox={self.bbox} strategy={self.strategy}>"

    @property
    def header(self):
        return [c if c is not None else "" for c in self._grid[0]] if self._grid else []

    @property
    def rows(self):
        """Per-row cell boxes (None where covered by a span), PyMuPDF-like."""
        out = [[None] * self.col_count for _ in range(self.row_count)]
        for c in self.cells:
            out[c["row"]][c["col"]] = c["bbox"]
        return out

    def extract(self):
        return [list(r) for r in self._grid]

    def to_markdown(self, header=True):
        from .markdown import table_markdown
        return table_markdown(self._grid, header=header)

    def to_text(self):
        return "\n".join("\t".join(c or "" for c in r) for r in self._grid)

    def to_csv(self, path=None, **kw):
        buf = io.StringIO()
        w = csv.writer(buf, **kw)
        for r in self._grid:
            w.writerow([c or "" for c in r])
        s = buf.getvalue()
        if path:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(s)
        return s

    def to_pandas(self, header=True):
        import pandas as pd
        rows = [[c or "" for c in r] for r in self._grid]
        if header and len(rows) > 1:
            cols, seen = [], {}
            for c in rows[0]:   # unique column names
                k = c or "col"
                seen[k] = seen.get(k, 0) + 1
                cols.append(k if seen[k] == 1 else f"{k}.{seen[k] - 1}")
            return pd.DataFrame(rows[1:], columns=cols)
        return pd.DataFrame(rows)

    def to_dict(self):
        return {"page": self.page_number, "bbox": self.bbox, "rows": self.extract(), "cells": self.cells,
                "row_count": self.row_count, "col_count": self.col_count, "strategy": self.strategy}


class TableList(list):
    """List of Table (also has .tables, like PyMuPDF's TableFinder)."""

    @property
    def tables(self):
        return list(self)


# ---------------------------------------------------------------------- building
def _cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _build(page, bbox, hs, vs, words, strategy, min_rows, min_cols, max_cols=20):
    x0, y0, x1, y1 = bbox
    inside = [w for w in words if x0 - 1 <= (w[0] + w[2]) / 2 <= x1 + 1 and y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1]
    if not inside:
        return None
    trows = _rows_of(inside)
    hgt = _median([w[3] - w[1] for w in inside]) or 8.0
    hys = sorted(_cluster([h[0] for h in hs], SNAP))
    # rows: ruled when rules separate (almost) every pair of consecutive text rows
    inner = [y for y in hys if y0 + 1 < y < y1 - 1]
    # ruled rows: the rules hold one text row per band (bands with several lines are wrapped
    # cells), e.g. full grids; otherwise one row per text line
    bands = list(zip([y0] + inner, inner + [y1]))
    per_band = [sum(1 for r in trows if a - 1 <= (r[0][0] + r[0][1]) / 2 <= b + 1) for a, b in bands]
    nonempty = [n for n in per_band if n]
    ruled_rows = (bool(vs) and len(trows) > 1 and bool(nonempty)
                  and sum(1 for n in nonempty if n == 1) >= 0.7 * len(nonempty))
    if ruled_rows:
        ys = _cluster([y0] + inner + [y1], SNAP)
    else:
        ys = [y0]
        for a, b in zip(trows, trows[1:]):
            ys.append((a[0][1] + b[0][0]) / 2)
        ys.append(y1)
        if not hs:   # text-only region: extend to the text
            ys[0] = min(ys[0], trows[0][0][0])
            ys[-1] = max(ys[-1], trows[-1][0][1])
    # columns: vertical rules spanning a good part of the table + whitespace corridors
    vx = [v[0] for v in vs if (min(v[2], y1) - max(v[1], y0)) >= 0.25 * (y1 - y0) and x0 + 1 < v[0] < x1 - 1]
    body = trows
    if not ruled_rows and inner:          # header rows (above the first inner rule) may span
        first = inner[0]
        bd = [r for r in trows if r[0][0] >= first - 1]
        if len(bd) >= 2:
            body = bd
    # word gaps inside a cell are ~0.25-0.35 em; column gaps are wider
    min_gap = max(3.0, 0.6 * hgt)
    allowed = 0 if len(body) < 4 else max(1, len(body) // 4)
    seps = _corridors(body, x0, x1, min_gap, allowed)
    # a corridor holding a vertical rule is that rule's column gap; full grids (ruled rows and
    # inner vertical rules) take their columns from the rules alone
    if ruled_rows and vx:
        seps = []
    xs_inner = sorted(_cluster(vx + [c for c, a, b in seps if not any(a - 2 <= v <= b + 2 for v in vx)], 2.0))
    xs = [x0] + [x for x in xs_inner if x0 + 1 < x < x1 - 1] + [x1]
    if not vs:   # rule-delimited / text tables: hug the text horizontally
        xs[0] = min(xs[0], min(w[0] for w in inside))
        xs[-1] = max(xs[-1], max(w[2] for w in inside))
    # drop boundaries that leave a column without any word (e.g. narrow gutters between rules)
    centres = [(w[0] + w[2]) / 2 for w in inside]
    k = 1
    while k < len(xs) - 1 and len(xs) > 2:
        if not any(xs[k - 1] <= c < xs[k] for c in centres):
            del xs[k if k > 1 else 1]
            k = max(1, k - 1)
            continue
        k += 1
    if len(xs) > 2 and not any(xs[-2] <= c <= xs[-1] for c in centres):
        del xs[-2]
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < min_rows or nc < min_cols or nc > max_cols:
        return None
    # unit cells -> merged cells (ruled grids only)
    parent = {(i, j): (i, j) for i in range(nr) for j in range(nc)}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def has_v(x, ya, yb):
        if x not in vx:
            return True        # text corridor: always a separator
        return any(abs(v[0] - x) <= SNAP and v[1] <= ya + 0.25 * (yb - ya) and v[2] >= yb - 0.25 * (yb - ya) for v in vs)

    def has_h(y, xa, xb):
        return any(abs(h[0] - y) <= SNAP and h[1] <= xa + 0.25 * (xb - xa) and h[2] >= xb - 0.25 * (xb - xa) for h in hs)

    if ruled_rows:
        for i in range(nr):
            for j in range(nc):
                if j + 1 < nc and not has_v(xs[j + 1], ys[i], ys[i + 1]):
                    parent[find((i, j + 1))] = find((i, j))
                if i + 1 < nr and not has_h(ys[i + 1], xs[j], xs[j + 1]):
                    parent[find((i + 1, j))] = find((i, j))
    groups = {}
    for k in parent:
        groups.setdefault(find(k), []).append(k)
    # only rectangular merges are kept; others fall back to unit cells
    cells_units = []
    for g in groups.values():
        rs = [k[0] for k in g]
        cs = [k[1] for k in g]
        if len(g) == (max(rs) - min(rs) + 1) * (max(cs) - min(cs) + 1):
            cells_units.append((min(rs), min(cs), max(rs) - min(rs) + 1, max(cs) - min(cs) + 1))
        else:
            cells_units += [(k[0], k[1], 1, 1) for k in g]
    # assign words
    import bisect
    owner = {}
    for (r, c, rs, cs) in cells_units:
        for i in range(r, r + rs):
            for j in range(c, c + cs):
                owner[(i, j)] = (r, c)
    content = {}
    for w in inside:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        i = min(max(bisect.bisect_right(ys, cy) - 1, 0), nr - 1)
        j = min(max(bisect.bisect_right(xs, cx) - 1, 0), nc - 1)
        content.setdefault(owner[(i, j)], []).append(w)
    cells = []
    filled = 0
    long_cells = 0
    for (r, c, rs, cs) in sorted(cells_units):
        ws = content.get((r, c), [])
        lines = _rows_of(ws)
        text = "\n".join(" ".join(w[4] for w in ln) for _, ln in lines)
        if text:
            filled += 1
            if len(text.split()) > 20:
                long_cells += 1
        cells.append({"row": r, "col": c, "rowspan": rs, "colspan": cs,
                      "bbox": (xs[c], ys[r], xs[c + cs], ys[r + rs]), "text": text})
    # validation: tables have short, mostly filled cells
    if filled < max(3, 0.3 * len(cells)) or long_cells > max(1, 0.1 * filled):
        return None
    multi = sum(1 for i in range(nr) if sum(1 for c in cells if c["row"] == i and c["text"]) >= 2)
    if multi < 2:
        return None
    return Table(page, (xs[0], ys[0], xs[-1], ys[-1]), xs, ys, cells, strategy)


def _text_regions(words, taken):
    """Candidate regions from text alignment alone: runs of >= 3 rows split into segments."""
    rows = _rows_of([w for w in words if not any(_in(w, t) for t in taken)])
    out = []
    run = []
    for (top, bot), ws in rows + [((1e9, 1e9), [])]:
        hgt = max(bot - top, 1.0)
        segs = 1
        for a, b in zip(ws, ws[1:]):
            if b[0] - a[2] >= max(6.0, 1.0 * hgt):
                segs += 1
        ok = segs >= 2 and len(ws) / segs <= 5
        if ok and (not run or top - run[-1][0][1] <= 1.5 * hgt):
            run.append(((top, bot), ws))
            continue
        if len(run) >= 3:
            allw = [w for _, r in run for w in r]
            out.append(((min(w[0] for w in allw), run[0][0][0], max(w[2] for w in allw), run[-1][0][1]), [], []))
        run = [((top, bot), ws)] if ok else []
    return out


def _in(w, b):
    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
    return b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1


def find_tables(page, clip=None, strategy="auto", *, rotated=False, min_rows=2, min_cols=2, text=None):
    """Detect tables. strategy: "auto" (ruled grids + rule-delimited tables; text-only tables
    too when text=True), "lines" (rules only), "text" (text alignment only)."""
    if strategy not in ("auto", "lines", "text"):
        raise ValueError("strategy must be 'auto', 'lines' or 'text'")
    words = page.get_text("words", rotated=rotated)
    if clip:
        words = [w for w in words if _in(w, clip)]
    regions = []
    if strategy in ("auto", "lines"):
        H, V = rules(page, rotated, clip)
        regions = _regions(H, V)
    found = []
    for bbox, hs, vs in sorted(regions, key=lambda r: (r[0][1], r[0][0])):
        if any(_contains(t.bbox, bbox) for t in found):
            continue
        t = _build(page, bbox, hs, vs, words, "lines" if vs else "rules", min_rows, min_cols)
        if t is not None:
            found.append(t)
    if strategy == "text" or (strategy == "auto" and text):
        for bbox, hs, vs in _text_regions(words, [t.bbox for t in found]):
            t = _build(page, bbox, hs, vs, words, "text", max(min_rows, 3), min_cols)
            if t is not None:
                found.append(t)
    found.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    return TableList(found)


def _contains(a, b, tol=2.0):
    return a[0] - tol <= b[0] and a[1] - tol <= b[1] and b[2] <= a[2] + tol and b[3] <= a[3] + tol
