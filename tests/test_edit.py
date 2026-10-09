# SPDX-License-Identifier: Apache-2.0
"""Editing features: content access, true redaction, text insertion, save."""
import io
import os
import re
import shutil
import subprocess
import sys
import warnings

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("fontTools")

import pdfwords  # noqa: E402
from conftest import FIXTURES, pdf_path  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _edgepdf import edge_pdf  # noqa: E402

W9 = os.path.join(FIXTURES, "irs_w9.pdf")
SIMPLE = os.path.join(FIXTURES, "simple_text.pdf")

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
TTF = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
needs_ttf = pytest.mark.skipif(TTF is None, reason="no Unicode TrueType font found on this system")
VI = "Tiếng Việt có dấu: Hà Nội, Đà Nẵng, ưởng"


def _norm(s):
    return re.sub(r"\s+", " ", s).lower()


def _ov(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _all_stream_bytes(data):
    """The file bytes plus every object serialised and every stream decoded (to grep for leaks)."""
    import logging

    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    r = PdfReader(io.BytesIO(data))
    out = [data]
    for i in range(1, int(r.trailer["/Size"])):
        try:
            o = r.get_object(i)
        except Exception:
            continue
        if o is None:
            continue
        if hasattr(o, "get_data"):
            try:
                out.append(o.get_data())
            except Exception:
                pass
        out.append(repr(o).encode("utf-8", "replace"))
    return b"\n".join(out)


# ---------------------------------------------------------------------- content access
def test_read_and_get_contents():
    doc = pdfwords.open(W9)
    pg = doc[0]
    xrefs = pg.get_contents()
    assert xrefs and all(isinstance(x, int) and x > 0 for x in xrefs)
    data = pg.read_contents()
    assert isinstance(data, bytes) and b"BT" in data and b"Tf" in data
    assert doc.xref_stream(xrefs[0]) in data
    assert "/Length" in doc.xref_object(xrefs[0]) or doc.xref_object(xrefs[0]).startswith("<<")
    assert doc.xref_length() > max(xrefs)


def test_set_contents_roundtrip():
    doc = pdfwords.open(SIMPLE)
    pg = doc[0]
    assert pg.get_text("words")
    x = pg.set_contents(b"q Q")
    assert pg.get_contents() == [x] and pg.read_contents() == b"q Q"
    assert pg.get_text("words") == []


# ---------------------------------------------------------------------- insertion
def test_insert_text_base14_roundtrip(backend):
    doc = pdfwords.open(SIMPLE, backend=backend)
    pg = doc[0]
    n = pg.insert_text((72, 760), "Hello inserted\nsecond line", fontsize=14, fontname="helv", color=(1, 0, 0))
    assert n == 2
    words = {w[4]: w for w in pg.get_text("words")}
    h = words["Hello"]
    assert abs(h[0] - 72) < 0.5 and h[1] < 760 < h[3] + 0.5   # baseline at y=760
    assert abs(words["second"][0] - 72) < 0.5 and words["second"][1] > h[1] + 10
    assert "Hello inserted" in pg.get_text("text")
    # colour is reported by the extractor
    spans = [s for b in pg.get_text("dict")["blocks"] for ln in b["lines"] for s in ln["spans"]]
    assert any(s["color"] == 0xFF0000 and "Hello" in s["text"] for s in spans)


def test_insert_text_survives_save_and_other_tools(tmp_path):
    doc = pdfwords.open(SIMPLE)
    doc[0].insert_text((72, 760), "Roundtrip check", fontsize=12, fontname="tibo")
    out = tmp_path / "o.pdf"
    doc.save(out, garbage=3, deflate=True)
    assert "Roundtrip check" in pdfwords.open(out)[0].get_text()
    import pypdfium2 as pdfium
    assert "Roundtrip check" in pdfium.PdfDocument(str(out))[0].get_textpage().get_text_range()


def test_insert_text_rejects_non_latin_without_fontfile():
    doc = pdfwords.open(SIMPLE)
    with pytest.raises(ValueError, match="fontfile"):
        doc[0].insert_text((72, 72), "Đà Nẵng")


@needs_ttf
def test_insert_text_ttf_unicode(tmp_path):
    doc = pdfwords.open(SIMPLE)
    doc[0].insert_text((72, 760), VI, fontsize=12, fontfile=TTF)
    out = tmp_path / "vi.pdf"
    doc.save(out)
    assert VI in pdfwords.open(out)[0].get_text()
    from pypdf import PdfReader
    assert VI in PdfReader(str(out)).pages[0].extract_text()
    try:
        import pymupdf
    except ImportError:
        return
    assert VI in pymupdf.open(str(out))[0].get_text()


def test_insert_text_rotated():
    doc = pdfwords.open(SIMPLE)
    pg = doc[0]
    pg.insert_text((560, 600), "upwards", fontsize=12, rotate=90)
    w = [x for x in pg.get_text("words") if x[4] == "upwards"][0]
    assert w[3] == pytest.approx(600, abs=0.5)        # starts at the point, runs up
    assert w[0] < 560 < w[2] + 0.5 and (w[3] - w[1]) > (w[2] - w[0])


def test_insert_textbox_align_and_overflow():
    doc = pdfwords.open(SIMPLE)
    pg = doc[0]
    rect = (300, 722, 500, 790)
    rc = pg.insert_textbox(rect, "right aligned words wrap inside the box nicely", fontsize=11, align=2)
    assert rc >= 0
    ws = [w for w in pg.get_text("words") if _ov(w[:4], rect)]
    assert ws and all(rect[0] - 0.5 <= w[0] and w[2] <= rect[2] + 0.5 for w in ws)
    line_ends = {}
    for w in ws:
        line_ends[round(w[1])] = max(line_ends.get(round(w[1]), 0), w[2])
    assert all(abs(x - rect[2]) < 0.5 for x in line_ends.values())
    before = pg.get_text("words")
    rc2 = pg.insert_textbox((300, 790, 320, 792), "this cannot fit", fontsize=12)
    assert rc2 < 0
    assert pg.get_text("words") == before          # nothing written on overflow


# ---------------------------------------------------------------------- redaction
def _redact_search(path, needle, page=0, **kw):
    doc = pdfwords.open(path)
    pg = doc[page]
    before = pg.get_text("words")
    hits = pg.search_for(needle)
    assert hits, needle
    for h in hits:
        pg.add_redact_annot(h, fill=(0, 0, 0))
    with warnings.catch_warnings():
        warnings.simplefilter("error")     # collateral damage would raise here
        assert pg.apply_redactions(**kw)
    return doc, pg, before, hits


@pytest.mark.parametrize("path,needle", [(W9, "Taxpayer Identification"), (SIMPLE, "quick brown"),
                                         (W9, "exempt payee")])
def test_redact_by_search_removes_all_and_keeps_neighbours(tmp_path, path, needle):
    doc, pg, before, hits = _redact_search(path, needle)
    out = tmp_path / "r.pdf"
    doc.save(out)
    data = out.read_bytes()
    after = pdfwords.open(out)[0].get_text("words")
    assert _norm(needle) not in _norm(pdfwords.open(out)[0].get_text())
    assert pdfwords.open(out)[0].search_for(needle) == []
    # neighbours keep identical text and position
    keep = [w for w in before if not any(_ov(w, h) for h in hits)]
    have = {(w[4], round(w[0], 2), round(w[1], 2)) for w in after}
    assert all((w[4], round(w[0], 2), round(w[1], 2)) in have for w in keep)
    # other extractors
    import pypdfium2 as pdfium
    assert _norm(needle) not in _norm(pdfium.PdfDocument(data)[0].get_textpage().get_text_range())
    from pypdf import PdfReader
    assert _norm(needle) not in _norm(PdfReader(io.BytesIO(data)).pages[0].extract_text())
    if shutil.which("pdftotext"):
        t = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(out), "-"], capture_output=True, text=True).stdout
        assert _norm(needle) not in _norm(t)


