# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - not yet released (version bumped in-tree; no tag or PyPI release)

### Added
- **Markdown for LLMs** (`docs/LLM.md`): `pdfwords.to_markdown(src, pages, page_chunks,
  sort, headers_footers, tables, images, image_dir, dpi, page_separators)` and
  `page.get_text("markdown")`.
  - Headings come from structure roles, the outline (TOC), font-size statistics and bold
    lines. Repeated font headings are demoted, and levels are harmonised with the TOC.
  - Also produced: lists (bullets, enumerators, nesting), code blocks, links, bold/italic,
    tagged tables and image placeholders (with the tagged `/Alt` text).
  - Running headers/footers, page numbers and vertical margin stamps are removed.
  - `page_chunks=True` returns per-page dicts with elements, boxes and metadata.
- `pdfwords.chunks(src, max_chars, min_chars, split_level, overlap)`: heading-aware RAG
  chunks. Each chunk has its heading path, pages, and per-element page+bbox provenance.
- **Tagged PDFs:**
  - `page.is_tagged` and `page.get_struct_tree()` (roles, MCIDs, alt/actual text, lang).
  - `get_text(sort="struct")`: structure-tree reading order, where untagged text keeps its
    xycut slot and blocks are split at element changes.
  - `get_text("dict", roles=True)`: block/span `role` and span `mcid`.
  - Both backends are supported.
- **Objects:**
  - `page.get_images()` (alias `get_image_info`): boxes, quads, size, colour space,
    filters, MCID, optional pixel digest.
  - `page.get_image(n)`: the image as PIL.
  - `page.get_drawings()`: vector paths as PyMuPDF-style items, colours, widths, dashes and
    opacity; Form XObjects are included.
- **Exports:**
  - `pdfwords.export(src, fmt)` and `page.get_text(fmt)` for `html` (positioned), `xhtml`
    (semantic, with data-page/data-bbox), `xml` (char level), `hocr` and `alto` (v4).
  - hOCR and ALTO are in pixel units at `dpi`, so they align with `page.render(dpi=...)`.
- **Integrations (optional extras):**
  - `pdfwords.integrations.langchain.PdfwordsLoader` (`[langchain]`).
  - `pdfwords.integrations.llama_index.PdfwordsReader` (`[llamaindex]`).
  - `pdfwords mcp`: an MCP stdio server (`[mcp]`). Its read-only tools are pdf_info,
    extract_text, to_markdown, search, get_links, get_tables and render_page. File access is
    restricted to `--root` directories.
- **New CLI subcommands:**
  - `pdfwords export -f markdown|html|xhtml|xml|hocr|alto`;
  - `pdfwords chunks` (JSON lines);
  - `pdfwords objects --kind images|drawings|struct`;
  - `--sort struct` and `--roles` on extraction.
- **Benchmark suite:** `tools/bench_suite.py` and `docs/BENCHMARKS.md` compare speed, word F1
  and reading-order concordance against pdftext, PyMuPDF, pdfplumber, pdf_oxide and raw
  pypdfium2. Accuracy is measured on held-out synthetic ground truth.
  `tools/make_synthetic.py` gained `--seed/--out-dir/--name`.
- Test fixture: a hand-written tagged PDF (`tests/_featurepdf.py: tagged_pdf`) whose structure
  order differs from its geometric order.
