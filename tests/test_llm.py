# SPDX-License-Identifier: Apache-2.0
"""0.3: tagged PDFs, image/drawing objects, Markdown / chunks, HTML/XHTML/XML/hOCR/ALTO export,
LLM integrations and the MCP tools."""
import json
import os
import subprocess
import sys
import xml.dom.minidom

import pytest

import pdfwords
from _featurepdf import tagged_pdf, text_pdf
from conftest import FIXTURES, BACKENDS, pdf_path

W9 = os.path.join(FIXTURES, "irs_w9.pdf")
EXPECTED_MD = """# Annual Report

Alpha comes first logically.

Beta is drawn higher on the page.

- First item
- Second item

| Year | Revenue |
|---|---|
| 2025 | 42 |

![A red square]()
"""


@pytest.fixture(scope="module")
def tagged():
    return tagged_pdf()


# ------------------------------------------------------------------ tagged PDFs
def test_struct_tree(tagged):
    p = pdfwords.open(tagged)[0]
    assert p.is_tagged
    t = p.get_struct_tree()
    assert [n["role"] for n in t] == ["Document"]
    kids = t[0]["children"]
    assert [k["role"] for k in kids] == ["H1", "P", "P", "L", "Table", "Figure"]
    assert kids[1]["mcids"] == [1] and kids[5]["alt"] == "A red square"
    assert not pdfwords.open(text_pdf(b"BT /F1 12 Tf 72 700 Td (x) Tj ET"))[0].is_tagged


@pytest.mark.parametrize("backend", BACKENDS)
def test_struct_sort(tagged, backend):
    p = pdfwords.open(tagged, backend=backend)[0]
    geo = p.get_text(sort="xycut").splitlines()
    log = p.get_text(sort="struct").splitlines()
    assert geo[1].startswith("Beta") and geo[2].startswith("Alpha")
    assert log[:3] == ["Annual Report", "Alpha comes first logically.", "Beta is drawn higher on the page."]
    words = [w[4] for w in p.get_text("words", sort="struct")]
    assert words.index("Alpha") < words.index("Beta")
    d = p.get_text("dict", sort="struct", roles=True)
    roles = [b["role"] for b in d["blocks"]]
    assert roles[:5] == ["H1", "P", "P", "LI", "LI"] and roles[-1] == ""   # artifact page number untagged
    assert all("mcid" in sp for b in d["blocks"][:-1] for ln in b["lines"] for sp in ln["spans"])


def test_struct_sort_untagged_is_xycut():
    p = pdfwords.open(W9)[1]
    q = pdfwords.open(pdf_path("arxiv_resnet.pdf"))[0]
    assert q.get_text(sort="struct") == q.get_text(sort="xycut")
    assert p.get_text(sort="struct")    # tagged W9: works and keeps all text
    assert sorted(p.get_text(sort="struct").split()) == sorted(p.get_text(sort="xycut").split())


# ------------------------------------------------------------------ objects
def test_images_and_drawings(tagged):
    p = pdfwords.open(tagged)[0]
    ims = p.get_images()
    assert len(ims) == 1
    im = ims[0]
    assert im["bbox"] == pytest.approx((400, 142, 500, 192))
    assert (im["width"], im["height"], im["mcid"], im["cs_name"]) == (2, 2, 9, "DeviceRGB")
    assert p.get_image_info() == ims
    dr = p.get_drawings()
    assert len(dr) == 6 and all(d["type"] == "s" and d["width"] == pytest.approx(0.5) for d in dr)
    assert dr[0]["items"][0][0] == "l"
    assert dr[0]["rect"] == pytest.approx((72, 312, 312, 312))
    assert p.get_drawings(clip=(0, 0, 100, 100)) == []


def test_get_image_pixels(tagged):
    pytest.importorskip("PIL")
    img = pdfwords.open(tagged)[0].get_image(0)
    assert img.size == (2, 2) and img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_drawings_rotated_and_forms():
    p = pdfwords.open(W9)[0]
    dr = p.get_drawings()
    assert len(dr) > 50
    assert all(0 <= d["rect"][0] <= d["rect"][2] <= p.rect[2] + 1 for d in dr)


# ------------------------------------------------------------------ markdown
def test_markdown_tagged(tagged):
    assert pdfwords.to_markdown(tagged) == EXPECTED_MD
    assert pdfwords.open(tagged)[0].get_text("markdown").strip() == EXPECTED_MD.strip()


def test_markdown_untagged_paper():
    md = pdfwords.to_markdown(pdf_path("arxiv_resnet.pdf"), [0, 1])
    lines = md.splitlines()
    assert lines[0] == "# Deep Residual Learning for Image Recognition"
    assert "## Abstract" in lines and "## 1. Introduction" in lines
    assert "arXiv:1512.03385" not in md          # vertical margin stamp dropped
    assert "\n\n\n" not in md


def test_markdown_headers_footers_and_pages():
    pc = pdfwords.to_markdown(W9, page_chunks=True)
    assert [c["metadata"]["page"] for c in pc] == list(range(6))
    assert all("Form W-9 (Rev. 3-2024)" not in c["text"] for c in pc[1:])
    keep = pdfwords.to_markdown(W9, [1, 2, 3], headers_footers=False)
    assert "Form W-9 (Rev. 3-2024)" in keep
    md = pdfwords.to_markdown(W9, [0, 1], page_separators=True)
    assert "<!-- page 1 -->" in md and "<!-- page 2 -->" in md


