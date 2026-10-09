# SPDX-License-Identifier: Apache-2.0
"""0.2 reading features: links, annotations, widgets, outline, flatten, span urls, search,
text quality, streaming/workers, table cells, debug overlay."""
import io

import pytest

import pdfwords
from _featurepdf import feature_pdf, text_pdf

BACKENDS = pdfwords.available_backends()


@pytest.fixture(scope="module")
def data():
    return feature_pdf()


def test_links(data):
    p = pdfwords.open(data)[0]
    links = p.get_links()
    assert [lk["kind"] for lk in links] == [pdfwords.LINK_URI, pdfwords.LINK_GOTO]
    assert links[0]["uri"] == "https://pdfwords.dev/docs"
    assert links[0]["from"] == (72.0, 60.0, 128.0, 76.0)          # top-left coordinates
    assert links[1]["page"] == 1 and links[1]["to"] == (72.0, 92.0)  # /XYZ 72 700 on page 2
    web = p.get_links(web=True)
    auto = [lk for lk in web if lk.get("auto")]
    assert len(auto) == 1 and auto[0]["uri"] == "https://example.com/page"
    words = {w[4]: w[:4] for w in p.get_text("words")}
    r = auto[0]["from"]
    assert r[0] <= words["https://example.com/page"][0] + 1 and r[2] >= words["https://example.com/page"][2] - 1


def test_annots_and_widgets(data):
    p = pdfwords.open(data)[0]
    hl = p.annots("Highlight")
    assert len(hl) == 1
    h = hl[0]
    assert h["contents"] == "Check this" and h["author"] == "Reviewer" and h["id"] == "hl-1"
    assert h["stroke"] == (1.0, 1.0, 0.0) and "fill" not in h
    assert h["quads"] == [((72.0, 120.0), (140.0, 120.0), (72.0, 136.0), (140.0, 136.0))]
    assert {a["type"] for a in p.annots()} == {"Link", "Highlight", "Widget"}
    w = {x["field_name"]: x for x in p.widgets()}
    assert w["name"]["field_type"] == "Text" and w["name"]["field_value"] == "John Doe"
    assert w["name"]["field_label"] == "Your name" and w["name"]["author"] == ""
    assert w["agree"]["field_type"] == "CheckBox" and w["agree"]["checked"] is True


def test_toc(data):
    d = pdfwords.open(data)
    assert d.get_toc() == [[1, "Introduction", 1], [1, "Hyphenation", 2]]
    full = d.get_toc(simple=False)
    assert full[1][3]["page"] == 1 and full[1][3]["kind"] == pdfwords.LINK_GOTO


@pytest.mark.parametrize("backend", BACKENDS)
def test_flatten(data, backend):
    assert "John Doe" not in pdfwords.open(data, backend=backend)[0].get_text()
    flat = pdfwords.open(data, backend=backend, flatten=True)
    assert "Name: John Doe" in flat[0].get_text()
    assert "John Doe" in flat[0].get_text("words")[-1][4] or any(w[4] == "Doe" for w in flat[0].get_text("words"))


@pytest.mark.parametrize("backend", BACKENDS)
def test_span_urls(data, backend):
    d = pdfwords.open(data, backend=backend)[0].get_text
    dd = d("dict", links=True)
    spans = [(s["text"], s["url"]) for b in dd["blocks"] for ln in b["lines"] for s in ln["spans"]]
    assert ("Click here", "https://pdfwords.dev/docs") in spans
    assert ("https://example.com/page", "https://example.com/page") in spans
    assert ("second page", "#page=2") in spans
    assert all("chars" not in s for b in dd["blocks"] for ln in b["lines"] for s in ln["spans"])
    raw = d("rawdict", links=True)
    for b in raw["blocks"]:
        for ln in b["lines"]:
            for s in ln["spans"]:
                assert "".join(c["c"] for c in s["chars"]) == s["text"]
    assert "url" not in d("dict")["blocks"][0]["lines"][0]["spans"][0]


def test_span_urls_parity(data):
    if len(BACKENDS) < 2:
        pytest.skip("native extension not built")
    a = pdfwords.open(data, backend="rust")[0].get_text("rawdict", links=True)
    b = pdfwords.open(data, backend="python")[0].get_text("rawdict", links=True)
    assert a == b


def test_search_hyphenation_and_hit_max(data):
    d = pdfwords.open(data)
    hits = d[1].search_for("hyphenation")
    assert len(hits) == 2 and hits[0][1] < hits[1][1]          # one rect per line
    assert d[1].search_for("hyphenation", dehyphenate=False) == []
    assert len(d[1].search_for("well-known")) == 1
    assert len(d[0].search_for("e")) > 2 and len(d[0].search_for("e", hit_max=2)) == 2


