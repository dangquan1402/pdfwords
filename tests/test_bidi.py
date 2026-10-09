"""Right-to-left / bidirectional text: UAX #9-lite reordering in both backends."""
import pytest

import pdfwords
from pdfwords.layout import _bidi_kind, bidi_levels, reorder

from _featurepdf import rtl_pdf

LOGICAL = ["שלום עולם", "פרק 2 ראשון", "abc שלום def", "(שלום)"]


@pytest.mark.parametrize(
    "visual,logical",
    [
        ("םולש", "שלום"),
        ("123 םולש", "שלום 123"),
        ("abc םולש def", "abc שלום def"),
        ("ןושאר 2 קרפ", "פרק 2 ראשון"),
        ("Hello, םולש!", "Hello, שלום!"),
        ("plain ascii 42", "plain ascii 42"),
    ],
)
def test_reorder_visual_to_logical(visual, logical):
    lev, _ = bidi_levels([_bidi_kind(c) for c in visual])
    assert "".join(reorder(list(visual), lev)) == logical


def test_paragraph_direction():
    assert bidi_levels([_bidi_kind(c) for c in "abc םו"])[1] == 0
    assert bidi_levels([_bidi_kind(c) for c in "ab םולש"])[1] == 1


@pytest.mark.parametrize("backend", pdfwords.available_backends())
def test_rtl_pdf_text(backend):
    page = pdfwords.open(rtl_pdf(), backend=backend)[0]
    assert page.get_text().splitlines() == LOGICAL
    words = [w[4] for w in page.get_text("words")]
    assert words == ["שלום", "עולם", "פרק", "2", "ראשון", "abc", "שלום", "def", "(שלום)"]
    # word boxes stay in visual (page) coordinates: "שלום" in line 1 is the right-hand word
    w = page.get_text("words")
    assert w[0][0] > w[1][0]


def test_rtl_parity():
    if len(pdfwords.available_backends()) < 2:
        pytest.skip("needs both backends")
    data = rtl_pdf()
    outs = [pdfwords.open(data, backend=b)[0].get_text("rawdict") for b in ("rust", "python")]
    assert outs[0] == outs[1]
