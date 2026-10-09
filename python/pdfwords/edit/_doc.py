# SPDX-License-Identifier: Apache-2.0
"""Editing back-end: a pypdf (BSD-3) writer holding the document object graph.

PDFium has no public API for raw content-stream bytes or for rewriting individual operators,
so editing goes through pypdf; extraction keeps running on PDFium, which re-opens the edited
bytes whenever the document changed.
"""
from __future__ import annotations

import io
import re

from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, IndirectObject,
                           NameObject, NumberObject, StreamObject, TextStringObject)

from . import _redact as RD
from ._insert import build_snippet, color_op, layout_point, layout_textbox
from ._fonts import load_font
from ._lexer import parse, fmt


def _resolve(o):
    return o.get_object() if o is not None and hasattr(o, "get_object") else o


def _stream(data):
    s = DecodedStreamObject()
    s.set_data(data)
    return s


def _da_parse(da):
    m = re.search(r"/(\S+)\s+([\d.]+)\s+Tf", da or "")
    return (m.group(1), float(m.group(2))) if m else ("Helv", 11.0)


class RedactAnnot:
    """Handle for a pending /Redact annotation (PyMuPDF-like: .rect, .xref, .info)."""

    def __init__(self, rect, xref, info):
        self.rect, self.xref, self.info = rect, xref, info
        self.type = (12, "Redact")

    def __repr__(self):
        return f"RedactAnnot(rect={self.rect}, xref={self.xref})"