def test_redact_cross_check_pymupdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc, pg, before, hits = _redact_search(W9, "Taxpayer")
    out = tmp_path / "r.pdf"
    doc.save(out)
    m = pymupdf.open(str(out))
    assert m[0].search_for("Taxpayer") == []
    assert "taxpayer" not in m[0].get_text().lower()


def test_redact_with_scrub_leaves_no_trace(tmp_path):
    doc, pg, _, _ = _redact_search(W9, "Taxpayer Identification")
    for p in doc:
        for h in p.search_for("Taxpayer Identification"):
            p.add_redact_annot(h)
        p.apply_redactions()
    doc.scrub()
    data = doc.tobytes(deflate=True)
    blob = _all_stream_bytes(data).lower()
    assert b"taxpayer identification" not in blob
    assert pdfwords.open(data).metadata.get("subject", "") == ""


def test_incremental_save_refused_after_redaction():
    doc, _, _, _ = _redact_search(SIMPLE, "quick")
    with pytest.raises(ValueError, match="incremental"):
        doc.tobytes(incremental=True)


def test_incremental_save_after_insert(tmp_path):
    orig = open(SIMPLE, "rb").read()
    doc = pdfwords.open(SIMPLE)
    doc[0].insert_text((72, 770), "appended")
    data = doc.tobytes(incremental=True)
    assert data.startswith(orig)
    assert "appended" in pdfwords.open(data)[0].get_text()


@pytest.mark.parametrize("name", ["arxiv_attention.pdf", "arxiv_resnet.pdf"])
def test_redact_many_words_real_papers(name):
    doc = pdfwords.open(pdf_path(name))
    for pg in list(doc)[:3]:
        ws = pg.get_text("words")[2::5]
        for w in ws:
            pg.add_redact_annot(w[:4])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            pg.apply_redactions()
        r = pg.redaction_report
        assert r["leftover_chars"] == [] and r["collateral_chars"] == []
        assert r["glyphs_removed"] >= sum(len(w[4]) for w in ws) * 0.9


