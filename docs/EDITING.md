# Editing: content streams, redaction, text insertion

Install: `pip install "pdfwords[edit]"` or `uv add "pdfwords[edit]"`. This pulls in pypdf (BSD-3-Clause), fontTools (MIT)
and Pillow (MIT-CMU). The design follows PyMuPDF's *public* API documentation and observable
behaviour. No MuPDF or PyMuPDF source was used.

## Engine choice

| need | PDFium (pypdfium2) | pypdf |
|---|---|---|
| raw/decoded content-stream bytes, stream xrefs | no public API | yes |
| remove *part* of a text object (single glyphs) and keep the positions of the rest | `FPDFText_SetText` re-lays the text, which loses `TJ` kerning; `FPDFPage_GenerateContent` regenerates the whole page | full control at operator level |
| copy-on-write of a shared Form XObject | no | yes |
| image pixel editing, metadata/XFA/JavaScript removal | partial | yes |
| text extraction, rendering, standard-14 metrics | **yes** | – |

pdfwords therefore uses **pypdf for the object graph** and its own **byte-faithful
content-stream lexer** (`pdfwords/edit/_lexer.py`).

* String operands keep their exact bytes. pypdf's own parser decodes some strings as text,
  which would corrupt 2-byte CID strings.
* Untouched operators are copied verbatim.

**PDFium remains the extraction engine.** After every edit, the document is re-serialised and
re-opened in PDFium, so `get_text()`, `search_for()` and the Rust backend always see the edited
content. PDFium is also the source of standard-14 font widths, both for non-embedded fonts
without `/Widths` and for the `/Widths` written with inserted base-14 text. Redaction
therefore uses the same metrics that PDFium's extractor uses.

## Redaction algorithm

`page.apply_redactions()` collects the page's `/Redact` annotations. `add_redact_annot()`
creates standard ones, and those made by other tools work too. It then:

1. **Concatenates all `/Contents` streams.** Operators may legally span stream boundaries,
   e.g. `BT` in one stream and `ET` in the next.
2. **Replays the content with an interpreter.** It tracks the graphics state (CTM, `q`/`Q`,
   line width, ExtGState font), the text state (`Tf Tc Tw Tz TL Ts`, `Tm`/`Tlm`, `Td TD T* ' "`)
   and the current path.
3. **Handles every glyph of `Tj` / `TJ` / `'` / `"`.**
   * The byte string is split into codes using the font's encoding: 1 byte for simple fonts;
     for Type0 fonts, the codespace ranges of the CMap (Identity-H/V, embedded CMaps, and the
     byte-length rules of the predefined CJK CMaps).
   * The advance width comes from `/Widths`, `/W`, `/DW`, `/W2`, `/MissingWidth`, the Type3
     `/FontMatrix`, or PDFium's standard-14 metrics.
   * The glyph box is computed in page space from the text rendering matrix:
     `[Tfs·Th 0 0 Tfs 0 Ts] × Tm × CTM`, with x from the advance and y from the font
     descriptor's ascent/descent, normalised exactly as the extractor's char boxes are.
4. **Removes a glyph** when a redaction rectangle covers
   * at least `min_overlap` (default 10 %) of its width, or half of a narrow rectangle's width;
   * **and** half of its height, or half of the rectangle's height.

   The height rule keeps tightly spaced neighbouring lines safe while still honouring thin
   strike-through rectangles. A removed glyph is replaced inside a `TJ` array by the number
   `-(w0·Tfs + Tc + Tw) / Tfs × 1000`. That is exactly its displacement, so every following
   glyph lands on its original position. `'` and `"` are rewritten into explicit `Tw`/`Tc`/`T*`
   + `TJ`.
5. **Marked content.** `/ActualText`, `/Alt` and `/E` of the enclosing `BDC` sequences are
   deleted when a glyph inside them was removed.
6. **Form XObjects** whose bounding box meets a rectangle are interpreted recursively. They
   start from the invoking graphics and text state, as the spec requires. A form that changed
   is **copied** to a new object and re-referenced under a new resource name. The original,
   possibly used elsewhere, is left alone.
7. **Images** (`images=`):
   * `PDF_REDACT_IMAGE_PIXELS` (default) decodes the image and overwrites the covered pixels
     with the fill colour. It also opens up the `/SMask` there, and handles stencil masks. The
     result is stored as a new Flate image (RGB or Gray, 8 bit). If decoding fails, the image
     is removed instead.
   * `PDF_REDACT_IMAGE_REMOVE` removes the whole image (the `Do`).
   * Inline images that are hit are always removed.
