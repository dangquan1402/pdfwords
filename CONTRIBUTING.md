# Contributing to pdfwords

Thanks for your interest! Issues and pull requests are welcome.

## Ground rules

* **Licence.** All contributions are Apache-2.0. By submitting a pull request you agree to
  license your contribution under the Apache License 2.0.
* **Clean-room.** Do **not** copy, translate or paraphrase code from MuPDF/PyMuPDF, which are
  AGPL, or from any other copyleft project. Treating PyMuPDF as a black box (comparing its
  output) is fine. Studying its *documentation* to keep the output format compatible is fine.
* **Two backends, one behaviour.** Any change to grouping or output must be made in both
  `python/pdfwords/` and `crates/pdfwords-core/`. `tests/test_parity.py` requires the
  outputs to be exactly equal.
* **Thresholds are data-driven.** Do not hand-tweak `LayoutParams`. Add test data and re-run
  `tools/tune_thresholds.py` (see `docs/THRESHOLDS.md`).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest numpy
pip install pymupdf pdftext pillow           # optional: accuracy tests, benchmarks, overlays
maturin develop --release                    # build the Rust extension into python/pdfwords/
python tools/fetch_test_pdfs.py              # optional: public test PDFs (tests skip without them)
```

## Checks (CI runs all of these)

```bash
pytest                                       # rust + python backends, parity, accuracy
PYTHONPATH=python PDFWORDS_EXPECT_BACKENDS=python pytest   # pure-Python fallback
                                             # (only when the extension is not built in-tree)
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Layout

```
python/pdfwords/        Python package (pure-Python backend + API; _native is the Rust extension)
crates/pdfwords-core/   pure-Rust core (PDFium FFI, grouping, reading order, output helpers)
crates/pdfwords-py/     PyO3 binding -> pdfwords._native
tests/                  pytest suite; fixtures/ are committed, data/ is downloaded
tools/                  fetch_test_pdfs, make_synthetic, tune_thresholds, make_overlay
benchmarks/             speed and PyMuPDF-compatibility scripts + results
docs/                   design notes, threshold derivation, images
```

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and `Cargo.toml` (`[workspace.package]`), and update
   `CHANGELOG.md`.
2. Tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml` builds the wheels and sdist,
   and publishes to PyPI with trusted publishing.