- **Rendering on PDFium** (`docs/RENDERING.md`):
  - `Page.render(dpi | scale | size | max_side, clip, rotated, alpha, grayscale, annots, forms,
    background, antialias, output = numpy | pil | bytes | png | jpeg | webp | tiff, timeout,
    out, max_pixels)`.
    - It renders straight into a numpy array or bytearray.
    - It sizes with `round()`, avoiding pypdfium2's `ceil()` 1-px stretch.
    - AcroForm values are drawn from a form-enabled handle.
    - `timeout=` uses progressive rendering and raises `pdfwords.RenderTimeout`.
  - `Page.get_pixmap()`: a PyMuPDF-style `Pixmap` shim (`samples`, `tobytes`, `save`,
    `pil_image`, numpy).
  - `Page.render_tiles()`, `Page.thumbnail()`.
  - `Page.pixel_to_pdf()`, `pdf_to_pixel()`, `bbox_to_pixel()` and `render_geometry()`.
  - `Document.iter_images()` (streaming), and `Document.to_images(pages, dpi, workers,
    fmt="jpeg", quality, out_dir, name)`.
  - `pdfwords.compat.pdf2image`: `convert_from_path`, `convert_from_bytes`,
    `pdfinfo_from_path` and `pdfinfo_from_bytes`. `pdfwords.convert_from_path` and
    `convert_from_bytes` are also available directly.
  - CLI `pdfwords render` (`--pages --dpi/--scale/--max-side/--size --fmt --quality --out-dir
    --name --workers --gray --alpha --no-annots --no-forms --no-antialias --unrotated --clip
    --timeout`, and `--overlay words,blocks -o debug.png`).
  - Rust: `pdfwords_core::render` (`render_page`, `render_loaded_page`, `RenderOptions`,
    `Geometry`, progressive timeout). It is bit-identical to the Python path, which a parity
    test checks.
  - `tools/bench_render.py`: speed and fidelity against pypdfium2, PyMuPDF and pdf2image.
- `pages=` accepts page-range strings such as `"0,2-5"` and `"-1"`.

### Changed
- The debug overlay (`pdfwords debug`, `pdfwords.debug.overlay`) uses the new renderer, with
  exact pixel scale. It also accepts `dpi=`.
- Page geometry (`page.rect`, `rotation`) no longer extracts the page's text on the Rust
  backend.
- The `extract` CLI `--sort` option gained `struct`.

## [0.2.0] - unreleased

### Added
- **Editing** (optional extra `pdfwords[edit]`: pypdf, fontTools, Pillow; all permissive):
  - Content access: `Page.read_contents()`, `Page.get_contents()`, `Page.set_contents()`,
    `Document.xref_object()`, `xref_stream()`, `xref_stream_raw()`, `update_stream()` and
    `xref_length()`.
  - True redaction: `Page.add_redact_annot()` and
    `Page.apply_redactions(images=, graphics=, text=)`, with the PyMuPDF constant names.
    - Glyph-level removal from `Tj`/`TJ`/`'`/`"` that keeps the remaining positions exact.
    - Copy-on-write Form XObjects.
    - Image pixel blanking or removal, and vector-art removal.
    - Removal of overlapping annotations/fields, `ActualText` and XFA.
    - PDFium-based verification (`RedactionError` / `RedactionWarning`,
      `page.redaction_report`).
  - `Document.scrub()` (metadata, XMP, JavaScript, attachments, thumbnails, XFA),
    `Document.metadata` and `set_metadata()`.
  - Text insertion: `Page.insert_text()` and `Page.insert_textbox()` (wrap, align
    left/center/right/justify, overflow return value as in PyMuPDF).
    - Supports base-14 fonts, embedded and subset TrueType/OpenType fonts (`fontfile=`) with
      Unicode, including Vietnamese, plus colour and `rotate`.
  - `Document.save(garbage=, deflate=, incremental=)` and `tobytes()`. Incremental save is
    refused after redaction.
- `Page.search_for(text | regex, quads=, clip=, hit_max=, dehyphenate=)`: matches across line
  breaks and line-end hyphens, with one rectangle or quad per line.
- **Links and annotations.**
  - `Page.get_links(web=)`: URI, GoTo (target `page` and `to` point), GoToR and Launch links,
    with PyMuPDF's `LINK_*` kinds. `web=True` adds URLs written in the text (PDFium web-link
    detection).
  - `Page.annots(types=)`: type, rect, quads, contents, author, subject, id, colours and flags.
  - `Page.widgets()`: form field name, label, type, value, checked state and choices.
  - `Document.get_toc(simple=)`.
  - `get_text("dict" | "rawdict" | "json", links=True)` splits spans at link boundaries and adds
    a `url` to each span (`#page=N` for internal targets).
