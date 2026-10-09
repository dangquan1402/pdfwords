#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate synthetic PDFs with exact ground truth for tuning and testing the grouping.

Every page is built word by word with PDFium's page-object API (pypdfium2), using the
standard-14 fonts, so the text, word boundaries, lines, blocks, superscripts and reading
order are known exactly. Typographic parameters are drawn (seeded) from ranges that cover
ordinary documents: see RANGES below and docs/THRESHOLDS.md.

    python tools/make_synthetic.py            # -> tests/fixtures/synthetic.pdf + synthetic.json

Ground truth JSON: {"pages": [{"kind": ..., "words": [[text, line_id, block_id|null, order], ...],
                               "superscripts": [word_index, ...]}]}
block_id is null where block grouping is ambiguous by design (table cells).
"""
from __future__ import annotations

import ctypes
import json
import os
import random

import pypdfium2 as pdfium
import pypdfium2.raw as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")

FONTS = [b"Helvetica", b"Times-Roman", b"Courier", b"Helvetica-Oblique", b"Times-Italic"]
BOLD = [b"Helvetica-Bold", b"Times-Bold"]
# typographic ranges (multiples of the font size unless noted)
RANGES = {
    "size_pt": (7.0, 13.0),
    "leading": (1.10, 1.30),          # baseline-to-baseline distance inside a paragraph
    "word_space": (0.20, 0.50),       # visible gap between words (justified text stretches it)
    "tracking": (-0.03, 0.06),        # extra letter spacing on letter-spaced pages
    "para_extra": (0.50, 1.20),       # additional vertical gap between spaced paragraphs
    "indent": (1.0, 2.5),             # first-line indent of indented paragraphs
    "line_start_jitter_pt": (0.0, 0.3),
    "column_gap": (1.5, 4.0),         # gap between table cells / text columns
    "sup_size": (0.55, 0.75), "sup_rise": (0.25, 0.45), "sub_drop": (0.12, 0.20),
    "baseline_jitter": (-0.06, 0.06),  # small per-word baseline shifts some producers emit
    "fake_bold_offset": (0.01, 0.04),
}

SYL = ["ka", "lo", "mi", "ten", "ra", "ble", "sto", "ver", "an", "dor", "fi", "ll", "pe", "qu", "ent",
       "sh", "ion", "ex", "mo", "tt", "ri", "um", "co", "net", "ss", "ar", "wi", "ze", "on", "pro"]
PUNCT = ["", "", "", "", ",", ".", ";", ":"]


def word(rng):
    w = "".join(rng.choice(SYL) for _ in range(rng.randint(1, 4)))
    if rng.random() < 0.12:
        w = w.capitalize()
    if rng.random() < 0.06:
        w = str(rng.randint(1, 9999))
    return w + rng.choice(PUNCT)


def u(rng, key):
    lo, hi = RANGES[key]
    return rng.uniform(lo, hi)


_ADV = {}


class Page:
    def __init__(self, pdf, kind, w=612.0, h=792.0):
        self.pdf, self.kind = pdf, kind
        self.page = pdf.new_page(w, h)
        self.words = []          # [text, line, block, order]
        self.sup = []
        self.fake_bold = []
        self.line = self.block = -1
        self.order = 0

    def _obj(self, s, font, size):
        o = R.FPDFPageObj_NewTextObj(self.pdf.raw, font, ctypes.c_float(size))
        b = (s + "\0").encode("utf-16-le")
        R.FPDFText_SetText(o, ctypes.cast(ctypes.c_char_p(b), ctypes.POINTER(R.FPDF_WCHAR)))
        return o

    def _right(self, s, font, size):
        o = self._obj(s, font, size)
        l, bt, r, t = (ctypes.c_float() for _ in range(4))
        R.FPDFPageObj_GetBounds(o, l, bt, r, t)
        R.FPDFPageObj_Destroy(o)
        return r.value

    def advance(self, s, font, size):
        """Advance width of s (pen movement), measured as right(s + "X") - right("X")."""
        key = (s, font, round(size, 4))
        if key not in _ADV:
            _ADV[key] = self._right(s + "X", font, size) - self._right("X", font, size)
        return _ADV[key]

    def put(self, s, font, size, x, y, track=0.0):
        """Draw s with baseline origin at (x, y) (PDF coords); return the pen position after it."""
        if track:
            for ch in s:
                x = self.put(ch, font, size, x, y) + track * size
            return x - track * size
        o = self._obj(s, font, size)
        R.FPDFPageObj_Transform(o, 1, 0, 0, 1, x, y)
        R.FPDFPage_InsertObject(self.page.raw, o)
        return x + self.advance(s, font, size)

    def next_line(self):
        self.line += 1

    def next_block(self):
        self.block += 1

    def add(self, text, block=True):
        self.words.append([text, self.line, self.block if block else None, self.order])
        self.order += 1

    def done(self):
        R.FPDFPage_GenerateContent(self.page.raw)
        return {"kind": self.kind, "words": self.words, "superscripts": self.sup, "fake_bold": self.fake_bold}


def paragraph(pg, rng, x0, y, width, font, size, *, indent=0.0, track=0.0, sup=True, fake_bold=True, bullet=False):
    """Typeset one paragraph starting with baseline y; return the next baseline."""
    lead = u(rng, "leading") * size
    gap = u(rng, "word_space")
    n_words = rng.randint(12, 60)
    pg.next_block()
    first = True
    pending = []
    i = 0
    while i < n_words:
        pg.next_line()
        jitter = 0.0 if first else u(rng, "line_start_jitter_pt")
        x = x0 + (indent * size if first else 0.0) + jitter
        if bullet and first:
            x = pg.put("\u2022", font, size, x0, y) + 0.6 * size
            pg.add("\u2022")
        elif bullet:
            x = x0 + 1.2 * size + jitter
        first = False
        line_start = x
        while i < n_words:
            w = pending.pop() if pending else word(rng)
            adv = pg.advance(w, font, size) + (len(w) - 1) * track * size
            if x > line_start and x + adv + 1.0 * size > x0 + width:
                pending.append(w)
                break  # does not fit: next line (keeps the right margin / column gutter clear)
            r = rng.random()
            yy = y + (u(rng, "baseline_jitter") * size if 0.5 < r < 0.56 else 0.0)  # jittered baseline
            right = pg.put(w, font, size, x, yy, track)
            text = w
            if sup and r < 0.04:  # footnote marker: superscript attached to the word
                mark = str(rng.randint(1, 9))
                right = pg.put(mark, font, size * u(rng, "sup_size"), right + 0.02 * size, y + u(rng, "sup_rise") * size)
                pg.sup.append(len(pg.words))
                text += mark
            elif sup and r < 0.06:  # subscript attached to the word (same word, same line)
                mark = str(rng.randint(0, 9))
                right = pg.put(mark, font, size * u(rng, "sup_size"), right + 0.02 * size, y - u(rng, "sub_drop") * size)
                text += mark
            elif fake_bold and r < 0.10:  # overprinted "fake bold": same word drawn again, offset
                pg.put(w, font, size, x + u(rng, "fake_bold_offset") * size, yy, track)
                pg.fake_bold.append(len(pg.words))
            pg.add(text)
            x = right + gap * size
            i += 1
        y -= lead
    return y


def make(seed=20261009, out_dir=OUT, name="synthetic"):
    rng = random.Random(seed)
    pdf = pdfium.PdfDocument.new()
    pages = []

    # 1-4: prose, single column, mixed paragraph styles
    for k in range(4):
        pg = Page(pdf, "prose")
        y = 740.0
        size = u(rng, "size_pt")
        font = rng.choice(FONTS)
        track = u(rng, "tracking") if k == 3 else 0.0
        # heading
        pg.next_block(); pg.next_line()
        hs = size * 1.6
        x = 72.0
        for w in ("Section", str(k + 1), word(rng).capitalize()):
            x = pg.put(w, rng.choice(BOLD), hs, x, y) + 0.3 * hs
            pg.add(w)
        y -= hs * 2.0
        style = ["spaced", "indent", "mixed", "spaced"][k]
        while y > 120:
            ind = style == "indent" or (style == "mixed" and rng.random() < 0.5)
            y = paragraph(pg, rng, 72.0, y, 468.0, font, size, indent=u(rng, "indent") if ind else 0.0, track=track)
            if not ind:
                y -= u(rng, "para_extra") * size
        pages.append(pg.done())

    # 5-6: bullet lists
    for k in range(2):
        pg = Page(pdf, "list")
        y, size, font = 740.0, u(rng, "size_pt"), rng.choice(FONTS)
        while y > 150:
            y = paragraph(pg, rng, 90.0, y, 400.0, font, size, bullet=True, sup=False)
            y -= u(rng, "para_extra") * size
        pages.append(pg.done())

    # 7-9: two columns; k==2 emits the paragraphs in shuffled content order
    for k in range(3):
        pg = Page(pdf, "two_column")
        size, font = u(rng, "size_pt"), rng.choice(FONTS)
        gutter = u(rng, "column_gap") * size
        colw = (468.0 - gutter) / 2
        paras = []
        for col in range(2):
            y = 740.0
            while y > 160:
                paras.append((72.0 + col * (colw + gutter), y, rng.random()))
                y -= 120.0
        order = list(range(len(paras)))
        if k == 2:
            rng.shuffle(order)
        # typeset into a scratch structure so that ground-truth order stays column-major
        results = {}
        for idx in order:
            x0, y0, _ = paras[idx]
            start = len(pg.words)
            paragraph(pg, rng, x0, y0, colw, font, size, indent=0.0)
            results[idx] = (start, len(pg.words))
        # reading order = column-major paragraph order
        o = 0
        for idx in range(len(paras)):
            a, b = results[idx]
            for wi in range(a, b):
                pg.words[wi][3] = o
                o += 1
        pages.append(pg.done())

    # 10-12: tables (each cell is its own line; block grouping ambiguous -> null)
    for k in range(3):
        pg = Page(pdf, "table")
        size, font = u(rng, "size_pt"), rng.choice(FONTS)
        ncol = rng.randint(3, 6)
        cg = u(rng, "column_gap") * size
        y = 740.0
        widths = [rng.uniform(40, 80) for _ in range(ncol)]
        while y > 100:
            x = 60.0
            for c in range(ncol):
                if x + widths[c] > 560:
                    break
                pg.next_line()
                xx = x
                for _ in range(rng.randint(1, 2)):
                    w = word(rng).rstrip(",.;:")
                    xx = pg.put(w, font, size, xx, y) + 0.25 * size
                    pg.add(w, block=False)
                x = max(x + widths[c], xx) + cg
            y -= rng.uniform(1.4, 2.2) * size
        pages.append(pg.done())

    os.makedirs(out_dir, exist_ok=True)
    pdf.save(os.path.join(out_dir, name + ".pdf"))
    with open(os.path.join(out_dir, name + ".json"), "w") as f:
        json.dump({"seed": seed, "ranges": RANGES, "pages": pages}, f)
    return pages


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--out-dir", default=OUT)
    ap.add_argument("--name", default="synthetic")
    a = ap.parse_args()
    ps = make(a.seed, a.out_dir, a.name)
    print(len(ps), "pages,", sum(len(p["words"]) for p in ps), "words")
