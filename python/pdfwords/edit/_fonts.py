# SPDX-License-Identifier: Apache-2.0
"""Font metrics for content-stream interpretation, and font resources for text insertion.

* ``FontModel`` answers the two questions the redaction interpreter needs for a font that is
  already in a PDF: how a byte string splits into character codes, and how far each code
  advances the pen (glyph-space width / 1000), plus ascent/descent for the glyph box.
  Widths come from the font dictionary (/Widths, /W, /DW, /MissingWidth, Type3 /FontMatrix);
  standard-14 fonts without /Widths are measured with PDFium's built-in standard fonts, i.e.
  the same metrics PDFium itself uses to lay the text out.
* ``Base14Font`` / ``EmbeddedFont`` build new font resources for ``insert_text``.
"""
from __future__ import annotations

import ctypes
import logging
import re
import unicodedata

from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                           NumberObject, StreamObject, TextStringObject)

_SUBSET = re.compile(r"^[A-Z]{6}\+")

# ---------------------------------------------------------------------- standard 14
STD14 = {"Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
         "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
         "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique", "Symbol", "ZapfDingbats"}

# PyMuPDF-style short codes accepted by insert_text(fontname=...)
SHORT_NAMES = {
    "helv": "Helvetica", "heit": "Helvetica-Oblique", "hebo": "Helvetica-Bold", "hebi": "Helvetica-BoldOblique",
    "tiro": "Times-Roman", "tiit": "Times-Italic", "tibo": "Times-Bold", "tibi": "Times-BoldItalic",
    "cour": "Courier", "coit": "Courier-Oblique", "cobo": "Courier-Bold", "cobi": "Courier-BoldOblique",
    "symb": "Symbol", "zadb": "ZapfDingbats",
}


def std14_name(base):
    """Map a /BaseFont name (incl. common TrueType aliases) to a standard-14 name, or None."""
    if not base:
        return None
    b = _SUBSET.sub("", str(base).lstrip("/"))
    if b in STD14:
        return b
    k = b.replace(" ", "").replace("-", "").replace(",", "").lower()
    fam = None
    for pre, f in (("arial", "Helvetica"), ("helvetica", "Helvetica"), ("timesnewroman", "Times"),
                   ("times", "Times"), ("couriernew", "Courier"), ("courier", "Courier")):
        if k.startswith(pre):
            fam, k = f, k[len(pre):]
            break
    if fam is None:
        return {"symbol": "Symbol", "symbolmt": "Symbol", "zapfdingbats": "ZapfDingbats"}.get(k)
    k = k.replace("psmt", "").replace("mt", "").replace("ps", "")
    bold = "bold" in k
    ital = "italic" in k or "oblique" in k
    if fam == "Times":
        return "Times-" + ("BoldItalic" if bold and ital else "Bold" if bold else "Italic" if ital else "Roman")
    suf = "BoldOblique" if bold and ital else "Bold" if bold else "Oblique" if ital else ""
    return fam + ("-" + suf if suf else "")


_std_doc = None
_std_fonts = {}
_std_width_cache = {}


def _pdfium_std_font(name):
    global _std_doc
    import pypdfium2 as pdfium
    import pypdfium2.raw as R
    if _std_doc is None:
        _std_doc = pdfium.PdfDocument.new()
    f = _std_fonts.get(name)
    if f is None:
        f = R.FPDFText_LoadStandardFont(_std_doc.raw, name.encode())
        _std_fonts[name] = f
    return f


def std14_width(name, ch):
    """Advance width (1/1000 em) of unicode char `ch` in standard font `name`, from PDFium."""
    key = (name, ch)
    w = _std_width_cache.get(key)
    if w is None:
        import pypdfium2.raw as R
        f = _pdfium_std_font(name)
        out = ctypes.c_float()
        w = 0.0
        if f and R.FPDFFont_GetGlyphWidth(f, ord(ch), ctypes.c_float(1000.0), out):
            w = float(out.value)
        _std_width_cache[key] = w
    return w


def std14_metrics(name):
    import pypdfium2.raw as R
    f = _pdfium_std_font(name)
    a, d = ctypes.c_float(), ctypes.c_float()
    asc = a.value if f and R.FPDFFont_GetAscent(f, ctypes.c_float(1.0), a) else 0.8
    dsc = d.value if f and R.FPDFFont_GetDescent(f, ctypes.c_float(1.0), d) else -0.2
    return asc, dsc


