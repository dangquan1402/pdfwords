# SPDX-License-Identifier: Apache-2.0
"""Links, annotations, form widgets and the outline (table of contents), read through PDFium.

All rectangles use pdfwords' coordinate system: PDF points, top-left origin, relative to the
CropBox of the unrotated page (pass rotated=True for the page as displayed), same as get_text().
"""
from __future__ import annotations

import ctypes as _ct

import pypdfium2.raw as R

# link kinds (same names/values as PyMuPDF's LINK_* constants)
LINK_NONE, LINK_GOTO, LINK_URI, LINK_LAUNCH, LINK_NAMED, LINK_GOTOR = 0, 1, 2, 3, 4, 5

ANNOT_TYPES = {
    1: "Text", 2: "Link", 3: "FreeText", 4: "Line", 5: "Square", 6: "Circle", 7: "Polygon",
    8: "PolyLine", 9: "Highlight", 10: "Underline", 11: "Squiggly", 12: "StrikeOut", 13: "Stamp",
    14: "Caret", 15: "Ink", 16: "Popup", 17: "FileAttachment", 18: "Sound", 19: "Movie",
    20: "Widget", 21: "Screen", 22: "PrinterMark", 23: "TrapNet", 24: "Watermark", 25: "3D",
    26: "RichMedia", 27: "XFAWidget", 28: "Redact",
}
FIELD_TYPES = {
    -1: "", 0: "", 1: "PushButton", 2: "CheckBox", 3: "RadioButton", 4: "ComboBox",
    5: "ListBox", 6: "Text", 7: "Signature",
}


# ---------------------------------------------------------------------- helpers
def _utf16(fn, *args):
    """Call a PDFium 'fill a UTF-16LE buffer, return byte length' getter."""
    n = fn(*args, None, 0)
    if n <= 2:
        return ""
    buf = _ct.create_string_buffer(n)
    at = getattr(fn, "argtypes", None)
    ptr = _ct.cast(buf, at[-2]) if at else buf
    fn(*args, ptr, n)
    return buf.raw[:n].decode("utf-16-le", "replace").rstrip("\x00")


class Transform:
    """PDF user space -> pdfwords top-left coordinates for one page."""

    def __init__(self, page_raw, crop, rotation, rotated=False):
        l, b, r, t = crop
        self.l, self.t = l, t
        self.w, self.h = abs(r - l), abs(t - b)
        self.rot = rotation if rotated else 0

    def pt(self, x, y):
        x, y = x - self.l, self.t - y
        r, w, h = self.rot, self.w, self.h
        if r == 90:
            return (h - y, x)
        if r == 180:
            return (w - x, h - y)
        if r == 270:
            return (y, w - x)
        return (x, y)

    def rect(self, l, b, r, t):
        a, c = self.pt(l, b), self.pt(r, t)
        return (min(a[0], c[0]), min(a[1], c[1]), max(a[0], c[0]), max(a[1], c[1]))


def _dest(pdf_raw, dest, page_xf):
    """(page index, target point or None) of an explicit destination."""
    pno = R.FPDFDest_GetDestPageIndex(pdf_raw, dest)
    hx, hy, hz = _ct.c_int(), _ct.c_int(), _ct.c_int()
    x, y, z = _ct.c_float(), _ct.c_float(), _ct.c_float()
    to = None
    zoom = 0.0
    if R.FPDFDest_GetLocationInPage(dest, hx, hy, hz, x, y, z):
        if hz.value:
            zoom = z.value
        if hx.value or hy.value:
            xf = page_xf(pno) if pno >= 0 else None
            if xf is not None:
                tx, ty = xf.pt(x.value if hx.value else xf.l, y.value if hy.value else xf.t)
                to = (tx if hx.value else 0.0, ty if hy.value else 0.0)
    return pno, to, zoom


