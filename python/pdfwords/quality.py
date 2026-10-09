# SPDX-License-Identifier: Apache-2.0
"""Text-layer quality report: does this page need OCR?

Signals, all read from PDFium (no rendering):
  chars              non-whitespace characters in the text layer
  invisible_ratio    share of those drawn with text render mode 3/7 (invisible: typical of an
                     existing OCR layer over a scan)
  unicode_error_ratio share of chars PDFium could not map to Unicode (FPDFText_HasUnicodeMapError)
  garbled_ratio      share of chars that are U+FFFD, private-use, control characters, or
                     unmapped-looking codes, plus unmapped chars
  image_coverage     fraction of the page covered by the largest image (scans are ~1.0)
  fonts              per font: name, embedded flag, glyphless (e.g. Tesseract's GlyphLessFont), chars
The verdict:
  needs_ocr = no usable text but images present, or garbled/unmapped text above `garbled_max`.
  An existing invisible OCR layer is reported (reason "ocr_layer") but does not by itself
  require OCR; pass ocr_layer_ok=False to treat it as needing (re-)OCR.
"""
from __future__ import annotations

import ctypes as _ct
import unicodedata as _ud

import pypdfium2.raw as R

_INVISIBLE = {3, 7}


def _is_garbled(c):
    o = ord(c)
    if o == 0xFFFD or 0xE000 <= o <= 0xF8FF or 0xF0000 <= o:
        return True
    cat = _ud.category(c)
    return cat in ("Cc", "Co", "Cs", "Cn") and c not in "\t\n\r"


def garbled_ratio(text):
    """Fraction of non-whitespace chars in `text` that look garbled."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(map(_is_garbled, chars)) / len(chars)


def _font_name(font):
    for fn in (R.FPDFFont_GetBaseFontName, R.FPDFFont_GetFamilyName):
        try:
            n = fn(font, None, 0)
        except Exception:
            continue
        if n > 1:
            buf = _ct.create_string_buffer(n)
            fn(font, buf, n)
            return buf.value.decode("utf-8", "replace")
    return ""


def _walk_objects(container, count, get, depth=0):
    for i in range(count(container)):
        o = get(container, i)
        if not o:
            continue
        yield o, depth
        if R.FPDFPageObj_GetType(o) == R.FPDF_PAGEOBJ_FORM and depth < 12:
            yield from _walk_objects(o, R.FPDFFormObj_CountObjects, R.FPDFFormObj_GetObject, depth + 1)


def _bounds(o):
    l, b, r, t = (_ct.c_float() for _ in range(4))
    if R.FPDFPageObj_GetBounds(o, l, b, r, t):
        return l.value, b.value, r.value, t.value
    return None


def text_quality(page, garbled_max=0.1, image_min=0.5, ocr_layer_ok=True):
    from .chars import crop_box
    pg = page._page
    cl, cb, cr, ct = crop_box(pg)
    parea = max(abs(cr - cl) * abs(ct - cb), 1e-9)

    tp = pg.get_textpage()
    try:
        n = R.FPDFText_CountChars(tp.raw)
        chars = 0
        unmapped = 0
        garbled = 0
        invisible = 0
        fonts = {}
        obj_info = {}
        for i in range(n):
            code = R.FPDFText_GetUnicode(tp.raw, i)
            if code in (32, 9, 10, 13, 0xA0) or R.FPDFText_IsGenerated(tp.raw, i) == 1:
                continue
            chars += 1
            bad_map = R.FPDFText_HasUnicodeMapError(tp.raw, i) == 1
            unmapped += bad_map
            try:
                ch = chr(code)
            except ValueError:
                ch = "\ufffd"
            garbled += bad_map or _is_garbled(ch)
            o = R.FPDFText_GetTextObject(tp.raw, i)
            key = _ct.cast(o, _ct.c_void_p).value if o else None
            info = obj_info.get(key)
            if info is None:
                mode = R.FPDFTextObj_GetTextRenderMode(o) if o else 0
                f = R.FPDFTextObj_GetFont(o) if o else None
                fkey = _ct.cast(f, _ct.c_void_p).value if f else None
                if fkey not in fonts:
                    name = _font_name(f) if f else ""
                    emb = bool(R.FPDFFont_GetIsEmbedded(f)) if f else False
                    fonts[fkey] = {"name": name, "embedded": emb,
                                   "glyphless": "glyphless" in name.lower(), "chars": 0}
                info = obj_info[key] = (mode, fkey)
            if info[0] in _INVISIBLE:
                invisible += 1
            fonts[info[1]]["chars"] += 1
    finally:
        tp.close()

    # images (top level and inside Form XObjects; form-relative bounds are a lower bound)
    largest = 0.0
    n_images = 0
    for o, _d in _walk_objects(pg.raw, R.FPDFPage_CountObjects, R.FPDFPage_GetObject):
        if R.FPDFPageObj_GetType(o) != R.FPDF_PAGEOBJ_IMAGE:
            continue
        bb = _bounds(o)
        if not bb:
            continue
        n_images += 1
        w = max(0.0, min(bb[2], max(cl, cr)) - max(bb[0], min(cl, cr)))
        h = max(0.0, min(bb[3], max(cb, ct)) - max(bb[1], min(cb, ct)))
        largest = max(largest, w * h / parea)
    largest = min(largest, 1.0)

    inv_r = invisible / chars if chars else 0.0
    unm_r = unmapped / chars if chars else 0.0
    gar_r = garbled / chars if chars else 0.0
    visible = chars - invisible
    reasons = []
    needs = False
    if chars == 0:
        if n_images:
            reasons.append("no_text_layer")
            needs = True
        else:
            reasons.append("empty_page")
    else:
        if inv_r > 0.5:
            reasons.append("ocr_layer")
            if not ocr_layer_ok:
                needs = True
        if gar_r > garbled_max:
            reasons.append("garbled_text")
            needs = True
        if unm_r > garbled_max:
            reasons.append("unicode_map_errors")
            needs = True
        if any(f["glyphless"] for f in fonts.values()):
            reasons.append("glyphless_font")
        if visible < 20 and largest >= image_min and inv_r <= 0.5:
            reasons.append("scan_with_little_text")
            needs = True
    if largest >= image_min:
        reasons.append("full_page_image" if largest >= 0.9 else "large_image")
    score = 0.0 if chars == 0 else max(0.0, 1.0 - max(gar_r, unm_r) / max(garbled_max * 5, 1e-9))
    if "scan_with_little_text" in reasons:
        score = min(score, 0.2)
    return {
        "page": page.number, "needs_ocr": needs, "score": round(score, 4), "reasons": reasons,
        "chars": chars, "invisible_ratio": round(inv_r, 4), "unicode_error_ratio": round(unm_r, 4),
        "garbled_ratio": round(gar_r, 4), "image_coverage": round(largest, 4), "images": n_images,
        "fonts": sorted(fonts.values(), key=lambda f: -f["chars"]),
    }
