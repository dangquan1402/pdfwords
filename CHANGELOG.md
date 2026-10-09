# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- `Page.search_for(text | regex, quads=)`.
- CLI subcommands: `pdfwords redact`, `pdfwords insert-text` and `pdfwords contents`.

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
[0.1.0]: https://github.com/dangquan1402/pdfwords/releases/tag/v0.1.0
