"""Vertical CJK writing: stacked glyphs become wmode-1 lines, columns read right to left."""
import pytest

import pdfwords

from _featurepdf import vertical_cjk_pdf


def _page(backend):
    page = pdfwords.open(vertical_cjk_pdf(), backend=backend)[0]
    if not page.get_text().strip():
        pytest.skip("PDFium could not load a substitute CJK font here")
    return page


@pytest.mark.parametrize("backend", pdfwords.available_backends())
def test_vertical_columns(backend):
    page = _page(backend)
    d = page.get_text("dict")
    lines = [("".join(s["text"] for s in ln["spans"]), ln["wmode"]) for b in d["blocks"] for ln in b["lines"]]
    assert ("縦書きの文章", 1) in lines and ("二行目です。", 1) in lines
    assert lines.index(("縦書きの文章", 1)) + 1 == lines.index(("二行目です。", 1))   # right column first
    # horizontal CJK text is left alone, also a one-glyph second line
    assert ("横書きの行", 0) in lines and ("次", 0) in lines
    vb = [b for b in d["blocks"] if any(ln["wmode"] for ln in b["lines"])]
    assert len(vb) == 1 and len(vb[0]["lines"]) == 2
    x0, y0, x1, y1 = vb[0]["lines"][0]["bbox"]
    assert y1 - y0 > 4 * (x1 - x0)   # tall, narrow column


def test_vertical_parity():
    if len(pdfwords.available_backends()) < 2:
        pytest.skip("needs both backends")
    data = vertical_cjk_pdf()
    out = [pdfwords.open(data, backend=b)[0].get_text("rawdict") for b in ("rust", "python")]
    assert out[0] == out[1]
