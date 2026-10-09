# OCR fallback (0.4)

Scanned pages have no text layer. pdfwords can run an OCR engine and return its result in
**the same schema** as the text layer (`text`, `words`, `blocks`, `dict`, `rawdict`,
markdown, chunks, exports and frames). Code written for the text layer then works on scans.

```python
page.get_text("words", ocr="auto")        # OCR only when page.needs_ocr() says the layer is unusable
page.get_text("dict", ocr=True)           # always OCR
page.get_text("text", ocr={"engine": "tesseract", "lang": "eng+deu", "dpi": 300, "min_conf": 0.3})
doc = pdfwords.open("scan.pdf", ocr="auto")   # default for every get_text() / to_markdown() of doc
page.ocr(engine="rapidocr")               # rawdict, cached per page and settings
pdfwords.ocr.available_engines()          # e.g. ["rapidocr", "tesseract"]
```

```bash
pdfwords scan.pdf --mode words --ocr auto --ocr-engine tesseract --ocr-lang eng
pdfwords export scan.pdf -f markdown --ocr auto
pdfwords frame scan.pdf -o words.parquet --ocr always
```

## Engines

All engines are optional, and `"auto"` picks the first one installed.

| Engine | Install | Licence | Notes |
|---|---|---|---|
| `ocrmac` | `pip install ocrmac` (macOS) | MIT (Apple Vision) | Tried first on macOS. Line-level boxes. |
| `rapidocr` | `pip install rapidocr_onnxruntime` (or `rapidocr` ≥ 2) | Apache-2.0 | Pure pip, CPU, line-level boxes. |
| `tesseract` | `pip install pytesseract` + the tesseract binary | Apache-2.0 | Word boxes, block/line structure, many languages. |
| callable | `f(PIL.Image) -> [(text, conf 0-1 or None, (x0, y0, x1, y1) px), ...]` | yours | Plug in any engine, cloud API or layout model. |

## How it works

1. The page is rendered upright (as displayed) at `dpi` (default 300) with pdfwords'
   renderer.
2. The engine returns text runs with pixel boxes. Runs are split into words at spaces, with
   widths in proportion to their character counts.
3. Lines and blocks come from the engine ids (Tesseract) or from geometry. Lines group words
   by vertical overlap and horizontal proximity. Blocks group lines that are close vertically
   and overlap horizontally.
4. Boxes are mapped back to PDF points: to the unrotated page for `rotated=False`, like the
   text layer, or to the displayed page for `rotated=True`. `/Rotate` scans therefore line up
   with the page.
5. The result is a rawdict:
   * spans use font `"OCR"` and add `"conf"` (mean confidence, 0–1);
   * chars split the word box evenly;
   * spaces between words are synthetic.

   Everything downstream (words, markdown, tables from OCR words, frames) reuses the normal
   code paths. Results are cached per page and settings.

`ocr="auto"` uses `page.needs_ocr()` (see `page.text_quality()` for the reasons). It
triggers on scans without a text layer and on garbled or unmapped text; pages with a usable
text layer are never OCRed.

## Measurements

These are for a 200-dpi scan of `tests/fixtures/simple_text.pdf` (287 words), run on CPU on the
development machine:

| Engine | Time (s/page) | Words recovered |
|---|---|---|
| tesseract 5.5 | 2.6 | 287 / 287 |
| rapidocr_onnxruntime 1.4 | 3.1 | 276 / 287 |

## Limitations

* Char boxes inside a word are evenly split, not glyph-accurate.
* Fonts, sizes and flags come from box height only, so bold and italic are unknown.
* Reading order follows the engine (Tesseract) or pdfwords' XY-cut (`sort=`). Multi-column
  scans depend on the engine's own segmentation.
