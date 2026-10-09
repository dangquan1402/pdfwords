# SPDX-License-Identifier: Apache-2.0
"""pdfwords - PyMuPDF-style text extraction with bounding boxes, on PDFium (Apache-2.0).

    import pdfwords
    doc = pdfwords.open("file.pdf")
    page = doc[0]
    page.get_text("words")    # [(x0, y0, x1, y1, "word", block_no, line_no, word_no), ...]
    page.get_text("dict")     # {"width", "height", "blocks": [{... "lines": [{... "spans": [...]}]}]}
    page.get_text("rawdict")  # like dict, spans carry "chars" with per-glyph bbox/origin
    page.get_text("blocks")   # [(x0, y0, x1, y1, "text", block_no, block_type), ...]
    page.get_text("text")     # plain text
    page.get_text("words", sort="xycut")   # column-aware reading order

Coordinates: PDF points, top-left origin, relative to the CropBox of the *unrotated*
page (identical to PyMuPDF). Pass rotated=True to get coordinates on the page as displayed.
"""
from __future__ import annotations

import ctypes as _ct
import json as _json
import os as _os

import pypdfium2 as pdfium

from .chars import page_glyphs, crop_box, TEXT_FONT_SUPERSCRIPT, TEXT_FONT_ITALIC, TEXT_FONT_SERIFED, TEXT_FONT_MONOSPACED, TEXT_FONT_BOLD  # noqa: F401
from .layout import build_blocks, make_spans, LayoutParams, DEFAULT_PARAMS  # noqa: F401
from .order import sort_simple, sort_xycut
from .render import RenderTimeout, Pixmap  # noqa: F401
from .annots import LINK_NONE, LINK_GOTO, LINK_URI, LINK_LAUNCH, LINK_NAMED, LINK_GOTOR  # noqa: F401

__version__ = "0.3.0"
__all__ = ["open", "Document", "Page", "available_backends", "default_backend", "parallel_words", "__version__",
           "extract", "iter_pages", "RenderTimeout", "Pixmap", "convert_from_path", "convert_from_bytes", "to_markdown", "chunks", "export", "LINK_NONE", "LINK_GOTO", "LINK_URI", "LINK_LAUNCH", "LINK_NAMED", "LINK_GOTOR",
           "PDF_REDACT_IMAGE_NONE", "PDF_REDACT_IMAGE_REMOVE", "PDF_REDACT_IMAGE_PIXELS",
           "PDF_REDACT_LINE_ART_NONE", "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", "PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED",
           "PDF_REDACT_TEXT_REMOVE", "PDF_REDACT_TEXT_NONE"]

# redaction options (same names and values as PyMuPDF's constants)
PDF_REDACT_IMAGE_NONE, PDF_REDACT_IMAGE_REMOVE, PDF_REDACT_IMAGE_PIXELS = 0, 1, 2
PDF_REDACT_LINE_ART_NONE, PDF_REDACT_LINE_ART_REMOVE_IF_COVERED, PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED = 0, 1, 2
PDF_REDACT_TEXT_REMOVE, PDF_REDACT_TEXT_NONE = 0, 1

LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
_WS = set(" \t\n\r\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000")

# ---------------------------------------------------------------------- backends
_rs_mod = None
_rs_err = None


def _rust():
    """Import the native extension and bind it to the PDFium library pypdfium2 has loaded."""
    global _rs_mod, _rs_err
    if _rs_mod is None:
        if _rs_err is not None:
            raise _rs_err
        try:
            from . import _native
            from ._libpath import find_pdfium_library
            _native.init(find_pdfium_library())
        except Exception as e:  # ImportError when built without the extension
            _rs_err = RuntimeError(f"pdfwords native (rust) backend unavailable: {e}")
            raise _rs_err from e
        _rs_mod = _native
    return _rs_mod


def available_backends():
    """Backends usable in this installation, fastest first: ["rust", "python"] or ["python"]."""
    try:
        _rust()
        return ["rust", "python"]
    except RuntimeError:
        return ["python"]


def default_backend():
    """$PDFWORDS_BACKEND if set, else "rust" when the native extension is available, else "python"."""
    env = _os.environ.get("PDFWORDS_BACKEND")
    if env:
        return env
    return available_backends()[0]


def open(source, password=None, backend=None, flatten=False):  # noqa: A001  (mirror pymupdf.open)
    """source: path, bytes or a binary file object.
    backend: "rust" (native core, default when the extension is built) | "python".
    flatten: render form-field values and annotation appearances into the page content before
        extraction (PDFium FPDFPage_Flatten), so filled-in form values come out as text."""
    return Document(source, password, backend=backend, flatten=flatten)


