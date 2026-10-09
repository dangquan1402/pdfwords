# SPDX-License-Identifier: Apache-2.0
"""Output compatibility with PyMuPDF (optional: skipped when pymupdf is not installed).

PyMuPDF (AGPL) is only used here, as a black-box reference; it is never a runtime dependency."""
import difflib

import pytest

import pdfwords
from conftest import pdf_path

pymupdf = pytest.importorskip("pymupdf")
LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def fold(t):
    return "".join(LIG.get(c, c) for c in t)


@pytest.mark.parametrize("name,min_recall", [("simple_text.pdf", 0.999), ("irs_w9.pdf", 0.99),
                                             ("arxiv_attention.pdf", 0.98), ("arxiv_resnet.pdf", 0.99),
                                             ("table_camelot.pdf", 0.999)])
def test_words_match_pymupdf(backend, name, min_recall):
    f = pdf_path(name)
    md, pd = pymupdf.open(f), pdfwords.open(f)
    for i in range(min(3, md.page_count)):
        a = md[i].get_text("words")
        b = pd[i].get_text("words")
        sm = difflib.SequenceMatcher(None, [fold(w[4]) for w in a], [fold(w[4]) for w in b], autojunk=False)
        pairs = [(i1 + k, j1 + k) for t, i1, i2, j1, j2 in sm.get_opcodes() if t == "equal" for k in range(i2 - i1)]
        assert len(pairs) >= min_recall * len(a) - 2
        close = sum(all(abs(a[x][k] - b[y][k]) < 0.5 for k in range(4)) for x, y in pairs)
        assert close >= 0.97 * len(pairs)


def test_rotation_matches_pymupdf(backend):
    f = pdf_path("rot90.pdf")
    a = pymupdf.open(f)[0]
    b = pdfwords.open(f)[0]
    assert b.rotation == 90 and b.rect == tuple(a.rect)
    wa, wb = a.get_text("words"), b.get_text("words")
    assert [w[4] for w in wa] == [w[4] for w in wb]
    assert all(abs(x[0] - y[0]) < 0.5 and abs(x[2] - y[2]) < 0.5 for x, y in zip(wa, wb))
    for x, y in zip(wa, b.get_text("words", rotated=True)):
        r = pymupdf.Rect(x[:4]) * a.rotation_matrix
        assert abs(r.x0 - y[0]) < 2.5 and abs(r.y0 - y[1]) < 0.5 and abs(r.y1 - y[3]) < 0.5


def test_xycut_two_columns(backend):
    pg = pdfwords.open(pdf_path("arxiv_resnet.pdf"))[2]
    blocks = [b for b in pg.get_text("blocks", sort="xycut") if b[4].strip()]
    mid = pg.rect[2] / 2
    cols = ["L" if (b[0] + b[2]) / 2 < mid else "R" for b in blocks if b[2] - b[0] < mid]
    assert sum(1 for x, y in zip(cols, cols[1:]) if x != y) <= 2
