# SPDX-License-Identifier: Apache-2.0
"""Table detection (page.find_tables) from ruling lines and text alignment."""
import pytest

import pdfwords
from _featurepdf import tables_pdf
from conftest import BACKENDS, pdf_path


@pytest.fixture(scope="module")
def doc():
    return pdfwords.open(tables_pdf())


def test_ruled_grid_with_span(doc):
    ts = doc[0].find_tables()
    assert len(ts) == 2 and ts.tables == list(ts)
    g = ts[0]
    assert (g.row_count, g.col_count, g.strategy) == (4, 3, "lines")
    assert g.bbox == pytest.approx((72, 92, 372, 172))
    assert g.extract() == [["Name", "Savings", None], ["", "Speed", "Idle"],
                           ["alpha", "5.9%", "17.4%"], ["beta", "2.4%", "2.7%"]]
    span = [c for c in g.cells if c["text"] == "Savings"][0]
    assert (span["row"], span["col"], span["rowspan"], span["colspan"]) == (0, 1, 1, 2)
    assert span["bbox"] == pytest.approx((172, 92, 372, 112))
    assert g.header == ["Name", "Savings", ""]
    assert g.rows[0][2] is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_rule_delimited(doc, backend):
    b = pdfwords.open(tables_pdf(), backend=backend)[0].find_tables()[1]
    assert b.strategy == "rules"
    assert b.extract() == [["Model", "BLEU", "Cost"], ["ByteNet", "23.75", "1.0e20"],
                           ["GNMT + RL", "24.6", "2.3e19"], ["Transformer", "28.4", "2.3e19"]]
    assert b.to_markdown().splitlines()[:3] == ["| Model | BLEU | Cost |", "|---|---|---|",
                                               "| ByteNet | 23.75 | 1.0e20 |"]
    assert b.to_csv().splitlines()[2] == "GNMT + RL,24.6,2.3e19"


def test_outputs(doc, tmp_path):
    t = doc[0].find_tables()[1]
    p = tmp_path / "t.csv"
    t.to_csv(str(p))
    assert p.read_text(encoding="utf-8").startswith("Model,BLEU,Cost")
    assert t.to_text().splitlines()[0] == "Model\tBLEU\tCost"
    d = t.to_dict()
    assert d["page"] == 0 and d["row_count"] == 4 and len(d["cells"]) == 12
    pd = pytest.importorskip("pandas")
    df = t.to_pandas()
    assert list(df.columns) == ["Model", "BLEU", "Cost"] and df.shape == (3, 3)
    assert isinstance(df, pd.DataFrame)


def test_prose_between_rules_is_not_a_table(doc):
    assert doc[1].find_tables() == []
    assert doc[1].find_tables(strategy="text") == []


def test_clip_and_strategy(doc):
    assert len(doc[0].find_tables(clip=(0, 0, 612, 200))) == 1
    ts = doc[0].find_tables(strategy="text")
    assert any(t.extract()[0] == ["Model", "BLEU", "Cost"] for t in ts)
    with pytest.raises(ValueError):
        doc[0].find_tables(strategy="magic")


def test_markdown_uses_detected_tables(doc):
    md = pdfwords.to_markdown(doc)
    assert "| Model | BLEU | Cost |" in md and "| alpha | 5.9% | 17.4% |" in md
    assert "This is ordinary prose" in md


def test_real_documents():
    cam = pdfwords.open(pdf_path("table_camelot.pdf"))[0].find_tables()
    assert len(cam) == 1
    rows = cam[0].extract()
    assert rows[0][:4] == ["Cycle\nName", "KI\n(1/km)", "Distance\n(mi)", "Percent Fuel Savings"]
    assert rows[1][3:] == ["Improved\nSpeed", "Decreased\nAccel", "Eliminate\nStops", "Decreased\nIdle"]
    assert rows[2] == ["2012_2", "3.30", "1.3", "5.9%", "9.5%", "29.2%", "17.4%"]
    att = pdfwords.open(pdf_path("arxiv_attention.pdf"))
    t3 = att[8].find_tables()[0]
    assert t3.col_count == 13
    assert t3.extract()[2] == ["base", "6", "512", "2048", "8", "64", "64", "0.1", "0.1", "100K", "4.92", "25.8", "65"]
    assert att[13].find_tables() == []      # attention visualisation is not a table
    res = pdfwords.open(pdf_path("arxiv_resnet.pdf"))[5].find_tables()
    assert [(t.row_count, t.col_count) for t in res] == [(11, 3), (11, 3), (7, 2)]
    assert res[2].extract()[-1] == ["ResNet (ILSVRC’15)", "3.57"]
