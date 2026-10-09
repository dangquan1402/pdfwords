# SPDX-License-Identifier: Apache-2.0
"""Images and vector drawings on a page, with bounding boxes in pdfwords coordinates.

    page.get_images()      # [{"number", "bbox", "quad", "width", "height", "bpc", "colorspace", ...}]
    page.get_image(0)      # PIL.Image of image #0 (decoded pixels; rendered=True applies masks)
    page.get_drawings()    # [{"items": [("l", p1, p2), ("re", rect), ("c", p1, c1, c2, p2)], "type": "s"|"f"|"fs",
                           #   "color", "fill", "width", "rect", "closePath", "even_odd", ...}]

Objects inside Form XObjects are included (their matrices are composed, so boxes are on the
page). Coordinates: PDF points, top-left origin, unrotated page (rotated=True: as displayed).
"""
from __future__ import annotations

import ctypes as _ct
import hashlib

import pypdfium2.raw as R

from .annots import Transform

_CS = {1: ("DeviceGray", 1), 2: ("DeviceRGB", 3), 3: ("DeviceCMYK", 4), 4: ("CalGray", 1), 5: ("CalRGB", 3),
       6: ("Lab", 3), 7: ("ICCBased", 0), 8: ("Separation", 1), 9: ("DeviceN", 0), 10: ("Indexed", 1),
       11: ("Pattern", 0)}


def _mat(o):
    m = R.FS_MATRIX()
    if R.FPDFPageObj_GetMatrix(o, m):
        return (m.a, m.b, m.c, m.d, m.e, m.f)
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m, n):
    """m then n (row-vector convention, as in PDF: [x y 1] * m * n)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _ap(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def walk(page_raw):
    """Yield (object, ctm to page space excluding the object's own matrix, depth, seqno) in
    painting order, descending into Form XObjects."""
    seq = [0]

    def rec(container, count, get, ctm, depth):
        for i in range(count(container)):
            o = get(container, i)
            if not o:
                continue
            yield o, ctm, depth, seq[0]
            seq[0] += 1
            if R.FPDFPageObj_GetType(o) == R.FPDF_PAGEOBJ_FORM and depth < 12:
                yield from rec(o, R.FPDFFormObj_CountObjects, R.FPDFFormObj_GetObject, _mul(_mat(o), ctm), depth + 1)

    yield from rec(page_raw, R.FPDFPage_CountObjects, R.FPDFPage_GetObject, _ID, 0)


def _xf(page, rotated):
    from .chars import crop_box
    pg = page._page
    return Transform(pg.raw, crop_box(pg), page.rotation, rotated)


def _str(fn, *args):
    n = fn(*args, None, 0)
    if n <= 1:
        return ""
    buf = _ct.create_string_buffer(n)
    fn(*args, buf, n)
    return buf.value.decode("latin-1")


def _box(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def get_images(page, *, rotated=False, hashes=False):
    pg = page._page
    xf = _xf(page, rotated)
    out = []
    for o, ctm, depth, seq in walk(pg.raw):
        if R.FPDFPageObj_GetType(o) != R.FPDF_PAGEOBJ_IMAGE:
            continue
        m = _mul(_mat(o), ctm)          # unit square -> page user space
        ul, ur, ll, lr = (xf.pt(*_ap(m, x, y)) for x, y in ((0, 1), (1, 1), (0, 0), (1, 0)))
        md = R.FPDF_IMAGEOBJ_METADATA()
        R.FPDFImageObj_GetImageMetadata(o, pg.raw, md)
        cs_name, ncomp = _CS.get(md.colorspace, ("", 0))
        filters = [_str(R.FPDFImageObj_GetImageFilter, o, k) for k in range(R.FPDFImageObj_GetImageFilterCount(o))]
        size = R.FPDFImageObj_GetImageDataRaw(o, None, 0)
        bpc = md.bits_per_pixel // ncomp if ncomp and md.bits_per_pixel else md.bits_per_pixel
        d = {"number": len(out), "bbox": _box((ul, ur, ll, lr)), "quad": (ul, ur, ll, lr),
             "width": md.width, "height": md.height, "bpc": bpc, "bits_per_pixel": md.bits_per_pixel,
             "colorspace": ncomp, "cs_name": cs_name, "xres": round(md.horizontal_dpi), "yres": round(md.vertical_dpi),
             "filters": filters, "size": size, "mcid": md.marked_content_id, "depth": depth, "seqno": seq}
        if hashes and size:
            buf = (_ct.c_ubyte * size)()
            R.FPDFImageObj_GetImageDataRaw(o, buf, size)
            d["digest"] = hashlib.md5(bytes(buf)).hexdigest()
        out.append(d)
    return out


def get_image(page, number, rendered=False):
    """Decoded pixels of image `number` (as numbered by get_images) as a PIL.Image.
    rendered=True applies the soft mask / image mask and the page's rendering of it."""
    import pypdfium2 as pdfium
    pg = page._page
    k = 0
    for o, _ctm, _d, _s in walk(pg.raw):
        if R.FPDFPageObj_GetType(o) != R.FPDF_PAGEOBJ_IMAGE:
            continue
        if k == number:
            bm = (R.FPDFImageObj_GetRenderedBitmap(page.parent._pdf.raw, pg.raw, o) if rendered
                  else R.FPDFImageObj_GetBitmap(o))
            if not bm:
                raise ValueError(f"PDFium cannot decode image {number}")
            return pdfium.PdfBitmap.from_raw(bm).to_pil()
        k += 1
    raise IndexError(f"page {page.number} has {k} images")


def _rgba(fn, o):
    r, g, b, a = (_ct.c_uint() for _ in range(4))
    if fn(o, r, g, b, a):
        return (r.value / 255, g.value / 255, b.value / 255), a.value / 255
    return None, 1.0


def _is_rect(segs):
    """segments [(type, (x, y)), ...] of one closed subpath with 4 axis-aligned corners."""
    pts = [p for _, p in segs]
    if len(pts) == 5 and abs(pts[0][0] - pts[4][0]) < 1e-6 and abs(pts[0][1] - pts[4][1]) < 1e-6:
        pts = pts[:4]
    if len(pts) != 4 or any(t == R.FPDF_SEGMENT_BEZIERTO for t, _ in segs):
        return None
    for i in range(4):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % 4]
        if abs(x0 - x1) > 1e-6 and abs(y0 - y1) > 1e-6:
            return None
    return _box(pts)