# encodings: code -> unicode char (pypdf ships the standard tables; cp1252 as last resort)
def _base_encoding(name):
    try:
        from pypdf import _codecs as C
        table = {"/WinAnsiEncoding": C._win_encoding, "/MacRomanEncoding": C._mac_encoding,
                 "/StandardEncoding": C._std_encoding, "/Symbol": C._symbol_encoding,
                 "/ZapfDingbats": C._zapfding_encoding, "/PDFDocEncoding": C._pdfdoc_encoding}.get(name)
        if table is not None:
            return list(table)
    except Exception:  # pragma: no cover - pypdf internals moved
        pass
    return [bytes([i]).decode("cp1252", "replace") for i in range(256)]


def _glyph_unicode(gname):
    try:
        from pypdf._codecs.adobe_glyphs import adobe_glyphs
        u = adobe_glyphs.get("/" + gname)
        if u:
            return u
    except Exception:  # pragma: no cover
        pass
    m = re.match(r"uni([0-9A-Fa-f]{4})", gname)
    return chr(int(m.group(1), 16)) if m else "\ufffd"


def _norm_asc_desc(asc, dsc):
    if asc <= 0 and dsc >= 0:
        asc, dsc = 0.8, -0.2
    h = asc - dsc
    if 0 < h < 1:  # same normalisation as the extractor's char boxes
        asc, dsc = asc / h, dsc / h
    return asc, dsc


def _num(v, default=0.0):
    try:
        return float(v.get_object() if hasattr(v, "get_object") else v)
    except Exception:
        return default


def _parse_W(arr):
    """CID /W array -> {cid: width}."""
    out = {}
    if arr is None:
        return out
    arr = [a.get_object() for a in arr.get_object()]
    i = 0
    while i < len(arr):
        first = int(_num(arr[i]))
        nxt = arr[i + 1] if i + 1 < len(arr) else None
        if isinstance(nxt, list):
            for k, w in enumerate(nxt):
                out[first + k] = _num(w)
            i += 2
        elif i + 2 < len(arr):
            last, w = int(_num(nxt)), _num(arr[i + 2])
            if last - first <= 65535:
                for c in range(first, last + 1):
                    out[c] = w
            i += 3
        else:
            break
    return out


