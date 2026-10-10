# Shrinking PDFs

`pdfwords.optimize()`, `Document.optimize()` and `pdfwords shrink` make PDFs smaller **without
rasterising them**. Text stays text: it remains selectable, searchable and sharp at any zoom.
Vector graphics, fonts, links, annotations, form fields, outlines and the tag tree are kept as
they are. In practice almost all of the size of a large PDF is images, and the optimiser
re-encodes those.

```bash
pip install "pdfwords[shrink]"        # or: uv add "pdfwords[shrink]"
```

```python
import pdfwords

rep = pdfwords.optimize("in.pdf", "out.pdf", preset="balanced")
print(f"{rep.bytes_before:,} -> {rep.bytes_after:,} bytes ({rep.reduction:.1%} smaller)")
for d in rep.images:                       # one decision per image
    print(d["object"], d["action"], d.get("effective_dpi"), d["bytes_before"], d["bytes_after"], d["reason"])

# the same on an open document (edits included); without a path, the bytes are in rep.data
doc = pdfwords.open("scan.pdf")
rep = doc.optimize(preset="max", ocr=True)          # + invisible OCR text layer on image-only pages
open("scan_small.pdf", "wb").write(rep.data)

# fine-tuning
pdfwords.optimize("in.pdf", "out.pdf", "balanced", target_dpi=200, jpeg_quality=80,
                  grayscale=False, mrc=False, strip_metadata=True,
                  progress=lambda stage, done, total: print(stage, done, total))
```

```bash
pdfwords shrink in.pdf -o out.pdf                                  # balanced
pdfwords shrink in.pdf -o out.pdf --preset max --report report.json
pdfwords shrink scan.pdf -o scan_small.pdf --ocr                   # searchable scan (needs an OCR engine)
pdfwords shrink in.pdf -o out.pdf --dpi 200 --quality 80 --grayscale never --mrc never --strip-metadata
```

## Presets

| | `lossless` | `balanced` (default) | `max` |
|---|---|---|---|
| Pixels changed | never | images only | images, plus very heavy vector pages |
| Colour / gray images | lossless JPEG re-optimisation (MozJPEG) | downsampled to **150 dpi** when drawn above 195 dpi, JPEG q72 | **100 dpi**, JPEG q55 |
| Per-image SSIM floor | n/a | 0.90 (q is raised once, then the image is kept) | 0.85 |
| Effectively gray colour images | kept | stored as gray | stored as gray |
| Line art / screenshots (< 256 colours) | kept | Flate, resampled only | Flate, resampled only |
| 1-bit images | kept | CCITT G4 or JBIG2 (lossless) | same |
| Scanned pages | kept | **MRC**: 300 dpi 1-bit mask + 100 dpi background | mask + 72 dpi background |
| Big ICC profiles | kept | CMYK profiles > 256 KB become DeviceCMYK | profiles > 100 KB become Device* |
| Document XMP | kept | kept | dropped |
| Page raster fallback | no | no | yes, when vector content > 400 KB and the raster is < 50 % of it |
| Page render check (PDFium, 150 dpi) | n/a | images restored if a page drops below SSIM 0.85 | 0.80 |

In every preset:

* byte-identical streams (fonts, images, ICC profiles, forms) are merged;
* page thumbnails and private application data (`/PieceInfo`, per-image XMP) are dropped;
* unreferenced resources are removed;
* everything is re-flated at level 9, with object streams and a cross-reference stream.

### Options

| Option | Default | Meaning |
|---|---|---|
| `target_dpi` | preset | Resolution for colour and gray images. Images are only downsampled when drawn above 1.3× this. |
| `jpeg_quality` | preset | 1–95. |
| `grayscale` | `"auto"` | `"auto"`: images that are effectively gray (99.5th-percentile chroma < 12). `True`: every re-encoded image. `False`: never. |
| `mrc` | `"auto"` | `"auto"`: scanned pages in lossy presets (an image covering > 85 % of a page with no visible text). `True`: any full-page image. `False`: off. |
| `ocr` | `False` | `True` or an engine name or spec (see [OCR](OCR.md)). Adds an invisible text layer to pages without text. |
| `strip_metadata` | preset | `True` drops the XMP packet and the Info dictionary. |
| `remove_thumbnails` | `True` | Drop `/Thumb` images. |
| `dedupe` | `True` | Merge byte-identical streams. |
| `min_ssim` | preset | Quality floor for each re-encoded image. |
| `max_raster_fallback` | preset | Maximum number of pages to rasterise (`0` = never, `-1` = no limit). |
| `verify` | `True` | Render the changed pages before and after. |
| `linearize` | `False` | Fast web view. |
| `progress` | `None` | `callable(stage, done, total)`. |

