# SPDX-License-Identifier: Apache-2.0
"""Core behaviour on committed fixtures; runs on every backend, no optional dependencies."""
import json
import os
import subprocess
import sys

import pytest

import pdfwords
from conftest import FIXTURES, pdf_path

SIMPLE = os.path.join(FIXTURES, "simple_text.pdf")
ROT90 = os.path.join(FIXTURES, "rot90.pdf")
W9 = os.path.join(FIXTURES, "irs_w9.pdf")


def test_expected_backends():
    exp = os.environ.get("PDFWORDS_EXPECT_BACKENDS")
    if exp:
        assert pdfwords.available_backends() == exp.split(",")


def test_output_shapes(backend):
    pg = pdfwords.open(SIMPLE)[0]
    assert pg.parent.backend == backend
    w = pg.get_text("words")[0]
    assert len(w) == 8 and isinstance(w[4], str) and w[0] < w[2] and w[1] < w[3]
    assert all(isinstance(x, int) for x in w[5:])
    b = pg.get_text("blocks")[0]
    assert len(b) == 7 and b[4].endswith("\n") and b[6] == 0
    d = pg.get_text("dict")
    assert set(d) == {"width", "height", "blocks"}
    sp = d["blocks"][0]["lines"][0]["spans"][0]
    for k in ("size", "flags", "font", "color", "ascender", "descender", "text", "origin", "bbox"):
        assert k in sp
    rd = pg.get_text("rawdict")
    ch = rd["blocks"][0]["lines"][0]["spans"][0]["chars"][0]
    assert set(ch) == {"origin", "bbox", "c", "synthetic"}
    assert isinstance(pg.get_text("text"), str)
    assert json.loads(pg.get_text("json"))["width"] == d["width"]


def test_text_content(backend):
    t = pdfwords.open(SIMPLE)[0].get_text()
    assert t.startswith("Simple test document")
    assert "The quick brown fox jumps over the lazy dog." in t
    assert "$1,234.56." in t


def test_ligatures_and_dehyphenate(backend):
    pg = pdfwords.open(SIMPLE)[0]
    assert "Efficient" in pg.get_text()
    assert "E\ufb03cient" in pg.get_text(ligatures=True)
    words = [w[4] for w in pg.get_text("words")]
    assert "Hyphen-ated" in words


def test_dehyphenate_across_lines(backend):
    pg = pdfwords.open(pdf_path("arxiv_resnet.pdf"))[0]
    plain = [w[4] for w in pg.get_text("words")]
    joined = [w[4] for w in pg.get_text("words", dehyphenate=True)]
    assert "learn-" in plain and "learning" in joined and len(joined) < len(plain)
    assert "learn-\ning" not in pg.get_text(dehyphenate=True)


def test_rotation(backend):
    pg = pdfwords.open(ROT90)[0]
    assert pg.rotation == 90
    assert pg.rect == (0.0, 0.0, pg._h, pg._w)
    w = pg.get_text("words")
    assert [x[4] for x in w] == ["Hello", "rotated", "world"]
    wr = pg.get_text("words", rotated=True)
    # 90 deg: (x, y) -> (h - y, x); horizontal text of the unrotated page becomes vertical
    for a, b in zip(w, wr):
        assert b[0] == pytest.approx(pg._h - a[3]) and b[1] == pytest.approx(a[0])


def test_form_page(backend):
    with pdfwords.open(W9) as d:
        assert len(d) == 6
        words = [w[4] for w in d[0].get_text("words")]
    assert "W-9" in words and "Taxpayer" in words


def test_clip_and_sort(backend):
    pg = pdfwords.open(W9)[0]
    allw = pg.get_text("words")
    clip = (0, 0, pg.rect[2], pg.rect[3] / 2)
    top = pg.get_text("words", clip=clip)
    assert 0 < len(top) < len(allw)
    assert all(w[1] < clip[3] for w in top)
    assert sorted(w[4] for w in pg.get_text("words", sort=True)) == sorted(w[4] for w in allw)
    assert sorted(w[4] for w in pg.get_text("words", sort="xycut")) == sorted(w[4] for w in allw)


def test_words_array(backend):
    np = pytest.importorskip("numpy")
    pg = pdfwords.open(W9)[0]
    w = pg.get_text("words", sort="xycut")
    bb, ids, txt = pg.words_array(sort="xycut")
    assert bb.dtype == np.float64 and bb.shape == (len(w), 4) and ids.shape == (len(w), 3)
    assert [tuple(map(float, r)) for r in bb] == [x[:4] for x in w]
    assert txt == [x[4] for x in w]


def test_unknown_backend():
    with pytest.raises(ValueError):
        pdfwords.open(SIMPLE, backend="nope")


def test_cli(backend, tmp_path):
    out = tmp_path / "o.json"
    subprocess.check_call([sys.executable, "-m", "pdfwords", SIMPLE, "--mode", "words", "--pages", "0",
                           "--sort", "xycut", "--backend", backend, "-o", str(out)])
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res[0]["page"] == 0 and res[0]["content"][0][4] == "Simple"


def test_parallel_words():
    with pdfwords.open(W9) as d:
        ref = [p.get_text("words") for p in d]
    res = pdfwords.parallel_words(W9, processes=2)
    assert [[tuple(x) for x in p] for p in res] == ref
