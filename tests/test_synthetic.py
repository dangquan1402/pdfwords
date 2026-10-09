# SPDX-License-Identifier: Apache-2.0
"""Accuracy against exact ground truth (tests/fixtures/synthetic.*, see tools/make_synthetic.py)."""
import difflib
import json
import os

import pdfwords
from conftest import FIXTURES

GT = json.load(open(os.path.join(FIXTURES, "synthetic.json"), encoding="utf-8"))
PDF = os.path.join(FIXTURES, "synthetic.pdf")


def _match(ref, out):
    sm = difflib.SequenceMatcher(None, ref, out, autojunk=False)
    return [(i1 + k, j1 + k) for t, i1, i2, j1, j2 in sm.get_opcodes() if t == "equal" for k in range(i2 - i1)]


def test_synthetic_ground_truth(backend):
    n = m = line_ok = line_n = block_ok = block_n = sup_ok = sup_n = 0
    with pdfwords.open(PDF) as d:
        for page, gt in zip(d, GT["pages"]):
            ref = gt["words"]
            out = page.get_text("words")
            pairs = _match([w[0] for w in ref], [w[4] for w in out])
            n += len(ref)
            m += len(pairs)
            for (a0, b0), (a1, b1) in zip(pairs, pairs[1:]):
                line_n += 1
                line_ok += (ref[a0][1] == ref[a1][1]) == (out[b0][5:7] == out[b1][5:7])
                if ref[a0][2] is not None and ref[a1][2] is not None:
                    block_n += 1
                    block_ok += (ref[a0][2] == ref[a1][2]) == (out[b0][5] == out[b1][5])
            # superscripts: every marked word has a superscript span
            rd = page.get_text("rawdict")
            sup_text = "".join(c["c"] for b in rd["blocks"] for ln in b["lines"] for sp in ln["spans"]
                               if sp["flags"] & pdfwords.TEXT_FONT_SUPERSCRIPT for c in sp["chars"])
            for i in gt["superscripts"]:
                sup_n += 1
                sup_ok += ref[i][0][-1] in sup_text
    assert m / n >= 0.999
    assert line_ok / line_n >= 0.999
    assert block_ok / block_n >= 0.995
    assert sup_ok == sup_n


def test_synthetic_reading_order(backend):
    with pdfwords.open(PDF) as d:
        for page, gt in zip(d, GT["pages"]):
            if gt["kind"] != "two_column":
                continue
            ref = sorted(gt["words"], key=lambda w: w[3])
            out = page.get_text("words", sort="xycut")
            pairs = _match([w[0] for w in ref], [w[4] for w in out])
            assert len(pairs) >= 0.995 * len(ref)