def get_drawings(page, *, rotated=False, clip=None):
    """Vector paths (lines, rectangles, curves) with stroke/fill colours and widths, in painting
    order. clip: only paths whose rect intersects this box."""
    pg = page._page
    xf = _xf(page, rotated)
    out = []
    has_dash = hasattr(R, "FPDFPageObj_GetDashCount")
    for o, ctm, depth, seq in walk(pg.raw):
        if R.FPDFPageObj_GetType(o) != R.FPDF_PAGEOBJ_PATH:
            continue
        m = _mul(_mat(o), ctm)
        fillmode, stroke = _ct.c_int(), _ct.c_int()
        R.FPDFPath_GetDrawMode(o, fillmode, stroke)
        if not fillmode.value and not stroke.value:
            continue   # clipping-only / invisible path
        n = R.FPDFPath_CountSegments(o)
        subpaths, cur, closed_any = [], [], False
        for k in range(n):
            s = R.FPDFPath_GetPathSegment(o, k)
            x, y = _ct.c_float(), _ct.c_float()
            R.FPDFPathSegment_GetPoint(s, x, y)
            t = R.FPDFPathSegment_GetType(s)
            p = xf.pt(*_ap(m, x.value, y.value))
            if t == R.FPDF_SEGMENT_MOVETO and cur:
                subpaths.append(cur)
                cur = []
            cur.append((t, p))
            if R.FPDFPathSegment_GetClose(s):
                closed_any = True
                cur.append(("close", cur[0][1]))
                subpaths.append(cur)
                cur = []
        if cur:
            subpaths.append(cur)
        items, allpts = [], []
        for sp in subpaths:
            segs = [(t, p) for t, p in sp if t != "close"]
            closed = sp[-1][0] == "close"
            allpts.extend(p for _, p in segs)
            r = _is_rect(segs + ([(R.FPDF_SEGMENT_LINETO, segs[0][1])] if closed and len(segs) == 4 else []))
            if r is not None and (closed or len(segs) == 5):
                items.append(("re", r))
                continue
            k = 1
            pts = segs
            while k < len(pts):
                t, p = pts[k]
                if t == R.FPDF_SEGMENT_BEZIERTO and k + 2 < len(pts):
                    items.append(("c", pts[k - 1][1], p, pts[k + 1][1], pts[k + 2][1]))
                    k += 3
                    continue
                items.append(("l", pts[k - 1][1], p))
                k += 1
            if closed and segs and len(segs) > 1 and segs[-1][1] != segs[0][1]:
                items.append(("l", segs[-1][1], segs[0][1]))
        if not allpts:
            continue
        rect = _box(allpts)
        if clip is not None and (rect[2] < clip[0] or rect[0] > clip[2] or rect[3] < clip[1] or rect[1] > clip[3]):
            continue
        color, sa = _rgba(R.FPDFPageObj_GetStrokeColor, o) if stroke.value else (None, 1.0)
        fill, fa = _rgba(R.FPDFPageObj_GetFillColor, o) if fillmode.value else (None, 1.0)
        w = _ct.c_float()
        width = None
        if stroke.value and R.FPDFPageObj_GetStrokeWidth(o, w):
            width = w.value * (abs(m[0] * m[3] - m[1] * m[2]) ** 0.5)
        d = {"items": items, "type": ("f" if fillmode.value else "") + ("s" if stroke.value else ""),
             "rect": rect, "color": color, "fill": fill, "width": width,
             "even_odd": fillmode.value == R.FPDF_FILLMODE_ALTERNATE, "closePath": closed_any,
             "stroke_opacity": sa if stroke.value else None, "fill_opacity": fa if fillmode.value else None,
             "lineCap": R.FPDFPageObj_GetLineCap(o) if stroke.value else None,
             "lineJoin": R.FPDFPageObj_GetLineJoin(o) if stroke.value else None,
             "seqno": seq, "depth": depth}
        if has_dash and stroke.value:
            nd = R.FPDFPageObj_GetDashCount(o)
            if nd > 0:
                arr = (_ct.c_float * nd)()
                R.FPDFPageObj_GetDashArray(o, arr, nd)
                d["dashes"] = [v for v in arr]
        out.append(d)
    return out