- **Text-layer quality.** `Page.text_quality()` reports `needs_ocr`, `score`, `reasons`,
  `invisible_ratio`, `unicode_error_ratio`, `garbled_ratio`, `image_coverage` and fonts
  (embedded, glyphless). Also `Page.needs_ocr()`, `Document.text_quality()` and
  `Document.needs_ocr()`.
- **pdftext compatibility.** `pdfwords.compat.pdftext` provides `plain_text_output`,
  `paginated_plain_text_output`, `dictionary_output` and `table_output`. It keeps pdftext's
  arguments and output schema (font weight and flags, `char_start_idx`/`char_end_idx`,
  `superscript`/`subscript`, `url`, `refs`, `quote_loosebox`, `workers`, `flatten_pdf`,
  `password`) and is 2–15× faster. There is a matching `pdfwords pdftext` CLI with pdftext's
  flags. See `docs/PDFTEXT.md`.
- **Throughput.**
  - `pages=` and `workers=` for every mode: `Document.extract()` and the streaming
    `Document.iter_pages()`, plus the module-level `pdfwords.extract()` and
    `pdfwords.iter_pages()`. They accept a path, bytes or a file object, and workers see
    edited documents.
  - `pdfwords.open(..., flatten=True)` renders form values and annotations into the page text.
- `get_text(..., extended=True)`: spans also carry `weight` and `pdf_flags`, and rawdict chars
  carry `idx` (the PDFium char index).
- `Page.table_cells(cells, image_size=)` puts words into caller-given table-cell boxes, given in
  page coordinates or image pixels.
- **Visual debugging.** `pdfwords.debug.overlay(page, show=...)` and
  `pdfwords debug file.pdf --page N --show words,lines,blocks,order,links -o out.png`. Layers:
  chars, words, lines, spans, blocks, order, links, annots, widgets, cells.
- CLI subcommands: `pdfwords redact`, `insert-text`, `contents`, `debug`, `links`, `annots`,
  `toc`, `quality`, `search` and `pdftext`. `pdfwords file.pdf` gained `--links`, `--flatten`,
  `--workers` and `--password`.
- Optional extras `edit` and `debug`.

### Changed
- File objects passed to `pdfwords.open()` are read into memory once.
- `parallel_words()` is now a thin wrapper over `Document.extract("words", workers=...)`.
- Font `pdf_flags` (used for the style flags) come from `FPDFText_GetFontInfo`, which adds
  PDFium's internal bits to the descriptor flags. The derived style flags are unchanged.

## [0.1.0] - 2026-10-09

### Added
- `pdfwords.open()` / `Document` / `Page.get_text()` with PyMuPDF-compatible modes `text`,
  `words`, `blocks`, `dict`, `rawdict`, `json` and `rawjson`. Options: `sort` (`True` or
  `"xycut"`), `clip`, `rotated`, `ligatures`, `dehyphenate`, `delimiters`.
- A native Rust backend (`pdfwords._native`, PyO3, abi3 wheels for CPython ≥ 3.9) with
  output identical to the pure-Python backend. Automatic fallback to Python when the
  extension is missing. Backend selection with `backend=`, `$PDFWORDS_BACKEND` or `--backend`.
- `Page.words_array()` (numpy) and `pdfwords.parallel_words()` (process pool).
- A pure-Rust crate `pdfwords-core` (no Python) for embedding in other languages.
- Robust PDFium library discovery (`dladdr` / `GetModuleHandleEx`, `$PDFWORDS_PDFIUM_LIB`).
- Data-derived grouping thresholds (`LayoutParams`): `tools/tune_thresholds.py`,
  `docs/THRESHOLDS.md`.
- A synthetic ground-truth test set, a parity test, and accuracy tests and benchmarks against
  PyMuPDF (optional).
- The `pdfwords` CLI.

[Unreleased]: https://github.com/dangquan1402/pdfwords/compare/v0.1.0...HEAD
[0.2.0]: https://github.com/dangquan1402/pdfwords/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dangquan1402/pdfwords/releases/tag/v0.1.0
