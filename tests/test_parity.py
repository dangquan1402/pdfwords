# SPDX-License-Identifier: Apache-2.0
"""The Rust backend must produce exactly the same output as the pure-Python backend."""
import os

import pytest

import pdfwords
from conftest import FIXTURES, fetch_test_pdfs

pytestmark = pytest.mark.skipif("rust" not in pdfwords.available_backends(), reason="native extension not built")

NAMES = ["simple_text.pdf", "rot90.pdf", "irs_w9.pdf", "synthetic.pdf"] + list(fetch_test_pdfs.REMOTE)
MODES = ["words", "blocks", "text", "dict", "rawdict", "rawjson"]
VARIANTS = [{}, {"sort": True}, {"sort": "xycut"}, {"rotated": True}, {"ligatures": True},
            {"dehyphenate": True}, {"clip": False}, {"clip": (50, 50, 300, 400)}, {"delimiters": ".,"}]


@pytest.mark.parametrize("name", NAMES)
def test_parity(name):
    path = os.path.join(FIXTURES, name) if name == "synthetic.pdf" else fetch_test_pdfs.path_of(name)
    if not path:
        pytest.skip(f"{name} not available")
    n = 0
    with pdfwords.open(path, backend="python") as dp, pdfwords.open(path, backend="rust") as dr:
        assert len(dp) == len(dr)
        for i in range(len(dp)):
            pp, pr = dp[i], dr[i]
            assert pp.rect == pr.rect and pp.rotation == pr.rotation
            for mode in MODES:
                for kw in VARIANTS:
                    if mode in ("dict", "rawdict", "rawjson") and ("dehyphenate" in kw or "delimiters" in kw):
                        continue
                    a, b = pp.get_text(mode, **kw), pr.get_text(mode, **kw)
                    assert a == b, (name, i, mode, kw)  # strict: values and types
                    n += 1
    assert n > 0
