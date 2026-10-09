# SPDX-License-Identifier: Apache-2.0
"""Stage 1: pull raw glyph records out of PDFium (via pypdfium2's raw ctypes API).

For every *non-generated* character PDFium reports we collect:
  unicode, pen origin (baseline start), advance vector, writing direction,
  effective font size, font ascender/descender, font name/flags/weight, fill colour.

Coordinates are converted from PDF user space (bottom-left origin) to the
*unrotated* page with a top-left origin relative to the CropBox -- the same
convention PyMuPDF uses for get_text().
"""
from __future__ import annotations

import math
import re
from ctypes import c_double, c_float, c_uint, create_string_buffer, byref

import pypdfium2 as pdfium
import pypdfium2.raw as R

# PDF FontDescriptor flag bits (PDF 32000 §9.8.2)
FD_FIXED_PITCH = 1 << 0
FD_SERIF = 1 << 1
FD_SYMBOLIC = 1 << 2
FD_ITALIC = 1 << 6
FD_FORCE_BOLD = 1 << 18

# PyMuPDF-compatible span flag bits
TEXT_FONT_SUPERSCRIPT = 1
TEXT_FONT_ITALIC = 2
TEXT_FONT_SERIFED = 4
TEXT_FONT_MONOSPACED = 8
TEXT_FONT_BOLD = 16

_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_BOLD_RE = re.compile(r"bold|black|heavy|-bd|\bbd\b|-blk|medi|^cmbx|^cmbsy|^cmmib|^cmb\d", re.I)
_ITALIC_RE = re.compile(r"italic|oblique|-it\b|-bdi|-ital|slant|kursiv", re.I)
_MONO_RE = re.compile(r"mono|courier|consol|code|typewriter|sftt|cmtt|menlo", re.I)
_SANS_RE = re.compile(r"sans|arial|helvetica|verdana|calibri|segoe|tahoma|gothic|grotesk|frutiger|futura|cmss", re.I)
_SERIF_RE = re.compile(r"times|roman|serif|georgia|garamond|nimbusrom|minion|cambria|palatino|book|cmr|cmmi|sil", re.I)


class Glyph:
    """One character as emitted by the content stream (after PDFium's ToUnicode mapping)."""
    __slots__ = ("c", "px", "py", "qx", "qy", "dx", "dy", "size", "font", "color", "hyphen", "idx", "cont", "gen_space")

    def __init__(self, c, px, py, qx, qy, dx, dy, size, font, color, hyphen, idx, cont=False):
        self.gen_space = False  # PDFium inserted a (generated) space right before this glyph
        self.cont = cont      # continuation of a multi-codepoint glyph (no pen advance)
        self.c = c            # str (one code point)
        self.px, self.py = px, py  # pen start (origin on baseline)
        self.qx, self.qy = qx, qy  # pen end (origin + advance)
        self.dx, self.dy = dx, dy  # unit writing direction (y down)
        self.size = size      # effective font size = Tf * sqrt|det(Tm x CTM)|
        self.font = font      # FontInfo
        self.color = color    # 0xRRGGBB
        self.hyphen = hyphen  # PDFium flagged as line-end hyphen
        self.idx = idx        # PDFium char index


class FontInfo:
    __slots__ = ("name", "flags", "weight", "ascender", "descender", "pdf_flags", "italic_angle", "embedded", "norm")

    def __init__(self, name, pdf_flags, weight, ascender, descender, italic_angle, embedded=True):
        self.embedded = embedded
        self.norm = None  # (asc, desc) normalised for bbox computation, filled lazily
        self.name = name
        self.pdf_flags = pdf_flags
        self.weight = weight
        self.italic_angle = italic_angle
        self.ascender = ascender
        self.descender = descender
        self.flags = self._style_flags()

    def _style_flags(self):
        n, f = self.name or "", self.pdf_flags
        flags = 0
        if (f & FD_ITALIC) or _ITALIC_RE.search(n) or (self.italic_angle or 0) < -1:
            flags |= TEXT_FONT_ITALIC
        if _MONO_RE.search(n) or (f & FD_FIXED_PITCH and not _SANS_RE.search(n)):
            flags |= TEXT_FONT_MONOSPACED
        # PyMuPDF's output marks practically every embedded font "serifed" (observed);
        # we follow that for output compatibility and only use name heuristics for non-embedded fonts.
        if self.embedded or _SERIF_RE.search(n) or ((f & FD_SERIF) and not _SANS_RE.search(n)):
            flags |= TEXT_FONT_SERIFED
        if (f & FD_FORCE_BOLD) or _BOLD_RE.search(n) or (self.weight or 0) >= 750:
            flags |= TEXT_FONT_BOLD
        return flags


