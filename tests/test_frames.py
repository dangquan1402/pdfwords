"""Columnar output (pyarrow / Parquet / pandas) and the tables / frame CLIs."""
import json
import subprocess
import sys

import pytest

import pdfwords
from pdfwords import frames

from _featurepdf import tables_pdf
from conftest import pdf_path


def _doc():
    return pdfwords.open(pdf_path("arxiv_attention.pdf"))


def test_records_match_get_text():
    with _doc() as doc:
        cols = frames.records(doc, "words", pages=[0, 1])
        n = sum(len(doc[i].get_text("words")) for i in (0, 1))
        assert len(cols["text"]) == n and set(cols["page"]) == {0, 1}
        ch = frames.records(doc, "chars", pages=[0])
        assert "".join(ch["text"]).replace(" ", "") == doc[0].get_text().replace(" ", "").replace("\n", "")
        for k in frames.KINDS:
            c = frames.records(doc, k, pages=[0])
            assert tuple(c) == frames._SCHEMA[k] and len({len(v) for v in c.values()}) == 1


def test_bad_kind():
    with pytest.raises(ValueError):
        frames.records(pdfwords.open(tables_pdf()), "glyphs")


def test_arrow_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    with _doc() as doc:
        t = doc.to_arrow("lines", pages=[0])
        assert t.schema.field("page").type == pa.int32() and t.schema.field("x0").type == pa.float64()
        p = doc.to_parquet(str(tmp_path / "c.parquet"), "chars", pages=[0])
        assert pq.read_table(p).num_rows == len(frames.records(doc, "chars", pages=[0])["text"])
        assert doc.to_arrow("words", pages=[]).num_rows == 0


def test_pandas():
    pytest.importorskip("pandas")
    with _doc() as doc:
        df = doc.to_pandas("spans", pages=[0])
        assert list(df.columns) == list(frames._SCHEMA["spans"]) and len(df) > 10


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "pdfwords", *args], capture_output=True, text=True, check=True).stdout


def test_tables_cli(tmp_path):
    f = tmp_path / "t.pdf"
    f.write_bytes(tables_pdf())
    res = json.loads(_cli("tables", str(f), "--pages", "0"))
    assert len(res) == 2 and res[0]["page"] == 0 and "rows" in res[0]
    assert "Model" in _cli("tables", str(f), "-f", "md")
    _cli("tables", str(f), "-f", "csv", "-o", str(tmp_path / "csv"))
    assert sorted(p.name for p in (tmp_path / "csv").iterdir())[0] == "page0_table0.csv"


def test_frame_cli(tmp_path):
    out = tmp_path / "w.csv"
    _cli("frame", pdf_path("arxiv_attention.pdf"), "-o", str(out), "--pages", "0")
    head = out.read_text(encoding="utf-8").splitlines()
    assert head[0] == "page,block,line,word,x0,y0,x1,y1,text" and len(head) > 50