class Document:
    def __init__(self, source, password=None, backend=None, flatten=False):
        self.backend = backend or default_backend()
        if self.backend not in ("rust", "python"):
            raise ValueError(f"unknown backend {self.backend!r}")
        if self.backend == "rust":
            _rust()
        if hasattr(source, "read"):          # file objects: read once, then behave like bytes
            if hasattr(source, "seek"):
                source.seek(0)
            source = source.read()
        if isinstance(source, (bytearray, memoryview)):
            source = bytes(source)
        self._source, self._password = source, password
        self.name = _os.fspath(source) if isinstance(source, (str, _os.PathLike)) else ""
        self._flatten = bool(flatten)
        self._pdf = self._open_pdfium(source)
        self._ed = None        # pypdf-based editor, created on first edit / raw access
        self._dirty = False    # editor has changes PDFium has not seen yet
        self._gen = 0          # bumped whenever PDFium re-opens the edited bytes
        self._buf = None
        self._fpdf = None      # second PDFium handle with the form environment (widgets())

    def _open_pdfium(self, src):
        pdf = pdfium.PdfDocument(src, password=self._password)
        self._flat_done = set()
        if self._flatten:
            _init_forms(pdf)   # must precede page loading
        return pdf

    def _flat(self, i):
        """Flatten page i once (flatten=True): forms/annotations become page content."""
        if self._flatten and i not in self._flat_done:
            import pypdfium2.raw as R
            p = self._pdf[i]
            try:
                R.FPDFPage_Flatten(p.raw, R.FLAT_NORMALDISPLAY)
            finally:
                p.close()
            self._flat_done.add(i)

    def _current_source(self):
        """Path or bytes of the document as it is now (edited bytes after edits)."""
        self._sync()
        return self._buf if self._gen > 0 else self._source

    def _forms_pdf(self):
        if self._fpdf is None:
            self._fpdf = pdfium.PdfDocument(self._current_source(), password=self._password if self._gen == 0 else None)
            _init_forms(self._fpdf)
        return self._fpdf

    # ------------------------------------------------------------------ editing plumbing
    def _editor(self):
        if self._ed is None:
            from .edit import _editor_cls
            src = self._source
            if isinstance(src, (str, _os.PathLike)):
                with _open_file(src, "rb") as f:
                    data = f.read()
            elif isinstance(src, (bytes, bytearray, memoryview)):
                data = bytes(src)
            else:
                raise TypeError("editing needs a path, bytes or a file object as source")
            self._ed = _editor_cls()(data, self._password)
        return self._ed

    def _touch(self):
        self._dirty = True

    def _sync(self):
        """Re-open the edited document in PDFium so extraction sees the changes."""
        if self._dirty:
            data = self._ed.to_bytes()
            old = self._pdf
            self._pdf = self._open_pdfium(data)
            self._buf = data
            old.close()
            if self._fpdf is not None:
                self._fpdf.close()
                self._fpdf = None
            self._gen += 1
            self._dirty = False

    @property
    def is_dirty(self):
        """True when the document has unsaved edits."""
        return self._ed is not None and (self._dirty or self._gen > 0)

    def save(self, filename, garbage=0, deflate=False, incremental=False, **_ignored):
        """Write the (edited) document. garbage>=1 drops unreferenced objects (always done after
        redactions/scrub), garbage>=3 also merges duplicates; deflate compresses uncompressed
        streams; incremental=True appends changes to the original bytes (refused after redaction,
        because the removed content would still be in the file)."""
        data = self.tobytes(garbage=garbage, deflate=deflate, incremental=incremental)
        if hasattr(filename, "write"):
            filename.write(data)
        else:
            with _open_file(filename, "wb") as f:
                f.write(data)

    def tobytes(self, garbage=0, deflate=False, incremental=False, **_ignored):
        return self._editor().to_bytes(incremental=incremental, garbage=garbage, deflate=deflate)

    write = tobytes

    def scrub(self, metadata=True, xml_metadata=True, javascript=True, embedded_files=True,
              thumbnails=True, xfa=True, **kw):
        """Remove document-level data that can leak information (Info dict, XMP, JavaScript,
        embedded/attached files, thumbnails, XFA form packets). Saved files are fully rewritten."""
        self._editor().scrub(metadata=metadata, xml_metadata=xml_metadata, javascript=javascript,
                             embedded_files=embedded_files, thumbnails=thumbnails, xfa=xfa, **kw)
        self._touch()

    @property
    def metadata(self):
        self._sync()
        md = self._pdf.get_metadata_dict(skip_empty=False)
        return {k[:1].lower() + k[1:]: v for k, v in md.items()}

    def set_metadata(self, md):
        self._editor().set_metadata(md)
        self._touch()

    # raw object access (xref numbers refer to the current, possibly edited, document)
    def xref_length(self):
        return self._editor().xref_length()

    def xref_object(self, xref, compressed=False):
        return self._editor().xref_object(xref, compressed)

    def xref_stream(self, xref):
        return self._editor().xref_stream(xref)

    def xref_stream_raw(self, xref):
        return self._editor().xref_stream_raw(xref)

    def update_stream(self, xref, data):
        self._editor().update_stream(xref, data)
        self._touch()

    @property
    def _addr(self):
        return _ct.cast(self._pdf.raw, _ct.c_void_p).value

    def __len__(self):
        self._sync()
        return len(self._pdf)

    page_count = property(__len__)

    def __getitem__(self, i):
        if i < 0:
            i += len(self)
        if not 0 <= i < len(self):
            raise IndexError(i)
        return Page(self, i)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    # ------------------------------------------------------------------ document-level reading
    def get_toc(self, simple=True):
        """Outline / bookmarks: [[level, title, page], ...] (1-based level and page, like PyMuPDF)."""
        from .annots import get_toc
        self._sync()
        return get_toc(self, simple)

    def text_quality(self, pages=None, **kw):
        """Per-page text-layer quality reports (see Page.text_quality)."""
        return [self[i].text_quality(**kw) for i in _page_list(pages, len(self))]

    def needs_ocr(self, pages=None, **kw):
        """Page numbers whose text layer is missing or unusable (see Page.text_quality)."""
        return [r["page"] for r in self.text_quality(pages, **kw) if r["needs_ocr"]]

    def iter_pages(self, mode="text", pages=None, workers=None, **kw):
        """Yield (page_number, page.get_text(mode, **kw)) in page order, streaming: with workers
        > 1 pages are extracted by a process pool and yielded as soon as they are in order."""
        yield from _iter_doc(self, mode, pages, workers, kw)

    def extract(self, mode="text", pages=None, workers=None, **kw):
        """[page.get_text(mode, **kw) for the selected pages], optionally with a process pool."""
        return [r for _, r in self.iter_pages(mode, pages, workers, **kw)]

    def iter_images(self, pages=None, dpi=None, *, workers=None, **kw):
        """Yield (page_number, image) streaming in page order; kw as Page.render."""
        from .render import iter_images
        return iter_images(self, pages, dpi, workers=workers, **kw)

    def to_images(self, pages=None, dpi=150, *, workers=None, fmt="jpeg", quality=90, out_dir=None,
                  name="{stem}-{page:04d}.{ext}", **kw):
        """Render pages to files in out_dir (returns the paths) or to a list of PIL images."""
        from .render import to_images
        return to_images(self, pages, dpi, workers=workers, fmt=fmt, quality=quality, out_dir=out_dir,
                         name=name, **kw)

    def close(self):
        if self._fpdf is not None:
            self._fpdf.close()
            self._fpdf = None
        self._pdf.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Page:
    def __init__(self, doc, number):
        self.parent, self.number = doc, number
        self._rust = doc.backend == "rust"
        self._pg = None
        self._cache = {}
        self._geom = None
        self._gen = doc._gen
        self.redaction_report = None

    def _fresh(self):
        """Drop cached PDFium state when the document was edited since it was loaded."""
        d = self.parent
        if d._dirty:
            d._sync()
        if self._gen != d._gen:
            self._pg, self._cache, self._geom, self._gen = None, {}, None, d._gen

    # pypdfium2 page (python backend), loaded lazily
    @property
    def _page(self):
        self._fresh()
        if self._pg is None:
            self.parent._flat(self.number)
            self._pg = self.parent._pdf[self.number]
        return self._pg

    def _rs(self, ligatures=False):
        self._fresh()
        key = ("rs", ligatures)
        if key not in self._cache:
            self.parent._flat(self.number)
            self._cache[key] = _rust().load_page(self.parent._addr, self.number, ligatures)
        return self._cache[key]

    def _geometry(self):
        self._fresh()
        if self._geom is None:
            rp = self._cache.get(("rs", False)) or self._cache.get(("rs", True))
            if rp is not None:
                self._geom = (rp.width, rp.height, rp.rotation % 360)
            else:   # page box only - no text extraction (render() and friends need just this)
                l, b, r, t = crop_box(self._page)
                self._geom = (abs(r - l), abs(t - b), self._page.get_rotation() % 360)
        return self._geom

    _w = property(lambda self: self._geometry()[0])
    _h = property(lambda self: self._geometry()[1])
    rotation = property(lambda self: self._geometry()[2])

    @property
    def rect(self):
        """Displayed page rectangle (like pymupdf Page.rect)."""
        return (0.0, 0.0, self._h, self._w) if self.rotation in (90, 270) else (0.0, 0.0, self._w, self._h)

    def _clip(self, clip):
        if clip is True:
            return (0.0, 0.0, self._w, self._h)
        return tuple(map(float, clip)) if clip else None

    def words_array(self, *, sort=False, clip=True, rotated=False):
        """Bulk words: (float64 array N x 4 bboxes, int32 array N x 3 (block, line, word), list of str).
        Same content as get_text("words"); avoids building N tuples. Requires numpy."""
        import numpy as np
        if self._rust:
            srt = "xycut" if sort == "xycut" else ("simple" if sort else "none")
            cb, ib, texts = self._rs(False).words_arrays(srt, self._clip(clip), rotated)
            return (np.frombuffer(cb, dtype=np.float64).reshape(-1, 4),
                    np.frombuffer(ib, dtype=np.int32).reshape(-1, 3), texts)
        w = self.get_text("words", sort=sort, clip=clip, rotated=rotated)
        return (np.array([x[:4] for x in w], dtype=np.float64).reshape(-1, 4),
                np.array([x[5:] for x in w], dtype=np.int32).reshape(-1, 3), [x[4] for x in w])

    # ------------------------------------------------------------------ core
    def _glyphs(self, ligatures):
        self._fresh()
        key = ("g", ligatures)
        if key not in self._cache:
            tp = self._page.get_textpage()
            try:
                self._cache[key] = page_glyphs(self._page, tp, ligatures=ligatures)
            finally:
                tp.close()
        return self._cache[key]

    def _blocks(self, sort=False, clip=True, ligatures=False):
        glyphs = self._glyphs(ligatures)
        cl = self._clip(clip)
        blocks = build_blocks(glyphs, cl)
        if sort == "xycut":
            blocks = sort_xycut(blocks, DEFAULT_PARAMS.xycut_gap_x, DEFAULT_PARAMS.xycut_gap_y)
        elif sort:
            blocks = sort_simple(blocks)
        for i, b in enumerate(blocks):
            b.number = i
        return blocks

    def _xf(self, rotated):
        """Return a point/rect transformer to output space."""
        r, w, h = self.rotation, self._w, self._h
        if not rotated or r == 0:
            return (lambda p: p), (lambda bb: bb)
        if r == 90:
            pt = lambda p: (h - p[1], p[0])  # noqa: E731
        elif r == 180:
            pt = lambda p: (w - p[0], h - p[1])  # noqa: E731
        else:
            pt = lambda p: (p[1], w - p[0])  # noqa: E731

        def rc(bb):
            a, b = pt((bb[0], bb[1])), pt((bb[2], bb[3]))
            return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        return pt, rc

    def get_text(self, option="text", *, sort=False, clip=True, rotated=False,
                 ligatures=False, dehyphenate=False, delimiters=None, extended=False, links=False, roles=False):
        """option: text | words | blocks | dict | rawdict | json | rawjson.
        sort: False (content order) | True (PyMuPDF-style y/x) | "xycut" (column-aware).
        ligatures: False (default) -> ligature glyphs come out as plain letters ("fi"), the first
            letter carrying the glyph box and the rest zero-width (as PyMuPDF does for multi-codepoint
            ToUnicode); True -> re-compose them into U+FB00..FB06 single chars.
        dehyphenate: join words broken by a line-end hyphen (text/words/blocks).
        extended: dict/rawdict only - spans also carry "weight" (font weight) and "pdf_flags"
            (font descriptor flags), rawdict chars carry "idx" (PDFium char index, i.e. content
            stream order; -1 for inserted spaces).
        links: dict/rawdict/json only - split spans at link boundaries and give every span a "url"
            ("" when not linked): link annotations (URIs, "#page=N" for internal targets) and URLs
            written in the text (auto-detected).
        sort="struct": tagged PDFs - the structure tree's (author's) reading order; blocks are split
            where the structure element changes; untagged text keeps its xycut position.
        roles: dict/rawdict only - blocks and spans carry "role" (P, H1, LI, TD, ...; "" untagged)
            and spans "mcid" (tagged PDFs)."""
        option = option.lower()
        if option in ("markdown", "md", "html", "xhtml", "xml", "hocr", "alto"):
            from . import exporters
            return exporters.page_export(self, option, sort=sort, clip=clip, rotated=rotated)
        if sort == "struct" or roles:
            return self._struct_text(option, sort, clip, rotated, ligatures, dehyphenate, delimiters, extended,
                                     links, roles)
        if links and option in ("dict", "rawdict", "json", "rawjson"):
            d = self.get_text("rawdict", sort=sort, clip=clip, rotated=rotated, ligatures=ligatures, extended=extended)
            _split_link_spans(d, self._link_rects(rotated), keep_chars=option.startswith("raw"))
            return _json.dumps(d, ensure_ascii=False) if option.endswith("json") else d
        if self._rust:
            srt = "xycut" if sort == "xycut" else ("simple" if sort else "none")
            if option in ("json", "rawjson"):
                d = self._rs(ligatures).get_text(option[:-4] + "dict", srt, self._clip(clip), rotated, dehyphenate,
                                                 delimiters, extended)
                return _json.dumps(d, ensure_ascii=False)
            return self._rs(ligatures).get_text(option, srt, self._clip(clip), rotated, dehyphenate, delimiters, extended)
        blocks = self._blocks(sort=sort, clip=clip, ligatures=ligatures)
        pt, rc = self._xf(rotated)
        lig = lambda s: s  # noqa: E731
        if option in ("dict", "rawdict", "json", "rawjson"):
            d = self._dict(blocks, raw=option.startswith("raw"), pt=pt, rc=rc, lig=lig, rotated=rotated, ext=extended)
            return _json.dumps(d, ensure_ascii=False) if option.endswith("json") else d
        if option == "words":
            return self._words(blocks, rc, lig, dehyphenate, delimiters)
        if option == "blocks":
            out = []
            for b in blocks:
                txt = self._block_text(b, lig, dehyphenate)
                out.append((*rc(b.bbox), txt, b.number, 0))
            return out
        if option == "text":
            return "".join(self._block_text(b, lig, dehyphenate) for b in blocks)
        raise ValueError(f"unsupported option {option!r}")

    def _struct_text(self, option, sort, clip, rotated, ligatures, dehyphenate, delimiters, extended, links, roles):
        from .tagged import annotate, struct_sort
        base = "xycut" if sort == "struct" else sort
        d = self.get_text("rawdict", sort=base, clip=clip, rotated=rotated, ligatures=ligatures, extended=True)
        order = annotate(self, d)
        if sort == "struct" and order:
            struct_sort(d)
        for b in d["blocks"]:
            b.pop("_rank", None)
            b.setdefault("role", "")
        if links and option in ("dict", "rawdict", "json", "rawjson"):
            _split_link_spans(d, self._link_rects(rotated), keep_chars=True)
        if not roles or option not in ("dict", "rawdict", "json", "rawjson"):
            for b in d["blocks"]:
                b.pop("role", None)
                for ln in b["lines"]:
                    for sp in ln["spans"]:
                        sp.pop("role", None)
                        sp.pop("mcid", None)
        if not extended:
            for b in d["blocks"]:
                for ln in b["lines"]:
                    for sp in ln["spans"]:
                        sp.pop("weight", None)
                        sp.pop("pdf_flags", None)
                        for ch in sp["chars"]:
                            ch.pop("idx", None)
        for b in d["blocks"]:
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    for ch in sp["chars"]:
                        ch.pop("mcid", None)
        return _from_rawdict(d, option, dehyphenate, delimiters)

    # ------------------------------------------------------------------ search
    def search_for(self, needle, *, quads=False, clip=None, regex=False, ignore_case=True, rotated=False,
                   hit_max=None, dehyphenate=True):
        """Rectangles (x0, y0, x1, y1) of every occurrence of `needle` (plain text, or a regular
        expression with regex=True), one rectangle per line an occurrence spans. Matching is
        case-insensitive by default and treats any run of whitespace / line breaks as one space.
        dehyphenate: a word broken by a line-end hyphen ("hyphen-" / "ation") matches "hyphenation"
            (and "well-known" still matches "well-" / "known").
        hit_max: stop after this many occurrences.
        quads=True returns 4-point tuples (ul, ur, ll, lr) instead."""
        return _search(self, needle, quads=quads, clip=clip, regex=regex, ignore_case=ignore_case, rotated=rotated,
                       hit_max=hit_max, dehyphenate=dehyphenate)

    # ------------------------------------------------------------------ links, annotations, forms
    def get_links(self, *, web=False, rotated=False):
        """Link annotations as dicts {kind, from, uri | page,to,zoom | file, id[, quads]}, `kind`
        one of LINK_URI / LINK_GOTO / LINK_GOTOR / LINK_LAUNCH / LINK_NONE (PyMuPDF values),
        `page` 0-based, `to` the target point on that page (our coordinates).
        web=True also returns URLs written in the text without an annotation ({..., auto: True})."""
        from .annots import get_links
        return get_links(self, web=web, rotated=rotated)

    def annots(self, types=None, *, rotated=False):
        """Annotations: dicts {type, rect, contents, author, subject, id, modified, flags,
        stroke, fill, quads, index}. `types`: optional set of type names ("Highlight", ...)."""
        from .annots import annots
        if isinstance(types, str):
            types = {types}
        return annots(self, types=set(types) if types else None, rotated=rotated)

    def widgets(self, *, rotated=False):
        """Form fields: annots of type "Widget" plus field_name, field_label, field_type,
        field_value, field_flags, checked/on_state (check boxes, radios), choices (list/combo)."""
        from .annots import widgets
        return widgets(self, rotated=rotated)

    def _link_rects(self, rotated):
        from .annots import link_url
        out = []
        for lk in self.get_links(web=True, rotated=rotated):
            url = link_url(lk)
            if not url:
                continue
            rects = lk.get("rects") or ([_quad_rect(q) for q in lk["quads"]] if lk.get("quads") else [lk["from"]])
            out.extend((r, url) for r in rects)
        return out

    # ------------------------------------------------------------------ quality, tables
    def text_quality(self, **kw):
        """Text-layer quality report: {needs_ocr, score, reasons, chars, invisible_ratio,
        unicode_error_ratio, garbled_ratio, image_coverage, images, fonts}. See pdfwords.quality."""
        from .quality import text_quality
        return text_quality(self, **kw)

    def needs_ocr(self, **kw):
        """True when the page has no usable text layer (scan without text, garbled/unmapped text)."""
        return self.text_quality(**kw)["needs_ocr"]

    def table_cells(self, cells, image_size=None, *, rotated=True, min_overlap=0.5):
        """Assign the page's words to given table-cell boxes (e.g. from a layout model).
        cells: [(x0, y0, x1, y1), ...] in page coordinates, or in pixels of an image of size
        image_size=(w, h) rendered from the displayed page. Returns, per cell,
        {"bbox", "text", "words": [(x0, y0, x1, y1, word), ...]} with words in reading order
        (by line, then x); a word goes to the cell covering most of it (>= min_overlap)."""
        from .tables import assign_cells
        return assign_cells(self, cells, image_size, rotated=rotated, min_overlap=min_overlap)

    # ------------------------------------------------------------------ images, drawings, structure
    def get_images(self, *, rotated=False, hashes=False):
        """Images on the page (incl. inside Form XObjects), in painting order: dicts {number, bbox,
        quad, width, height, bpc, colorspace (components), cs_name, xres, yres, filters, size
        (encoded bytes), mcid[, digest with hashes=True]}."""
        from .objects import get_images
        return get_images(self, rotated=rotated, hashes=hashes)

    get_image_info = get_images

    def get_image(self, number, *, rendered=False):
        """Decoded image `number` (see get_images) as a PIL.Image (rendered=True applies masks)."""
        from .objects import get_image
        return get_image(self, number, rendered=rendered)

    def get_drawings(self, *, rotated=False, clip=None):
        """Vector paths as dicts {items: [("l", p1, p2) | ("re", rect) | ("c", p1, c1, c2, p2)],
        type: "f"|"s"|"fs", rect, color, fill (RGB 0..1), width, even_odd, closePath, opacities,
        lineCap, lineJoin, dashes, seqno} - PyMuPDF-like, in our coordinates."""
        from .objects import get_drawings
        return get_drawings(self, rotated=rotated, clip=clip)

    def get_struct_tree(self):
        """Tagged PDF structure elements of this page: [{role, type, title, alt, actual_text,
        lang, mcids, children}, ...] ([] when the page is not tagged)."""
        from .tagged import struct_tree
        return struct_tree(self)

    @property
    def is_tagged(self):
        return bool(self.get_struct_tree())

    # ------------------------------------------------------------------ rendering
    def render(self, dpi=None, **kw):
        """Rasterize the page with PDFium. Returns a numpy uint8 array (H, W, 3) RGB by default.
        dpi (default 150) | scale= | size=(w, h) (one may be None) | max_side=: resolution.
        clip=(x0, y0, x1, y1): region in get_text() coordinates (pixel-aligned with full renders).
        rotated=True: the page as displayed (False: unrotated, matching rotated=False bboxes).
        alpha=True: RGBA, transparent background. grayscale=True: (H, W), ~2x faster.
        annots / forms (AcroForm values): drawn by default. background=(r, g, b[, a]).
        antialias=False: crisp, unsmoothed text/paths/images. output: "numpy" | "pil" | "bytes"
        (raw samples) | "png" | "jpeg" | "webp" | "tiff" (encoded, needs Pillow).
        timeout=seconds: abort with pdfwords.RenderTimeout. out=: reuse a preallocated array.
        max_pixels: guard against huge pages (default 2**28; None disables)."""
        from .render import render
        return render(self, dpi, **kw)

    def get_pixmap(self, **kw):
        """PyMuPDF-style: get_pixmap(matrix=, dpi=, colorspace="rgb"|"gray", clip=, alpha=,
        annots=) -> Pixmap (width, height, n, samples, tobytes("png"), save(), pil_image())."""
        from .render import get_pixmap
        return get_pixmap(self, **kw)

    def render_tiles(self, dpi=None, tile=2048, **kw):
        """Iterate ((x, y), image) tiles of the full-page raster (pixel-identical to a full render)."""
        from .render import render_tiles
        return render_tiles(self, dpi, tile, **kw)

    def thumbnail(self, max_side=256, **kw):
        """Small preview (longest side max_side px), without annotations/forms by default."""
        kw.setdefault("annots", False)
        kw.setdefault("forms", False)
        return self.render(max_side=max_side, **kw)

    def render_geometry(self, dpi=None, **kw):
        """Pixel geometry of render(dpi, **kw): {full_w, full_h, sx, sy, px0, py0, w, h, rotated}."""
        from .render import geometry
        kw = {k: v for k, v in kw.items() if k in ("scale", "size", "max_side", "clip", "rotated")}
        return geometry(self, dpi=dpi, **kw).as_dict()

    def pixel_to_pdf(self, px, py, dpi=None, **kw):
        """Pixel (px, py) of render(dpi, **kw) -> (x, y) in get_text() coordinates (rotated=True
        by default: the displayed page, i.e. get_text(..., rotated=True))."""
        from .render import pixel_to_pdf
        return pixel_to_pdf(self, px, py, dpi=dpi, **kw)

    def pdf_to_pixel(self, x, y, dpi=None, **kw):
        """(x, y) in get_text() coordinates -> pixel position in render(dpi, **kw)."""
        from .render import pdf_to_pixel
        return pdf_to_pixel(self, x, y, dpi=dpi, **kw)

    def bbox_to_pixel(self, bbox, dpi=None, **kw):
        """A bbox (x0, y0, x1, y1) in get_text() coordinates -> pixel box in render(dpi, **kw)."""
        a = self.pdf_to_pixel(bbox[0], bbox[1], dpi, **kw)
        b = self.pdf_to_pixel(bbox[2], bbox[3], dpi, **kw)
        return (a[0], a[1], b[0], b[1])

    # ------------------------------------------------------------------ editing (pdfwords[edit])
    def read_contents(self):
        """Decoded page content: all /Contents streams concatenated (bytes)."""
        return self.parent._editor().read_contents(self.number)

    def get_contents(self):
        """xref numbers of the page's content streams."""
        return self.parent._editor().get_contents(self.number)

    def set_contents(self, data):
        """Replace the page content with one new stream; returns its xref."""
        x = self.parent._editor().set_contents(self.number, bytes(data))
        self.parent._touch()
        return x

    def add_redact_annot(self, quad, text=None, fontname="Helv", fontsize=11, align=0, fill=(1, 1, 1),
                         text_color=(0, 0, 0), cross_out=True):
        """Mark an area for redaction (a /Redact annotation). quad: (x0, y0, x1, y1) in page
        coordinates (as returned by get_text / search_for, unrotated page). Nothing is removed
        until apply_redactions(). fill: RGB 0..1 of the box drawn afterwards (None: no box);
        text: optional overlay text drawn in the box."""
        q = tuple(map(float, quad[:4])) if len(quad) >= 4 and not hasattr(quad[0], "__len__") else _quad_rect(quad)
        a = self.parent._editor().add_redact_annot(self.number, q, text=text, fontname=fontname, fontsize=fontsize,
                                                   align=align, fill=fill, text_color=text_color, cross_out=cross_out)
        self.parent._touch()
        return a

    def apply_redactions(self, images=PDF_REDACT_IMAGE_PIXELS, graphics=PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                         text=PDF_REDACT_TEXT_REMOVE, *, annotations=True, min_overlap=0.1, verify=True, strict=True):
        """Apply this page's redaction annotations: remove the text, image pixels/images and vector
        art under them from the page content (incl. Form XObjects), delete annotations/form fields
        that overlap them, then draw the fill boxes and overlay texts.

        verify=True re-extracts the page with PDFium afterwards: any character still mostly
        inside a redaction area raises RedactionError (strict=True) / warns (strict=False), and
        characters outside the areas that moved or disappeared trigger a RedactionWarning.
        The result is stored in page.redaction_report. Returns True if anything was applied."""
        import warnings
        from .edit import RedactionError, RedactionWarning
        ed = self.parent._editor()
        if not ed.redact_annots(self.number):
            return False
        before = _page_chars(self) if verify else None
        ok, stats, urects, specs = ed.apply_redactions(self.number, images=images, graphics=graphics, text=text,
                                                annotations=annotations, min_overlap=min_overlap)
        self.parent._touch()
        rects = [ed.rect_from_user(self.number, r) for r in urects]
        report = dict(stats, rects=rects)
        if verify:
            after = _page_chars(self)
            left = [(c, bb) for c, bb in after if any(_covered_frac(bb, r) >= 0.5 for r in rects)]
            outside = [(c, bb) for c, bb in before if not any(_overlap(bb, r) for r in rects)]
            moved = _unmatched(outside, after)
            report.update(leftover_chars=left, collateral_chars=moved)
            self.redaction_report = report
            if left:
                msg = (f"page {self.number}: {len(left)} characters are still extractable inside the redaction "
                       f"area(s): {''.join(c for c, _ in left)[:80]!r}")
                if strict:
                    ed.draw_redaction_overlays(self.number, specs)
                    self.parent._touch()
                    raise RedactionError(msg)
                warnings.warn(msg, RedactionWarning, stacklevel=2)
            if moved:
                warnings.warn(f"page {self.number}: {len(moved)} characters outside the redaction areas moved or "
                              f"disappeared: {''.join(c for c, _ in moved)[:80]!r}", RedactionWarning, stacklevel=2)
        else:
            self.redaction_report = report
        ed.draw_redaction_overlays(self.number, specs)
        self.parent._touch()
        return ok

    def insert_text(self, point, text, fontsize=11, fontname="helv", fontfile=None, color=(0, 0, 0), rotate=0,
                    lineheight=None, render_mode=0):
        """Write `text` starting at `point` (baseline start of the first line, page coordinates,
        unrotated page). "\n" starts a new line. fontname: base-14 short code (helv, tiro, cour,
        hebo, ...) or fontfile=path to a TrueType/OpenType font (embedded and subset, full
        Unicode). rotate: 0/90/180/270 (counter-clockwise). Returns the number of lines."""
        if isinstance(text, (list, tuple)):
            text = "\n".join(text)
        n = self.parent._editor().insert_text(self.number, tuple(map(float, point[:2])), text, fontsize=fontsize,
                                              fontname=fontname, fontfile=fontfile, color=color, rotate=rotate,
                                              lineheight=lineheight, render_mode=render_mode)
        self.parent._touch()
        return n

    def insert_textbox(self, rect, text, fontsize=11, fontname="helv", fontfile=None, color=(0, 0, 0), align=0,
                       rotate=0, lineheight=None, render_mode=0):
        """Fill `rect` with word-wrapped `text`. align: 0 left, 1 center, 2 right, 3 justify.
        Returns the unused height (>= 0); a negative value means the text does not fit and
        nothing was written (its magnitude is the missing height), as in PyMuPDF."""
        if isinstance(text, (list, tuple)):
            text = "\n".join(text)
        rc = self.parent._editor().insert_textbox(self.number, tuple(map(float, rect[:4])), text, fontsize=fontsize,
                                                  fontname=fontname, fontfile=fontfile, color=color, align=align,
                                                  rotate=rotate, lineheight=lineheight, render_mode=render_mode)
        if rc >= 0:
            self.parent._touch()
        return rc

    # ------------------------------------------------------------------ formatters
    @staticmethod
    def _line_text(ln, lig):
        return lig("".join(c.c for c in ln.chars))

    def _block_text(self, b, lig, dehyphenate):
        s = ""
        for ln in b.lines:
            t = self._line_text(ln, lig)
            if dehyphenate and t.endswith("-") and len(t) > 1 and t[-2].isalpha():
                s += t[:-1]
            else:
                s += t + "\n"
        return s

    def _words(self, blocks, rc, lig, dehyphenate, delimiters):
        brk = _WS | set(delimiters or "")
        out = []
        for b in blocks:
            for li, ln in enumerate(b.lines):
                wn = 0
                cur = []
                for ch in ln.chars + [None]:
                    if ch is None or ch.c in brk:
                        if cur:
                            x0 = min(c.bbox[0] for c in cur); y0 = min(c.bbox[1] for c in cur)  # noqa: E702
                            x1 = max(c.bbox[2] for c in cur); y1 = max(c.bbox[3] for c in cur)  # noqa: E702
                            out.append([*rc((x0, y0, x1, y1)), lig("".join(c.c for c in cur)), b.number, li, wn])
                            wn += 1
                            cur = []
                        continue
                    cur.append(ch)
        if dehyphenate:  # merge "exam-" + "ple" across consecutive lines of a block
            merged = []
            for w in out:
                if (merged and merged[-1][4].endswith("-") and len(merged[-1][4]) > 1 and merged[-1][4][-2].isalpha()
                        and merged[-1][5] == w[5] and merged[-1][6] == w[6] - 1 and w[7] == 0):
                    merged[-1][4] = merged[-1][4][:-1] + w[4]
                    continue
                merged.append(w)
            out = merged
        return [tuple(w) for w in out]

    def _dict(self, blocks, raw, pt, rc, lig, rotated, ext=False):
        x0, y0, x1, y1 = self.rect if rotated else (0, 0, self._w, self._h)
        res = {"width": x1, "height": y1, "blocks": []}
        for b in blocks:
            lines = []
            for ln in b.lines:
                spans = []
                for sp in make_spans(ln):
                    f = sp["font"]
                    s = {"size": sp["size"], "flags": sp["flags"], "bidi": int(sp["chars"][0].bidi),
                         "char_flags": 16 if not any(c.synthetic for c in sp["chars"]) else 0,
                         "font": f.name, "color": sp["color"], "alpha": 255,
                         "ascender": f.ascender, "descender": f.descender}
                    if ext:
                        s["weight"], s["pdf_flags"] = f.weight, f.pdf_flags
                    if raw:
                        if ext:
                            s["chars"] = [{"origin": pt(c.origin), "bbox": rc(c.bbox), "c": c.c, "synthetic": c.synthetic,
                                           "idx": c.idx} for c in sp["chars"]]
                        else:
                            s["chars"] = [{"origin": pt(c.origin), "bbox": rc(c.bbox), "c": c.c, "synthetic": c.synthetic}
                                          for c in sp["chars"]]
                    else:
                        s["text"] = lig("".join(c.c for c in sp["chars"]))
                    s["origin"] = pt(sp["chars"][0].origin)
                    s["bbox"] = rc(sp["bbox"])
                    spans.append(s)
                d = ln.dir
                if rotated and self.rotation:  # rotate direction vector as well
                    p0, p1 = pt((0, 0)), pt(d)
                    d = (p1[0] - p0[0], p1[1] - p0[1])
                lines.append({"spans": spans, "wmode": ln.wmode, "dir": d, "bbox": rc(ln.bbox)})
            res["blocks"].append({"type": 0, "number": b.number, "flags": 0, "bbox": rc(b.bbox), "lines": lines})
        return res