def _parse_W2(arr):
    out = {}
    if arr is None:
        return out
    arr = [a.get_object() for a in arr.get_object()]
    i = 0
    while i < len(arr):
        first = int(_num(arr[i]))
        nxt = arr[i + 1] if i + 1 < len(arr) else None
        if isinstance(nxt, list):
            for k in range(0, len(nxt) - 2, 3):
                out[first + k // 3] = _num(nxt[k])
            i += 2
        elif i + 4 < len(arr):
            for c in range(first, min(int(_num(nxt)), first + 65535) + 1):
                out[c] = _num(arr[i + 2])
            i += 5
        else:
            break
    return out


class _CMap:
    """Code splitting + code->CID for a Type0 font's /Encoding."""

    def __init__(self, enc):
        self.ranges = []      # (nbytes, lo, hi)
        self.cid = None       # dict code->cid, or None for identity
        self.identity = True
        self.vertical = False
        self.exact = True     # False when code->CID is unknown (predefined non-identity CMap)
        if enc is None:
            self.ranges = [(2, 0, 0xFFFF)]
            return
        enc = enc.get_object()
        if isinstance(enc, StreamObject):
            self._from_stream(enc)
        else:
            self._predefined(str(enc).lstrip("/"))

    def _predefined(self, name):
        self.vertical = name.endswith("-V")
        if name.startswith("Identity"):
            self.ranges = [(2, 0, 0xFFFF)]
            return
        self.identity, self.exact = False, False
        if "UCS2" in name or "UTF16" in name:
            self.ranges = [(2, 0, 0xFFFF)]
        elif "UTF32" in name:
            self.ranges = [(4, 0, 0xFFFFFFFF)]
        elif "RKSJ" in name:
            self.ranges = [(1, 0x00, 0x80), (1, 0xA0, 0xDF), (2, 0x8140, 0x9FFC), (2, 0xE040, 0xFCFC)]
        else:  # EUC / GBK / Big5 / KSC style: lead byte >= 0x81 starts a 2-byte code
            self.ranges = [(1, 0x00, 0x80), (2, 0x8140, 0xFEFE)]

    def _from_stream(self, s):
        try:
            txt = s.get_data().decode("latin-1")
        except Exception:
            txt = ""
        wm = re.search(r"/WMode\s+(\d)", txt)
        self.vertical = bool(wm and wm.group(1) == "1")
        use = s.get("/UseCMap")
        if use is not None and not isinstance(use.get_object(), StreamObject):
            self._predefined(str(use.get_object()).lstrip("/"))
        for blk in re.findall(r"begincodespacerange(.*?)endcodespacerange", txt, re.S):
            for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                self.ranges.append((len(a) // 2, int(a, 16), int(b, 16)))
        cid = {}
        for blk in re.findall(r"begincidrange(.*?)endcidrange", txt, re.S):
            for a, b, c in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\d+)", blk):
                lo, hi, c = int(a, 16), int(b, 16), int(c)
                if hi - lo <= 65535:
                    for k in range(lo, hi + 1):
                        cid[k] = c + k - lo
        for blk in re.findall(r"begincidchar(.*?)endcidchar", txt, re.S):
            for a, c in re.findall(r"<([0-9A-Fa-f]+)>\s*(\d+)", blk):
                cid[int(a, 16)] = int(c)
        if not self.ranges:
            self.ranges = [(2, 0, 0xFFFF)]
        if cid:
            self.cid, self.identity, self.exact = cid, False, True

    def split(self, b):
        """Shortest match against the codespace ranges, compared byte by byte (as the spec says)."""
        if not hasattr(self, "_rb"):
            self._rb = sorted(((nb, lo.to_bytes(nb, "big"), hi.to_bytes(nb, "big")) for nb, lo, hi in self.ranges),
                              key=lambda r: r[0])
        rb = self._rb
        out = []
        i, n = 0, len(b)
        while i < n:
            for nb, lo, hi in rb:
                if i + nb > n:
                    continue
                if all(lo[k] <= b[i + k] <= hi[k] for k in range(nb)):
                    out.append((int.from_bytes(b[i:i + nb], "big"), nb))
                    i += nb
                    break
            else:
                nb = rb[0][0]
                out.append((int.from_bytes(b[i:i + nb], "big"), nb))
                i += nb
        return out

    def to_cid(self, code):
        if self.cid is not None:
            return self.cid.get(code, 0)
        return code


class FontModel:
    """Metrics of a font resource as needed to replay text positioning."""

    def __init__(self, fdict):
        f = fdict.get_object() if fdict is not None else DictionaryObject()
        self.subtype = str(f.get("/Subtype", "/Type1"))
        self.base = str(f.get("/BaseFont", "")).lstrip("/")
        self.type0 = self.subtype == "/Type0"
        self.exact = True
        self.vertical = False
        fd = None
        self.fmatrix_scale = 1.0
        if self.type0:
            df = f.get("/DescendantFonts")
            d = df.get_object()[0].get_object() if df is not None and len(df.get_object()) else DictionaryObject()
            self.cmap = _CMap(f.get("/Encoding"))
            self.exact = self.cmap.exact
            self.vertical = self.cmap.vertical
            self.W = _parse_W(d.get("/W"))
            self.DW = _num(d.get("/DW"), 1000.0)
            dw2 = d.get("/DW2")
            self.DW2y = _num(dw2.get_object()[1], -1000.0) if dw2 is not None else -1000.0
            self.W2 = _parse_W2(d.get("/W2")) if self.vertical else {}
            fd = d.get("/FontDescriptor")
        else:
            self.cmap = None
            self.first = int(_num(f.get("/FirstChar"), 0))
            ws = f.get("/Widths")
            self.widths = [_num(w) for w in ws.get_object()] if ws is not None else None
            fd = f.get("/FontDescriptor")
            self.missing = _num(fd.get_object().get("/MissingWidth"), 0.0) if fd is not None else 0.0
            if self.subtype == "/Type3":
                fm = f.get("/FontMatrix")
                self.fmatrix_scale = _num(fm.get_object()[0], 0.001) * 1000 if fm is not None else 1.0
            self.std = None
            self.enc = None
            if self.widths is None:
                self.std = std14_name(self.base) or "Helvetica"
                if std14_name(self.base) is None:
                    self.exact = False
                self.enc = self._encoding(f)
        asc = dsc = None
        if fd is not None:
            fdo = fd.get_object()
            asc = _num(fdo.get("/Ascent"), 0.0) / 1000.0
            dsc = _num(fdo.get("/Descent"), 0.0) / 1000.0
        if (asc is None or (asc == 0 and dsc == 0)) and not self.type0 and self.subtype != "/Type3":
            nm = std14_name(self.base)
            if nm:
                asc, dsc = std14_metrics(nm)
        if self.subtype == "/Type3":
            bb = f.get("/FontBBox")
            fm = f.get("/FontMatrix")
            if bb is not None and fm is not None:
                bb = [_num(x) for x in bb.get_object()]
                sy = _num(fm.get_object()[3], 0.001)
                asc, dsc = max(bb[1], bb[3]) * sy, min(bb[1], bb[3]) * sy
        self.asc, self.desc = _norm_asc_desc(asc or 0.0, dsc or 0.0)
        self._wcache = {}

    def _encoding(self, f):
        enc = f.get("/Encoding")
        std = self.std
        base = "/Symbol" if std == "Symbol" else "/ZapfDingbats" if std == "ZapfDingbats" else "/StandardEncoding"
        diffs = None
        if enc is not None:
            enc = enc.get_object()
            if isinstance(enc, dict):
                base = str(enc.get("/BaseEncoding", base))
                diffs = enc.get("/Differences")
            else:
                base = str(enc)
        table = _base_encoding(base)
        if diffs is not None:
            code = 0
            for x in diffs.get_object():
                x = x.get_object()
                if isinstance(x, (int, float)):
                    code = int(x)
                else:
                    if 0 <= code < 256:
                        table[code] = _glyph_unicode(str(x).lstrip("/"))
                    code += 1
        return table

    def split(self, b):
        """bytes -> [(code, nbytes)]"""
        if self.cmap is None:
            return [(c, 1) for c in b]
        return self.cmap.split(b)

    def width(self, code):
        """Horizontal advance in 1/1000 text-space units (before font size)."""
        w = self._wcache.get(code)
        if w is not None:
            return w
        if self.type0:
            w = self.W.get(self.cmap.to_cid(code), self.DW)
        elif self.widths is not None:
            k = code - self.first
            w = self.widths[k] if 0 <= k < len(self.widths) else self.missing
            w *= self.fmatrix_scale
        else:
            ch = self.enc[code] if 0 <= code < 256 else "\ufffd"
            w = std14_width(self.std, ch) if ch not in ("\ufffd", "", None) else 0.0
        self._wcache[code] = w
        return w

    def vwidth(self, code):
        """Vertical advance (w1y, negative = downwards) for vertical-mode CID fonts."""
        return self.W2.get(self.cmap.to_cid(code), self.DW2y)


# ---------------------------------------------------------------------- new fonts for insertion
def _stream(data, **entries):
    s = DecodedStreamObject()
    s.set_data(data)
    for k, v in entries.items():
        s[NameObject("/" + k)] = v
    return s


class Base14Font:
    """A non-embedded standard-14 font with WinAnsi encoding (Symbol/ZapfDingbats: built-in)."""

    def __init__(self, name):
        name = SHORT_NAMES.get(name.lower(), name) if isinstance(name, str) else name
        if name not in STD14:
            raise ValueError(f"unknown base-14 font {name!r}; use one of {sorted(SHORT_NAMES)} or pass fontfile=")
        self.name = name
        self.symbolic = name in ("Symbol", "ZapfDingbats")
        if self.symbolic:
            table = _base_encoding("/" + name)
        else:
            table = _base_encoding("/WinAnsiEncoding")
        self.table = table
        self.rev = {}
        for code in range(255, 31, -1):
            ch = table[code]
            if ch and ch != "\ufffd":
                self.rev[ch] = code
        self.asc, self.desc = std14_metrics(name)
        self.key = ("std", name)

    def encode(self, text):
        out = bytearray()
        bad = []
        for ch in unicodedata.normalize("NFC", text):
            c = self.rev.get(ch)
            if c is None:
                bad.append(ch)
                continue
            out.append(c)
        if bad:
            raise ValueError(f"characters {''.join(sorted(set(bad)))!r} are not available in {self.name} "
                             "(WinAnsi encoding); pass fontfile= with a TrueType/OpenType font for full Unicode")
        return bytes(out)

    def code_width(self, code):
        return std14_width(self.name, self.table[code])

    def text_width(self, text, size):
        return sum(self.code_width(c) for c in self.encode(text)) * size / 1000.0

    def space_codes(self):
        return {32}

    def pdf_object(self, writer, used=None):
        d = DictionaryObject()
        d[NameObject("/Type")] = NameObject("/Font")
        d[NameObject("/Subtype")] = NameObject("/Type1")
        d[NameObject("/BaseFont")] = NameObject("/" + self.name)
        if not self.symbolic:
            d[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
        d[NameObject("/FirstChar")] = NumberObject(32)
        d[NameObject("/LastChar")] = NumberObject(255)
        d[NameObject("/Widths")] = ArrayObject([FloatObject(round(self.code_width(c), 3)) for c in range(32, 256)])
        return writer._add_object(d)


class EmbeddedFont:
    """TrueType/OpenType font embedded as a Type0 (Identity-H) font, subset to the glyphs used.

    Uses fontTools (MIT) for parsing and subsetting. Character -> glyph is the font's Unicode
    cmap without shaping (fine for Latin/Cyrillic/Greek/Vietnamese precomposed letters; complex
    scripts that need GSUB/GPOS shaping are not supported)."""

    def __init__(self, path, index=0):
        try:
            from fontTools.ttLib import TTFont
        except ImportError as e:  # pragma: no cover
            raise ImportError("embedding fonts needs fontTools: pip install 'pdfwords[edit]'") from e
        self.path = path
        tt = TTFont(path, fontNumber=index, lazy=False)
        if "fvar" in tt:  # pin variable fonts to their default instance
            from fontTools.varLib import instancer
            tt = instancer.instantiateVariableFont(tt, {}, inplace=False)
        self.tt = tt
        self.cff = "CFF " in tt or "CFF2" in tt
        self.upem = tt["head"].unitsPerEm
        self.cmap = tt.getBestCmap() or {}
        self.order = tt.getGlyphOrder()
        self.gid = {n: i for i, n in enumerate(self.order)}
        self.hmtx = tt["hmtx"].metrics
        os2 = tt["OS/2"] if "OS/2" in tt else None
        hh = tt["hhea"]
        asc = (os2.sTypoAscender if os2 is not None and os2.sTypoAscender else hh.ascent) / self.upem
        dsc = (os2.sTypoDescender if os2 is not None and os2.sTypoDescender else hh.descent) / self.upem
        self.asc, self.desc = asc, dsc
        self.cap = (getattr(os2, "sCapHeight", 0) or int(asc * self.upem * 0.7)) / self.upem if os2 is not None else asc * 0.7
        h = tt["head"]
        self.bbox = [h.xMin, h.yMin, h.xMax, h.yMax]
        post = tt["post"] if "post" in tt else None
        self.italic = float(getattr(post, "italicAngle", 0) or 0)
        self.fixed = bool(getattr(post, "isFixedPitch", 0))
        name = None
        try:
            name = tt["name"].getDebugName(6)
        except Exception:
            pass
        self.psname = re.sub(r"[^A-Za-z0-9_.-]", "", name or "EmbeddedFont") or "EmbeddedFont"
        self.key = ("ttf", path, index)

    def _gid_of(self, ch):
        g = self.cmap.get(ord(ch))
        return None if g is None else self.gid.get(g)

    def encode(self, text):
        gids = []
        bad = []
        for ch in unicodedata.normalize("NFC", text):
            g = self._gid_of(ch)
            if g is None:
                # try the decomposed form (base + combining marks), each must exist
                parts = [self._gid_of(c) for c in unicodedata.normalize("NFD", ch)]
                if parts and all(p is not None for p in parts):
                    gids.extend((p, c) for p, c in zip(parts, unicodedata.normalize("NFD", ch)))
                    continue
                bad.append(ch)
                continue
            gids.append((g, ch))
        if bad:
            raise ValueError(f"font {self.path} has no glyphs for {''.join(sorted(set(bad)))!r}")
        return gids

    def gid_width(self, gid):
        return self.hmtx[self.order[gid]][0] * 1000.0 / self.upem

    def text_width(self, text, size):
        return sum(self.gid_width(g) for g, _ in self.encode(text)) * size / 1000.0

    def pdf_object(self, writer, used):
        """used: iterable of (gid, char). Returns an IndirectObject for a Type0 font dict."""
        import io
        import random
        from fontTools import subset
        used = list(used)
        gids = sorted({g for g, _ in used} | {0})
        opts = subset.Options()
        opts.retain_gids = True
        opts.notdef_outline = True
        opts.name_IDs = ["*"]
        opts.name_languages = ["*"]
        opts.layout_features = []
        opts.hinting = False
        opts.desubroutinize = True
        opts.drop_tables += ["DSIG", "GSUB", "GPOS", "GDEF", "kern", "BASE", "JSTF", "MATH", "STAT", "FFTM"]
        logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
        import copy
        tt = copy.deepcopy(self.tt)
        sub = subset.Subsetter(opts)
        sub.populate(gids=gids)
        sub.subset(tt)
        buf = io.BytesIO()
        tt.flavor = None
        tt.save(buf)
        data = buf.getvalue()
        tag = "".join(random.Random(hash((self.key, tuple(gids)))).choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
        base = NameObject("/" + tag + "+" + self.psname)
        if self.cff:
            ff = writer._add_object(_stream(data, Subtype=NameObject("/OpenType")).flate_encode())
            ffkey = "/FontFile3"
        else:
            ff = writer._add_object(_stream(data, Length1=NumberObject(len(data))).flate_encode())
            ffkey = "/FontFile2"
        s = 1000.0 / self.upem
        flags = 4  # symbolic (glyph access via CIDs)
        if self.fixed:
            flags |= 1
        if self.italic:
            flags |= 64
        fd = DictionaryObject({
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): base,
            NameObject("/Flags"): NumberObject(flags),
            NameObject("/FontBBox"): ArrayObject([FloatObject(round(v * s, 1)) for v in self.bbox]),
            NameObject("/ItalicAngle"): FloatObject(self.italic),
            NameObject("/Ascent"): FloatObject(round(self.asc * 1000, 1)),
            NameObject("/Descent"): FloatObject(round(self.desc * 1000, 1)),
            NameObject("/CapHeight"): FloatObject(round(self.cap * 1000, 1)),
            NameObject("/StemV"): NumberObject(80),
            NameObject(ffkey): ff,
        })
        fd_ref = writer._add_object(fd)
        W = ArrayObject()
        for g in gids:
            W.append(NumberObject(g))
            W.append(ArrayObject([FloatObject(round(self.gid_width(g), 2))]))
        cid = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0" if self.cff else "/CIDFontType2"),
            NameObject("/BaseFont"): base,
            NameObject("/CIDSystemInfo"): DictionaryObject({
                NameObject("/Registry"): TextStringObject("Adobe"), NameObject("/Ordering"): TextStringObject("Identity"),
                NameObject("/Supplement"): NumberObject(0)}),
            NameObject("/FontDescriptor"): fd_ref,
            NameObject("/DW"): NumberObject(1000),
            NameObject("/W"): W,
        })
        if not self.cff:
            cid[NameObject("/CIDToGIDMap")] = NameObject("/Identity")
        cid_ref = writer._add_object(cid)
        # ToUnicode: gid -> text (first char wins; decomposed sequences map each mark separately)
        m = {}
        for g, ch in used:
            m.setdefault(g, ch)
        lines = []
        items = sorted(m.items())
        for k in range(0, len(items), 100):
            chunk = items[k:k + 100]
            lines.append(f"{len(chunk)} beginbfchar")
            for g, ch in chunk:
                lines.append(f"<{g:04X}> <{ch.encode('utf-16-be').hex().upper()}>")
            lines.append("endbfchar")
        tu = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
              "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
              "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
              "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + "\n".join(lines) +
              "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
        tu_ref = writer._add_object(_stream(tu.encode()).flate_encode())
        t0 = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type0"),
            NameObject("/BaseFont"): base,
            NameObject("/Encoding"): NameObject("/Identity-H"),
            NameObject("/DescendantFonts"): ArrayObject([cid_ref]),
            NameObject("/ToUnicode"): tu_ref,
        })
        return writer._add_object(t0)


def load_font(fontname="helv", fontfile=None):
    if fontfile:
        return EmbeddedFont(str(fontfile))
    return Base14Font(fontname)
