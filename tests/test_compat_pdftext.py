# SPDX-License-Identifier: Apache-2.0
"""pdfwords.compat.pdftext: pdftext's API and output schema."""
import math
import os

import pytest

import pdfwords
from pdfwords.compat import pdftext as pt
from _featurepdf import feature_pdf
from conftest import FIXTURES

SPAN_KEYS = {"bbox", "text", "font", "rotation", "char_start_idx", "char_end_idx", "url", "superscript", "subscript"}
CHAR_KEYS = {"bbox", "char", "rotation", "font", "char_idx"}


@pytest.fixture(scope="module")
def feat(tmp_path_factory):
    f = tmp_path_factory.mktemp("pt") / "feat.pdf"
    f.write_bytes(feature_pdf())
    return str(f)


def _spans(page):
    return [s for b in page["blocks"] for ln in b["lines"] for s in ln["spans"]]


def test_schema(feat):
    pages = pt.dictionary_output(feat, keep_chars=True)
    assert len(pages) == 2
    p = pages[0]
    assert set(p) == {"page", "bbox", "width", "height", "rotation", "blocks", "refs"}
    assert p["width"] == 612 and p["height"] == 792 and p["rotation"] == 0 and isinstance(p["width"], int)
    for b in p["blocks"]:
        assert set(b) == {"bbox", "lines"} and isinstance(b["bbox"], list)
        for ln in b["lines"]:
            assert set(ln) == {"bbox", "spans"}
            for s in ln["spans"]:
                assert set(s) == SPAN_KEYS | {"chars"}
                assert set(s["font"]) == {"name", "flags", "size", "weight"}
                assert s["char_start_idx"] <= s["char_end_idx"]
                for c in s["chars"]:
                    assert set(c) == CHAR_KEYS
    sp = _spans(p)
    nl = [s for s in sp if s["text"] == "\n"]
    assert nl and all(s["font"] == {"name": "", "flags": 0, "size": 1.0, "weight": -1} for s in nl)
    assert nl[0]["char_end_idx"] == nl[0]["char_start_idx"] + 1
    assert sp[-1]["text"] != "\n"                                    # not after the page's last line
    idx = [c["char_idx"] for s in sp for c in s["chars"]]
    assert idx == sorted(idx)                                       # content order
    assert {s["font"]["name"] for s in sp if s["text"].strip()} == {"Helvetica"}


def test_links_refs_scripts(feat):
    pages = pt.dictionary_output(feat)
    sp = _spans(pages[0])
    urls = {s["text"]: s["url"] for s in sp if s["url"]}
    assert urls == {"Click here": "https://pdfwords.dev/docs", "second page": "#page-1-0"}
    assert pages[0]["refs"] == [] and len(pages[1]["refs"]) == 1
    r = pages[1]["refs"][0]
    assert (r.idx, r.page, r.coord, r.url) == (0, 1, [71.0, 91.0], "#page-1-0")   # as pdftext reports it
    sup = [s["text"] for s in sp if s["superscript"]]
    assert sup == ["2"]
    assert all("chars" not in s for s in sp)
    off = pt.dictionary_output(feat, disable_links=True)
    assert all(not s["url"] for s in _spans(off[0])) and off[1]["refs"] == []


def test_plain_text(feat):
    t = pt.plain_text_output(feat)
    assert t.startswith("Click here for the docs.\n\n")
    assert "hyphenation and continues." in t
    assert "hyphen-\nation" in pt.plain_text_output(feat, hyphens=True)
    per = pt.paginated_plain_text_output(feat, page_range=[1])
    assert len(per) == 1 and per[0].startswith("This line")
    with pytest.raises(ValueError):
        pt.plain_text_output(feat, page_range=[5])


def test_sort_and_rotation():
    path = os.path.join(FIXTURES, "rot90.pdf")
    p = pt.dictionary_output(path)[0]
    with pdfwords.open(path) as d:
        w, h = d[0].rect[2], d[0].rect[3]
    assert p["rotation"] == 90 and (p["width"], p["height"]) == (math.ceil(w), math.ceil(h))
    sp = [s for s in _spans(p) if s["text"].strip()]
    assert all(s["bbox"][2] <= p["width"] + 1 and s["bbox"][3] <= p["height"] + 1 for s in sp)
    blocks = pt.dictionary_output(os.path.join(FIXTURES, "synthetic.pdf"), sort=True, page_range=[0])[0]["blocks"]
    keys = [round(b["bbox"][1] / 1.25) * 1.25 for b in blocks]
    assert keys == sorted(keys)


def test_table_output(feat):
    tables = pt.table_output(feat, [{"tables": [[0, 0, 1224, 400]], "img_size": [1224, 1584]},
                                     {"tables": [[100, 100, 1224, 1584]], "img_size": [1224, 1584]}])
    t0 = tables[0][0]
    assert [c["text"] for c in t0][:2] == ["Click here for the docs.", "Visit https://example.com/page today."]
    assert t0[0]["bbox"][0] == pytest.approx(144.0)                    # 2x scale, table-relative
    assert tables[1][0][0]["bbox"][0] == pytest.approx(44.0)
    with pytest.raises(ValueError):
        pt.table_output(feat, [{"tables": [[0, 0, 1]], "img_size": [1, 1]}] * 2)
    with pytest.raises(ValueError):
        pt.table_output(feat, [], pages=pt.dictionary_output(feat))


def test_workers_and_bytes(feat, tmp_path):
    from _featurepdf import text_pdf
    data = text_pdf(b"BT /F1 12 Tf 72 700 Td (Page {n}) Tj ET", pages=24)
    f = tmp_path / "many.pdf"
    f.write_bytes(data)
    a = pt.dictionary_output(str(f))
    b = pt.dictionary_output(str(f), workers=2)
    assert a == b and [p["page"] for p in b] == list(range(24))
    assert pt.paginated_plain_text_output(data, workers=2)[23] == "Page 23"


def test_backends_identical(feat):
    if "rust" not in pdfwords.available_backends():
        pytest.skip("native extension not built")
    out = []
    for be in ("rust", "python"):
        os.environ["PDFWORDS_BACKEND"] = be
        try:
            out.append([pt.dictionary_output(p, keep_chars=True, quote_loosebox=q)
                        for p in (feat, os.path.join(FIXTURES, "simple_text.pdf"), os.path.join(FIXTURES, "rot90.pdf"))
                        for q in (True, False)])
        finally:
            del os.environ["PDFWORDS_BACKEND"]
    assert out[0] == out[1]


def test_password_error(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter(clone_from=os.path.join(FIXTURES, "simple_text.pdf"))
    w.encrypt("pw", algorithm="RC4-128")
    f = tmp_path / "enc.pdf"
    with open(f, "wb") as fh:
        w.write(fh)
    with pytest.raises(pt.PdfPasswordError):
        pt.plain_text_output(str(f))
    assert "Simple test document" in pt.plain_text_output(str(f), password="pw")