def test_markdown_image_dir(tagged, tmp_path):
    pytest.importorskip("PIL")
    md = pdfwords.to_markdown(tagged, image_dir=str(tmp_path))
    files = os.listdir(tmp_path)
    assert len(files) == 1 and f"![A red square]({os.path.join(str(tmp_path), files[0])})" in md


def test_chunks():
    cs = pdfwords.chunks(pdf_path("arxiv_resnet.pdf"), max_chars=1500)
    assert len(cs) > 10
    assert all(len(c["text"]) <= 1500 + 200 for c in cs)
    assert cs[0]["headings"][0] == "Deep Residual Learning for Image Recognition"
    for c in cs:
        assert c["pages"] and c["provenance"]
        for pr in c["provenance"]:
            x0, y0, x1, y1 = pr["bbox"]
            assert x0 <= x1 and y0 <= y1 and pr["page"] in c["pages"]
    ov = pdfwords.chunks(pdf_path("arxiv_resnet.pdf"), [0], max_chars=800, overlap=100)
    assert len(ov) >= 2


# ------------------------------------------------------------------ export
@pytest.mark.parametrize("fmt", ["xhtml", "xml", "hocr", "alto"])
def test_export_wellformed(fmt, tagged):
    for src in (tagged, W9):
        s = pdfwords.export(src, fmt, pages=[0])
        xml.dom.minidom.parseString(s.encode("utf-8"))


def test_export_contents(tagged):
    doc = pdfwords.open(tagged)
    nwords = len(doc[0].get_text("words"))
    hocr = pdfwords.export(doc, "hocr")
    assert hocr.count("class='ocrx_word'") + hocr.count('class="ocrx_word"') == nwords
    alto = pdfwords.export(doc, "alto", dpi=144)
    dom = xml.dom.minidom.parseString(alto.encode())
    strings = dom.getElementsByTagName("String")
    assert len(strings) == nwords
    s0 = strings[0]
    assert s0.getAttribute("CONTENT") == "Annual"
    assert float(s0.getAttribute("HPOS")) == pytest.approx(144, abs=0.5)   # 72 pt at 144 dpi
    html = pdfwords.export(doc, "html")
    assert "Annual Report" in html and "position:absolute" in html
    assert pdfwords.export(doc, "md") == pdfwords.to_markdown(doc)
    with pytest.raises(ValueError):
        pdfwords.export(doc, "docx")
    for fmt in ("html", "xhtml", "hocr", "alto", "markdown"):
        assert "Annual" in doc[0].get_text(fmt)
    assert 'c="A"' in doc[0].get_text("xml")      # char-level XML


# ------------------------------------------------------------------ integrations / MCP / CLI
def test_langchain_loader():
    pytest.importorskip("langchain_core")
    from pdfwords.integrations.langchain import PdfwordsLoader
    docs = PdfwordsLoader(W9).load()
    assert len(docs) == 6 and docs[2].metadata["page"] == 2 and docs[0].metadata["total_pages"] == 6
    ch = PdfwordsLoader(W9, mode="chunks", pages=[0]).load()
    assert ch and ch[0].metadata["provenance"][0]["page"] == 0


def test_llama_index_reader(tagged):
    pytest.importorskip("llama_index.core")
    from pdfwords.integrations.llama_index import PdfwordsReader
    docs = PdfwordsReader(mode="markdown_page").load_data(tagged, extra_info={"k": 1})
    assert len(docs) == 1 and docs[0].text.startswith("# Annual Report") and docs[0].metadata["k"] == 1


def test_mcp_tools(tmp_path, monkeypatch):
    from pdfwords import mcp_server as m
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(tagged_pdf())
    m.set_roots([str(tmp_path)])
    info = json.loads(m.pdf_info(str(pdf)))
    assert info["pages"] == 1 and info["tagged"]
    assert "\n# Annual Report\n" in m.to_markdown(str(pdf))
    assert "Alpha" in m.extract_text(str(pdf), "0", "text", sort="struct")
    hits = json.loads(m.search(str(pdf), "revenue"))
    assert hits and hits[0]["page"] == 0
    assert "| Year | Revenue |" in m.get_tables(str(pdf), 0)
    assert m.extract_text(str(pdf), max_chars=10).endswith("request fewer pages]")
    with pytest.raises(PermissionError):
        m.pdf_info(W9)
    pytest.importorskip("numpy")
    assert m.render_page_png(str(pdf), 0, 20)[:4] == b"\x89PNG"


def test_cli_export_chunks_objects(tmp_path, tagged):
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(tagged)
    run = lambda *a: subprocess.run([sys.executable, "-m", "pdfwords", *a], capture_output=True, text=True,
                                    check=True, encoding="utf-8").stdout
    assert run("export", str(pdf)).strip() == EXPECTED_MD.strip()
    xml.dom.minidom.parseString(run("export", str(pdf), "-f", "alto").encode())
    lines = run("chunks", str(pdf)).strip().splitlines()
    assert json.loads(lines[0])["headings"] == ["Annual Report"]
    obj = json.loads(run("objects", str(pdf), "--kind", "images"))
    assert obj[0]["images"][0]["width"] == 2
    st = json.loads(run("objects", str(pdf), "--kind", "struct"))
    assert st[0]["struct"][0]["role"] == "Document"
    assert run(str(pdf), "--mode", "text", "--sort", "struct").splitlines()[1].startswith("Alpha")