# ---------------------------------------------------------------------- helpers
import builtins as _builtins  # noqa: E402


def _from_rawdict(d, option, dehyphenate=False, delimiters=None):
    """Format a (re-ordered / annotated) rawdict as any get_text option (same rules as the
    native formatters: words split at whitespace + delimiters, dehyphenation, block texts)."""
    if option in ("rawdict", "rawjson"):
        return _json.dumps(d, ensure_ascii=False) if option == "rawjson" else d
    if option in ("dict", "json"):
        for b in d["blocks"]:
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    if "chars" in sp:
                        sp["text"] = "".join(c["c"] for c in sp.pop("chars"))
                    sp.setdefault("text", "")
                    # keep PyMuPDF key order: text before origin/bbox
                    for k in ("origin", "bbox"):
                        if k in sp:
                            sp[k] = sp.pop(k)
        return _json.dumps(d, ensure_ascii=False) if option == "json" else d
    lines_txt = []
    if option == "words":
        brk = _WS | set(delimiters or "")
        out = []
        for b in d["blocks"]:
            for li, ln in enumerate(b["lines"]):
                wn, cur = 0, []
                chars = [c for sp in ln["spans"] for c in sp["chars"]] + [None]
                for ch in chars:
                    if ch is None or ch["c"] in brk:
                        if cur:
                            bb = [c["bbox"] for c in cur]
                            out.append([min(x[0] for x in bb), min(x[1] for x in bb), max(x[2] for x in bb),
                                        max(x[3] for x in bb), "".join(c["c"] for c in cur), b["number"], li, wn])
                            wn += 1
                            cur = []
                        continue
                    cur.append(ch)
        if dehyphenate:
            merged = []
            for w in out:
                if (merged and merged[-1][4].endswith("-") and len(merged[-1][4]) > 1 and merged[-1][4][-2].isalpha()
                        and merged[-1][5] == w[5] and merged[-1][6] == w[6] - 1 and w[7] == 0):
                    merged[-1][4] = merged[-1][4][:-1] + w[4]
                    continue
                merged.append(w)
            out = merged
        return [tuple(w) for w in out]

    def block_text(b):
        s = ""
        for ln in b["lines"]:
            t = "".join(c["c"] for sp in ln["spans"] for c in sp["chars"])
            if dehyphenate and t.endswith("-") and len(t) > 1 and t[-2].isalpha():
                s += t[:-1]
            else:
                s += t + "\n"
        return s
    if option == "blocks":
        return [(*b["bbox"], block_text(b), b["number"], 0) for b in d["blocks"]]
    if option == "text":
        return "".join(block_text(b) for b in d["blocks"])
    del lines_txt
    raise ValueError(f"unsupported option {option!r}")