def _action(pdf_raw, action, page_xf):
    out = {}
    at = R.FPDFAction_GetType(action)
    if at == R.PDFACTION_URI:
        n = R.FPDFAction_GetURIPath(pdf_raw, action, None, 0)
        uri = ""
        if n > 0:
            buf = _ct.create_string_buffer(n)
            R.FPDFAction_GetURIPath(pdf_raw, action, buf, n)
            uri = buf.raw[:n].rstrip(b"\x00").decode("utf-8", "replace")
        out.update(kind=LINK_URI, uri=uri)
    elif at in (R.PDFACTION_GOTO, R.PDFACTION_EMBEDDEDGOTO):
        d = R.FPDFAction_GetDest(pdf_raw, action)
        out["kind"] = LINK_GOTO
        if d:
            pno, to, zoom = _dest(pdf_raw, d, page_xf)
            out.update(page=pno, to=to, zoom=zoom)
    elif at in (R.PDFACTION_REMOTEGOTO, R.PDFACTION_LAUNCH):
        n = R.FPDFAction_GetFilePath(action, None, 0)
        f = ""
        if n > 0:
            buf = _ct.create_string_buffer(n)
            R.FPDFAction_GetFilePath(action, buf, n)
            f = buf.raw[:n].rstrip(b"\x00").decode("utf-8", "replace")
        out.update(kind=LINK_GOTOR if at == R.PDFACTION_REMOTEGOTO else LINK_LAUNCH, file=f)
    else:
        out["kind"] = LINK_NONE
    return out


def _page_xf_factory(doc, rotated):
    if getattr(doc, "_xf_gen", None) != doc._gen:
        doc._xf_cache, doc._xf_gen = {}, doc._gen
    cache = doc._xf_cache.setdefault(rotated, {})

    def get(pno):
        if pno not in cache:
            try:
                p = doc._pdf[pno]
            except Exception:
                cache[pno] = None
            else:
                from .chars import crop_box
                cache[pno] = Transform(p.raw, crop_box(p), p.get_rotation() % 360, rotated)
                p.close()
        return cache[pno]
    return get


# ---------------------------------------------------------------------- links
def link_annotations(page, rotated=False):
    """Link annotations of a page: list of dicts
    {kind, from, uri | page,to,zoom | file, id, xref:0}. `from` is the link rectangle."""
    from .chars import crop_box
    pg = page._page
    pdf_raw = page.parent._pdf.raw
    xf = Transform(pg.raw, crop_box(pg), page.rotation, rotated)
    page_xf = _page_xf_factory(page.parent, rotated)
    out = []
    pos = _ct.c_int(0)
    link = R.FPDF_LINK()
    rc = R.FS_RECTF()
    k = 0
    while R.FPDFLink_Enumerate(pg.raw, pos, link):
        if not R.FPDFLink_GetAnnotRect(link, rc):
            continue
        d = {"from": xf.rect(rc.left, rc.bottom, rc.right, rc.top)}
        dest = R.FPDFLink_GetDest(pdf_raw, link)
        if dest:
            pno, to, zoom = _dest(pdf_raw, dest, page_xf)
            d.update(kind=LINK_GOTO, page=pno, to=to, zoom=zoom)
        else:
            act = R.FPDFLink_GetAction(link)
            if not act:
                continue
            d.update(_action(pdf_raw, act, page_xf))
        # quads (QuadPoints) when the link is a multi-line area
        n = R.FPDFLink_CountQuadPoints(link)
        if n > 0:
            qs = []
            q = R.FS_QUADPOINTSF()
            for j in range(n):
                if R.FPDFLink_GetQuadPoints(link, j, q):
                    qs.append((xf.pt(q.x1, q.y1), xf.pt(q.x2, q.y2), xf.pt(q.x3, q.y3), xf.pt(q.x4, q.y4)))
            if qs:
                d["quads"] = qs
        d["id"] = f"link-{k}"
        k += 1
        out.append(d)
    return out


def web_links(page, rotated=False):
    """URLs written in the page text (auto-detected by PDFium), as
    {kind: LINK_URI, from, uri, rects, auto: True, char_range}."""
    from .chars import crop_box
    pg = page._page
    xf = Transform(pg.raw, crop_box(pg), page.rotation, rotated)
    tp = pg.get_textpage()
    out = []
    try:
        wl = R.FPDFLink_LoadWebLinks(tp.raw)
        if not wl:
            return out
        try:
            for i in range(R.FPDFLink_CountWebLinks(wl)):
                n = R.FPDFLink_GetURL(wl, i, None, 0)
                buf = (_ct.c_ushort * max(n, 1))()
                R.FPDFLink_GetURL(wl, i, buf, n)
                uri = bytes(buf)[: 2 * max(n - 1, 0)].decode("utf-16-le", "replace")
                rects = []
                l, t, r, b = (_ct.c_double() for _ in range(4))
                for j in range(R.FPDFLink_CountRects(wl, i)):
                    if R.FPDFLink_GetRect(wl, i, j, l, t, r, b):
                        rects.append(xf.rect(l.value, b.value, r.value, t.value))
                if not rects:
                    continue
                s, c = _ct.c_int(), _ct.c_int()
                R.FPDFLink_GetTextRange(wl, i, s, c)
                fr = (min(x[0] for x in rects), min(x[1] for x in rects),
                      max(x[2] for x in rects), max(x[3] for x in rects))
                out.append({"kind": LINK_URI, "from": fr, "uri": uri, "rects": rects, "auto": True,
                            "char_range": (s.value, s.value + c.value)})
        finally:
            R.FPDFLink_CloseWebLinks(wl)
    finally:
        tp.close()
    return out