def _font_info(textobj, cache):
    font = R.FPDFTextObj_GetFont(textobj)
    key = R.ctypes.cast(font, R.ctypes.c_void_p).value if font else None
    fi = cache.get(key)
    if fi is not None:
        return fi
    name = ""
    flags = weight = 0
    asc = 0.8
    dsc = -0.2
    ia = 0
    emb = True
    if font:
        emb = bool(R.FPDFFont_GetIsEmbedded(font))
        buf = create_string_buffer(256)
        n = R.FPDFFont_GetBaseFontName(font, buf, 256)
        if n:
            name = buf.value.decode("utf-8", "replace")
        name = _SUBSET_RE.sub("", name)
        flags = R.FPDFFont_GetFlags(font)
        flags = flags if flags > 0 else 0
        weight = R.FPDFFont_GetWeight(font)
        a, d = c_float(), c_float()
        if R.FPDFFont_GetAscent(font, c_float(1.0), byref(a)):
            asc = a.value
        if R.FPDFFont_GetDescent(font, c_float(1.0), byref(d)):
            dsc = d.value
        iav = R.ctypes.c_int()
        if R.FPDFFont_GetItalicAngle(font, byref(iav)):
            ia = iav.value
    fi = FontInfo(name, flags, weight, asc, dsc, ia, emb)
    cache[key] = fi
    return fi


def _fast(fn, restype, *argtypes):
    """Re-type a pypdfium2 raw function with plain c_void_p handles (no per-call
    pointer casts) -- ~2x cheaper per call in the hot per-char loop."""
    return R.ctypes.cast(fn, R.ctypes.CFUNCTYPE(restype, *argtypes))


_vp, _int = R.ctypes.c_void_p, R.ctypes.c_int
_P = R.ctypes.POINTER
_get_unicode = _fast(R.FPDFText_GetUnicode, c_uint, _vp, _int)
_is_generated = _fast(R.FPDFText_IsGenerated, _int, _vp, _int)
_get_origin = _fast(R.FPDFText_GetCharOrigin, _int, _vp, _int, _P(c_double), _P(c_double))
_get_loose = _fast(R.FPDFText_GetLooseCharBox, _int, _vp, _int, _P(R.FS_RECTF))
_get_obj = _fast(R.FPDFText_GetTextObject, _vp, _vp, _int)



def crop_box(page):
    """CropBox -> MediaBox -> content bounding box -> US Letter (same chain as the Rust core)."""
    import ctypes
    import pypdfium2.raw as R
    l, b, r, t = (ctypes.c_float() for _ in range(4))
    for fn in (R.FPDFPage_GetCropBox, R.FPDFPage_GetMediaBox):
        if fn(page.raw, l, b, r, t):
            return l.value, b.value, r.value, t.value
    rc = R.FS_RECTF()
    if R.FPDF_GetPageBoundingBox(page.raw, rc):
        return rc.left, rc.bottom, rc.right, rc.top
    return 0.0, 0.0, 612.0, 792.0

