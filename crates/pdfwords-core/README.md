# pdfwords-core

Pure-Rust core of [pdfwords](https://github.com/dangquan1402/pdfwords). It extracts text from
PDFs with character, word, line and block bounding boxes, in the same shapes and coordinates as
PyMuPDF's `get_text()`, on top of [PDFium](https://pdfium.googlesource.com/pdfium/).
It has no Python dependency and is licensed Apache-2.0.

PDFium is loaded at runtime from a shared library. Use the one shipped by pypdfium2 or a
[pdfium-binaries](https://github.com/bblanchon/pdfium-binaries) build.

```rust
use pdfwords_core::*;

let pdfium = Pdfium::load("/path/to/libpdfium.so")?;
unsafe {
    let doc = open_document(&pdfium, "file.pdf", None)?;
    let page = extract_page(&pdfium, doc, 0, false)?;               // glyphs + fonts + timings
    let blocks = layout_page(&page, Sort::XyCut, Some([0.0, 0.0, page.width, page.height]),
                             &LayoutParams::default());              // blocks -> lines -> chars
    let xf = output::Xf { rotation: page.rotation, w: page.width, h: page.height, on: false };
    for w in output::words(&blocks, xf, false, "") {
        println!("{:?} {}", w.bbox, w.text);
    }
    (pdfium.FPDF_CloseDocument)(doc);
}
```

**Thread safety.** PDFium is not thread-safe, so use one thread, or a mutex, for all PDFium calls.

Benchmark: `cargo run --release -p pdfwords-core --example bench -- /path/libpdfium.so file.pdf`.
