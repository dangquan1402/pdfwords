# LLM-ready output: Markdown, chunks, tagged PDFs, objects, exports

Everything on this page is pure Python on top of the extraction pipeline (either backend) and
needs no extra dependency, except the optional integrations at the end.

## Markdown

```python
import pdfwords

md = pdfwords.to_markdown("paper.pdf")                       # one string
md = pdfwords.to_markdown("paper.pdf", pages="0-3", page_separators=True)   # <!-- page N -->
pages = pdfwords.to_markdown("paper.pdf", page_chunks=True)  # one dict per page:
# {"metadata": {"page", "page_count", "file_path", "width", "height", ...}, "text",
#  "elements": [{"type", "text", "md", "bbox", "page", "level", "role"}, ...],
#  "toc_items", "tables", "images"}
page.get_text("markdown")                                     # a single page
```

Options: `sort="auto"` (structure order for tagged PDFs, else column-aware xycut) |
`"xycut"` | `"struct"`; `headers_footers=True` removes running headers/footers;
`tables=True`; `images=True` places `![alt]()` placeholders, and `image_dir="img/"` also
writes each image region as a PNG (`dpi=150`) and links it.

What it produces:

| element | how it is found |
|---|---|
| headings `#`…`####` | (1) tagged role `H1`–`H6`; (2) the document outline (TOC) entry with the same text (section numbers ignored); (3) font size: sizes clearly larger than the body size (the most common size by characters) map to up to four levels; (4) short all-bold lines. Font-derived headings repeated more than twice in the document (or twice on a page) are demoted; levels are aligned with TOC levels of the same font size and compressed to start at H1. |
| paragraphs | the extraction blocks, lines joined with de-hyphenation; bold / italic / monospace spans as `**`/`_`/`` ` ``, links as `[text](url)` |
| lists | tagged `L`/`LI`, or lines starting with bullets (•, ‣, -, *, …) or enumerators (`1.`, `a)`, `iv.`) with hanging indents; nesting from the indent |
| code | blocks whose spans are all monospace → fenced code |
| tables | tagged `Table`/`TR`/`TH`/`TD` → GitHub table; untagged tables via `page.find_tables()` when available (0.4) |
| removed | running headers/footers (same normalised text in the top/bottom 10% on ≥ 40% of the pages, digits ignored), bare page numbers, vertical text in the side margins (arXiv stamps, line numbers) |

### Chunks for retrieval

```python
for c in pdfwords.chunks("paper.pdf", max_chars=2000, overlap=1):
    c["text"]        # Markdown, prefixed with its parent headings "Title > 2. Method"
    c["headings"]    # heading path, incl. the chunk's own leading heading
    c["pages"]       # pages it spans
    c["provenance"]  # [{"page", "bbox", "type"}] per element -> highlight the source
```

A new chunk starts at every heading of level ≤ `split_level` (default 2) once the current
chunk has `min_chars`, and whenever `max_chars` would be exceeded. Elements are not split,
except paragraphs longer than `max_chars`, which are cut at sentence boundaries.

## Tagged PDFs (structure tree)

```python
page.is_tagged                         # has a structure tree
page.get_struct_tree()                 # [{"role", "type", "id", "mcids", "children", "alt", "title", "lang", ...}]
page.get_text(sort="struct")           # author's logical reading order
page.get_text("dict", sort="struct", roles=True)   # blocks/spans carry "role" (P, H1, LI, TD, ...) and "mcid"
```

Each character is mapped to the marked-content ID (MCID) of its text object, and MCIDs to the
structure element that owns them. `sort="struct"` reorders **tagged** blocks by their rank in
the tree; untagged text (artifacts, or text the tagger missed) keeps its xycut slot, so nothing
is ever dropped. Blocks are split where the block-level element changes (two `P`s that the
geometric grouping merged become two blocks). Common non-standard role names from word
processors (`Heading1`, `Para`, `ListItem`, ...) are mapped to standard roles; PDFium does
not expose the document's `/RoleMap`, so other custom roles are reported as-is.

## Images and vector drawings

```python
page.get_images()            # [{"number", "bbox", "quad", "width", "height", "bpc", "colorspace",
                             #   "cs_name", "xres", "yres", "filters", "size", "mcid", ...}] (alias get_image_info)
