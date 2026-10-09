# Migrating from pdftext

`pdfwords.compat.pdftext` implements the extraction API of
[pdftext](https://github.com/datalab-to/pdftext) 0.7 (Apache-2.0) on top of pdfwords' layout
engine: same functions, same arguments, same output schema.

```python
# before
from pdftext.extraction import plain_text_output, paginated_plain_text_output, dictionary_output, table_output
# after
from pdfwords.compat.pdftext import plain_text_output, paginated_plain_text_output, dictionary_output, table_output
```

```bash
pdftext doc.pdf --json --sort --page_range 0,5-10 --workers 4      # before
pdfwords pdftext doc.pdf --json --sort --page_range 0,5-10 --workers 4   # after (same flags)
```

## What is the same

| | |
|---|---|
| Functions | `plain_text_output`, `paginated_plain_text_output`, `dictionary_output`, `table_output`; also `Reference`, `PageReference`, `PdfPasswordError`, `sort_blocks` |
| Arguments | `sort`, `hyphens`, `page_range` (validated the same way), `flatten_pdf`, `quote_loosebox`, `keep_chars`, `disable_links`, `workers` (same 10-pages-per-worker rule), `password`, `pages=` for `table_output` |
| Input | path, bytes or file object |
| Page | `{page, bbox, width, height, rotation, blocks, refs}`, with `width`/`height` as `ceil` ints, swapped (with `bbox`) for 90°/270° pages |
| Block / line | `{bbox, lines}` / `{bbox, spans}`, bboxes as lists |
| Span | `{bbox, text, font: {name, flags, size, weight}, rotation (radians), char_start_idx, char_end_idx, url, superscript, subscript[, chars]}` |
| Char | `{bbox, char, rotation, font, char_idx}` where `char_idx` is the PDFium char index (content-stream order) |
| Line ends | a `"\n"` span with the empty font `{name: "", flags: 0, size: 1.0, weight: -1}` covering PDFium's `\r\n` pair (`char_end_idx = char_start_idx + 1`); none after the page's last line |
| Generated spaces | spaces PDFium inserts between words are their own spans with the empty font |
| Fonts | `name`, `flags` (PDFium's `FPDFText_GetFontInfo` flags) and `weight` are PDFium's values, identical to pdftext's |
| Links | link annotations split spans; external links give `url`, internal ones `#page-{p}-{i}` with `refs` on the target page (`Reference(idx, page, coord)`) |
| Tables | `table_output(pdf, [{"tables": [[x0, y0, x1, y1], ...], "img_size": [w, h]}, ...])` returns `{text, bbox}` fragments relative to each table, in image pixels |

`dictionary_output(...)` with the Rust and the pure-Python backend gives identical results
(`tests/test_compat_pdftext.py::test_backends_identical`).

## What differs, on purpose

* **Grouping.** Blocks, lines and spans come from pdfwords' layout (column-aware lines,
  font-run spans), not pdftext's median-gap heuristics, so a page can have a different number of
  blocks. The text in content order and the char indices are the same.
* **Font size** is the effective size (`Tf` × text matrix scale). pdftext reports PDFium's
  `FPDFText_GetFontSize`, which is `1.0` for the many PDFs that scale text through the matrix
  (every span of the IRS W-9 form, for example).
* **Superscript / subscript** use a baseline-shift test against the line's main text (smaller
  font, baseline raised ≥ 15% of the size → superscript, lowered ≥ 8% → subscript). pdftext
  flags subscripts such as the *k* in d<sub>k</sub> as superscripts, and so do chart tick labels.
* **Link extent.** A char belongs to a link when the link rectangle covers at least half of it,
  and a space only when the link continues on both sides. pdftext includes any char the
  rectangle touches, such as the brackets around `[41]`.
* **Char boxes** are pdfwords' boxes (font ascender/descender, like PyMuPDF). With
  `quote_loosebox=False`, apostrophes get PDFium's tight box, as in pdftext.
* `sort` also accepts `"xycut"` (column-aware reading order).
* pdfwords' extra features are available on the same document: `pdfwords.open(path)` then
  `page.get_links(web=True)` for URLs written in the text, `page.text_quality()`, `page.widgets()`, and so on.

## Measured against pdftext

Char-level agreement, keyed by `char_idx`, over 8 documents (arXiv ×2, IRS W-9, camelot
table, synthetic, rotated page, simple text, link/form fixture), ≈ 200k chars:

* char text identical: 99.4–100% (all eight documents; ≥ 99.66% on the real-world ones);
* font `name`, `flags` and `weight` identical: 100% (size differs only where pdftext reports
  the raw `Tf`);
* the same `refs` (51 and 81 on the two arXiv papers);
* `url` identical: 99.25–100%, with the differences listed above.

Speed: best of 3, whole document, Linux x86-64 8-vCPU Xeon, CPython 3.13, pypdfium2 5.10.1,
pdftext 0.7.1, Rust backend. Reproduce with `tools/bench_pdftext.py`:

| file | pages | `plain_text_output` | `dictionary_output` | `…(disable_links=True)` | `…(keep_chars=True)` |
|---|---:|---|---|---|---|
| arxiv_attention.pdf | 15 | 323 → 89 ms (**3.6×**) | 443 → 193 ms (**2.3×**) | 325 → 111 ms (**2.9×**) | 455 → 202 ms (**2.3×**) |
| arxiv_resnet.pdf | 12 | 402 → 46 ms (**8.7×**) | 511 → 196 ms (**2.6×**) | 404 → 83 ms (**4.9×**) | 522 → 208 ms (**2.5×**) |
| table_camelot.pdf | 1 | 19 → 3 ms (**5.9×**) | 23 → 5 ms (**4.9×**) | 19 → 4 ms (**5.4×**) | 23 → 6 ms (**3.7×**) |
| irs_w9.pdf | 6 | 242 → 16 ms (**14.6×**) | 274 → 25 ms (**11.0×**) | 238 → 22 ms (**10.9×**) | 267 → 48 ms (**5.6×**) |
| synthetic.pdf | 12 | 273 → 42 ms (**6.6×**) | 346 → 78 ms (**4.4×**) | 315 → 59 ms (**5.3×**) | 349 → 121 ms (**2.9×**) |

With links enabled, about half the remaining time is PDFium loading each page a second time to
enumerate link annotations (the same work pdftext does).

## Not implemented

* pdftext's private helpers (`pdftext.pdf.*`, `pdftext.postprocessing.*` apart from
  `sort_blocks`) and its pydantic `settings` object. `WORKER_PAGE_THRESHOLD` is a module
  constant.

pdftext's code was read as the reference for its public API and output. The compat layer is
written independently on pdfwords' engine; no pdftext code is copied.
