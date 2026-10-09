# Design

## Pipeline

```
PDF ──PDFium──▶ glyphs ──grouping──▶ blocks / lines / chars ──order──▶ output (words, dict, …)
     stage 1             stage 2                               stage 3
```

**Stage 1: glyphs** (`python/pdfwords/chars.py`, `crates/pdfwords-core/src/glyphs.rs`).

* It uses only PDFium's public C API: `FPDFText_*`, `FPDFTextObj_*` and `FPDFFont_*`.
* For each character it gets the code point, origin, loose box, text matrix, font and colour.
  Writing direction and size come from the text matrix. Font metrics, flags and names are
  cached per text object.
* PDFium-specific fixes:
  * generated spaces are dropped, but kept as a word-break hint;
  * ligature code points that PDFium expands are marked as zero-width continuations;
  * the right edge of italic glyph boxes is clamped to the next glyph's origin.

**Stage 2: grouping** (`layout.py`, `layout.rs`). A single pass follows the pen through the
content-stream order. For each glyph it measures, in units of the font size:

* **spacing**: the distance along the writing direction from where the previous glyph ended;
* **baseline offset**: the perpendicular distance from the previous glyph's baseline.

It then decides:

| Measurement | Result |
|---|---|
| tiny spacing | same word |
| forward spacing below `column_gap` | same line, with a synthetic space if spacing ≥ `word_gap` |
| larger jumps | new line |
| baseline shift below `baseline_tol` | same line |
| baseline shift up to `block_gap` | next line of the same block, unless the line is indented by more than `indent_pt` |
| larger shift or a direction change | new block |

* Overprinted duplicates closer than `dup_dist` are dropped (fake bold).
* Character boxes span ascender to descender, normalised to at least one font size, which
  matches PyMuPDF's output convention.
* Superscripts are flagged when a character's baseline is raised by more than
  `superscript_rise`.
* RTL lines emitted in visual order are reordered.

All thresholds are fields of `LayoutParams`. Their values were derived from data; see
[THRESHOLDS.md](THRESHOLDS.md).

**Stage 3: order** (`order.py`, `order.rs`).

* `sort=True` sorts by (y1, x0), as PyMuPDF does.
* `sort="xycut"` is a recursive XY-cut:
  * It tries vertical cuts first (gutters ≥ `xycut_gap_x`), then horizontal cuts
    (gaps ≥ `xycut_gap_y`).
  * Consecutive horizontal strips that share a column gutter are merged before cutting, so
    paragraph breaks that line up across two columns do not interleave them.
  * Rotated text is appended at the end.

**Output** (`__init__.py`, `output.rs`, `crates/pdfwords-py/src/lib.rs`): words, blocks and
text, plus dict and rawdict with PyMuPDF's keys, coordinate system and span flag bits.
`rotated=True` maps the coordinates onto the displayed page.

## Two backends, one behaviour

* The Rust core is a line-by-line port of the Python code, with the same arithmetic order,
  tie-breaking and rounding keys. `tests/test_parity.py` requires bit-identical output.
* The PyO3 extension makes **one FFI call per page**: page load, text load, the char loop and
  closing the page all happen natively, and the glyphs are kept in a flat `Vec`. `get_text`
  then runs grouping and ordering in Rust and builds the Python tuples and dicts directly.
* Documents are opened by pypdfium2. Rust receives the raw `FPDF_DOCUMENT` handle and binds to
  the **same** PDFium library: the file that contains pypdfium2's `FPDF_InitLibrary` symbol,
  found via `dladdr` / `GetModuleHandleExW` (see `_libpath.py`).
* The wheel therefore does not bundle a second copy of PDFium.

## Threads

PDFium keeps global state and has no locking. Every call is serialised:

* the extension holds the GIL for each call;
* parallelism uses processes (`parallel_words`);
* in Rust or Swift, use one thread that owns PDFium.

## Mobile (roadmap)

`pdfwords-core` has no Python dependency.

* **iOS:**
  * build PDFium for iOS (pdfium-binaries) as an xcframework;
  * add a `static` feature that links PDFium instead of `dlopen`;
  * expose `words(page)` through uniffi or a small C ABI (flat arrays: `f32×4` boxes,
    `u32×3` ids, a UTF-8 blob).
* **Android:**
  * `cargo ndk` builds plus the pdfium-binaries Android `.so` files;
  * uniffi/JNI from Kotlin.