8. **Vector art** (`graphics=`):
   * `PDF_REDACT_LINE_ART_REMOVE_IF_COVERED` (default) removes painted paths whose bounding
     box, including half the stroke width, lies inside an area;
   * `..._REMOVE_IF_TOUCHED` removes any that overlap.
   * Clipping paths are never removed.
   * The default deliberately keeps page backgrounds and table rules that merely cross the area.
9. **Annotations** that overlap an area are deleted, together with their pop-ups. Widgets are
   also removed from `/AcroForm /Fields`, so field values go too (`annotations=False` keeps
   them). The document's `/XFA` packets are dropped because they duplicate all form text.
10. **Verification** (`verify=True`, default): the page is re-extracted with PDFium.
    * Any non-space character still ≥ 50 % inside an area raises `RedactionError`
      (`strict=False`: a warning).
    * Every character that was entirely outside the areas must still be there with the same
      text and a bbox within 0.05 pt. If not, a `RedactionWarning` is issued.
    * Results go to `page.redaction_report`.
11. **Overlays.** After verification, the fill boxes and overlay texts are drawn. Overlay
    text is word-wrapped into the box and shrinks until it fits.
12. **Saving.** `doc.save()` always writes a full file and drops unreferenced objects after
    redaction or `scrub()`, so the old content streams and object streams are gone.
    `incremental=True` is refused in that case.

## Test results

All runs below found 0 leftover characters, 0 moved neighbours and 0 errors (verify on).

**Stress test:** every 7th word on every page of the test corpora was redacted.

| corpus | words redacted | glyphs removed |
|---|---|---|
| arXiv "Attention" (15 pp, Type1 + ligatures) | 873 | 4 797 |
| arXiv "ResNet" (12 pp) | 1 401 | 7 103 |
| IRS W-9 (6 pp, hybrid AcroForm/XFA form) | 897 | 4 464 |
| synthetic (base-14 without `/Widths`, tight leading) | 698 | 4 439 |
| `simple_text.pdf` (Identity-H CID font) | 45 | 240 |
| camelot table | 60 | 312 |

**Search-and-redact:** after search-and-redact, pdfwords, PyMuPDF, pypdfium2, poppler's
`pdftotext` and pypdf could not find the redacted phrases. All neighbouring words kept
identical text and coordinates.

`tests/test_edit.py` covers:

* partial `TJ` with kerning, and `'`/`"`;
* text split across two content streams;
* `ActualText`;
* a Form XObject shared by two pages;
* image pixel blanking (checked by rendering);
* vector removal, keeping a full-page background;
* inline images;
* XFA/metadata leaks (every object and decoded stream is grepped);
* incremental-save refusal;
* the CLI.

## Limits

* **Font metrics:** glyph positions are exact when the font carries widths.
  * Non-embedded base-14 fonts use PDFium's metrics, which match Adobe's.
  * For Type0 fonts with a *predefined non-Identity* CMap (e.g. `UniJIS-UCS2-H`), code→CID is
    unknown, so `/DW` is used. The font is then listed in `redaction_report["inexact_fonts"]`.
  * Removal still happens. A wrong width only matters for glyphs after a removed one in the
    *same* show operator, and verification would report them.
* **Text drawn as vector outlines or inside images** (scans) is handled by the graphics/images
  options, not by text removal. Use `IMAGE_PIXELS` for scans.
* **Type3 glyph procedures** are not rewritten; their characters are removed like other text.
* **Document-level copies are not touched by page redaction**, apart from XFA. This covers the
  structure tree (`/Alt`, `/ActualText` on structure elements), bookmarks, named
  destinations, and annotations on other pages. `doc.scrub()` removes metadata, XMP,
  JavaScript, attachments, thumbnails and XFA. Review outlines/structure for sensitive
  titles (`scrub(outlines=True)` removes the outline tree).
* **Shadings (`sh`) and patterns** are kept.
* **Encrypted documents** are decrypted for editing and saved unencrypted.
* **Text insertion:**
  * fonts are mapped through their Unicode cmap without shaping;
  * base-14 fonts are limited to WinAnsi (Latin-1 + cp1252); other characters raise
    `ValueError` and suggest `fontfile=`;
  * `rotate` must be a multiple of 90.
* **Coordinates** are in the unrotated page space. Use `get_text(rotated=False)` boxes,
  which are the default.