# ---------------------------------------------------------------------- content-stream edge cases
@pytest.fixture
def edge():
    return pdfwords.open(edge_pdf())


def _words(pg):
    return {w[4]: w for w in pg.get_text("words")}


def test_redact_partial_TJ_keeps_kerning(edge):
    pg = edge[0]
    before = _words(pg)
    pg.add_redact_annot(before["lo"][:4])
    pg.apply_redactions()
    after = _words(pg)
    assert "lo" not in after
    for k in ("Hel", "World"):
        assert after[k][:4] == pytest.approx(before[k][:4], abs=1e-3)


def test_redact_quote_operators(edge):
    pg = edge[0]
    ws = pg.get_text("words")
    target = [w for w in ws if w[4] == "line"][1]          # "Fourth line" uses the " operator
    pg.add_redact_annot(target[:4])
    pg.apply_redactions()
    after = pg.get_text("words")
    assert [w[4] for w in after].count("line") == 1
    four = [w for w in after if w[4] == "Fourth"][0]
    assert four[:4] == pytest.approx([w for w in ws if w[4] == "Fourth"][0][:4], abs=1e-3)


def test_redact_form_xobject_is_copied_not_shared(edge):
    pg = edge[0]
    w = _words(pg)
    pg.add_redact_annot(w["FormSecret"][:4])
    pg.apply_redactions()
    after = _words(pg)
    assert "FormSecret" not in after and after["FormKeep"][:4] == pytest.approx(w["FormKeep"][:4], abs=1e-3)
    assert pg.redaction_report["forms_rewritten"] == 1
    assert "FormSecret" in _words(edge[1])                # page 2 uses the same form: untouched


def test_redact_actualtext_removed(edge):
    pg = edge[0]
    pg.add_redact_annot((60, 175, 120, 200))              # covers "Hidden" (ActualText "ActualSecret")
    pg.apply_redactions()
    data = edge.tobytes()
    assert b"ActualSecret" not in _all_stream_bytes(data)
    assert "ActualSecret" not in pg.get_text()


def test_redact_image_pixels_and_vector_art(edge):
    pg = edge[0]
    pg.add_redact_annot((330, 330, 360, 350), fill=(0, 1, 0))   # inside the red image
    pg.add_redact_annot((60, 460, 140, 500), fill=None)         # covers the blue box entirely
    pg.add_redact_annot((395, 455, 435, 497), fill=None)        # inline image
    pg.apply_redactions()
    rep = pg.redaction_report
    assert rep["images_blanked"] == 1 and rep["paths_removed"] == 1 and rep["images_removed"] == 1
    import pypdfium2 as pdfium
    img = pdfium.PdfDocument(edge.tobytes())[0].render(scale=1).to_pil().convert("RGB")
    assert img.getpixel((345, 340)) == (0, 255, 0)        # fill drawn over blanked pixels
    assert img.getpixel((310, 360)) == (255, 0, 0)        # rest of the image intact
    assert img.getpixel((90, 480))[2] < 250 or img.getpixel((90, 480))[0] > 200  # blue box gone
    assert img.getpixel((5, 5)) == img.getpixel((200, 700))  # full-page background kept


def test_redact_image_remove_option(edge):
    pg = edge[0]
    pg.add_redact_annot((330, 330, 360, 350))
    pg.apply_redactions(images=pdfwords.PDF_REDACT_IMAGE_REMOVE)
    assert pg.redaction_report["images_removed"] == 1


def test_redact_overlay_text(edge):
    pg = edge[0]
    pg.add_redact_annot((60, 270, 200, 300), text="REDACTED", fill=(0, 0, 0), text_color=(1, 1, 1))
    pg.apply_redactions()
    assert "REDACTED" in pg.get_text() and "FormSecret" not in pg.get_text()


def test_no_redactions_returns_false(edge):
    assert edge[0].apply_redactions() is False


# ---------------------------------------------------------------------- search
def test_search_for_basic_and_regex():
    pg = pdfwords.open(SIMPLE)[0]
    hits = pg.search_for("QUICK BROWN")
    assert hits and all(len(h) == 4 for h in hits)
    q = pg.search_for("quick", quads=True)
    assert q and len(q[0]) == 4 and len(q[0][0]) == 2
    nums = pg.search_for(r"\d+\.\d+", regex=True)
    assert nums


# ---------------------------------------------------------------------- CLI
def test_cli_redact_and_insert(tmp_path):
    from pdfwords.__main__ import main
    out1, out2 = tmp_path / "r.pdf", tmp_path / "i.pdf"
    main(["redact", W9, "-o", str(out1), "--search", "Taxpayer", "--pages", "0", "--scrub"])
    main(["insert-text", str(out1), "-o", str(out2), "--text", "cli works", "--point", "72,72"])
    t = pdfwords.open(out2)[0].get_text()
    assert "cli works" in t and "taxpayer" not in t.lower()
