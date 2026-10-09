# pdfwords

[![CI](https://github.com/dangquan1402/pdfwords/actions/workflows/ci.yml/badge.svg)](https://github.com/dangquan1402/pdfwords/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**PDF text extraction with character, word, line and block bounding boxes.** Its output matches
PyMuPDF's `page.get_text()`, it is built on [PDFium](https://pdfium.googlesource.com/pdfium/)
through [pypdfium2](https://github.com/pypdfium2-team/pypdfium2), and it is licensed
**Apache-2.0**. An optional Rust core makes it about as fast as PyMuPDF.

![W-9 page with word (blue) and block (red, numbered in reading order) boxes](docs/overlay_w9.png)

## Why

`PyMuPDF.get_text("words" | "dict" | "rawdict")` is the de-facto API for "text + where it is on
the page". However, PyMuPDF/MuPDF is **AGPL-3.0**, which is a problem for closed-source,
on-device or commercial apps. pdfwords provides the same output shapes and coordinate
conventions:

* on a permissively licensed engine: PDFium is BSD-3/Apache-2.0, pypdfium2 is Apache-2.0/BSD-3;
* with a grouping algorithm written from scratch and its thresholds
  [derived from data](docs/THRESHOLDS.md);
* with a pure-Rust core (`crates/pdfwords-core`) that can be embedded in iOS/Android/desktop apps.

## Install

```bash
pip install pdfwords            # once published to PyPI (abi3 wheels: Linux, macOS, Windows)
```

From source (needs a Rust toolchain for the fast backend):

```bash
git clone https://github.com/dangquan1402/pdfwords && cd pdfwords
pip install .                   # builds the Rust extension with maturin
# or, pure Python only (no Rust needed): put python/ on the path
PYTHONPATH=python python -c "import pdfwords; print(pdfwords.available_backends())"   # ['python']
```

The only runtime dependency is `pypdfium2`. `numpy` is optional, for `Page.words_array()`.
Editing (content streams, redaction, text insertion) needs the `edit` extra:
`pip install "pdfwords[edit]"`. It adds pypdf (BSD-3-Clause), fontTools (MIT) and
Pillow (MIT-CMU).

## Quick start

```python
import pdfwords

doc = pdfwords.open("file.pdf")          # like pymupdf.open
page = doc[0]

page.get_text("words")
# [(x0, y0, x1, y1, "word", block_no, line_no, word_no), ...]   PDF points, top-left origin

page.get_text("words", sort="xycut")     # column-aware reading order (also sort=True: y, x)

d = page.get_text("dict")                # {"width", "height", "blocks": [{"bbox", "lines": [{"bbox", "dir",
for b in d["blocks"]:                    #     "spans": [{"text", "font", "size", "flags", "color", "bbox", "origin"}]}]}]}
    for line in b["lines"]:
        for span in line["spans"]:
            print(span["bbox"], span["font"], span["size"], span["text"])

raw = page.get_text("rawdict")           # spans carry "chars": [{"c", "bbox", "origin", "synthetic"}]
page.get_text("blocks")                  # [(x0, y0, x1, y1, "text", block_no, 0), ...]
page.get_text("text")                    # plain text;  also "json" / "rawjson"

# options shared by all modes
page.get_text("words", clip=(0, 0, 300, 400), rotated=False, ligatures=False,
              dehyphenate=False, delimiters=None)

bboxes, ids, texts = page.words_array()  # numpy: float64 [N,4], int32 [N,3] (block, line, word), list[str]
pdfwords.parallel_words("big.pdf", processes=8)   # one process per worker (PDFium is single-threaded)
```

**Coordinates.** Coordinates are in PDF points with a top-left origin, relative to the CropBox
of the *unrotated* page, which is the same as PyMuPDF. Pass `rotated=True` to get coordinates
on the page as displayed. `page.rect` and `page.rotation` behave like PyMuPDF's.

**Span flags.** These use the PyMuPDF bit values: superscript 1, italic 2, serif 4,
monospaced 8, bold 16.

## Editing: page content, redaction, text insertion

These are PyMuPDF-style methods (`pip install "pdfwords[edit]"`). They use the same
coordinates as `get_text()`: top-left origin, unrotated page.

```python
doc = pdfwords.open("in.pdf")
page = doc[0]

page.read_contents()            # b"q 1 0 0 1 ... BT /F1 12 Tf ... ET Q"  (all content streams, decoded)
page.get_contents()             # [12, 13]  xrefs of the content streams
doc.xref_object(12)             # "<< /Length 1234 /Filter /FlateDecode >>"
doc.xref_stream(12)             # decoded stream bytes; also xref_stream_raw / update_stream / xref_length

# search + true redaction
for rect in page.search_for("Jane Doe"):          # also regex=True, quads=True
    page.add_redact_annot(rect, text="REDACTED", fill=(0, 0, 0), text_color=(1, 1, 1))
page.apply_redactions()         # images=PDF_REDACT_IMAGE_PIXELS, graphics=PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
page.redaction_report           # {"glyphs_removed": 8, "images_blanked": 0, ..., "leftover_chars": [], "collateral_chars": []}
doc.scrub()                     # metadata, XMP, JavaScript, attachments, thumbnails, XFA

# text insertion
page.insert_text((72, 72), "Hello\nWorld", fontsize=12, fontname="helv", color=(1, 0, 0), rotate=0)
page.insert_text((72, 120), "Tiếng Việt có dấu", fontfile="DejaVuSans.ttf")   # embedded + subset, Unicode
rc = page.insert_textbox((72, 150, 300, 250), long_text, fontsize=10, align=3)  # 0 left 1 center 2 right 3 justify
# rc >= 0: unused height;  rc < 0: did not fit, nothing written (as in PyMuPDF)

doc.save("out.pdf", garbage=3, deflate=True)       # incremental=True allowed unless redacted/scrubbed
```

**Redaction removes content. It does not just paint over it.** `apply_redactions()` rewrites
the page's content streams. Every glyph under a redaction rectangle is deleted from its
`Tj`/`TJ` string and replaced by the equivalent `TJ` displacement, so the remaining glyphs
keep their exact positions. Form XObjects that are hit are copied and rewritten, so shared
forms on other pages stay intact. Image pixels under the area are overwritten, or the image
is removed. Vector art inside the area is removed, and so are annotations and form fields
that overlap it. `/ActualText` around removed glyphs and the document's XFA packets are
dropped. After rewriting, the page is re-extracted with PDFium:

* any character still mostly inside an area raises `RedactionError`;
* any character outside the areas that moved or vanished raises a `RedactionWarning`.

Saving always rewrites the file and garbage-collects the old streams. See
[docs/EDITING.md](docs/EDITING.md) for the method, guarantees and limits.

## CLI

```bash
pdfwords file.pdf --mode words --sort xycut --pages 0,2-4 -o words.json
pdfwords file.pdf --mode text
pdfwords file.pdf --mode rawdict --rotated --backend python

pdfwords redact in.pdf -o out.pdf --search "Jane Doe" --search "555-0100" --rect 72,700,300,720 \
    --fill 0,0,0 --text REDACTED --images pixels --graphics covered --scrub
pdfwords insert-text in.pdf -o out.pdf --page 0 --point 72,72 --text "Approved\nQ. Dang" --fontname hebo
pdfwords insert-text in.pdf -o out.pdf --rect 72,100,300,200 --align justify --text "..." --fontfile font.ttf
pdfwords contents in.pdf --pages 0      # decoded content streams
```

## Backends

| backend | when | speed |
|---|---|---|
| `rust` | default when the compiled extension `pdfwords._native` is present (all wheels) | ≈ PyMuPDF |
| `python` | automatic fallback; pure Python + pypdfium2 ctypes | 3–11× slower |

You can select one with `pdfwords.open(path, backend="python")`, `$PDFWORDS_BACKEND`, or
`--backend` on the CLI. Both backends produce **identical** output, which `tests/test_parity.py`
asserts with `==` on every mode and option.

The Rust backend binds to the same PDFium library that pypdfium2 has loaded: it is located with
`dladdr` / `GetModuleHandleEx`. Override it with `PDFWORDS_PDFIUM_LIB=/path/to/libpdfium.so`.

## Benchmarks

The numbers below are ms per page, best of 5, for a whole document including open, on Linux
x86-64 (CPython 3.13, pypdfium2 5.10 / PDFium 151, PyMuPDF 1.28.2, pdftext 0.7.1). They come
from `benchmarks/bench_backends.py`; raw output is in `benchmarks/results/bench_backends.json`.

| PDF | pages | **pdfwords rust** words | rust rawdict | pdfwords python words | PyMuPDF words | PyMuPDF rawdict | pdftext |
|---|---|---|---|---|---|---|---|
| arXiv "Attention" | 15 | **5.93** | 7.47 | 17.39 | 5.18 | 15.61 | 28.60 |
| arXiv "ResNet" (2-col) | 12 | **3.96** | 6.89 | 25.95 | 4.43 | 8.08 | 39.39 |
| IRS W-9 (form) | 6 | **2.86** | 6.54 | 32.86 | 3.26 | 7.04 | 41.92 |
| camelot table | 1 | **3.23** | 4.62 | 16.90 | 3.87 | 5.19 | 20.56 |
| simple text | 2 | **1.18** | 1.84 | 5.97 | 1.55 | 1.91 | 7.88 |

About 75–85% of the Rust time is PDFium itself (`FPDF_LoadPage` + `FPDFText_LoadPage`).
pdfwords' own grouping and Python object creation cost about 0.4 ms per page.

## Accuracy vs PyMuPDF

`benchmarks/compare_pymupdf.py` compares pdfwords with PyMuPDF on 6 documents (37 pages,
22,907 PyMuPDF words). PyMuPDF is used only as a black-box reference.

| PDF | word recall | median word-bbox IoU | same-line agreement | same-block agreement |
|---|---|---|---|---|
| arxiv_attention | 0.9962 | 1.0 | 0.9997 | 0.9988 |
| arxiv_resnet | 0.9994 | 1.0 | 0.9988 | 0.9982 |
| irs_w9 | 0.9978 | 1.0 | 1.0 | 0.9970 |
| table_camelot | 1.0 | 1.0 | 0.9976 | 0.9953 |
| simple_text | 1.0 | 1.0 | 1.0 | 1.0 |
| rot90 | 1.0 | 0.85* | 1.0 | 1.0 |

**Weighted word recall: 99.81%.** \*rot90 uses a non-embedded base-14 font, and PDFium's
substitute font metrics make the boxes slightly taller; x positions are identical.

On our synthetic ground-truth set (`tests/fixtures/synthetic.*`), word F1, line agreement and
superscript detection are all 1.0, and block agreement is 0.996. See
[docs/THRESHOLDS.md](docs/THRESHOLDS.md).

## Limitations

* **No OCR.** Scanned pages without a text layer return nothing. Pair pdfwords with Tesseract,
  Apple Vision or ML Kit.
* **No vertical CJK writing mode** (`wmode` is always 0). RTL support is a minimal
  visual→logical reordering, not full UAX #9.
* **Ligatures** come out decomposed by default ("fi"), because PDFium does not expose the
  original code point. `ligatures=True` re-composes U+FB00–FB06.
* **Text blocks only.** There are no image blocks and no underline/strike-out detection yet.
* **PDFium is not thread-safe.** Do not call pdfwords or pypdfium2 from several threads at once.
  The Rust backend holds the GIL for each call. Use processes for parallelism
  (`parallel_words`).
* **Base-14 metrics.** Boxes of non-embedded base-14 fonts follow PDFium's substitute metrics,
  which differ slightly from PyMuPDF's.
* **Editing** has no text shaping: complex scripts that need GSUB/GPOS shaping (Arabic,
  Devanagari, ...) are not supported. Precomposed Latin, including Vietnamese, works. For other
  redaction limits see [docs/EDITING.md](docs/EDITING.md#limits).

## Roadmap

* **iOS / Android via `pdfwords-core`.** The pure-Rust crate already does the whole pipeline.
  Next steps:
  * a uniffi or C ABI layer, giving an xcframework (with PDFium iOS binaries) and an Android AAR
    built with `cargo ndk`;
  * static linking of PDFium.
* Publish `pdfwords-core` to crates.io.
* Image blocks, a header/footer filter, and better reading order.
* An optional OCR fallback that emits the same schema.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest numpy pymupdf      # pymupdf: optional, accuracy tests only
maturin develop --release                     # builds the Rust extension into python/pdfwords
python tools/fetch_test_pdfs.py               # optional: download public test PDFs
pytest                                        # both backends + parity + accuracy
cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings && cargo fmt --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

* The output format and API are designed to be **compatible with PyMuPDF's** `Page.get_text()`,
  so that existing code can switch easily. pdfwords is an independent project, **not affiliated
  with or endorsed by Artifex or the PyMuPDF project**, and it contains no MuPDF/PyMuPDF code.
  PyMuPDF is used only as an optional, black-box reference in tests and benchmarks.
* Text extraction is powered by [PDFium](https://pdfium.googlesource.com/pdfium/) and
  [pypdfium2](https://github.com/pypdfium2-team/pypdfium2). See [NOTICE](NOTICE).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