## Guarantees

* **Never grow.** If the result is not smaller, the original bytes are returned unchanged
  (`kept_original=True`, with a `reason`).
* **Text is never rasterised** in `lossless` and `balanced`.
  * MRC turns a scanned image into a mask and a background drawn in the same place, so an
    existing OCR text layer stays aligned.
  * The `max` raster fallback keeps the page's text as an invisible layer, so search and copy
    still work. It is skipped for tagged PDFs and for text the WinAnsi layer can't represent.
* **Kept as they are:** links and other annotations, AcroForm fields and their values,
  outlines, named destinations, page labels, the structure tree (tags), optional content,
  JavaScript, embedded files, and the Info dictionary (unless `strip_metadata=True`).
* **Left alone:**
  * signed PDFs, because rewriting them would invalidate the signatures;
  * encrypted PDFs, because re-saving would drop or change the encryption.
* **Valid output.** The tests check every output:
  * with qpdf's structural check (`pikepdf`, and `qpdf --check` when installed);
  * by opening and rendering it in PDFium;
  * for word recall, which is 100 % on text PDFs, compared with `get_text("words")`.

## How it works

1. Images are placed with `Page.get_images()` (PDFium). It gives the drawn quad and the pixel
   size, and so the **effective DPI** of every image on every page. Each entry is matched to
   its PDF object by width, height and encoded size. A content-stream walk is the fallback.
   An image is resampled for its sharpest use.
2. Pages are classified with `Page.text_quality()`. Pages with no text, or with only invisible
   (OCR) text, behind a full-page image are treated as scans.
3. Each image is decoded, resampled and re-encoded. The result is kept only if it is at least
   10 % smaller and passes the SSIM floor.
4. **MRC for scans:**
   * Sauvola local thresholding gives the text mask.
   * The background is filled under the text and downsampled. The foreground is the ink
     colour, or a low-resolution colour layer for colourful scans.
   * The image object becomes a small Form XObject that draws the background and then the
     masked foreground. Every page and form that used the image picks up the change.
   * The layers are composited at 150 dpi and must reach SSIM ≥ 0.80 (0.75 for `max`).
5. Changed pages are rendered with PDFium at 150 dpi, before and after. If a page falls below
   the preset's page-SSIM floor, the images on it are restored and the file is rebuilt.

### Codecs

| Codec | Where it comes from | Used for |
|---|---|---|
| JPEG (baseline/progressive, optimised Huffman) | Pillow (always) | photos and gray images |
| MozJPEG lossless re-optimisation | `mozjpeg-lossless-optimization` (BSD-3, wheels, in the extra) | `lossless` preset, and a final pass on new JPEGs |
| MozJPEG trellis quantisation | a `cjpeg` executable from MozJPEG (`PDFWORDS_CJPEG` or PATH; detected by its version string) | optional, about 5–10 % smaller JPEGs |
| CCITT G4 | Pillow's bundled libtiff (always) | 1-bit images and MRC masks |
| JBIG2 generic region (lossless) | the `jbig2` executable from jbig2enc, Apache-2.0 (`apt install jbig2`, `brew install jbig2enc`; `PDFWORDS_JBIG2` or PATH) | used instead of G4 when smaller, usually 30–50 % |

`pdfwords.shrink.available_codecs()` shows what was found. JBIG2 symbol coding (lossy
pattern matching) is deliberately not used: it can swap similar-looking characters.

Licences: pdfwords is Apache-2.0. The `shrink` extra uses pikepdf (MPL-2.0, used as an
unmodified dependency, which the MPL allows in Apache/proprietary software), Pillow (MIT-CMU),
numpy (BSD-3) and mozjpeg-lossless-optimization (BSD-3). No Ghostscript or MuPDF code or
binary is used. Ghostscript only appears below, as a reference point.

## Benchmarks

The numbers come from an internal corpus of 87 pages. It is not in the repository because some
of the inputs are third-party documents. It contains two arXiv papers, the IRS W-9 form, an
image-heavy report, an 8-page photo album, 300 dpi gray, colour and 27-page scans, and a CAD-like
vector plan. Sizes are in MB, with the saving in parentheses.

