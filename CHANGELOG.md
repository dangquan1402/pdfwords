# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
