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

__version__ = "0.1.0"
__all__ = ["open", "Document", "Page", "available_backends", "default_backend", "parallel_words", "__version__",
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


def open(source, password=None, backend=None):  # noqa: A001  (mirror pymupdf.open)
    """backend: "rust" (native core, default when pdfwords_rs is installed) | "python"."""
    return Document(source, password, backend=backend)


class Document:
    def __init__(self, source, password=None, backend=None):
        self.backend = backend or default_backend()
        if self.backend not in ("rust", "python"):
            raise ValueError(f"unknown backend {self.backend!r}")
        if self.backend == "rust":
            _rust()
        self._source, self._password = source, password
        self.name = _os.fspath(source) if isinstance(source, (str, _os.PathLike)) else ""
        self._pdf = pdfium.PdfDocument(source, password=password)
        self._ed = None        # pypdf-based editor, created on first edit / raw access
        self._dirty = False    # editor has changes PDFium has not seen yet
        self._gen = 0          # bumped whenever PDFium re-opens the edited bytes
        self._buf = None

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
            elif hasattr(src, "read"):
                if hasattr(src, "seek"):
                    src.seek(0)
                data = src.read()
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
            self._pdf = pdfium.PdfDocument(data)
            self._buf = data
            old.close()
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

    def close(self):
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
            self._pg = self.parent._pdf[self.number]
        return self._pg

    def _rs(self, ligatures=False):
        self._fresh()
        key = ("rs", ligatures)
        if key not in self._cache:
            self._cache[key] = _rust().load_page(self.parent._addr, self.number, ligatures)
        return self._cache[key]

    def _geometry(self):
        self._fresh()
        if self._geom is None:
            if self._rust:
                rp = self._rs(False)
                self._geom = (rp.width, rp.height, rp.rotation % 360)
            else:
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
                 ligatures=False, dehyphenate=False, delimiters=None, extended=False):
        """option: text | words | blocks | dict | rawdict | json | rawjson.
        sort: False (content order) | True (PyMuPDF-style y/x) | "xycut" (column-aware).
        ligatures: False (default) -> ligature glyphs come out as plain letters ("fi"), the first
            letter carrying the glyph box and the rest zero-width (as PyMuPDF does for multi-codepoint
            ToUnicode); True -> re-compose them into U+FB00..FB06 single chars.
        dehyphenate: join words broken by a line-end hyphen (text/words/blocks).
        extended: dict/rawdict only - spans also carry "weight" (font weight) and "pdf_flags"
            (font descriptor flags), rawdict chars carry "idx" (PDFium char index, i.e. content
            stream order; -1 for inserted spaces)."""
        option = option.lower()
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

    # ------------------------------------------------------------------ search
    def search_for(self, needle, *, quads=False, clip=None, regex=False, ignore_case=True, rotated=False):
        """Rectangles (x0, y0, x1, y1) of every occurrence of `needle` (plain text, or a regular
        expression with regex=True), one rectangle per line an occurrence spans. Matching is
        case-insensitive by default and treats any run of whitespace / line breaks as one space.
        quads=True returns 4-point tuples (ul, ur, ll, lr) instead."""
        return _search(self, needle, quads=quads, clip=clip, regex=regex, ignore_case=ignore_case, rotated=rotated)

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


def _search(page, needle, quads=False, clip=None, regex=False, ignore_case=True, rotated=False):
    import re
    d = page.get_text("rawdict", clip=clip if clip is not None else True, rotated=rotated)
    text = []
    boxes = []   # parallel to text: (line_key, bbox) or None for separators
    for bi, b in enumerate(d["blocks"]):
        for li, ln in enumerate(b["lines"]):
            if text and text[-1] != " ":
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
    if not pat:
        return []
    rx = re.compile(pat, re.IGNORECASE if ignore_case else 0)
    out = []
    for m in rx.finditer(s):
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


# ---------------------------------------------------------------------- parallelism
def _pw_worker(args):
    path, pages, kw, backend = args
    with open(path, backend=backend) as d:
        return [(i, d[i].get_text("words", **kw)) for i in pages]


def parallel_words(path, pages=None, processes=None, backend=None, **kw):
    """get_text("words") for many pages using a process pool. PDFium is not thread-safe
    (global state, no locking), so parallelism is by process: each worker opens its own
    document. Returns a list indexed like `pages`."""
    import concurrent.futures as cf
    with open(path, backend=backend) as d:
        n = len(d)
        backend = d.backend
    pages = list(range(n)) if pages is None else list(pages)
    processes = processes or min(len(pages), _os.cpu_count() or 1)
    chunks = [pages[i::processes] for i in range(processes)]
    res = {}
    with cf.ProcessPoolExecutor(processes) as ex:
        for part in ex.map(_pw_worker, [(path, c, kw, backend) for c in chunks if c]):
            res.update(part)
    return [res[i] for i in pages]