page.get_images(hashes=True) # + "digest" (MD5 of the decoded pixels) to spot repeated logos
page.get_image(0)            # PIL image of image #0 (decoded, needs Pillow); rendered=True: as drawn
page.get_drawings()          # [{"items": [("l", p1, p2) | ("re", rect, orient) | ("c", p1, p2, p3, p4)],
                             #   "type": "f" | "s" | "fs", "rect", "color", "fill", "width",
                             #   "even_odd", "closePath", "fill_opacity", "stroke_opacity",
                             #   "lineCap", "lineJoin", "dashes", "seqno", "depth"}]
```

Objects inside Form XObjects are included (their matrices composed). Coordinates follow the
rest of the API: PDF points, top-left origin, unrotated page unless `rotated=True`. The shapes
follow PyMuPDF's `get_image_info()` / `get_drawings()` closely (checked black-box): on the W-9
both report the same 71 paths.

## Export formats

```python
pdfwords.export("doc.pdf", "html")    # positioned spans (absolute CSS), one <div> per page
pdfwords.export("doc.pdf", "xhtml")   # semantic: h1-h4, p, ul/li, table, img; data-page / data-bbox
pdfwords.export("doc.pdf", "xml")     # page > block > line > font > char with boxes
pdfwords.export("doc.pdf", "hocr", dpi=300)   # ocr_page / ocr_carea / ocr_par / ocr_line / ocrx_word, bbox in px
pdfwords.export("doc.pdf", "alto", dpi=300)   # ALTO v4: TextBlock / TextLine / String / SP, styles
page.get_text("hocr")                 # any format, single page
```

hOCR and ALTO use pixel units at `dpi` (default 72 = PDF points), so the text layer aligns with
images rendered at the same dpi (`page.render(dpi=...)`): useful for OCR tooling, search
overlays and digitisation pipelines. All XML outputs are well-formed (tested).

## Integrations (optional extras)

```python
# pip install "pdfwords[langchain]"
from pdfwords.integrations.langchain import PdfwordsLoader
docs = PdfwordsLoader("paper.pdf", mode="chunks", max_chars=1500).load()

# pip install "pdfwords[llamaindex]"
from pdfwords.integrations.llama_index import PdfwordsReader
docs = PdfwordsReader(mode="markdown_page").load_data("paper.pdf")
```

`mode`: `page` (plain text per page, default) | `single` | `markdown_page` | `markdown` |
`chunks` (metadata: `page`, `pages`, `headings`, `provenance` with boxes).

### MCP server

```bash
pip install "pdfwords[mcp]"
pdfwords mcp --root ~/Documents      # stdio transport
```

Claude Desktop / IDE config:

```json
{"mcpServers": {"pdfwords": {"command": "pdfwords", "args": ["mcp", "--root", "/Users/me/Documents"]}}}
```

Read-only tools: `pdf_info` (pages, metadata, TOC, tagged, pages needing OCR), `extract_text`
(text / words / blocks / markdown, page ranges, sort), `to_markdown`, `search` (hits with page,
box and context), `get_links`, `get_tables`, `render_page` (PNG, ≤ 300 dpi). Files outside the
`--root` directories (default: the working directory) are refused, and long outputs are
truncated with a note.

## CLI

```bash
pdfwords export doc.pdf -f markdown --pages 0-3 -o doc.md     # also html|xhtml|xml|hocr|alto
pdfwords export doc.pdf --image-dir img/ --page-separators
pdfwords chunks doc.pdf --max-chars 1500 > chunks.jsonl
pdfwords objects doc.pdf --pages 0 --kind images|drawings|struct
pdfwords doc.pdf --mode dict --sort struct --roles
```

## Limits

* Markdown is heuristic. Headings come from tags, the outline or font statistics; documents
  that style headings only by colour or position may get none. Untagged tables are not
  detected before 0.4's `find_tables`.
* Formulas are emitted as their text (no LaTeX).
* Images are placed after the text above them (untagged) or at their structure position (tagged).