def _iou_cover(a, b):
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = max((b[2] - b[0]) * (b[3] - b[1]), 1e-9)
    return ix * iy / area


def get_links(page, web=False, rotated=False):
    links = link_annotations(page, rotated)
    if web:
        for w in web_links(page, rotated):
            # an explicit link annotation over the same text wins
            if any(lk.get("uri") and _iou_cover(lk["from"], w["rects"][0]) > 0.5 for lk in links):
                continue
            links.append(w)
    return links


# ---------------------------------------------------------------------- annotations
def _annot_dict(page, annot, xf, form=None):
    st = R.FPDFAnnot_GetSubtype(annot)
    rc = R.FS_RECTF()
    rect = xf.rect(rc.left, rc.bottom, rc.right, rc.top) if R.FPDFAnnot_GetRect(annot, rc) else None
    sv = R.FPDFAnnot_GetStringValue
    is_widget = st == R.FPDF_ANNOT_WIDGET
    d = {"type": ANNOT_TYPES.get(st, "Unknown"), "rect": rect,
         "contents": _utf16(sv, annot, b"Contents"),
         "author": "" if is_widget else _utf16(sv, annot, b"T"),   # a widget's /T is its field name
         "subject": _utf16(sv, annot, b"Subj"), "id": _utf16(sv, annot, b"NM"),
         "modified": _utf16(sv, annot, b"M"), "flags": R.FPDFAnnot_GetFlags(annot)}
    n = R.FPDFAnnot_CountAttachmentPoints(annot)
    if n:
        q = R.FS_QUADPOINTSF()
        qs = []
        for j in range(n):
            if R.FPDFAnnot_GetAttachmentPoints(annot, j, q):
                qs.append((xf.pt(q.x1, q.y1), xf.pt(q.x2, q.y2), xf.pt(q.x3, q.y3), xf.pt(q.x4, q.y4)))
        d["quads"] = qs
    for key, ct, pk in (("stroke", R.FPDFANNOT_COLORTYPE_Color, b"C"),
                        ("fill", R.FPDFANNOT_COLORTYPE_InteriorColor, b"IC")):
        r, g, b, a = (_ct.c_uint() for _ in range(4))
        if R.FPDFAnnot_HasKey(annot, pk) and R.FPDFAnnot_GetColor(annot, ct, r, g, b, a):
            d[key] = (r.value / 255, g.value / 255, b.value / 255)
    if form is not None and is_widget:
        d.update(_widget_fields(form, annot))
    return d


def _widget_fields(form, annot):
    ft = R.FPDFAnnot_GetFormFieldType(form, annot)
    d = {"field_name": _utf16(R.FPDFAnnot_GetFormFieldName, form, annot),
         "field_label": _utf16(R.FPDFAnnot_GetFormFieldAlternateName, form, annot),
         "field_type": FIELD_TYPES.get(ft, str(ft)),
         "field_value": _utf16(R.FPDFAnnot_GetFormFieldValue, form, annot),
         "field_flags": R.FPDFAnnot_GetFormFieldFlags(form, annot)}
    if ft in (R.FPDF_FORMFIELD_CHECKBOX, R.FPDF_FORMFIELD_RADIOBUTTON):
        d["checked"] = bool(R.FPDFAnnot_IsChecked(form, annot))
        d["on_state"] = _utf16(R.FPDFAnnot_GetFormFieldExportValue, form, annot)
    if ft in (R.FPDF_FORMFIELD_COMBOBOX, R.FPDF_FORMFIELD_LISTBOX):
        n = R.FPDFAnnot_GetOptionCount(form, annot)
        d["choices"] = [_utf16(R.FPDFAnnot_GetOptionLabel, form, annot, i) for i in range(max(n, 0))]
    return d


