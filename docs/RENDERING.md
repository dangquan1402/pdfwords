# Rendering

pdfwords renders pages with PDFium, which is the same engine that extracts the text. Pixels
and text boxes therefore come from one engine and line up exactly. There is no new runtime
dependency: numpy is needed for array output, and Pillow for PIL output or encoded images.

```python
import pdfwords

doc = pdfwords.open("file.pdf")
page = doc[0]

img = page.render()                              # numpy uint8 (H, W, 3) RGB at 150 dpi
img = page.render(dpi=300, grayscale=True)       # (H, W); about 1.6x faster at 300 dpi
img = page.render(scale=2)                       # or size=(w, h) (one may be None), max_side=1600
img = page.render(alpha=True)                    # (H, W, 4) RGBA with a transparent background
img = page.render(clip=(72, 72, 300, 200))       # region, in get_text() coordinates
img = page.render(rotated=False)                 # ignore /Rotate: matches get_text() default boxes
pil = page.render(output="pil")                  # PIL.Image; "bytes" = raw samples
png = page.render(output="png")                  # encoded: png | jpeg | webp | tiff | bmp | ppm
img = page.render(annots=False, forms=False, antialias=False, background=(0, 0, 0))
img = page.render(timeout=2.0)                   # raises pdfwords.RenderTimeout after 2 s
page.render(dpi=150, out=buf)                    # reuse a preallocated array (streaming jobs)

for (x, y), tile in page.render_tiles(dpi=600, tile=2048):   # huge pages / very high DPI
    ...
thumb = page.thumbnail(256)                      # longest side 256 px, no annots/forms

# map between pixels and text coordinates (same keyword arguments as render())
x, y = page.pixel_to_pdf(px, py, dpi=150)
px, py = page.pdf_to_pixel(x, y, dpi=150)
box = page.bbox_to_pixel(word[:4], dpi=150)      # word from get_text("words", rotated=True)
page.render_geometry(150)                        # {full_w, full_h, sx, sy, px0, py0, w, h, rotated}

pix = page.get_pixmap(dpi=144)                   # PyMuPDF-style shim (72 dpi default)
pix.width, pix.height, pix.n, pix.samples, pix.tobytes("png"), pix.save("p.png"), pix.pil_image()

# documents
for i, img in doc.iter_images(pages="0,2-5", dpi=150):     # streaming, constant memory
    ...
paths = doc.to_images(dpi=150, workers=8, fmt="jpeg", quality=90, out_dir="out/",
                      name="{stem}-{page:04d}.{ext}")
images = doc.to_images(pages=[0, 1], dpi=100)               # out_dir=None: list of PIL images

# pdf2image drop-in (no poppler)
from pdfwords.compat.pdf2image import convert_from_path, convert_from_bytes, pdfinfo_from_path
images = convert_from_path("file.pdf", dpi=200)             # also pdfwords.convert_from_path
```

CLI:

```bash
pdfwords render file.pdf --pages 0,2-4 --dpi 150 --fmt jpeg --quality 90 --out-dir out/ --workers 8
pdfwords render file.pdf --pages 0 -o page.png --max-side 2000 --gray --no-annots --clip 0,0,300,200
pdfwords render file.pdf --pages 0 --overlay words,blocks,order -o debug.png     # extraction overlay
```

## Coordinates and sizing

* **Pixel size.** It is `round(points × dpi / 72)`. pypdfium2's `PdfPage.render()` uses
  `ceil()`. Floating-point noise then turns US Letter at 150 dpi
  (792 × 150/72 = 1650.0000000002) into 1651 px, and the page is stretched by one pixel.
  pdfwords' sizes always equal PyMuPDF's in the benchmark below. pypdfium2 differs on 0 of 34
  pages at 72 dpi, but on 28 of 34 pages at 150 and 300 dpi.
* **Effective scale.** It is `sx = width_px / width_pt` (likewise `sy`). These are the values
  that `pixel_to_pdf`, `pdf_to_pixel` and `render_geometry` use, so the mappings are exact.
* **Rotation.** `rotated=True` (the default) renders the page as displayed, which is the space
  of `get_text(..., rotated=True)`. `rotated=False` renders the unrotated page, which is the
  space of the default `get_text()` boxes.
* **Clip.** `clip` is given in the same coordinates and selects a window of the full-page
  raster at integer pixel offsets.
  * Grayscale and `antialias=False` renders of a window are bit-identical to the same region
    of a full render. So are tiles from `render_tiles()`.
  * Colour anti-aliased text can differ in a handful of pixels for glyphs that straddle the
    window's left edge, because PDFium's colour glyph rasterizer depends on the horizontal
    origin. This affects under 2% of pixels in a window (0.1–1% in tests).
* **Forms.** AcroForm field values are drawn (`FPDF_FFLDraw`) from a second, form-enabled
  document handle when the document has a form. A document opened with `flatten=True` already
  has the values in its content.
* **Timeout.** `timeout=` uses PDFium's progressive renderer and aborts between page objects
  (`pdfwords.RenderTimeout`). A single huge image cannot be interrupted midway.
* **Guard.** `max_pixels=` (default 2^28) refuses absurd output sizes, such as a 200-inch page
  at 600 dpi. Use `render_tiles()` or `max_pixels=None` for those.
* **Threads.** PDFium is not thread-safe, so parallelism uses processes (`workers=`), with one
  document per worker.

## Speed

