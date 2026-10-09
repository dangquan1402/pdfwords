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
__all__ = ["open", "Document", "Page", "available_backends", "default_backend", "parallel_words", "__version__"]

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
        self._pdf = pdfium.PdfDocument(source, password=password)

    @property
    def _addr(self):
        return _ct.cast(self._pdf.raw, _ct.c_void_p).value

    def __len__(self):
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

    # pypdfium2 page (python backend), loaded lazily
    @property
    def _page(self):
        if self._pg is None:
            self._pg = self.parent._pdf[self.number]
        return self._pg

    def _rs(self, ligatures=False):
        key = ("rs", ligatures)
        if key not in self._cache:
            self._cache[key] = _rust().load_page(self.parent._addr, self.number, ligatures)
        return self._cache[key]

    def _geometry(self):
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
                 ligatures=False, dehyphenate=False, delimiters=None):
        """option: text | words | blocks | dict | rawdict | json | rawjson.
        sort: False (content order) | True (PyMuPDF-style y/x) | "xycut" (column-aware).
        ligatures: False (default) -> ligature glyphs come out as plain letters ("fi"), the first
            letter carrying the glyph box and the rest zero-width (as PyMuPDF does for multi-codepoint
            ToUnicode); True -> re-compose them into U+FB00..FB06 single chars.
        dehyphenate: join words broken by a line-end hyphen (text/words/blocks)."""
        option = option.lower()
        if self._rust:
            srt = "xycut" if sort == "xycut" else ("simple" if sort else "none")
            if option in ("json", "rawjson"):
                d = self._rs(ligatures).get_text(option[:-4] + "dict", srt, self._clip(clip), rotated, dehyphenate, delimiters)
                return _json.dumps(d, ensure_ascii=False)
            return self._rs(ligatures).get_text(option, srt, self._clip(clip), rotated, dehyphenate, delimiters)
        blocks = self._blocks(sort=sort, clip=clip, ligatures=ligatures)
        pt, rc = self._xf(rotated)
        lig = lambda s: s  # noqa: E731
        if option in ("dict", "rawdict", "json", "rawjson"):
            d = self._dict(blocks, raw=option.startswith("raw"), pt=pt, rc=rc, lig=lig, rotated=rotated)
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

    def _dict(self, blocks, raw, pt, rc, lig, rotated):
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
                    if raw:
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