def _init_forms(pdf):
    """pdf.init_forms() without pypdfium2's "no XFA support" log line (AcroForm still works)."""
    import logging
    lg = logging.getLogger("pypdfium2._helpers.document")
    old = lg.level
    lg.setLevel(logging.ERROR)
    try:
        pdf.init_forms()
    finally:
        lg.setLevel(old)


_open_file = _builtins.open  # pdfwords.open shadows the builtin in this module


def _quad_rect(q):
    pts = [tuple(p) for p in q]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(a, b):
    """Boxes intersect (a zero-width/height box counts when it lies within the other's span)."""
    for lo, hi, rlo, rhi in ((a[0], a[2], b[0], b[2]), (a[1], a[3], b[1], b[3])):
        if hi - lo <= 1e-6:
            if not rlo <= lo <= rhi:
                return False
        elif min(hi, rhi) - max(lo, rlo) <= 1e-6:
            return False
    return True


def _covered_frac(a, r):
    """min over axes of the fraction of box `a` covered by `r` (degenerate axes count as covered
    when inside)."""
    fr = []
    for lo, hi, rlo, rhi in ((a[0], a[2], r[0], r[2]), (a[1], a[3], r[1], r[3])):
        ext = hi - lo
        inter = min(hi, rhi) - max(lo, rlo)
        if ext <= 1e-9:
            fr.append(1.0 if rlo <= lo <= rhi else 0.0)
        else:
            fr.append(max(0.0, inter) / ext)
    return min(fr)


