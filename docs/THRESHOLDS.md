# Grouping thresholds: how the defaults were derived

pdfwords groups PDFium's glyphs into words, lines and blocks with a small set of
thresholds (`pdfwords.LayoutParams` in Python, `pdfwords_core::LayoutParams` in Rust; the two
must stay identical, which `tests/test_parity.py` enforces). This page describes how the
defaults were obtained. Everything is reproducible:

```bash
python tools/make_synthetic.py        # regenerate the synthetic ground-truth PDF (seeded)
python tools/fetch_test_pdfs.py       # download the public test documents (optional)
pip install pymupdf                   # optional: enables the compatibility half of the objective
python tools/tune_thresholds.py       # sweep; writes tools/tune_results.json (curves + chosen values)
python tools/tune_thresholds.py --eval
```

## The thresholds

Distances are in multiples of the current font size unless marked *pt*.

| Parameter | Meaning |
|---|---|
| `word_gap` | Forward gap at or above which a word space is inserted. Smaller gaps are treated as kerning. |
| `column_gap` | Forward gap at or above which the line ends (table cells, columns). |
| `baseline_tol` | A baseline shift below this keeps the glyph on the same line. |
| `block_gap` | A baseline shift above this starts a new block. |
| `dup_dist` | The same character drawn again closer than this is an overprint ("fake bold") and is dropped. |
| `gen_space_min` | The lowest gap still accepted as a word break when PDFium reports a generated space. |
| `indent_pt` (pt) | A line starting more than this to the right of the previous line's start begins a new block (paragraph indent). |
| `superscript_rise` | A baseline raised by more than this above the line's first baseline sets the superscript flag. |
| `xycut_gap_x` (pt) | XY-cut (`sort="xycut"`): the minimum vertical gutter that separates columns. |
| `xycut_gap_y` (pt) | XY-cut: the minimum horizontal gap that separates strips. |

## Data

**A. Synthetic ground truth (ours, committed).** `tools/make_synthetic.py` builds
`tests/fixtures/synthetic.pdf` (12 pages, ~4.9k words) word by word with PDFium's
page-object API and the standard-14 fonts. It writes the exact words, line/block membership,
superscripts, overprinted words and reading order to `synthetic.json`.

* **Page types:**
  * prose (spaced and indented paragraphs, one letter-spaced page)
  * bullet lists
  * two-column pages, one of them written in shuffled content order
  * tables
* **Typography** is drawn from ordinary ranges (seeded):

  | Property | Range |
  |---|---|
  | size | 7–13 pt |
  | leading | 1.10–1.30 |
  | word space | 0.20–0.50 |
  | tracking | −0.03 to 0.06 |
  | paragraph gap | +0.5 to 1.2 |
  | indent | 1–2.5 em |
  | line-start jitter | 0–0.3 pt |
  | cell / column gaps | 1.5–4 em |
  | footnote superscripts | size 0.55–0.75, rise 0.25–0.45 |
  | subscripts | drop 0.12–0.20 |
  | baseline jitter | ±0.06 |
  | fake-bold offsets | 0.01–0.04 |

**B. Real documents (optional).** These are:
* the IRS Form W-9 (US government work, committed);
* our generated `simple_text.pdf`;
* the camelot sample table;
* two arXiv papers, downloaded by `tools/fetch_test_pdfs.py` and pinned by SHA-256.

On B we measure *output compatibility* with PyMuPDF, treated as a black box: we compare its
`get_text("words")` and `rawdict` output with ours. PyMuPDF is never imported by the library
itself.

## Objective

Metrics are grouped by the threshold that controls them, and each threshold is optimised on
its own group. For each group, objective = (A-score + B-score) / 2, or the A-score alone when
pymupdf is missing.

| Group | Thresholds | A (synthetic, exact truth) | B (real, vs PyMuPDF output) |
|---|---|---|---|
| grouping | word_gap, column_gap, baseline_tol, block_gap, dup_dist, gen_space_min, indent_pt | word F1, same-line and same-block agreement on consecutive word pairs, fake-bold de-duplication | word F1, same-line, same-block |
| superscript | superscript_rise | superscript F1 | superscript-char F1 |
| order | xycut_gap_x, xycut_gap_y | reading order of two-column pages | content order of the LaTeX papers (≈ reading order) |

## Search

1. **Start from neutral round numbers**, not from any existing implementation: word_gap 0.25,
   column_gap 1.0, baseline_tol 0.5, block_gap 2.0, dup_dist 0.25, gen_space_min 0,
   indent_pt 1, superscript_rise 0.2, xycut 10 / 2 pt.
