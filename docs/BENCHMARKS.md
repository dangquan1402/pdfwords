# Benchmarks

Two suites, both reproducible from the repository:

* `tools/bench_suite.py`: **text extraction** against other Python PDF libraries. It measures
  speed, word accuracy and reading order (below).
* `tools/bench_render.py`: **rendering** against pypdfium2, PyMuPDF and pdf2image (see
  [RENDERING.md](RENDERING.md#speed)).

```bash
python tools/fetch_test_pdfs.py          # third-party PDFs (arXiv papers, camelot sample) -> tests/data/
pip install pdftext pymupdf pdfplumber pdf_oxide     # whichever you want to compare
python tools/bench_suite.py --runs 5 --json results.json --md results.md
```

Each library is used only as a black box, through its public API with default settings. Where
a library has a reading-order option (`sort=True`), that variant is listed too. The raw results
of the run below are in `benchmarks/results/bench_suite.json`.

## Method

* **Speed**: wall time to open the document and extract every page, divided by the page count.
  The table shows the best of 5 runs after a warm-up.
  * "Plain text" is each library's text API.
  * "Words" is its word-with-boxes API.
  * pdftext is timed per document, because its API is per document.
* **Word F1** is measured on **held-out synthetic documents**.
  * `tools/make_synthetic.py` builds 36 pages word by word, so text and reading order are
    known exactly. It uses seeds 1–3. The pdfwords thresholds were tuned on a different seed
    (`tests/fixtures/synthetic.pdf`), so this set was never used for tuning.
  * The page kinds are prose (justified, indented, letter-spaced, superscripts), lists, two
    columns and tables.
  * The score is the multiset F1 between the whitespace tokens of the engine's plain text and
    the ground-truth words. Any merged, split or lost word costs.
* **Reading order** is measured on the same pages. Output tokens are matched to ground-truth
  words, and the score is the fraction of word pairs that appear in ground-truth order
  (1.0 = perfect, about 0.5 = random). The metric is Kendall-tau style, rescaled to [0, 1].
* **Agreement with PyMuPDF** compares word multisets with PyMuPDF's `get_text("words")` on
  real documents. It measures agreement, not truth.

## Results

Run on Linux x86-64, CPython 3.13, pdfwords 0.3.0 (Rust backend unless noted), pypdfium2 5.10
(PDFium 151), PyMuPDF 1.28.2, pdftext 0.7.1, pdfplumber 0.11.10 and pdf_oxide 0.3.78.

### Speed: plain text, ms/page

| engine | arxiv_attention.pdf | arxiv_resnet.pdf | table_camelot.pdf | irs_w9.pdf | synthetic.pdf |
|---|---:|---:|---:|---:|---:|
| pdfwords 0.3.0 | 5.68 | 3.62 | 3.31 | 2.6 | 3.33 |
| pdfwords (python) 0.3.0 | 17.16 | 24.91 | 15.29 | 31.57 | 18.55 |
| pdftext 0.7.1 | 20.29 | 30.29 | 17.02 | 35.45 | 20.76 |
| pdftext sort=True 0.7.1 | 20.41 | 30.63 | 16.75 | 35.7 | 20.91 |
| PyMuPDF 1.28.2 | 4.56 | 3.64 | 3.25 | 2.62 | 2.74 |
| PyMuPDF sort=True 1.28.2 | 15.86 | 25.53 | 15.6 | 31.05 | 13.97 |
| pdfplumber 0.11.10 | 107.18 | 109.06 | 69.67 | 112.09 | 122.4 |
| pdf_oxide 0.3.78 | 8.3 | 10.12 | 5.11 | 5.53 | 7.5 |
| pypdfium2 (raw) 5.10.1 | 5.72 | 3.43 | 3.39 | 2.48 | 3.31 |

### Speed: words with boxes, ms/page

| engine | arxiv_attention.pdf | arxiv_resnet.pdf | table_camelot.pdf | irs_w9.pdf | synthetic.pdf |
|---|---:|---:|---:|---:|---:|
| pdfwords | 5.7 | 3.75 | 3.1 | 2.79 | 3.47 |
| pdfwords (python) | 17.57 | 27.04 | 16.99 | 34.54 | 19.08 |
| pdftext | 22.35 | 32.03 | 16.9 | 37.45 | 20.52 |
| pdftext sort=True | 22.22 | 32.17 | 17.03 | 36.41 | 20.57 |
| PyMuPDF | 4.99 | 4.34 | 3.64 | 3.23 | 2.93 |
| PyMuPDF sort=True | 7.91 | 10.13 | 6.32 | 10.49 | 6.03 |
| pdfplumber | 103.72 | 102.08 | 71.39 | 96.16 | 118.19 |
| pdf_oxide | 7.32 | 9.53 | 4.39 | 6.16 | 8.16 |
| pypdfium2 (raw) | 5.6 | 3.43 | 3.3 | 2.41 | 3.22 |

### Held-out synthetic ground truth (seeds 1-3): word F1 / reading-order concordance

| engine | prose | list | two_column | table | all |
|---|---:|---:|---:|---:|---:|
| pdfwords | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 0.970 | 1.000 / 1.000 | 1.000 / 0.993 |
| pdfwords (python) | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 0.970 | 1.000 / 1.000 | 1.000 / 0.993 |
| pdftext | 0.991 / 1.000 | 1.000 / 1.000 | 1.000 / 0.818 | 1.000 / 1.000 | 0.996 / 0.954 |
| pdftext sort=True | 0.991 / 1.000 | 1.000 / 1.000 | 1.000 / 0.790 | 1.000 / 1.000 | 0.996 / 0.947 |
| PyMuPDF | 0.979 / 0.998 | 0.953 / 0.996 | 0.977 / 0.816 | 1.000 / 1.000 | 0.976 / 0.953 |
| PyMuPDF sort=True | 0.958 / 0.998 | 0.901 / 0.996 | 0.899 / 0.770 | 1.000 / 1.000 | 0.940 / 0.941 |
| pdfplumber | 0.685 / 0.981 | 0.685 / 0.985 | 0.743 / 0.755 | 0.540 / 0.963 | 0.681 / 0.920 |
| pdf_oxide | 0.957 / 0.993 | 0.851 / 0.993 | 0.870 / 0.970 | 0.895 / 0.780 | 0.911 / 0.934 |
| pypdfium2 (raw) | 0.973 / 0.999 | 1.000 / 1.000 | 0.986 / 0.817 | 1.000 / 1.000 | 0.984 / 0.954 |

### Word agreement with PyMuPDF (multiset F1)

| engine | arxiv_attention.pdf | arxiv_resnet.pdf | table_camelot.pdf | irs_w9.pdf |
|---|---:|---:|---:|---:|
| pdfwords | 0.9953 | 0.9872 | 1.0 | 0.9983 |
| pdfwords (python) | 0.9953 | 0.9872 | 1.0 | 0.9983 |
| pdftext | 0.9848 | 0.958 | 1.0 | 0.9981 |
| pdftext sort=True | 0.9848 | 0.958 | 1.0 | 0.9981 |
| PyMuPDF sort=True | 1.0 | 1.0 | 1.0 | 1.0 |
| pdfplumber | 0.2517 | 0.5383 | 1.0 | 0.9983 |
| pdf_oxide | 0.9835 | 0.9853 | 1.0 | 0.9981 |
| pypdfium2 (raw) | 0.9838 | 0.9607 | 1.0 | 0.9981 |

### Markdown and exports (pdfwords only), ms/page

| PDF | `to_markdown` | `export("hocr")` | `export("alto")` | `get_drawings` | `get_images` |
|---|---:|---:|---:|---:|---:|
| arXiv "Attention" (15 p.) | 18.8 | 6.9 | 9.8 | 8.4 | 5.3 |
| arXiv "ResNet" (12 p.) | 19.4 | 7.1 | 12.4 | 5.9 | 2.7 |
| IRS W-9 (6 p., tagged) | 33.3 | 5.5 | 11.4 | 1.5 | 1.1 |

## Reading the numbers

* **Speed.**
  * The Rust backend is on par with PyMuPDF's default (unsorted) extraction and with raw
    pypdfium2. That is the floor, because all of them spend most of their time inside the PDF
    engine.
  * pdfwords' plain text is already in column-aware reading order. It is 3.5–14× faster than
    PyMuPDF `sort=True` and pdftext, and 20–40× faster than pdfplumber.
  * The pure-Python backend is about as fast as pdftext.
* **Words.**
  * pdfwords gets every word of the held-out set right (F1 1.000).
  * The others lose some words. For example, pdftext and PyMuPDF glue superscript footnote
    marks to the preceding word (`word.9`), and letter-spaced lines split or merge words.
  * pdfplumber's default `x_tolerance` merges words in tightly set LaTeX text that has no
    space characters: `Recurrentneuralnetworks,...` on the Attention paper. Hence its low
    agreement there.
* **Reading order.**
  * Prose, lists and tables are easy for everyone.
  * Two-column pages separate the engines:
    * pdfwords' xycut ordering scores 0.97;
    * pdf_oxide also scores 0.97;
    * stream-order engines are about 0.82;
    * `sort=True` variants (top-to-bottom, left-to-right) score 0.77–0.79, because they
      interleave the columns.
* **Caveat.**
  * The synthetic generator and the pdfwords grouping were written by the same authors, and
    the documents are simple (base-14 fonts, clean layouts).
  * Treat the synthetic numbers as a regression check of known layouts, not as a general
    accuracy claim.
  * The agreement table on real documents is the more neutral signal.