def annots(page, types=None, rotated=False, _pdfpage=None, _form=None):
    """All annotations of a page (links and widgets included unless filtered by `types`, a
    collection of type names such as {"Highlight", "Text"})."""
    from .chars import crop_box
    pg = _pdfpage or page._page
    xf = Transform(pg.raw, crop_box(pg), page.rotation, rotated)
    out = []
    for i in range(R.FPDFPage_GetAnnotCount(pg.raw)):
        a = R.FPDFPage_GetAnnot(pg.raw, i)
        if not a:
            continue
        try:
            st = R.FPDFAnnot_GetSubtype(a)
            if types is not None and ANNOT_TYPES.get(st) not in types:
                continue
            d = _annot_dict(page, a, xf, _form)
            d["index"] = i
            out.append(d)
        finally:
            R.FPDFPage_CloseAnnot(a)
    return out


def widgets(page, rotated=False):
    """Form fields on the page: annots(types={"Widget"}) plus field name/type/value/checked/choices."""
    doc = page.parent
    fdoc = doc._forms_pdf()
    if fdoc.formenv is None:      # no AcroForm (pypdfium2 creates no form environment)
        return []
    p = fdoc[page.number]
    try:
        return annots(page, types={"Widget"}, rotated=rotated, _pdfpage=p, _form=fdoc.formenv.raw)
    finally:
        p.close()


# ---------------------------------------------------------------------- outline
def get_toc(doc, simple=True):
    """Outline as [[level, title, page, (dest dict if not simple)], ...] with 1-based level and
    page (page = -1 when the target is not a page of this document), like PyMuPDF."""
    pdf = doc._pdf
    out = []
    for bm in pdf.get_toc():
        dest = R.FPDFBookmark_GetDest(pdf.raw, bm.raw)
        uri = ""
        if not dest:
            act = R.FPDFBookmark_GetAction(bm.raw)
            if act:
                if R.FPDFAction_GetType(act) == R.PDFACTION_URI:
                    uri = _action(pdf.raw, act, lambda _p: None).get("uri", "")
                else:
                    dest = R.FPDFAction_GetDest(pdf.raw, act)
        pno = R.FPDFDest_GetDestPageIndex(pdf.raw, dest) if dest else -1
        row = [bm.level + 1, bm.get_title(), pno + 1 if pno >= 0 else -1]
        if not simple:
            info = {"kind": LINK_GOTO if dest else (LINK_URI if uri else LINK_NONE),
                    "collapse": bm.get_count() < 0}
            if uri:
                info["uri"] = uri
            if dest:
                _p, to, zoom = _dest(pdf.raw, dest, _page_xf_factory(doc, False))
                info.update(page=pno, to=to, zoom=zoom)
            row.append(info)
        out.append(row)
    return out


# ---------------------------------------------------------------------- span url splitting
def char_urls(chars_boxes, links, min_cover=0.5, texts=None):
    """For each char bbox return the url of the link covering most of it ('' if none).
    links: [(rect, url)]. With `texts` (the chars), whitespace only belongs to a link when the
    link continues on both sides."""
    out = []
    if chars_boxes and links:   # only links touching these chars matter
        x0 = min(b[0] for b in chars_boxes) - 1
        y0 = min(b[1] for b in chars_boxes) - 1
        x1 = max(b[2] for b in chars_boxes) + 1
        y1 = max(b[3] for b in chars_boxes) + 1
        links = [lk for lk in links if lk[0][0] <= x1 and lk[0][2] >= x0 and lk[0][1] <= y1 and lk[0][3] >= y0]
    if not links:
        return [""] * len(chars_boxes)
    for bb in chars_boxes:
        w = bb[2] - bb[0]
        h = bb[3] - bb[1]
        best, url = 0.0, ""
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        for r, u in links:
            if w <= 1e-6 or h <= 1e-6:
                cov = 1.0 if r[0] <= cx <= r[2] and r[1] <= cy <= r[3] else 0.0
            else:
                ix = min(bb[2], r[2]) - max(bb[0], r[0])
                iy = min(bb[3], r[3]) - max(bb[1], r[1])
                cov = (ix * iy) / (w * h) if ix > 0 and iy > 0 else 0.0
            if cov > best:
                best, url = cov, u
        out.append(url if best >= min_cover else "")
    if texts is not None:
        raw = list(out)
        for k, t in enumerate(texts):
            if not t.strip():
                prev = raw[k - 1] if k > 0 else ""
                nxt = raw[k + 1] if k + 1 < len(raw) else ""
                out[k] = prev if prev and prev == nxt else ""
    return out


def link_url(lk):
    """The url a link resolves to: the URI, or '#page=N' (1-based) for internal links."""
    if lk.get("uri"):
        return lk["uri"]
    if lk.get("kind") == LINK_GOTO and lk.get("page") is not None and lk["page"] >= 0:
        return f"#page={lk['page'] + 1}"
    if lk.get("file"):
        return lk["file"]
    return ""