def _page_chars(page):
    out = []
    d = page.get_text("rawdict", clip=False)
    for b in d["blocks"]:
        for ln in b["lines"]:
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    if ch["c"].strip():
                        out.append((ch["c"], tuple(ch["bbox"])))
    return out


def _unmatched(chars, pool, tol=0.05):
    by = {}
    for c, bb in pool:
        by.setdefault(c, []).append(bb)
    bad = []
    for c, bb in chars:
        cands = by.get(c, [])
        for k, ob in enumerate(cands):
            if all(abs(u - v) <= tol for u, v in zip(bb, ob)):
                cands.pop(k)
                break
        else:
            bad.append((c, bb))
    return bad


_HYPHENS = "-\u00ad\u2010"


def _search(page, needle, quads=False, clip=None, regex=False, ignore_case=True, rotated=False,
            hit_max=None, dehyphenate=True):
    import re
    d = page.get_text("rawdict", clip=clip if clip is not None else True, rotated=rotated)
    text = []
    boxes = []   # parallel to text: (line_key, bbox) or None for separators
    for bi, b in enumerate(d["blocks"]):
        for li, ln in enumerate(b["lines"]):
            first = next((ch["c"] for sp in ln["spans"] for ch in sp["chars"] if ch["c"].strip()), "")
            if (dehyphenate and li > 0 and len(text) >= 2 and text[-1] in _HYPHENS and text[-2].isalpha()
                    and first.isalpha()):
                text.pop()   # line-end hyphen: join the word halves
                boxes.pop()
            elif text and text[-1] != " ":
                text.append(" ")
                boxes.append(None)
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    c = ch["c"]
                    if not c.strip():
                        if text and text[-1] == " ":
                            continue
                        c = " "
                    text.append(c)
                    boxes.append(((bi, li), ch["bbox"]) if c != " " else None)
    s = "".join(text)
    if regex:
        pat = needle
    else:
        pat = r"\s+".join(re.escape(w) for w in needle.split())
        if dehyphenate:   # "well-known" must still match a line-broken "well-" / "known"
            pat = pat.replace(re.escape("-"), "-?")
    if not pat:
        return []
    rx = re.compile(pat, re.IGNORECASE if ignore_case else 0)
    out = []
    for hits, m in enumerate(rx.finditer(s)):
        if hit_max is not None and hits >= hit_max:
            break
        groups = {}
        order = []
        for k in range(m.start(), m.end()):
            bx = boxes[k]
            if bx is None:
                continue
            key, bb = bx
            if key not in groups:
                groups[key] = list(bb)
                order.append(key)
            else:
                g = groups[key]
                g[0], g[1], g[2], g[3] = min(g[0], bb[0]), min(g[1], bb[1]), max(g[2], bb[2]), max(g[3], bb[3])
        for key in order:
            x0, y0, x1, y1 = groups[key]
            out.append(((x0, y0), (x1, y0), (x0, y1), (x1, y1)) if quads else (x0, y0, x1, y1))
    return out