Ghostscript (`pdfwrite -dPDFSETTINGS=...`) is shown **only as a reference point**. It is
AGPL-licensed and is not used by pdfwords.

| File (pages) | Original MB | lossless | balanced | max | gs /ebook | gs /screen |
|---|---:|---:|---:|---:|---:|---:|
| text_attention (15) | 2.22 | 1.54 (30%) | 1.43 (35%) | 1.39 (37%) | 1.23 (44%) | 1.19 (46%) |
| text_resnet (12) | 0.82 | 0.70 (15%) | 0.31 (62%) | 0.31 (62%) | 0.20 (75%) | 0.20 (75%) |
| form_w9 (6) | 0.14 | 0.12 (17%) | 0.12 (17%) | 0.11 (19%) | 0.06 (60%) | 0.06 (60%) |
| mixed_report (6) | 23.77 | 22.86 (4%) | 0.29 (99%) | 0.11 (100%) | 0.31 (99%) | 0.09 (100%) |
| photos (8) | 34.49 | 32.26 (6%) | 1.39 (96%) | 0.50 (99%) | 1.23 (96%) | 0.38 (99%) |
| scan_gray (6) | 8.17 | 7.11 (13%) | 0.40 (95%) | 0.39 (95%) | 1.60 (80%) | 0.59 (93%) |
| scan_color (6) | 8.91 | 7.68 (14%) | 0.43 (95%) | 0.35 (96%) | 1.22 (86%) | 0.44 (95%) |
| large_scan (27) | 58.23 | 50.54 (13%) | 2.16 (96%) | 1.78 (97%) | 6.19 (89%) | 2.30 (96%) |
| vector_plan (1) | 2.34 | 2.33 (0%) | 2.33 (0%) | 0.67 (71%) | 2.33 (0%) | 2.33 (0%) |

Totals: 139.1 MB in total becomes 125.1 MB with `lossless` (−10 %), 8.9 MB with `balanced`
(−93.6 %) and 5.6 MB with `max` (−96.0 %). For comparison, gs /ebook gives 14.4 MB (−89.7 %)
and gs /screen gives 7.6 MB (−94.5 %).

Quality:

* **Word recall** (`get_text("words")`, original vs output) is 1.000 for every text-bearing
  file and preset.
* **Mean page SSIM** (PDFium renders at 150 dpi):
  * `balanced`: ≥ 0.94 on every file; scans 0.94–0.95, photos 0.97, text files 1.000.
  * `max`: ≥ 0.93, except photos at 0.93 mean and 0.87 min.
* **Time:**
  * text PDFs: 0.02–0.05 s per page;
  * photos and image-heavy pages: about 0.5 s per page;
  * MRC scans: 1.3–2.5 s per page (single core).

Ghostscript is smaller on born-digital text PDFs (W-9: −60 % vs −17 %) because it re-writes
every font, converting Type 1 to CFF and re-subsetting, which the optimiser does not do yet (see
Limits). Ghostscript is larger on scans and keeps the vector plan as it is.

Run `python benchmarks/bench_shrink.py DIR --gs --qpdf` to reproduce the table on your own
files.

## Limits

* **Image types:** CMYK, Indexed, Lab, DeviceN, 16-bit and `/Decode`-array images are left
  as they are, as are images that are already JPEG 2000, JBIG2 or CCITT. Inline images are not
  touched.
* **Fonts are only deduplicated** (byte-identical font programs). Two things are planned:
  * Type 1 to CFF conversion, and re-subsetting fully embedded fonts. fontTools can do this,
    but it can't yet be made safe across arbitrary encodings, `/Differences` arrays and
    `/Widths` tables.
  * Merging duplicate subsets of the same font.
* **Vector content is not simplified.** There is no path flattening or merging of many tiny
  objects. Heavy vector pages can only be rasterised, and only by the `max` fallback.
* **MRC is pure numpy.** It takes about 1.5–2.5 s per 300 dpi page on one core. Other pages
  take tens of milliseconds.
* The OCR and raster-fallback text layers use a WinAnsi Helvetica, so non-Latin OCR text is
  replaced with `?`. The raster fallback skips such pages.
* **Encrypted and signed files are returned unchanged.** Decrypt them yourself first if you
  want them optimised (and re-sign afterwards).
* Pages are processed in one process, and the whole document is held in memory.