def lines_and_rects(drawings, min_len=2.0, max_thickness=2.5):
    """Split drawings into straight horizontal / vertical rule segments (x0, y0, x1, y1), as used
    by table detection and underline detection: stroked lines, thin filled rectangles and the
    edges of stroked rectangles."""
    h, v = [], []
    for d in drawings:
        for it in d["items"]:
            if it[0] == "l":
                (x0, y0), (x1, y1) = it[1], it[2]
                if abs(y0 - y1) < 0.5 and abs(x1 - x0) >= min_len and "s" in d["type"]:
                    h.append((min(x0, x1), y0, max(x0, x1), y1))
                elif abs(x0 - x1) < 0.5 and abs(y1 - y0) >= min_len and "s" in d["type"]:
                    v.append((x0, min(y0, y1), x1, max(y0, y1)))
            elif it[0] == "re":
                x0, y0, x1, y1 = it[1]
                w, hh = x1 - x0, y1 - y0
                if "f" in d["type"] and hh <= max_thickness and w >= min_len:
                    h.append((x0, (y0 + y1) / 2, x1, (y0 + y1) / 2))
                elif "f" in d["type"] and w <= max_thickness and hh >= min_len:
                    v.append(((x0 + x1) / 2, y0, (x0 + x1) / 2, y1))
                elif "s" in d["type"] or "f" in d["type"]:
                    if "f" in d["type"] and d.get("fill") and min(d["fill"]) > 0.95 and "s" not in d["type"]:
                        continue   # white background boxes are not rules
                    h += [(x0, y0, x1, y0), (x0, y1, x1, y1)]
                    v += [(x0, y0, x0, y1), (x1, y0, x1, y1)]
    return h, v