class Editor:
    def __init__(self, data: bytes, password=None):
        from pypdf import PdfReader, PdfWriter
        self.reader = PdfReader(io.BytesIO(data))
        if self.reader.is_encrypted:
            self.reader.decrypt(password or "")
        self.writer = PdfWriter(self.reader, incremental=True) if not self.reader.is_encrypted else PdfWriter(clone_from=self.reader)
        self.wrapped = set()
        self.redacted = False
        self.font_cache = {}

    # ------------------------------------------------------------------ pages
    def page(self, pno):
        return self.writer.pages[pno]

    def geom(self, pno):
        """(x0, y0, x1, y1) CropBox in user space (normalised)."""
        pg = self.page(pno)
        try:
            cb = pg.cropbox
        except Exception:
            cb = pg.mediabox
        x0, y0, x1, y1 = (float(v) for v in (cb.left, cb.bottom, cb.right, cb.top))
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def to_user_fn(self, pno):
        cx0, _, _, cy1 = self.geom(pno)
        return lambda x, y: (x + cx0, cy1 - y)

    def rect_to_user(self, pno, r):
        cx0, _, _, cy1 = self.geom(pno)
        x0, y0, x1, y1 = (float(v) for v in r)
        return (min(x0, x1) + cx0, cy1 - max(y0, y1), max(x0, x1) + cx0, cy1 - min(y0, y1))

    def rect_from_user(self, pno, r):
        cx0, _, _, cy1 = self.geom(pno)
        return (r[0] - cx0, cy1 - r[3], r[2] - cx0, cy1 - r[1])

    def resources(self, pno):
        pg = self.page(pno)
        node = pg
        while node is not None:
            r = node.get("/Resources")
            if r is not None:
                r = _resolve(r)
                if node is not pg:  # inherited: give the page its own copy of the dict
                    r = DictionaryObject(dict(r))
                    pg[NameObject("/Resources")] = r
                return r
            node = _resolve(node.get("/Parent"))
        r = DictionaryObject()
        pg[NameObject("/Resources")] = r
        return r

    def content_refs(self, pno):
        c = self.page(pno).get("/Contents")
        if c is None:
            return []
        if isinstance(c, IndirectObject):
            o = c.get_object()
            if isinstance(o, StreamObject):
                return [c]
            return list(o)
        if isinstance(c, StreamObject):
            return [c]
        return list(c)

    def get_contents(self, pno):
        return [r.idnum if isinstance(r, IndirectObject) else 0 for r in self.content_refs(pno)]

    def read_contents(self, pno):
        parts = []
        for r in self.content_refs(pno):
            try:
                parts.append(_resolve(r).get_data())
            except Exception:
                pass
        return b"\n".join(parts)

    def set_contents(self, pno, data):
        ref = self.writer._add_object(_stream(data))
        self.page(pno)[NameObject("/Contents")] = ref
        return ref.idnum

    def append_content(self, pno, snippet):
        """Append a self-contained snippet, wrapping the existing content in q/Q once so the
        page's own graphics-state changes cannot leak into it."""
        pg = self.page(pno)
        refs = self.content_refs(pno)
        new = []
        if pno not in self.wrapped:
            depth = 0
            data = self.read_contents(pno)
            if data:
                for op in parse(data):
                    if op.op == b"q":
                        depth += 1
                    elif op.op == b"Q":
                        depth = max(0, depth - 1)
                new.append(self.writer._add_object(_stream(b"q\n")))
                new.extend(refs)
                snippet = b"Q\n" * (depth + 1) + snippet
            self.wrapped.add(pno)
        else:
            new.extend(refs)
        new.append(self.writer._add_object(_stream(snippet)))
        pg[NameObject("/Contents")] = ArrayObject(new)

    # ------------------------------------------------------------------ fonts
    def font_resource(self, pno, font, used=None):
        """Register a font on the page; base-14 fonts are shared per document."""
        res = self.resources(pno)
        if used is None:
            ref = self.font_cache.get(font.key)
            if ref is None:
                ref = font.pdf_object(self.writer)
                self.font_cache[font.key] = ref
        else:
            ref = font.pdf_object(self.writer, used)
        fd = _resolve(res.get("/Font"))
        if fd is not None:
            for k, v in fd.items():
                if isinstance(v, IndirectObject) and v.idnum == ref.idnum:
                    return k.lstrip("/")
        return RD.add_resource(res, "/Font", ref, "PWF")

    # ------------------------------------------------------------------ insert text
    def _write_runs(self, pno, font, runs, fontsize, color, rotate, render_mode=0):
        to_user = self.to_user_fn(pno)
        from ._fonts import EmbeddedFont
        if isinstance(font, EmbeddedFont):
            used = []
            for r in runs:
                used.extend(font.encode(r.text))
            name = self.font_resource(pno, font, used)
        else:
            name = self.font_resource(pno, font)
        from ._lexer import Name
        snippet, _ = build_snippet(font, Name(name), runs, fontsize, color, rotate, to_user, render_mode)
        self.append_content(pno, snippet)

    def insert_text(self, pno, point, text, fontsize=11, fontname="helv", fontfile=None, color=(0, 0, 0),
                    rotate=0, lineheight=None, render_mode=0):
        font = self._font(fontname, fontfile)
        runs = layout_point(font, text, point, fontsize, rotate, lineheight)
        self._write_runs(pno, font, runs, fontsize, color, rotate, render_mode)
        return len(runs)

    def insert_textbox(self, pno, rect, text, fontsize=11, fontname="helv", fontfile=None, color=(0, 0, 0),
                       align=0, rotate=0, lineheight=None, render_mode=0):
        font = self._font(fontname, fontfile)
        runs, rc = layout_textbox(font, text, rect, fontsize, align, rotate, lineheight)
        if rc >= -1e-6 and runs:
            self._write_runs(pno, font, runs, fontsize, color, rotate, render_mode)
        return rc

    def _font(self, fontname, fontfile):
        key = ("load", fontname, str(fontfile) if fontfile else None)
        f = self.font_cache.get(key)
        if f is None:
            f = load_font(fontname, fontfile)
            self.font_cache[key] = f
        return f

    # ------------------------------------------------------------------ redaction
    def add_redact_annot(self, pno, rect, text=None, fontname="Helv", fontsize=11, align=0,
                         fill=(1, 1, 1), text_color=(0, 0, 0), cross_out=True):
        pg = self.page(pno)
        ux0, uy0, ux1, uy1 = self.rect_to_user(pno, rect)
        a = DictionaryObject()
        a[NameObject("/Type")] = NameObject("/Annot")
        a[NameObject("/Subtype")] = NameObject("/Redact")
        a[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (ux0, uy0, ux1, uy1)])
        a[NameObject("/QuadPoints")] = ArrayObject([FloatObject(v) for v in (ux0, uy1, ux1, uy1, ux0, uy0, ux1, uy0)])
        a[NameObject("/F")] = NumberObject(4)
        a[NameObject("/C")] = ArrayObject([FloatObject(1), FloatObject(0), FloatObject(0)])
        if fill:
            fl = (fill,) if isinstance(fill, (int, float)) else tuple(fill)
            a[NameObject("/IC")] = ArrayObject([FloatObject(float(v)) for v in fl])
        if text:
            a[NameObject("/OverlayText")] = TextStringObject(text)
            tc = (text_color,) if isinstance(text_color, (int, float)) else tuple(text_color or (0,))
            a[NameObject("/DA")] = TextStringObject(
                f"/{fontname.lstrip('/')} {float(fontsize):g} Tf " + color_op(tc).decode().strip())
            a[NameObject("/Q")] = NumberObject(int(align))
        ref = self.writer._add_object(a)
        a[NameObject("/P")] = pg.indirect_reference if pg.indirect_reference is not None else NameObject("/null")
        annots = pg.get("/Annots")
        if annots is None:
            pg[NameObject("/Annots")] = ArrayObject([ref])
        else:
            _resolve(annots).append(ref)
        return RedactAnnot(tuple(float(v) for v in rect), ref.idnum, {"text": text, "fill": fill})

    def redact_annots(self, pno):
        pg = self.page(pno)
        out = []
        for ref in _resolve(pg.get("/Annots")) or []:
            a = _resolve(ref)
            if a is not None and a.get("/Subtype") == "/Redact":
                out.append((ref, a))
        return out

    def _annot_rects(self, a):
        qp = a.get("/QuadPoints")
        rects = []
        if qp is not None:
            q = [float(v) for v in _resolve(qp)]
            for k in range(0, len(q) - 7, 8):
                xs, ys = q[k:k + 8:2], q[k + 1:k + 8:2]
                rects.append((min(xs), min(ys), max(xs), max(ys)))
        if not rects:
            r = [float(v) for v in _resolve(a["/Rect"])]
            rects.append((min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])))
        return rects

    def apply_redactions(self, pno, images=RD.IMAGE_PIXELS, graphics=RD.LINE_ART_REMOVE_IF_COVERED,
                         text=RD.TEXT_REMOVE, annotations=True, min_overlap=0.1):
        """Remove the content under all /Redact annotations of a page (overlays are drawn by
        draw_redaction_overlays). Returns (applied, stats, user rects, overlay specs)."""
        items = self.redact_annots(pno)
        if not items:
            return False, {}, [], []
        pg = self.page(pno)
        specs = []
        all_rects = []
        for ref, a in items:
            rects = self._annot_rects(a)
            fill = [float(v) for v in _resolve(a.get("/IC"))] if a.get("/IC") is not None else None
            specs.append((rects, fill, a.get("/OverlayText"), str(a.get("/DA", "")), int(a.get("/Q", 0))))
            all_rects.extend(rects)
        first_fill = next((s[1] for s in specs if s[1]), None)
        ctx = RD.Context(self.writer, all_rects, images=images, graphics=graphics, text=text,
                         min_overlap=min_overlap, pixel_fill=tuple(first_fill) if first_fill else (1, 1, 1))
        data = self.read_contents(pno)
        interp = RD.Interpreter(ctx, self.resources(pno), RD.ID, 0)
        new = interp.run(data)
        if new is not None:
            self.set_contents(pno, new)
        # drop the redact annots, and any other annotation touching a redacted area
        redact_ids = {id(a) for _, a in items}
        keep = ArrayObject()
        removed_annots = []
        for ref in _resolve(pg.get("/Annots")) or []:
            a = _resolve(ref)
            if a is None or id(a) in redact_ids:
                continue
            if annotations and "/Rect" in a:
                r = [float(v) for v in _resolve(a["/Rect"])]
                box = (min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3]))
                if any(RD.overlaps(box, rr) for rr in all_rects):
                    removed_annots.append(a)
                    continue
            keep.append(ref)
        # popups belonging to removed annotations go too
        if removed_annots:
            rid = {id(a) for a in removed_annots}
            keep = ArrayObject([r for r in keep if id(_resolve(_resolve(r).get("/Parent"))) not in rid])
            self._remove_fields(removed_annots)
        if keep:
            pg[NameObject("/Annots")] = keep
        elif "/Annots" in pg:
            del pg["/Annots"]
        self.redacted = True
        st = ctx.stats.as_dict()
        # XFA packets duplicate the whole form (static text included) outside the page content;
        # they cannot be redacted selectively, so they are dropped (AcroForm fields remain).
        st["xfa_removed"] = self.drop_xfa()
        st["annotations_removed"] = len(removed_annots)
        return True, st, all_rects, specs

    def draw_redaction_overlays(self, pno, specs):
        """Fill boxes and overlay texts of applied redactions (drawn after verification)."""
        snippet = b""
        for rects, fill, otext, da, q in specs:
            for r in rects:
                if fill:
                    snippet += b"q\n" + color_op(fill) + b" ".join(
                        fmt(v) for v in (r[0], r[1], r[2] - r[0], r[3] - r[1])) + b" re f\nQ\n"
        if snippet:
            self.append_content(pno, snippet)
        for rects, fill, otext, da, q in specs:
            if not otext:
                continue
            fname, fsize = _da_parse(da)
            m = re.search(r"([\d.\s]+)\s(g|rg|k)\s*$", da.strip())
            col = tuple(float(v) for v in m.group(1).split()) if m else (0,)
            short = {"helv": "helv", "tiro": "tiro", "cour": "cour"}.get(fname.lower(), "helv")
            box = self.rect_from_user(pno, (min(x[0] for x in rects), min(x[1] for x in rects),
                                            max(x[2] for x in rects), max(x[3] for x in rects)))
            size = fsize
            while size >= 4:
                if self.insert_textbox(pno, box, str(otext), fontsize=size, fontname=short, color=col, align=q) >= 0:
                    break
                size -= 0.5

    def drop_xfa(self):
        af = _resolve(self.writer._root_object.get("/AcroForm"))
        if af is not None and "/XFA" in af:
            del af["/XFA"]
            if "/NeedsRendering" in self.writer._root_object:
                del self.writer._root_object["/NeedsRendering"]
            return True
        return False

    def _remove_fields(self, annots):
        root = self.writer._root_object
        af = _resolve(root.get("/AcroForm"))
        if af is None:
            return
        rid = {id(a) for a in annots}

        def prune(arr):
            out = ArrayObject()
            for ref in arr:
                f = _resolve(ref)
                if f is None or id(f) in rid:
                    continue
                kids = f.get("/Kids")
                if kids is not None:
                    nk = prune(_resolve(kids))
                    if not nk:
                        continue
                    f[NameObject("/Kids")] = nk
                out.append(ref)
            return out
        fields = af.get("/Fields")
        if fields is not None:
            af[NameObject("/Fields")] = prune(_resolve(fields))

    # ------------------------------------------------------------------ document level
    def scrub(self, metadata=True, xml_metadata=True, javascript=True, embedded_files=True,
              thumbnails=True, xfa=True, page_labels=False, outlines=False):
        w = self.writer
        if xfa:
            self.drop_xfa()
        root = w._root_object
        if metadata:
            info = DictionaryObject()
            w._info = w._add_object(info) if not hasattr(w, "_info") or w._info is None else w._info
            try:
                obj = _resolve(w._info)
                for k in list(obj.keys()):
                    del obj[k]
            except Exception:
                pass
            try:
                w.metadata = None
            except Exception:
                pass
        if xml_metadata and "/Metadata" in root:
            del root["/Metadata"]
        names = _resolve(root.get("/Names"))
        if names is not None:
            if javascript and "/JavaScript" in names:
                del names["/JavaScript"]
            if embedded_files and "/EmbeddedFiles" in names:
                del names["/EmbeddedFiles"]
        if javascript and "/OpenAction" in root:
            oa = _resolve(root["/OpenAction"])
            if isinstance(oa, dict) and oa.get("/S") == "/JavaScript":
                del root["/OpenAction"]
        if javascript and "/AA" in root:
            del root["/AA"]
        if page_labels and "/PageLabels" in root:
            del root["/PageLabels"]
        if outlines and "/Outlines" in root:
            del root["/Outlines"]
        for pg in w.pages:
            if thumbnails and "/Thumb" in pg:
                del pg["/Thumb"]
            if xml_metadata and "/Metadata" in pg:
                del pg["/Metadata"]
            if javascript and "/AA" in pg:
                del pg["/AA"]
            if embedded_files or javascript:
                annots = _resolve(pg.get("/Annots"))
                if annots:
                    keep = ArrayObject()
                    for ref in annots:
                        a = _resolve(ref)
                        st = a.get("/Subtype") if a is not None else None
                        if embedded_files and st == "/FileAttachment":
                            continue
                        act = _resolve(a.get("/A")) if a is not None else None
                        if javascript and isinstance(act, dict) and act.get("/S") == "/JavaScript":
                            continue
                        keep.append(ref)
                    pg[NameObject("/Annots")] = keep
        self.scrubbed = True

    def set_metadata(self, md):
        meta = {}
        for k, v in md.items():
            key = k if k.startswith("/") else "/" + k[:1].upper() + k[1:]
            meta[key] = "" if v is None else str(v)
        self.writer.add_metadata(meta)

    # ------------------------------------------------------------------ raw objects
    def xref_length(self):
        return len(self.writer._objects) + 1

    def obj(self, xref):
        try:
            return self.writer.get_object(int(xref))
        except Exception:
            return None

    def xref_object(self, xref, compressed=False):
        o = self.obj(xref)
        if o is None:
            return "null"
        buf = io.BytesIO()
        if isinstance(o, StreamObject):
            d = DictionaryObject({k: v for k, v in o.items()})
            d.write_to_stream(buf)
        else:
            o.write_to_stream(buf)
        s = buf.getvalue().decode("latin-1")
        if compressed:
            return re.sub(r"\s+", " ", s)
        return s

    def xref_stream(self, xref):
        o = self.obj(xref)
        return o.get_data() if isinstance(o, StreamObject) else None

    def xref_stream_raw(self, xref):
        o = self.obj(xref)
        if not isinstance(o, StreamObject):
            return None
        return getattr(o, "_data", None) if "/Filter" in o else o.get_data()

    def update_stream(self, xref, data):
        o = self.obj(xref)
        if not isinstance(o, StreamObject):
            raise ValueError(f"xref {xref} is not a stream")
        new = _stream(bytes(data))
        for k, v in o.items():
            if k not in ("/Filter", "/DecodeParms", "/Length"):
                new[NameObject(k)] = v
        self.writer._objects[int(xref) - 1] = new
        new.indirect_reference = IndirectObject(int(xref), 0, self.writer)

    # ------------------------------------------------------------------ output
    def to_bytes(self, incremental=False, garbage=0, deflate=False):
        w = self.writer
        if incremental:
            if self.redacted or getattr(self, "scrubbed", False):
                raise ValueError("incremental save would keep the redacted/scrubbed data in the file; "
                                 "save without incremental=True")
            if not w.incremental:
                raise ValueError("incremental save is not possible for this document")
            buf = io.BytesIO()
            w.write(buf)
            return buf.getvalue()
        was = w.incremental
        w.incremental = False
        try:
            if deflate:
                self._deflate()
            if garbage or self.redacted or getattr(self, "scrubbed", False):
                self._gc(garbage)
            buf = io.BytesIO()
            w.write(buf)
            return buf.getvalue()
        finally:
            w.incremental = was

    def _gc(self, garbage):
        import inspect
        f = self.writer.compress_identical_objects
        ps = inspect.signature(f).parameters
        dup = garbage >= 3
        if "remove_unreferenced" in ps:
            f(remove_duplicates=dup, remove_unreferenced=True)
        else:  # pypdf < 6
            f(remove_identicals=dup, remove_orphans=True)

    def _deflate(self):
        objs = self.writer._objects
        for i, o in enumerate(objs):
            if isinstance(o, DecodedStreamObject) and "/Filter" not in o and len(o.get_data()) > 32:
                try:
                    enc = o.flate_encode()
                except Exception:
                    continue
                enc.indirect_reference = getattr(o, "indirect_reference", None)
                objs[i] = enc