def _split_link_spans(d, links, keep_chars):
    """Split rawdict spans where the covering link changes; every span gets "url"."""
    from .annots import char_urls
    for b in d["blocks"]:
        for ln in b["lines"]:
            new = []
            lb = ln["bbox"]
            near = [lk for lk in links if lk[0][0] <= lb[2] + 1 and lk[0][2] >= lb[0] - 1
                    and lk[0][1] <= lb[3] + 1 and lk[0][3] >= lb[1] - 1]
            for sp in ln["spans"]:
                chars = sp["chars"]
                if not near and chars:   # fast path: no link near this line (span bbox == char union)
                    ns = {key: v for key, v in sp.items() if key not in ("chars", "bbox", "origin", "text")}
                    ns["bbox"] = sp["bbox"]
                    ns["origin"] = chars[0]["origin"]
                    ns["text"] = "".join(c["c"] for c in chars)
                    ns["url"] = ""
                    if keep_chars:
                        ns["chars"] = chars
                    new.append(ns)
                    continue
                urls = (char_urls([c["bbox"] for c in chars], near, texts=[c["c"] for c in chars])
                        if near else [""] * len(chars))
                start = 0
                for k in range(1, len(chars) + 1):
                    if k == len(chars) or urls[k] != urls[start]:
                        part = chars[start:k]
                        ns = {key: v for key, v in sp.items() if key not in ("chars", "bbox", "origin", "text")}
                        bbs = [c["bbox"] for c in part]
                        ns["bbox"] = (min(x[0] for x in bbs), min(x[1] for x in bbs),
                                      max(x[2] for x in bbs), max(x[3] for x in bbs))
                        ns["origin"] = part[0]["origin"]
                        ns["text"] = "".join(c["c"] for c in part)
                        ns["url"] = urls[start]
                        if keep_chars:
                            ns["chars"] = part
                        new.append(ns)
                        start = k
                if not chars:
                    ns = {key: v for key, v in sp.items() if key != "chars"}
                    ns.setdefault("text", "")
                    ns["url"] = ""
                    if keep_chars:
                        ns["chars"] = []
                    new.append(ns)
            ln["spans"] = new