ms per page, whole document including open, RGB numpy output, single process, best of 5.
`tools/bench_render.py` measured these on Linux x86-64 with 8 vCPU, CPython 3.13, pypdfium2
5.10.1, PyMuPDF 1.28.2, and pdf2image 1.17 with poppler 25.03.

| PDF (pages) | dpi | **pdfwords** | pdfwords gray | pypdfium2 `render()` | PyMuPDF | pdf2image |
|---|---|---|---|---|---|---|
| arxiv_attention (15) | 72 | 12.2 | 11.4 | 12.0 | 13.4 | 37.1 |
| arxiv_resnet (12) | 72 | 6.2 | 5.4 | 6.3 | 6.8 | 29.1 |
| irs_w9 form (6) | 72 | 5.7 | 4.9 | 4.7 | 5.4 | 18.3 |
| arxiv_attention (15) | 150 | **18.9** | 16.0 | 19.2 | 22.4 | 74.9 |
| arxiv_resnet (12) | 150 | 10.7 | 7.9 | 11.5 | 9.7 | 59.3 |
| table_camelot (1) | 150 | 10.0 | 7.0 | 11.2 | 6.8 | 66.4 |
| irs_w9 form (6) | 150 | 10.2 | 7.4 | 9.6 | 7.2 | 36.3 |
| arxiv_attention (15) | 300 | **38.4** | 30.3 | 42.4 | 46.3 | 190.3 |
| arxiv_resnet (12) | 300 | 24.0 | 14.8 | 27.5 | 17.7 | 101.6 |
| table_camelot (1) | 300 | 21.0 | 12.2 | 24.1 | 12.3 | 112.3 |
| irs_w9 form (6) | 300 | 23.8 | 14.2 | 25.1 | 13.4 | 93.2 |

* pdfwords is 3–6× faster than pdf2image/poppler.
* It is level with PyMuPDF at ≤ 150 dpi on text-heavy pages. PyMuPDF is about 1.5–1.8× faster
  at 300 dpi on vector-light pages. `grayscale=True` closes most of that gap.
* It is about level with pypdfium2's `render()` at ≤ 150 dpi and 1–14% faster at 300 dpi,
  because it renders straight into the output array with no intermediate bitmap or copy.

**Fidelity.** These are global SSIM scores against PyMuPDF on 3×3 box-blurred grayscale. The
differences are glyph anti-aliasing and hinting. No content differs.

| | 72 dpi | 150 dpi | 300 dpi |
|---|---|---|---|
| arxiv_attention | 0.986 | 0.991 | 0.992 |
| arxiv_resnet | 0.965 | 0.990 | 0.993 |
| table_camelot | 0.971 | 0.972 | 0.981 |
| irs_w9 | 0.982 | 0.989 | 0.986 |

pypdfium2 cannot be scored at 150–300 dpi on 28 of the 34 pages, because its 1-px-taller
images do not match PyMuPDF's size.

**Throughput.** These figures are for 90 pages (Attention × 6) at 150 dpi, `doc.to_images()` /
`iter_images()`:

| workers | JPEG q90 files | PNG files | in-memory arrays |
|---|---|---|---|
| 1 | 43 p/s | 18 p/s | 48 p/s |
| 4 | 103 p/s | 42 p/s | 55 p/s |
| 8 | 146 p/s | 55 p/s | 64 p/s |

* Encoding dominates once files are written. PNG costs about 4× the render, so JPEG is the
  default for files. PNG uses `compress_level=1`.
* Arrays returned from worker processes are pickled back to the parent, which limits scaling.
  For many pages, write files with workers, or render serially in each of your own worker
  processes.

## Rust

`pdfwords-core` has the same renderer for Rust users (iOS, Android, servers):
`pdfwords_core::render_page(&pdfium, doc, index, &RenderOptions { .. })` returns
`(Geometry, Vec<u8>)`, and `render::render_loaded_page` renders into a caller buffer. It uses
the same geometry rules, and its output is bit-identical to the Python path (parity test).

* AcroForm drawing is Python-only for now, because it needs a form-fill environment.
* The Python package keeps its ctypes path. PDFium's raster time dominates and the Python
  overhead is about 30 µs per page, so a native call would not be measurably faster.

## pdf2image compatibility

`pdfwords.compat.pdf2image` has the signatures of `convert_from_path`, `convert_from_bytes`,
`pdfinfo_from_path` and `pdfinfo_from_bytes` (pdf2image 1.17). It also has the same return
values and the same exception names.

* Pages are always rendered within the CropBox. pdftoppm's default is the MediaBox, so
  `use_cropbox` is accepted and ignored.
* Sizes use `round()`. pdftoppm sometimes gives 1 px more with `size=(w, None)`.
* `thread_count` uses processes. `poppler_path`, `use_pdftocairo` and `strict` are ignored.
* File names follow pdf2image (`{output_file}0001-{page}.{ext}`, zero-padded, and
  `{output_file}.{ext}` with `single_file`).
* `pdfinfo` returns a subset of poppler's keys: Title, Subject, Keywords, Author, Creator,
  Producer, CreationDate, ModDate, Pages, Encrypted, Page size, Page rot and PDF version. The
  dates are raw PDF date strings.
* Memory: pdf2image keeps every page in RAM (899 MB peak for 12 pages at 300 dpi). The
  pdfwords version renders one page at a time, but still returns a list. Use `iter_images()`
  or `output_folder` + `paths_only=True` for constant memory.
