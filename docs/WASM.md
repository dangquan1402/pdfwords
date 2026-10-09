# WebAssembly feasibility spike (0.4)

Goal: run pdfwords text extraction (chars/words/lines/blocks + boxes) in a browser or in a
WASI runtime, with no server.

## What was checked

`cargo check -p pdfwords-core --target wasm32-unknown-unknown` (Rust 1.9x, October 2026):

* **One compile error:** `ffi.rs` imports `libloading::Library`, which does not exist on
  wasm32. `pdfwords-core` loads PDFium at run time with `dlopen`/`LoadLibrary`, and there is
  no dynamic loader in a WASM module.
* Everything else type-checks: `layout.rs` (line/block builder, bidi, vertical CJK),
  `order.rs` (simple / XY-cut reading order) and `output.rs` (words/blocks/text) are pure
  Rust. The dependencies (`regex`, `unicode-bidi`, `unicode-general-category`) build for
  wasm32.
* **Runtime hazard:** `glyphs.rs` and `render.rs` call `std::time::Instant::now()` (8 call
  sites) for timings and the render time budget. On `wasm32-unknown-unknown` this panics at
  run time. It must go behind a `cfg` or use `web-time`.

## PDFium in WebAssembly

| Source | Notes |
|---|---|
| [bblanchon/pdfium-binaries](https://github.com/bblanchon/pdfium-binaries/releases) `pdfium-wasm.tgz` (~2.4 MB, Emscripten) | Same project as our native builds. pdfium-render's docs say these builds use a non-growable heap and run out of memory on longer documents. |
| [paulocoutinhox/pdfium-lib](https://github.com/paulocoutinhox/pdfium-lib/releases) WASM builds | Growable heap. pdfium-render recommends these for the browser. |
| Building PDFium ourselves with Emscripten | Full control (static link with our code), but a heavy GN/ninja + emsdk toolchain in CI. |

All three are PDFium (BSD-3 / Apache-2.0), so licensing stays clean.

## Two viable architectures

1. **Two WASM modules (the pdfium-render model).** PDFium is an Emscripten module, and
   pdfwords-core is a `wasm-bindgen` module.
   * A small JS shim exposes the ~40 `FPDF*` functions we use (see `ffi.rs`), and the Rust
     side calls them through `extern "C"` imports instead of `libloading`.
   * Page bytes and glyph arrays cross the module boundary by copying into PDFium's heap. Our
     glyph extraction makes ~10 PDFium calls per character, so this boundary would dominate
     the run time. Fix: add one batched "dump all chars of a page" C helper compiled into the
     PDFium module.
2. **One module (`wasm32-unknown-emscripten`).** Link PDFium statically with pdfwords-core and
   export a C ABI (`pdfwords_extract(page_bytes) -> JSON/Arrow`). This is fastest (no boundary)
   and is the same static-linking work as the iOS/Android roadmap item. It needs an Emscripten
   PDFium static library (build it ourselves, or check whether pdfium-lib ships `.a` files).

## Required refactor (small, both architectures)

1. Move `Glyph`, `FontInfo`, `PageGlyphs` out of `glyphs.rs` into a PDFium-free `model.rs`,
   so that `layout`/`order`/`output` compile without FFI.
2. Make `libloading` an optional default feature (`dlopen`). Behind
   `cfg(target_arch = "wasm32")`, `ffi::Pdfium` gets its function table from static
   `extern "C"` symbols (architecture 2) or JS imports (architecture 1).
3. Replace `Instant` with a tiny clock trait (`web-time` on wasm).
4. Add a `pdfwords-wasm` crate (wasm-bindgen) with `extract(bytes, page, mode) -> JsValue`.

The pure-Python package already runs in **Pyodide** only if pypdfium2 does; it does not
(pypdfium2 ships native wheels only). So a browser build has to come from the Rust core.

## Verdict

Feasible. The layout engine is already portable, and the work is about PDFium packaging:
roughly 1–2 days for the refactor plus architecture 1 using a prebuilt PDFium WASM. The
open decision is which PDFium WASM build to depend on and whether to own an Emscripten
PDFium build in CI. It is not shipped in 0.4.
