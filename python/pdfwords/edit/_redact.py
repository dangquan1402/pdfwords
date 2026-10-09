# SPDX-License-Identifier: Apache-2.0
"""True redaction by content-stream rewriting.

The page content (all /Contents streams, concatenated) is replayed with a small PDF
interpreter that tracks the graphics state (CTM, q/Q), the text state (font, size, Tc, Tw,
Tz, TL, Ts, Tm/Tlm) and the current path.  For every glyph shown by Tj/TJ/'/" the glyph box
is computed in page space; glyphs that fall inside a redaction rectangle are deleted from the
string and replaced by the equivalent TJ displacement, so every remaining glyph keeps its
exact position.  Form XObjects that are hit are copied, rewritten and re-referenced (the
original, possibly shared, form is left alone).  Images are removed or have the covered
pixels overwritten; vector paths are removed according to the `graphics` option.  Marked
content /ActualText, /Alt and /E entries around removed glyphs are dropped as well.
"""
from __future__ import annotations

import math

from pypdf.generic import DecodedStreamObject, NameObject, NumberObject, StreamObject

from ._fonts import FontModel
from ._lexer import Name, Op, PdfStr, parse, serialize

IMAGE_NONE, IMAGE_REMOVE, IMAGE_PIXELS = 0, 1, 2
LINE_ART_NONE, LINE_ART_REMOVE_IF_COVERED, LINE_ART_REMOVE_IF_TOUCHED = 0, 1, 2
TEXT_REMOVE, TEXT_NONE = 0, 1

ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_PATH_CONS = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}
_STROKES = {b"S", b"s", b"B", b"B*", b"b", b"b*"}
_MAX_DEPTH = 12