def page_glyphs(page: pdfium.PdfPage, textpage: pdfium.PdfTextPage, ligatures: bool = False):
    """Return Glyphs for a page in content-stream order (PDFium-generated spaces/newlines dropped)."""
    raw = textpage.raw
    tp = R.ctypes.cast(raw, _vp).value
    n = R.FPDFText_CountChars(raw)
    l, b, r, t = crop_box(page)
    ox, oy = l, t  # top-left of cropbox in PDF space

    get_unicode, is_generated, get_origin, get_loose, get_obj = _get_unicode, _is_generated, _get_origin, _get_loose, _get_obj
    x, y = c_double(), c_double()
    xr, yr = byref(x), byref(y)
    m = R.FS_MATRIX()
    rect = R.FS_RECTF()
    rr = byref(rect)
    fsz = c_float()
    cr, cg, cb, ca = c_uint(), c_uint(), c_uint(), c_uint()
    font_cache = {}
    obj_cache = {}  # text object -> (fontinfo, size, dx, dy, color, |dx|, |dy|)
    hypot, sqrt = math.hypot, math.sqrt
    out = []
    append = out.append
    prev = None
    gen_space = False
    for i in range(n):
        code = get_unicode(tp, i)
        if code == 32 or code == 13 or code == 10:
            if is_generated(tp, i) == 1:
                gen_space = gen_space or code == 32
                continue
            if code != 32:
                continue
        hy = False
        if code == 2:           # PDFium's line-end hyphen marker
            code, hy = 0x2D, True
        elif code == 0 or code >= 0xFFFE or 0xD800 <= code <= 0xDFFF:
            code = 0xFFFD
        obj = get_obj(tp, i)
        st = obj_cache.get(obj)
        if st is None:
            # the char matrix, font and fill colour are per text object -> query once
            R.FPDFText_GetMatrix(raw, i, m)
            if obj:
                pobj = R.ctypes.cast(obj, R.FPDF_PAGEOBJECT)
                R.FPDFTextObj_GetFontSize(pobj, fsz)
                tf = fsz.value
                fi = _font_info(pobj, font_cache)
            else:
                tf = R.FPDFText_GetFontSize(raw, i)
                fi = FontInfo("", 0, 400, 0.8, -0.2, 0)
            a, bb, cc, d = m.a, m.b, m.c, m.d
            det = abs(a * d - bb * cc)
            size = tf * sqrt(det) if det > 0 else tf
            nrm = hypot(a, bb) or 1.0
            dx, dy = a / nrm, -bb / nrm   # writing direction, y flipped for top-left output
            color = 0
            if R.FPDFText_GetFillColor(raw, i, cr, cg, cb, ca):
                color = (cr.value << 16) | (cg.value << 8) | cb.value
            st = (fi, size, dx, dy, color, abs(dx), abs(dy))
            obj_cache[obj] = st
        fi, size, dx, dy, color, adx, ady = st
        get_origin(tp, i, xr, yr)
        get_loose(tp, i, rr)
        px, py = x.value - ox, oy - y.value
        # advance: recover from PDFium's loose box (origin .. origin+advance along dir)
        if ady < 1e-3:
            adv = abs(rect.right - rect.left)
        elif adx < 1e-3:
            adv = abs(rect.top - rect.bottom)
        else:  # general rotation: loose box is the AABB of the rotated glyph cell
            W, H = abs(rect.right - rect.left), abs(rect.top - rect.bottom)
            h = size * (fi.ascender - fi.descender)
            adv = max(0.0, (W - h * ady) / adx) if adx >= ady else max(0.0, (H - h * adx) / ady)
        qx, qy = px + adv * dx, py + adv * dy
        g = Glyph(chr(code), px, py, qx, qy, dx, dy, size, fi, color, hy, i)
        g.gen_space, gen_space = gen_space, False
        if prev is not None and prev.px == px and prev.py == py and prev.qx == qx and prev.qy == qy and prev.font is fi:
            # PDFium expands one glyph with a multi-codepoint ToUnicode (e.g. ligature U+FB03 -> "ffi")
            # into several chars sharing the same origin and box.  Re-cluster them.
            g.cont = True
        append(g)
        prev = g
    _fix_overhang(out)
    return _recombine_ligatures(out) if ligatures else out


def _fix_overhang(glyphs):
    """PDFium's loose box is origin + *glyph-bbox* width, which is wider than the advance for
    italic overhangs ('f', 'r', ...).  When the next glyph starts on the same baseline inside
    that box, it marks the true pen position: clamp the end to it (horizontal LTR text)."""
    for a, b in zip(glyphs, glyphs[1:]):
        if a.dy == 0.0 and a.dx > 0 and b.py == a.py and b.dx > 0 and a.px < b.px < a.qx and not b.cont and not b.gen_space:
            a.qx = b.px


_LIG_REV = {"ff": "\ufb00", "fi": "\ufb01", "fl": "\ufb02", "ffi": "\ufb03", "ffl": "\ufb04", "st": "\ufb06"}


def _recombine_ligatures(glyphs):
    """Turn clusters like f,f(cont),i(cont) back into the ligature code point.
    PDFium decomposes U+FB00..FB06 *and* multi-codepoint ToUnicode entries into the same
    shape, so we cannot know which one the PDF contained.  Only used when ligatures=True."""
    res = []
    i, n = 0, len(glyphs)
    while i < n:
        g = glyphs[i]
        j = i + 1
        while j < n and glyphs[j].cont:
            j += 1
        if j - i > 1:
            txt = "".join(x.c for x in glyphs[i:j])
            lig = _LIG_REV.get(txt)
            if lig:
                g.c = lig
                res.append(g)
            else:
                res.extend(glyphs[i:j])
        else:
            res.append(g)
        i = j
    return res