def test_quality_text_page(data):
    q = pdfwords.open(data)[0].text_quality()
    assert q["needs_ocr"] is False and q["chars"] > 50 and q["score"] == 1.0
    assert q["fonts"][0]["name"] == "Helvetica" and q["fonts"][0]["embedded"] is False


def test_quality_invisible_layer():
    pdf = text_pdf(b"BT 3 Tr /F1 12 Tf 72 700 Td (An invisible OCR text layer over a scan) Tj ET")
    q = pdfwords.open(pdf)[0].text_quality()
    assert q["invisible_ratio"] == 1.0 and "ocr_layer" in q["reasons"] and q["needs_ocr"] is False
    assert pdfwords.open(pdf)[0].needs_ocr(ocr_layer_ok=False) is True


def test_quality_scanned_image():
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (850, 1100), "white").save(buf, "PDF", resolution=100)
    d = pdfwords.open(buf.getvalue())
    q = d[0].text_quality()
    assert q["needs_ocr"] is True and "no_text_layer" in q["reasons"] and q["image_coverage"] > 0.95
    assert d.needs_ocr() == [0]


def test_quality_empty_and_garbled():
    from pdfwords.quality import garbled_ratio
    q = pdfwords.open(text_pdf(b""))[0].text_quality()
    assert q["needs_ocr"] is False and q["reasons"] == ["empty_page"]
    assert garbled_ratio("abc\ufffd\ue001 x") == pytest.approx(2 / 6)
    assert garbled_ratio("Tiếng Việt có dấu") == 0.0


def test_iter_pages_and_workers(tmp_path):
    pdf = text_pdf(b"BT /F1 12 Tf 72 700 Td (Page number {n} of the test) Tj ET", pages=6)
    f = tmp_path / "six.pdf"
    f.write_bytes(pdf)
    serial = pdfwords.extract(str(f), "words")
    assert len(serial) == 6 and serial[3][2][4] == "3"
    assert pdfwords.extract(str(f), "words", workers=2) == serial
    assert pdfwords.extract(pdf, "words", pages=[5, 0], workers=2) == [serial[5], serial[0]]   # bytes input
    assert pdfwords.extract(io.BytesIO(pdf), "text", pages=-1) == [pdfwords.extract(pdf, "text")[5]]
    got = list(pdfwords.iter_pages(str(f), "text", pages=range(1, 4)))
    assert [i for i, _ in got] == [1, 2, 3]
    with pdfwords.open(str(f)) as d:
        assert d.extract("blocks", pages=2) == [d[2].get_text("blocks")]
        with pytest.raises(IndexError):
            d.extract("text", pages=[7])
    assert pdfwords.parallel_words(str(f), processes=2) == serial


def test_iter_pages_after_edit():
    pytest.importorskip("pypdf")
    pdf = text_pdf(b"BT /F1 12 Tf 72 700 Td (Secret {n} here) Tj ET", pages=3)
    d = pdfwords.open(pdf)
    for p in d:
        for r in p.search_for("Secret"):
            p.add_redact_annot(r)
        p.apply_redactions()
    out = d.extract("text", workers=2)      # workers see the edited document
    assert all("Secret" not in t and "here" in t for t in out)


def test_table_cells(data):
    p = pdfwords.open(data)[0]
    cells = [(60, 55, 300, 78), (60, 78, 300, 98), (60, 175, 300, 200)]
    res = p.table_cells(cells)
    assert res[0]["text"] == "Click here for the docs."
    assert res[1]["text"] == "Visit https://example.com/page today."
    assert res[2]["text"] == "Name:"
    # the same cells in pixels of a 2x rendering
    px = p.table_cells([tuple(v * 2 for v in c) for c in cells], image_size=(1224, 1584))
    assert [c["text"] for c in px] == [c["text"] for c in res]
    assert px[0]["words"][0][0] == pytest.approx(2 * res[0]["words"][0][0])


def test_debug_overlay(data, tmp_path):
    pytest.importorskip("PIL")
    from pdfwords.debug import overlay
    img = overlay(pdfwords.open(data)[0], show="chars,words,lines,spans,blocks,order,links,annots,widgets", scale=1)
    assert img.size == (612, 792)
    with pytest.raises(ValueError):
        overlay(pdfwords.open(data)[0], show="nope")
    from pdfwords.__main__ import main
    f = tmp_path / "f.pdf"
    f.write_bytes(data)
    out = tmp_path / "o.png"
    main(["debug", str(f), "--page", "0", "--show", "words,blocks", "-o", str(out)])
    assert out.stat().st_size > 1000