2. **Coordinate descent over two rounds.** For each threshold, sweep its grid with the others
   fixed, using these grids:

   | Parameter | Grid |
   |---|---|
   | word_gap | 0.02–0.40 step 0.01 |
   | column_gap | 0.40–3.0 step 0.05 |
   | baseline_tol | 0.10–1.40 step 0.05 |
   | block_gap | 1.0–3.0 step 0.05 |
   | dup_dist | 0–0.6 step 0.01 |
   | gen_space_min | −1.0 to 0.3 step 0.02 |
   | indent_pt | 0.1–12 step 0.1 |
   | superscript_rise | 0–0.4 step 0.01 |
   | xycut_gap_x | 1–30 step 0.5 |
   | xycut_gap_y | 0.25–10 step 0.25 |

3. **Pick the plateau's midpoint.** The plateau is the contiguous run of grid values whose
   objective is within 0.0002 of the best. Taking its midpoint gives the value farthest from
   where results start to degrade, which is more robust than the arg-max. When a plateau
   reaches the end of the grid, the threshold is insensitive on that side for our data. The
   midpoint is then partly a choice, and this is noted below.

## Result

| Parameter | Chosen | Plateau (round 2) | Notes |
|---|---|---|---|
| word_gap | **0.14** | 0.07 – 0.21 | Lower bound: tight letter-spacing; upper bound: narrow justified word spaces. |
| column_gap | **0.85** | 0.75 – 0.95 | Upper bound: cell / column gaps; lower bound: wide justified spaces. |
| baseline_tol | **0.85** | 0.80 – 0.95 | Must exceed superscript/subscript shifts and stay below the line pitch. |
| block_gap | **1.5** | 1.50 – 1.50 | Narrow: between normal leading and paragraph spacing. |
| dup_dist | **0.10** | 0.00 – 0.21 | Upper bound: genuine double letters (`ll`, `ss`). The lower side is flat because PDFium already removes exact overprints. |
| gen_space_min | **−0.46** | −1.00 – 0.10 | One-sided: insensitive below 0.10 on our data. |
| indent_pt | **4.1** | 0.4 – 7.9 | Lower bound: line-start jitter; upper bound: the smallest paragraph indent. |
| superscript_rise | **0.21** | 0.18 – 0.24 | Bounded by baseline jitter (below) and real footnote marks (above). |
| xycut_gap_x | **12.5** pt | 9.0 – 16.0 | Lower bound: in-column gaps (tables, figures); upper bound: the narrowest gutters. |
| xycut_gap_y | **2.0** pt | 1.5 – 2.5 | |

### Scores

Scores use the tuner's metrics. "Legacy" is the hand-set values used before v0.1.0.

| | grouping | superscript | order |
|---|---|---|---|
| neutral start | 0.9793 | 0.6269 | 0.9329 |
| legacy | 0.9988 | 0.9213 | 0.9390 |
| **tuned (default)** | **0.9987** | **0.9192** | **0.9415** |

Breakdown (tuned / legacy):

| Metric | tuned | legacy |
|---|---|---|
| synthetic word F1 | 1.0000 | 0.9997 |
| synthetic same-line | 1.0000 | 0.9998 |
| synthetic superscript F1 | 1.0000 | 0.9964 |
| real word F1 | 0.9976 | 0.9977 |
| real same-block | 0.9980 | 0.9989 |
| real reading order | 0.8829 | 0.8780 |

### Compatibility with PyMuPDF, full documents

From `benchmarks/compare_pymupdf.py [--legacy-params]`:

| PDF | word recall legacy → tuned | same-block legacy → tuned | same-line legacy → tuned |
|---|---|---|---|
| arxiv_attention (15 p) | 0.9962 → 0.9962 | 0.9997 → 0.9988 | 1.0000 → 0.9997 |
| arxiv_resnet (12 p) | 0.9995 → 0.9994 | 0.9995 → 0.9982 | 1.0000 → 0.9988 |
| irs_w9 (6 p) | 0.9981 → 0.9978 | 0.9973 → 0.9970 | 0.9992 → 1.0000 |
| table_camelot, simple_text, rot90 | 1.0 → 1.0 | unchanged | unchanged |
| **weighted, 22,907 words** | **0.99825 → 0.99813** | | |

Word bounding boxes are unchanged: the median word IoU is 1.0 on every document.

## Re-tuning

Add documents, ideally with ground truth, to the synthetic generator or the real set, then re-run
`tools/tune_thresholds.py`. Copy the new values into `LayoutParams` in
`python/pdfwords/layout.py` **and** `crates/pdfwords-core/src/layout.rs`, then run `pytest`
(the parity test fails if the two disagree).