def _page_list(pages, n):
    """Normalise pages=None | int | range | iterable (negative numbers count from the end)."""
    if pages is None:
        return list(range(n))
    if isinstance(pages, str):   # "0,2-5", "-1"
        spec, pages = pages, []
        for part in spec.replace(" ", "").split(","):
            if not part:
                continue
            lo, sep, hi = part[1:].partition("-") if part.startswith("-") else part.partition("-")
            if part.startswith("-"):
                lo = "-" + lo
            if sep:
                a, b = int(lo), int(hi) if hi else n - 1
                pages.extend(range(a, b + 1))
            else:
                pages.append(int(lo))
    if isinstance(pages, int):
        pages = [pages]
    out = []
    for p in pages:
        q = p + n if p < 0 else p
        if not 0 <= q < n:
            raise IndexError(f"page {p} out of range (document has {n} pages)")
        out.append(q)
    return out


# ---------------------------------------------------------------------- parallelism
# PDFium is not thread-safe (global state, no locking), so parallelism is by process: each
# worker opens its own copy of the document.
def _pool_worker(args):
    src, password, backend, flatten, pages, mode, kw = args
    with open(src, password, backend=backend, flatten=flatten) as d:
        return [(i, d[i].get_text(mode, **kw)) for i in pages]


def _iter_doc(doc, mode, pages, workers, kw):
    pages = _page_list(pages, len(doc))
    if not workers or workers <= 1 or len(pages) < 2:
        for i in pages:
            yield i, doc[i].get_text(mode, **kw)
        return
    import concurrent.futures as cf
    src = doc._current_source()
    password = doc._password if doc._gen == 0 else None
    workers = min(workers, len(pages))
    size = max(1, min(32, -(-len(pages) // (workers * 4))))   # small contiguous chunks -> streaming
    chunks = [pages[i:i + size] for i in range(0, len(pages), size)]
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = [ex.submit(_pool_worker, (src, password, doc.backend, doc._flatten, c, mode, kw)) for c in chunks]
        for f in futs:            # in page order; later chunks keep running meanwhile
            yield from f.result()


def iter_pages(source, mode="text", pages=None, workers=None, *, password=None, backend=None, flatten=False, **kw):
    """Stream (page_number, result) for a path / bytes / file object; see Document.iter_pages."""
    with open(source, password, backend=backend, flatten=flatten) as d:
        yield from d.iter_pages(mode, pages, workers, **kw)


def extract(source, mode="text", pages=None, workers=None, *, password=None, backend=None, flatten=False, **kw):
    """[get_text(mode, **kw) per selected page] for a path / bytes / file object.
    pages: None (all) | int | iterable of page numbers; workers: process count (None = serial)."""
    return [r for _, r in iter_pages(source, mode, pages, workers, password=password, backend=backend,
                                     flatten=flatten, **kw)]


def parallel_words(path, pages=None, processes=None, backend=None, **kw):
    """get_text("words") for many pages using a process pool (kept for 0.1 compatibility;
    same as extract(path, "words", pages, workers=processes or cpu_count)).
    Returns a list indexed like `pages`."""
    with open(path, backend=backend) as d:
        pages = _page_list(pages, len(d))
        workers = processes or min(len(pages), _os.cpu_count() or 1)
        return d.extract("words", pages, workers, **kw)


def to_markdown(source, pages=None, **kw):
    """Markdown (or per-page chunks with page_chunks=True) of a path / bytes / Document; see
    pdfwords.markdown.to_markdown."""
    from .markdown import to_markdown as f
    return f(source, pages, **kw)


def chunks(source, pages=None, **kw):
    """Heading-aware RAG chunks with page + bbox provenance; see pdfwords.markdown.chunks."""
    from .markdown import chunks as f
    return f(source, pages, **kw)


def export(source, fmt="markdown", pages=None, **kw):
    """Whole-document export: markdown | html | xhtml | xml | hocr | alto (see pdfwords.exporters)."""
    from .exporters import export as f
    return f(source, fmt, pages, **kw)


def convert_from_path(pdf_path, dpi=200, **kw):
    """pdf2image.convert_from_path drop-in (PDFium, no poppler): see pdfwords.compat.pdf2image."""
    from .compat.pdf2image import convert_from_path as f
    return f(pdf_path, dpi, **kw)


def convert_from_bytes(pdf_file, dpi=200, **kw):
    """pdf2image.convert_from_bytes drop-in: see pdfwords.compat.pdf2image."""
    from .compat.pdf2image import convert_from_bytes as f
    return f(pdf_file, dpi, **kw)