def mmul(m, n):
    """m then n (row-vector convention, as in PDF)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def mapply(m, x, y):
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def minv(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def aabb(m, x0, y0, x1, y1):
    pts = [mapply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def overlaps(a, b):
    """Positive-area intersection of two (x0, y0, x1, y1) boxes."""
    return min(a[2], b[2]) - max(a[0], b[0]) > 1e-9 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-9


class GState:
    __slots__ = ("ctm", "font", "fs", "tc", "tw", "th", "tl", "rise", "lw")

    def __init__(self, ctm=ID):
        self.ctm, self.font, self.fs = ctm, None, 0.0
        self.tc = self.tw = self.tl = self.rise = 0.0
        self.th, self.lw = 1.0, 1.0

    def copy(self):
        g = GState.__new__(GState)
        for k in GState.__slots__:
            setattr(g, k, getattr(self, k))
        return g


class Stats:
    def __init__(self):
        self.glyphs = 0
        self.images = 0
        self.image_pixels = 0
        self.paths = 0
        self.forms = 0
        self.inexact_fonts = set()

    def as_dict(self):
        return {"glyphs_removed": self.glyphs, "images_removed": self.images, "images_blanked": self.image_pixels,
                "paths_removed": self.paths, "forms_rewritten": self.forms,
                "inexact_fonts": sorted(self.inexact_fonts)}


class Context:
    def __init__(self, writer, rects, *, images=IMAGE_PIXELS, graphics=LINE_ART_REMOVE_IF_COVERED,
                 text=TEXT_REMOVE, min_overlap=0.1, pixel_fill=(1.0, 1.0, 1.0)):
        self.writer = writer
        self.rects = [tuple(map(float, r)) for r in rects]
        self.images, self.graphics, self.text = images, graphics, text
        self.min_overlap = min_overlap
        self.pixel_fill = pixel_fill
        self.stats = Stats()
        self.fonts = {}
        self.ubox = (min(r[0] for r in self.rects), min(r[1] for r in self.rects),
                     max(r[2] for r in self.rects), max(r[3] for r in self.rects)) if self.rects else None

    def font(self, resources, name):
        f = None
        try:
            fonts = resources.get("/Font") if resources is not None else None
            f = fonts.get_object().get("/" + name) if fonts is not None else None
        except Exception:
            f = None
        key = (f.idnum, f.generation) if hasattr(f, "idnum") else id(f)
        m = self.fonts.get(key)
        if m is None:
            try:
                m = FontModel(f)
            except Exception:
                m = FontModel(None)
                m.exact = False
            self.fonts[key] = m
        return m

    def glyph_hit(self, box):
        """Is a glyph box (page space) to be removed?

        Along the writing direction (x) the rectangle must cover at least `min_overlap` of the
        glyph width (or half of the rectangle lies on the glyph: thin rectangles); across it (y)
        it must cover half the glyph height or half of the rectangle height. The second rule
        keeps glyphs of an adjacent, tightly spaced line from being caught by a rectangle that
        only grazes them, while a tight box around lowercase letters or a thin strike-through
        rectangle still removes the line it sits on."""
        fr = self.min_overlap
        gw, gh = box[2] - box[0], box[3] - box[1]
        for r in self.rects:
            iw = min(box[2], r[2]) - max(box[0], r[0])
            ih = min(box[3], r[3]) - max(box[1], r[1])
            rw, rh = r[2] - r[0], r[3] - r[1]
            if gw > 1e-9:
                okx = iw > 1e-9 and (iw >= fr * gw or iw >= 0.5 * rw)
            else:
                okx = r[0] <= box[0] <= r[2]
            if gh > 1e-9:
                oky = ih > 1e-9 and (ih >= 0.5 * gh or ih >= 0.5 * rh)
            else:
                oky = r[1] <= box[1] <= r[3]
            if okx and oky:
                return True
        return False

    def any_overlap(self, box):
        return any(overlaps(box, r) for r in self.rects)

    def covered(self, box):
        return any(r[0] <= box[0] and r[1] <= box[1] and box[2] <= r[2] and box[3] <= r[3] for r in self.rects)


def _resolve(o):
    return o.get_object() if o is not None and hasattr(o, "get_object") else o


def add_resource(resources, cat, ref, prefix):
    """Add `ref` under a fresh name in resources[cat]; returns the new name (no slash)."""
    sub = resources.get(cat)
    if sub is None:
        from pypdf.generic import DictionaryObject
        sub = DictionaryObject()
        resources[NameObject(cat)] = sub
    sub = _resolve(sub)
    i = 1
    while f"/{prefix}{i}" in sub:
        i += 1
    name = f"{prefix}{i}"
    sub[NameObject("/" + name)] = ref
    return name


class Interpreter:
    def __init__(self, ctx: Context, resources, ctm, depth=0, gstate=None):
        self.ctx = ctx
        self.res = resources
        self.depth = depth
        if gstate is not None:  # a form XObject starts from the invoking graphics state
            self.gs = gstate.copy()
            self.gs.ctm = ctm
        else:
            self.gs = GState(ctm)
        self.stack = []
        self.tm = self.tlm = ID
        self.path_start = None
        self.path_pts = []
        self.clip = False
        self.mc = []          # open marked-content ops (Op or None)
        self.changed = False

    # ------------------------------------------------------------------ text
    def _font(self):
        f = self.gs.font
        if f is None:
            f = self.ctx.font(None, "")
            self.gs.font = f
        return f

    def show(self, op, items):
        gs, ctx = self.gs, self.ctx
        font = self._font()
        fs, th, tc, tw, rise = gs.fs, gs.th, gs.tc, gs.tw, gs.rise
        vert = font.vertical
        asc, dsc = font.asc, font.desc
        out = []
        removed = 0
        tm = self.tm
        ctm = gs.ctm
        scale = (fs * th, 0.0, 0.0, fs, 0.0, rise)

        def add_num(n):
            if out and isinstance(out[-1], float):
                out[-1] += n
            else:
                out.append(float(n))

        for it in items:
            if isinstance(it, (int, float)) and not isinstance(it, bool):
                n = float(it)
                if vert:
                    tm = mmul((1, 0, 0, 1, 0, -n / 1000.0 * fs), tm)
                else:
                    tm = mmul((1, 0, 0, 1, -n / 1000.0 * fs * th, 0), tm)
                add_num(n)
                continue
            if not isinstance(it, bytes):
                continue
            buf = bytearray()
            pos = 0
            for code, nb in font.split(it):
                raw = it[pos:pos + nb]
                pos += nb
                wsp = tw if (nb == 1 and code == 32) else 0.0
                trm = mmul(scale, mmul(tm, ctm))
                if vert:
                    w1 = font.vwidth(code) / 1000.0
                    box = aabb(trm, -0.5, w1, 0.5, 0.0)
                    disp = w1 * fs + tc + wsp
                    adv = (1, 0, 0, 1, 0, disp)
                else:
                    w0 = font.width(code) / 1000.0
                    box = aabb(trm, 0.0, dsc, w0, asc)
                    disp = w0 * fs + tc + wsp
                    adv = (1, 0, 0, 1, disp * th, 0)
                if ctx.glyph_hit(box):
                    if buf:
                        out.append(PdfStr(bytes(buf), hex=getattr(it, "hex", False)))
                        buf = bytearray()
                    if fs:
                        add_num(-disp / fs * 1000.0)
                    removed += 1
                else:
                    buf += raw
                tm = mmul(adv, tm)
            if buf:
                out.append(PdfStr(bytes(buf), hex=getattr(it, "hex", False)))
        self.tm = tm
        if removed:
            if not font.exact:
                ctx.stats.inexact_fonts.add(font.base or "?")
            ctx.stats.glyphs += removed
            self._scrub_marked_content()
            return out
        return None

    def _scrub_marked_content(self):
        for o in self.mc:
            if o is None or len(o.args) < 2 or not isinstance(o.args[1], dict):
                continue
            d = o.args[1]
            for k in ("ActualText", "Alt", "E"):
                if k in d:
                    del d[k]
                    o.changed = True
                    self.changed = True

    # ------------------------------------------------------------------ xobjects
    def do_xobject(self, op):
        ctx = self.ctx
        if not op.args or not isinstance(op.args[0], Name):
            return
        name = op.args[0]
        xd = _resolve(self.res.get("/XObject")) if self.res is not None else None
        ref = xd.get("/" + name) if xd is not None else None
        xo = _resolve(ref)
        if not isinstance(xo, StreamObject):
            return
        st = xo.get("/Subtype")
        ctm = self.gs.ctm
        if st == "/Image":
            box = aabb(ctm, 0, 0, 1, 1)
            if ctx.images == IMAGE_NONE or not ctx.any_overlap(box):
                return
            if ctx.images == IMAGE_PIXELS:
                new = blank_image_pixels(ctx, xo, ctm)
                if new is not None:
                    nref = ctx.writer._add_object(new)
                    op.args[0] = Name(add_resource(self.res, "/XObject", nref, "PWRedImg"))
                    op.changed = self.changed = True
                    ctx.stats.image_pixels += 1
                    return
            op.op = None
            self.changed = True
            ctx.stats.images += 1
        elif st == "/Form":
            if self.depth >= _MAX_DEPTH:
                return
            bb = xo.get("/BBox")
            mat = xo.get("/Matrix")
            m = tuple(float(x) for x in _resolve(mat)) if mat is not None else ID
            m = mmul(m, ctm)
            if bb is not None:
                b = [float(x) for x in _resolve(bb)]
                box = aabb(m, min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))
                if not ctx.any_overlap(box):
                    return
            res = _resolve(xo.get("/Resources")) or self.res
            try:
                data = xo.get_data()
            except Exception:
                return
            sub = Interpreter(ctx, res, m, self.depth + 1, self.gs)
            new = sub.run(data)
            if new is None:
                return
            clone = DecodedStreamObject()
            for k, v in xo.items():
                if k not in ("/Filter", "/DecodeParms", "/Length"):
                    clone[NameObject(k)] = v
            clone.set_data(new)
            nref = ctx.writer._add_object(clone)
            op.args[0] = Name(add_resource(self.res, "/XObject", nref, "PWRedFm"))
            op.changed = self.changed = True
            ctx.stats.forms += 1

    def inline_image(self, op):
        ctx = self.ctx
        if ctx.images == IMAGE_NONE:
            return
        if ctx.any_overlap(aabb(self.gs.ctm, 0, 0, 1, 1)):
            op.op = None
            self.changed = True
            ctx.stats.images += 1

    # ------------------------------------------------------------------ paths
    def paint(self, ops, i, op):
        ctx = self.ctx
        start, pts, clip = self.path_start, self.path_pts, self.clip
        self.path_start, self.path_pts, self.clip = None, [], False
        if ctx.graphics == LINE_ART_NONE or start is None or clip or op.op == b"n" or not pts:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        box = [min(xs), min(ys), max(xs), max(ys)]
        if op.op in _STROKES:
            c = self.gs.ctm
            hw = self.gs.lw * math.sqrt(abs(c[0] * c[3] - c[1] * c[2])) / 2.0
            box = [box[0] - hw, box[1] - hw, box[2] + hw, box[3] + hw]
        hit = ctx.covered(box) if ctx.graphics == LINE_ART_REMOVE_IF_COVERED else ctx.any_overlap(box)
        if hit:
            for k in range(start, i + 1):
                ops[k].op = None
            self.changed = True
            ctx.stats.paths += 1

    # ------------------------------------------------------------------ main loop
    def run(self, data):
        ops = parse(data)
        self.run_ops(ops)
        return serialize(data, ops) if self.changed else None

    def run_ops(self, ops):
        ctx = self.ctx
        gs = self.gs
        for i, op in enumerate(ops):
            o, a = op.op, op.args
            if o in _PATH_CONS:
                if self.path_start is None:
                    self.path_start = i
                ctm = gs.ctm
                if o == b"re" and len(a) >= 4:
                    x, y, w, h = (_num(v) for v in a[:4])
                    self.path_pts += [mapply(ctm, x, y), mapply(ctm, x + w, y), mapply(ctm, x, y + h), mapply(ctm, x + w, y + h)]
                elif o != b"h":
                    nums = [_num(v) for v in a]
                    for k in range(0, len(nums) - 1, 2):
                        self.path_pts.append(mapply(ctm, nums[k], nums[k + 1]))
                continue
            if o in (b"W", b"W*"):
                self.clip = True
                continue
            if o in _PAINT:
                self.paint(ops, i, op)
                continue
            if o == b"q":
                self.stack.append(gs.copy())
            elif o == b"Q":
                if self.stack:
                    gs = self.gs = self.stack.pop()
            elif o == b"cm" and len(a) >= 6:
                gs.ctm = mmul(tuple(_num(v) for v in a[:6]), gs.ctm)
            elif o == b"w" and a:
                gs.lw = _num(a[0])
            elif o == b"BT":
                self.tm = self.tlm = ID
            elif o == b"Tf" and len(a) >= 2:
                gs.font = ctx.font(self.res, str(a[0])) if isinstance(a[0], Name) else ctx.font(None, "")
                gs.fs = _num(a[1])
            elif o == b"Tc" and a:
                gs.tc = _num(a[0])
            elif o == b"Tw" and a:
                gs.tw = _num(a[0])
            elif o == b"Tz" and a:
                gs.th = _num(a[0]) / 100.0
            elif o == b"TL" and a:
                gs.tl = _num(a[0])
            elif o == b"Ts" and a:
                gs.rise = _num(a[0])
            elif o in (b"Td", b"TD") and len(a) >= 2:
                tx, ty = _num(a[0]), _num(a[1])
                if o == b"TD":
                    gs.tl = -ty
                self.tlm = self.tm = mmul((1, 0, 0, 1, tx, ty), self.tlm)
            elif o == b"Tm" and len(a) >= 6:
                self.tlm = self.tm = tuple(_num(v) for v in a[:6])
            elif o == b"T*":
                self.tlm = self.tm = mmul((1, 0, 0, 1, 0, -gs.tl), self.tlm)
            elif o in (b"Tj", b"TJ", b"'", b'"'):
                if o == b'"' and len(a) >= 3:
                    gs.tw, gs.tc = _num(a[0]), _num(a[1])
                if o in (b"'", b'"'):
                    self.tlm = self.tm = mmul((1, 0, 0, 1, 0, -gs.tl), self.tlm)
                if not a:
                    continue
                items = a[-1] if o == b"TJ" else [a[-1]]
                if not isinstance(items, list):
                    items = [items]
                new = self.show(op, items) if ctx.text == TEXT_REMOVE else None
                if new is not None:
                    self.changed = True
                    op.changed = True
                    if o in (b"'", b'"'):  # becomes explicit state ops + TJ with the same effect
                        pre = [Op(b"Tw", [a[0]], None, None), Op(b"Tc", [a[1]], None, None)] if o == b'"' else []
                        op.pre = pre + [Op(b"T*", [], None, None)]
                    op.op, op.args = b"TJ", [new]
            elif o == b"Do":
                self.do_xobject(op)
            elif o == b"BI":
                self.inline_image(op)
            elif o in (b"BDC", b"BMC"):
                self.mc.append(op if o == b"BDC" else None)
            elif o == b"EMC":
                if self.mc:
                    self.mc.pop()
            elif o == b"gs" and a and isinstance(a[0], Name):
                self._extgstate(str(a[0]))

    def _extgstate(self, name):
        try:
            eg = _resolve(_resolve(self.res.get("/ExtGState")).get("/" + name))
        except Exception:
            return
        if eg is None:
            return
        if "/LW" in eg:
            self.gs.lw = float(eg["/LW"])
        fnt = eg.get("/Font")
        if fnt is not None:
            fnt = _resolve(fnt)
            try:
                key = fnt[0]
                m = self.ctx.fonts.get(("gs", id(_resolve(key))))
                if m is None:
                    m = FontModel(key)
                    self.ctx.fonts[("gs", id(_resolve(key)))] = m
                self.gs.font, self.gs.fs = m, float(fnt[1])
            except Exception:
                pass


def blank_image_pixels(ctx, xo, ctm):
    """Return a new image XObject with the pixels under the redaction rects overwritten,
    or None when the image cannot be decoded (caller then removes the image)."""
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError:
        return None
    inv = minv(ctm)
    if inv is None:
        return None
    try:
        W, H = int(xo["/Width"]), int(xo["/Height"])
        stencil = bool(xo.get("/ImageMask", False))
        img = xo.decode_as_image()
    except Exception:
        return None
    if img is None or img.size != (W, H):
        return None
    boxes = []
    for r in ctx.rects:
        u0, v0, u1, v1 = aabb(inv, *r)
        if u1 <= 0 or v1 <= 0 or u0 >= 1 or v0 >= 1:
            continue
        x0, x1 = max(0, math.floor(u0 * W)), min(W, math.ceil(u1 * W))
        y0, y1 = max(0, math.floor((1 - v1) * H)), min(H, math.ceil((1 - v0) * H))
        if x1 > x0 and y1 > y0:
            boxes.append((x0, y0, x1 - 1, y1 - 1))
    if not boxes:
        return None
    from PIL import ImageDraw
    new = DecodedStreamObject()
    new[NameObject("/Type")] = NameObject("/XObject")
    new[NameObject("/Subtype")] = NameObject("/Image")
    new[NameObject("/Width")] = NumberObject(W)
    new[NameObject("/Height")] = NumberObject(H)
    if stencil:
        dec = xo.get("/Decode")
        inverted = dec is not None and float(_resolve(dec)[0]) == 1.0
        img = img.convert("1")
        dr = ImageDraw.Draw(img)
        # stencil: paint where sample == 0 (default Decode) -> set to "unpainted"
        for b in boxes:
            dr.rectangle(b, fill=0 if inverted else 1)
        new[NameObject("/ImageMask")] = xo["/ImageMask"]
        if dec is not None:
            new[NameObject("/Decode")] = dec
        new[NameObject("/BitsPerComponent")] = NumberObject(1)
        new.set_data(img.tobytes())
    else:
        gray = img.mode in ("1", "L", "LA", "I", "I;16", "F")
        img = img.convert("L" if gray else "RGB")
        fill = ctx.pixel_fill or (0.0, 0.0, 0.0)
        col = tuple(int(round(255 * max(0.0, min(1.0, c)))) for c in fill)
        dr = ImageDraw.Draw(img)
        for b in boxes:
            dr.rectangle(b, fill=(int(round(sum(col) / 3)) if gray else col))
        new[NameObject("/ColorSpace")] = NameObject("/DeviceGray" if gray else "/DeviceRGB")
        new[NameObject("/BitsPerComponent")] = NumberObject(8)
        new.set_data(img.tobytes())
        sm = _resolve(xo.get("/SMask"))
        if isinstance(sm, StreamObject):
            try:
                mimg = sm.decode_as_image().convert("L")
                if mimg.size != (W, H):
                    mimg = mimg.resize((W, H))
                md = ImageDraw.Draw(mimg)
                for b in boxes:
                    md.rectangle(b, fill=255)
                ms = DecodedStreamObject()
                ms[NameObject("/Type")] = NameObject("/XObject")
                ms[NameObject("/Subtype")] = NameObject("/Image")
                ms[NameObject("/Width")] = NumberObject(W)
                ms[NameObject("/Height")] = NumberObject(H)
                ms[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
                ms[NameObject("/BitsPerComponent")] = NumberObject(8)
                ms.set_data(mimg.tobytes())
                new[NameObject("/SMask")] = ctx.writer._add_object(ms.flate_encode())
            except Exception:
                return None
    for k in ("/Interpolate", "/Intent"):
        if k in xo:
            new[NameObject(k)] = xo[k]
    return new.flate_encode()
