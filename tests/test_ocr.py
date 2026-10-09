# SPDX-License-Identifier: Apache-2.0
"""OCR fallback: same schema as the text layer, auto-triggered by needs_ocr()."""
import io
import os

import pytest

import pdfwords
from pdfwords import ocr as O
from conftest import FIXTURES

pytest.importorskip("PIL")
pytest.importorskip("numpy")

SIMPLE = os.path.join(FIXTURES, "simple_text.pdf")
ROT90 = os.path.join(FIXTURES, "rot90.pdf")


def scan_of(path, rotate=0, dpi=150):
    """Image-only PDF of page 0 (rendered unrotated), with /Rotate set like the source."""
    import pypdfium2 as pdfium
    img = pdfwords.open(path)[0].render(dpi=dpi, rotated=False, output="pil")
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=dpi)
    if not rotate:
        return buf.getvalue()
    pdf = pdfium.PdfDocument(buf.getvalue())
    pdf[0].set_rotation(rotate)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def oracle(path):
    """A fake engine that 'recognises' the source document's words exactly (displayed page)."""
    src = pdfwords.open(path)[0]
    calls = []

    def engine(img):
        calls.append(img.size)
        sx = img.size[0] / src.rect[2]
        return [{"text": w[4], "conf": 0.9, "bbox": (w[0] * sx, w[1] * sx, w[2] * sx, w[3] * sx),
                 "line": (w[5], w[6]), "block": w[5]} for w in src.get_text("words", rotated=True)]
    engine.calls = calls
    return engine


def _close(a, b, tol=0.6):
    return all(abs(x - y) <= tol for x, y in zip(a[:4], b[:4])) and a[4] == b[4]


@pytest.mark.parametrize("rotated", [False, True])
def test_oracle_roundtrip(rotated):
    scan = scan_of(SIMPLE)
    eng = oracle(SIMPLE)
    p = pdfwords.open(scan)[0]
    assert p.get_text() == "" and p.needs_ocr()
    got = p.get_text("words", ocr=eng, rotated=rotated)
    ref = pdfwords.open(SIMPLE)[0].get_text("words", rotated=rotated)
    assert len(got) == len(ref)
    assert all(_close(a, b) for a, b in zip(got, ref))
    d = p.get_text("dict", ocr=eng)
    sp = d["blocks"][0]["lines"][0]["spans"][0]
    assert sp["font"] == "OCR" and sp["conf"] == pytest.approx(0.9) and sp["text"] == "Simple test document"
    assert p.get_text("text", ocr=eng).startswith("Simple test document\nParagraph 0:")


def test_rotated_page_mapping():
    """/Rotate 90 scan: OCR runs on the upright image; boxes map back to both coordinate systems."""
    scan = scan_of(ROT90, rotate=90)
    eng = oracle(ROT90)
    p = pdfwords.open(scan)[0]
    assert p.rotation == 90
    for rotated in (False, True):
        got = p.get_text("words", ocr=eng, rotated=rotated)
        ref = pdfwords.open(ROT90)[0].get_text("words", rotated=rotated)
        assert [w[4] for w in got] == [w[4] for w in ref]
        assert all(_close(a, b, tol=1.0) for a, b in zip(got, ref))
    w, h = eng.calls[0]
    assert w > h        # rendered upright (displayed landscape page)


def test_auto_mode_and_document_default():
    calls = []

    def eng(img):
        calls.append(1)
        return [("hello world", 0.8, (10, 10, 110, 30))]
    text_pdf = pdfwords.open(SIMPLE, ocr={"engine": eng, "mode": "auto"})
    assert "Simple test document" in text_pdf[0].get_text() and not calls     # has a text layer
    scan = pdfwords.open(scan_of(SIMPLE), ocr={"engine": eng, "mode": "auto", "dpi": 72})
    ws = scan[0].get_text("words")
    assert [w[4] for w in ws] == ["hello", "world"] and len(calls) == 1
    assert ws[0][:4] == pytest.approx((10, 10, 10 + 500 / 11, 30))   # 72 dpi: px == pt; split by chars
    scan[0].get_text("blocks")
    assert len(calls) == 1                                      # cached per page + settings
    assert scan[0].get_text("words", ocr=False) == []
    md = pdfwords.to_markdown(scan)
    assert "hello world" in md


def test_min_conf_clip_and_errors():
    def eng(img):
        return [("keep", 0.9, (10, 10, 50, 30)), ("drop", 0.1, (100, 10, 140, 30)),
                {"text": "far", "conf": None, "bbox": (10, 500, 40, 520)}]
    p = pdfwords.open(scan_of(SIMPLE))[0]
    ws = p.get_text("words", ocr={"engine": eng, "dpi": 72, "min_conf": 0.5})
    assert [w[4] for w in ws] == ["keep", "far"]
    ws = p.get_text("words", ocr={"engine": eng, "dpi": 72}, clip=(0, 0, 200, 100))
    assert [w[4] for w in ws] == ["keep", "drop"]
    with pytest.raises(ValueError):
        p.get_text("words", ocr={"engine": "nope"})
    rd = p.ocr(eng, dpi=72)
    assert rd["blocks"] and rd["width"] == pytest.approx(612)


@pytest.mark.parametrize("engine", ["tesseract", "rapidocr", "ocrmac"])
def test_real_engines(engine):
    if engine not in O.available_engines():
        pytest.skip(f"{engine} not installed")
    p = pdfwords.open(scan_of(SIMPLE, dpi=200))[0]
    got = p.get_text("words", ocr={"engine": engine, "dpi": 200})
    ref = pdfwords.open(SIMPLE)[0].get_text("words")
    a = [w[4] for w in got]
    b = [w[4] for w in ref]
    import collections
    common = sum((collections.Counter(a) & collections.Counter(b)).values())
    assert common / len(b) > 0.85
    first = got[0]
    assert first[4] == "Simple" and abs(first[0] - ref[0][0]) < 4
