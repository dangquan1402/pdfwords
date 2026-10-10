# SPDX-License-Identifier: Apache-2.0
"""Structure-preserving PDF optimiser (see docs/SHRINK.md).

Only image streams are re-encoded. Text, vector graphics, fonts, links, annotations, form
fields, outlines and the structure tree are left as they are. On top of that: byte-identical
streams are merged, thumbnails / private application data are dropped, oversized ICC profiles
can be replaced, everything is re-flated into object streams, and the result is never larger
than the input.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import time
import zlib
from dataclasses import dataclass, field, replace

import numpy as np
import pikepdf
from pikepdf import Name, PdfImage
from PIL import Image

from . import _codecs as codecs
from . import _imaging as imaging

# ---------------------------------------------------------------------------- presets


@dataclass(frozen=True)
class Preset:
    """Tuning knobs of a preset (see PRESETS). All sizes in bytes, resolutions in dpi."""
    name: str
    lossy: bool = True
    color_dpi: int = 150            # target resolution for colour / gray images
    mono_dpi: int = 300             # target resolution for 1-bit images (not resampled below this)
    resample_above: float = 1.3     # downsample only when effective dpi > target * this
    jpeg_quality: int = 72
    min_ssim: float = 0.90          # per-image guard: re-encoded vs source at the new size
    mrc: bool = True                # mixed raster content for scanned pages
    mrc_mask_dpi: int = 300
    mrc_bg_dpi: int = 100
    mrc_bg_quality: int = 50
    mrc_fg_dpi: int = 50
    mrc_min_ssim: float = 0.80
    min_page_ssim: float = 0.85     # whole-page render check (PDFium, 150 dpi) after re-encoding
    drop_xmp: bool = False          # document XMP packet
    icc_to_device_above: int = 0    # replace ICC profiles bigger than this by Device* (0 = never)
    icc_cmyk_only: bool = False
    raster_fallback: int = 0        # max pages to rasterise when that is much smaller (-1 = no limit)


PRESETS = {
    "lossless": Preset("lossless", lossy=False, mrc=False),
    "balanced": Preset("balanced", icc_to_device_above=256_000, icc_cmyk_only=True),
    "max": Preset("max", color_dpi=100, mono_dpi=200, jpeg_quality=55, min_ssim=0.85, mrc_bg_dpi=72,
                  mrc_bg_quality=40, mrc_fg_dpi=36, mrc_min_ssim=0.75, min_page_ssim=0.80, drop_xmp=True,
                  icc_to_device_above=100_000, raster_fallback=-1),
}


# ---------------------------------------------------------------------------- report


@dataclass
class OptimizeReport:
    """Result of optimize(). ``data`` holds the output bytes (also when written to a file)."""
    preset: str
    bytes_before: int = 0
    bytes_after: int = 0
    kept_original: bool = False
    reason: str = ""
    pages: int = 0
    seconds: float = 0.0
    images: list = field(default_factory=list)          # per-image decisions
    pages_rasterized: list = field(default_factory=list)
    ocr_pages: list = field(default_factory=list)
    page_checks: list = field(default_factory=list)     # [{"page", "ssim"}] PDFium render check
    counts: dict = field(default_factory=dict)
    codecs: dict = field(default_factory=dict)
    output: str = ""
    data: bytes = field(default=b"", repr=False)

    @property
    def saved_bytes(self):
        return self.bytes_before - self.bytes_after

    @property
    def reduction(self):
        """Fraction saved (0.0 .. 1.0)."""
        return self.saved_bytes / self.bytes_before if self.bytes_before else 0.0

    def to_dict(self):
        d = {k: getattr(self, k) for k in ("preset", "bytes_before", "bytes_after", "kept_original", "reason",
                                           "pages", "seconds", "images", "pages_rasterized", "ocr_pages",
                                           "page_checks", "counts", "codecs", "output")}
        d["reduction"] = round(self.reduction, 4)
        return d


# ---------------------------------------------------------------------------- image placements

def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _walk(stream, resources, ctm, out, page_no, page_area, depth=0):
    """Content-stream walk (fallback when PDFium's image list can't be matched to objects)."""
    if depth > 12 or resources is None:
        return
    xobjs = resources.get("/XObject", {})
    stack = []
    try:
        ops = pikepdf.parse_content_stream(stream)
    except Exception:
        return
    for operands, op in ops:
        op = str(op)
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ctm
        elif op == "cm" and len(operands) == 6:
            try:
                ctm = _mul(tuple(float(x) for x in operands), ctm)
            except Exception:
                pass
        elif op == "Do" and operands:
            xo = xobjs.get(operands[0]) if hasattr(xobjs, "get") else None
            if not isinstance(xo, pikepdf.Stream):
                continue
            st = xo.get("/Subtype")
            if st == "/Image":
                a, b, c, d = ctm[:4]
                wpt, hpt = math.hypot(a, b), math.hypot(c, d)
                if wpt > 0 and hpt > 0:
                    dpi = min(int(xo.get("/Width", 0)) / (wpt / 72), int(xo.get("/Height", 0)) / (hpt / 72))
                    out.setdefault(xo.objgen, []).append((dpi, abs(a * d - b * c) / page_area, page_no))
            elif st == "/Form":
                try:
                    m = tuple(float(x) for x in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                except Exception:
                    m = (1, 0, 0, 1, 0, 0)
                _walk(xo, xo.get("/Resources", resources), _mul(m, ctm), out, page_no, page_area, depth + 1)


def _page_images(res, out, seen, depth=0):
    """Image XObjects reachable from a resource dict: (w, h, raw_len) -> {objgen}."""
    if res is None or depth > 12:
        return
    for _, xo in (res.get("/XObject") or {}).items():
        if not isinstance(xo, pikepdf.Stream) or xo.objgen in seen:
            continue
        seen.add(xo.objgen)
        st = xo.get("/Subtype")
        if st == "/Image":
            try:
                key = (int(xo.get("/Width", 0)), int(xo.get("/Height", 0)), len(xo.read_raw_bytes()))
            except Exception:
                continue
            out.setdefault(key, set()).add(xo.objgen)
        elif st == "/Form":
            _page_images(xo.get("/Resources"), out, seen, depth + 1)


def placements(pdf, wdoc):
    """objgen -> [(effective_dpi, page_coverage, page_no)] for every image drawn by a page.

    Uses pdfwords' get_images() (PDFium: drawn quad, pixel size, encoded size) and matches each
    entry to its pikepdf object by (width, height, encoded length). Pages with images that can't
    be matched unambiguously fall back to a content-stream walk."""
    out = {}
    for i, page in enumerate(pdf.pages):
        table, seen = {}, set()
        _page_images(page.get("/Resources"), table, seen)
        if not table:
            continue
        wp = wdoc[i]
        x0, y0, x1, y1 = wp.rect
        area = max(1.0, (x1 - x0) * (y1 - y0))
        matched, ok = set(), True
        try:
            infos = wp.get_images()
        except Exception:
            infos, ok = [], False
        for d in infos:
            cands = table.get((int(d["width"]), int(d["height"]), int(d.get("size") or 0)))
            if not cands or len(cands) != 1:
                ok = False
                continue
            og = next(iter(cands))
            ul, ur, ll, _lr = d["quad"]
            wpt, hpt = math.dist(ul, ur), math.dist(ul, ll)
            if wpt <= 0 or hpt <= 0:
                continue
            dpi = min(d["width"] / (wpt / 72), d["height"] / (hpt / 72))
            out.setdefault(og, []).append((dpi, wpt * hpt / area, i))
            matched.add(og)
        reachable = set().union(*table.values())
        if not ok or reachable - matched:
            walked = {}
            _walk(page, page.get("/Resources"), (1, 0, 0, 1, 0, 0), walked, i, area)
            for og, uses in walked.items():
                if og not in matched:
                    out.setdefault(og, []).extend(uses)
    return out


# ---------------------------------------------------------------------------- structural cleanup

def _stream_kind(o):
    st = o.get("/Subtype")
    if st == "/Image":
        return "image"
    if st == "/Form":
        return "form"
    if "/Length1" in o or "/Length2" in o or st in ("/Type1C", "/CIDFontType0C", "/OpenType"):
        return "font"
    if "/N" in o:
        return "icc"
    return "other"


def dedupe_streams(pdf):
    """Merge byte-identical streams (fonts, images, ICC profiles, forms). Returns {kind: n}."""
    canon, remap, kinds = {}, {}, {}
    for o in pdf.objects:
        if isinstance(o, pikepdf.Stream):
            try:
                d = o.stream_dict.copy()
                if "/Length" in d:
                    del d["/Length"]
                key = hashlib.sha1(d.unparse() + b"\0" + o.read_raw_bytes()).digest()
            except Exception:
                continue
            if key in canon:
                remap[o.objgen] = canon[key]
                k = _stream_kind(o)
                kinds[k] = kinds.get(k, 0) + 1
            else:
                canon[key] = o
    if not remap:
        return kinds

    def fix(obj, depth=0):
        if depth > 40:
            return
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            for k in list(obj.keys()):
                v = obj.get(k)
                if isinstance(v, pikepdf.Object) and v.is_indirect and v.objgen in remap:
                    obj[k] = remap[v.objgen]
                elif isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
                    fix(v, depth + 1)
        elif isinstance(obj, pikepdf.Array):
            for i in range(len(obj)):
                v = obj[i]
                if isinstance(v, pikepdf.Object) and v.is_indirect and v.objgen in remap:
                    obj[i] = remap[v.objgen]
                elif isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
                    fix(v, depth + 1)

    for o in list(pdf.objects):
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
            fix(o)
    fix(pdf.trailer)
    return kinds


def cleanup(pdf, remove_thumbnails, drop_xmp, drop_info):
    n = 0
    for page in pdf.pages:
        for k in (("/Thumb", "/PieceInfo") if remove_thumbnails else ("/PieceInfo",)):
            if k in page.obj:
                del page.obj[k]
                n += 1
    root = pdf.Root
    if "/PieceInfo" in root:
        del root["/PieceInfo"]
        n += 1
    for o in pdf.objects:  # per-object XMP / application private data on images and forms
        if isinstance(o, pikepdf.Stream) and o.get("/Subtype") in ("/Image", "/Form"):
            for k in ("/Metadata", "/PieceInfo"):
                if k in o:
                    del o[k]
                    n += 1
    if drop_xmp and "/Metadata" in root:
        del root["/Metadata"]
        n += 1
    if drop_info and "/Info" in pdf.trailer:
        del pdf.trailer["/Info"]
        n += 1
    return n


def replace_big_icc(pdf, limit, cmyk_only=False):
    """Swap oversized ICC profiles (typically 0.5 MB print CMYK profiles) for DeviceGray/RGB/CMYK."""
    dev = {1: Name.DeviceGray, 3: Name.DeviceRGB, 4: Name.DeviceCMYK}
    big = {}
    for o in pdf.objects:
        if isinstance(o, pikepdf.Stream) and "/N" in o and o.get("/Subtype") is None:
            try:
                if len(o.read_raw_bytes()) > limit:
                    big[o.objgen] = dev.get(int(o.get("/N", 0)))
            except Exception:
                pass
    big = {k: v for k, v in big.items() if v is not None and (v == Name.DeviceCMYK or not cmyk_only)}
    if not big:
        return 0

    def target(v):
        if isinstance(v, pikepdf.Array) and len(v) == 2 and v[0] == Name.ICCBased and isinstance(v[1], pikepdf.Stream):
            return big.get(v[1].objgen)
        return None

    n = 0
    for o in list(pdf.objects):
        t = target(o) if isinstance(o, pikepdf.Array) and o.is_indirect else None
        if t is not None:
            pdf._replace_object(o.objgen, t)
            n += 1
    for o in list(pdf.objects):
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)):
            for k in list(o.keys()):
                v = o.get(k)
                if isinstance(v, pikepdf.Object) and v.is_indirect:
                    continue
                t = target(v)
                if t is not None:
                    o[k] = t
                    n += 1
                elif isinstance(v, pikepdf.Dictionary):   # ColorSpace resource dicts
                    for kk in list(v.keys()):
                        vv = v.get(kk)
                        t = target(vv) if isinstance(vv, pikepdf.Array) and not vv.is_indirect else None
                        if t is not None:
                            v[kk] = t
                            n += 1
        elif isinstance(o, pikepdf.Array):
            for i in range(len(o)):
                v = o[i]
                if isinstance(v, pikepdf.Array) and not v.is_indirect and target(v) is not None:
                    o[i] = target(v)
                    n += 1
    oi = pdf.Root.get("/OutputIntents")
    if oi is not None:
        keep = [x for x in oi if not (isinstance(x.get("/DestOutputProfile"), pikepdf.Stream)
                                      and x.DestOutputProfile.objgen in big)]
        if len(keep) != len(oi):
            pdf.Root.OutputIntents = pikepdf.Array(keep)
    return n


# ---------------------------------------------------------------------------- per-image recompression

def _set_image(obj, data, filt, w, h, cs, bpc=8, parms=None):
    obj.write(data, filter=filt, decode_parms=parms)
    obj.Width, obj.Height, obj.BitsPerComponent = w, h, bpc
    if cs is not None:
        obj.ColorSpace = cs
    for k in ("/Decode", "/Interpolate"):
        if k in obj:
            del obj[k]


def _bilevel_stream(im1):
    """PIL mode-'1' image (0 = black) -> (data, filter, decode_parms, codec) smallest of JBIG2 / G4."""
    cands = []
    jb = codecs.jbig2_generic(im1)
    if jb:
        cands.append((jb, Name.JBIG2Decode, None, "jbig2"))
    g4, black_is_1 = codecs.ccitt_g4(im1)
    cands.append((g4, Name.CCITTFaxDecode,
                  pikepdf.Dictionary(K=-1, Columns=im1.width, Rows=im1.height, BlackIs1=bool(black_is_1)), "g4"))
    return min(cands, key=lambda c: len(c[0]))


def _filters(obj):
    f = obj.get("/Filter")
    return [str(x) for x in (f if isinstance(f, pikepdf.Array) else [f] if f is not None else [])]


def _fix_smask(obj, nw, nh):
    sm = obj.get("/SMask")
    if not isinstance(sm, pikepdf.Stream) or (int(sm.Width), int(sm.Height)) == (nw, nh):
        return
    try:
        m = PdfImage(sm).as_pil_image().convert("L").resize((nw, nh), Image.LANCZOS)
    except Exception:
        return
    _set_image(sm, zlib.compress(m.tobytes(), 9), Name.FlateDecode, nw, nh, Name.DeviceGray)


def recompress_image(obj, eff_dpi, P, opts):
    """Re-encode one image XObject in place. Returns a decision dict."""
    raw_len = len(obj.read_raw_bytes())
    w0, h0 = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
    dec = {"object": f"{obj.objgen[0]} {obj.objgen[1]} R", "width": w0, "height": h0,
           "effective_dpi": round(eff_dpi, 1) if eff_dpi else None, "bytes_before": raw_len,
           "bytes_after": raw_len, "action": "kept", "reason": ""}
    filters = _filters(obj)
    if any(f in ("/JBIG2Decode", "/JPXDecode", "/CCITTFaxDecode") for f in filters):
        dec["reason"] = "already a specialised codec"
        return dec
    bpc = int(obj.get("/BitsPerComponent", 1 if obj.get("/ImageMask") else 8))
    if bool(obj.get("/ImageMask", False)) or bpc == 1:
        cs = obj.get("/ColorSpace")
        if "/Decode" in obj or not filters or filters[-1] == "/DCTDecode" or (
                not bool(obj.get("/ImageMask", False)) and cs not in (Name.DeviceGray, None)):
            dec["reason"] = "unsupported 1-bit layout"
            return dec
        try:
            im1 = Image.frombytes("1", (w0, h0), obj.read_bytes())
        except Exception:
            dec["reason"] = "could not decode"
            return dec
        new, f, parms, codec = _bilevel_stream(im1)
        if len(new) < 0.9 * raw_len:
            obj.write(new, filter=f, decode_parms=parms)
            dec.update(action=codec, bytes_after=len(new))
        else:
            dec["reason"] = "not smaller"
        return dec
    cs = obj.get("/ColorSpace")
    cs_ok = cs in (Name.DeviceRGB, Name.DeviceGray) or (
        isinstance(cs, pikepdf.Array) and len(cs) == 2 and cs[0] == Name.ICCBased
        and isinstance(cs[1], pikepdf.Stream) and int(cs[1].get("/N", 0)) in (1, 3))
    if not cs_ok or "/Decode" in obj or bpc != 8:
        dec["reason"] = "colour space / bit depth not handled (CMYK, indexed, Lab, 16-bit, /Decode)"
        return dec
    is_dct = bool(filters) and filters[-1] == "/DCTDecode"
    if not P.lossy:
        if is_dct and len(filters) == 1:
            new = codecs.jpeg_lossless(obj.read_raw_bytes())
            if new and len(new) < raw_len:
                obj.write(new, filter=Name.DCTDecode)
                dec.update(action="jpeg-lossless", bytes_after=len(new))
            else:
                dec["reason"] = "lossless JPEG optimisation unavailable or not smaller"
        else:
            dec["reason"] = "lossless preset (Flate recompression happens on save)"
        return dec
    try:
        im = PdfImage(obj).as_pil_image()
    except Exception:
        dec["reason"] = "could not decode"
        return dec
    if im.mode in ("RGBA", "LA"):   # alpha comes from /SMask, which is resized separately
        im = im.convert(im.mode[:-1])
    if im.mode not in ("L", "RGB"):
        dec["reason"] = f"image mode {im.mode}"
        return dec
    w, h = im.size
    scale = 1.0
    if eff_dpi and eff_dpi > P.color_dpi * P.resample_above:
        scale = P.color_dpi / eff_dpi
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    src = im.resize((nw, nh), Image.LANCZOS) if scale < 1 else im
    gs = opts["grayscale"]
    gray = im.mode == "L" or gs is True or (gs == "auto" and imaging.gray_like(src))
    if not is_dct and imaging.few_colors(src):   # line art / screenshots: stay lossless
        if scale < 1:
            data = zlib.compress(src.convert("L" if gray else "RGB").tobytes(), 9)
            if len(data) < raw_len:
                _set_image(obj, data, Name.FlateDecode, nw, nh, Name.DeviceGray if gray and im.mode == "RGB" else None)
                _fix_smask(obj, nw, nh)
                dec.update(action="flate-resampled", bytes_after=len(data), new_width=nw, new_height=nh)
                return dec
        dec["reason"] = "line art kept lossless"
        return dec
    q = P.jpeg_quality
    ok, s, new = False, 0.0, b""
    for _ in range(2):
        new = codecs.jpeg(src, q, gray=gray)
        s = imaging.ssim(src, Image.open(io.BytesIO(new)))
        if s >= P.min_ssim:
            ok = True
            break
        q = min(92, q + 15)
    dec["ssim"] = round(s, 4)
    if not ok:
        dec["reason"] = f"SSIM {s:.3f} below {P.min_ssim}"
        return dec
    if len(new) >= 0.9 * raw_len:
        dec["reason"] = "not >=10% smaller"
        return dec
    newcs = Name.DeviceGray if gray else cs
    _set_image(obj, new, Name.DCTDecode, nw, nh, newcs)
    _fix_smask(obj, nw, nh)
    dec.update(action="jpeg", bytes_after=len(new), new_width=nw, new_height=nh, quality=q,
               grayscale=bool(gray and im.mode == "RGB"))
    return dec


# ---------------------------------------------------------------------------- MRC for scanned pages

def mrc_layers(im, dpi, P):
    """Split a scanned page image into (1-bit mask, low-res background JPEG, foreground colour/JPEG)."""
    if dpi > P.mrc_mask_dpi * 1.1:
        k = P.mrc_mask_dpi / dpi
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        dpi = P.mrc_mask_dpi
    color = im.mode == "RGB" and not imaging.gray_like(im)
    rgb = np.asarray(im.convert("RGB" if color else "L"), dtype=np.float32)
    if rgb.ndim == 2:
        rgb = rgb[..., None]
    g = np.asarray(im.convert("L"))
    mask = imaging.sauvola_mask(g, dpi)
    ink = float(mask.mean())
    if not 0.002 < ink < 0.35:
        return None
    grown = imaging.dilate(mask, max(1, round(2 * dpi / 300)))
    W, H = im.size
    bs = (max(1, round(W * P.mrc_bg_dpi / dpi)), max(1, round(H * P.mrc_bg_dpi / dpi)))
    bg = imaging.norm_resize(rgb, (~grown).astype(np.float32), bs)
    bg_im = Image.fromarray(np.ascontiguousarray(bg if color else bg[..., 0]))
    bg_jpg = codecs.jpeg(bg_im, P.mrc_bg_quality, gray=not color)
    inkpx = rgb[mask]
    if not color or inkpx.std(axis=0).max() < 28:
        fg = ("solid", tuple(float(v) / 255 for v in np.percentile(inkpx, 15, axis=0)))  # ink core, not AA edges
        fg_bytes = 0
    else:
        fs = (max(1, round(W * P.mrc_fg_dpi / dpi)), max(1, round(H * P.mrc_fg_dpi / dpi)))
        fgarr = imaging.norm_resize(rgb, mask.astype(np.float32), fs)
        fg_jpg = codecs.jpeg(Image.fromarray(fgarr), 60)
        fg = ("image", fg_jpg, fs)
        fg_bytes = len(fg_jpg)
    im1 = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert("1", dither=Image.NONE)
    mdata, mfilt, mparms, codec = _bilevel_stream(im1)
    # composite at 150 dpi for the quality guard
    cs = (max(8, round(W * 150 / dpi)), max(8, round(H * 150 / dpi)))
    comp = np.asarray(bg_im.convert("RGB").resize(cs, Image.BILINEAR), dtype=np.float32)
    cov = np.asarray(Image.fromarray(mask.astype(np.float32)).resize(cs, Image.BOX))[..., None]
    if fg[0] == "solid":
        col = np.array(fg[1] * (3 if len(fg[1]) == 1 else 1)) * 255
        fgc = np.broadcast_to(col, comp.shape)
    else:
        fgc = np.asarray(Image.open(io.BytesIO(fg[1])).convert("RGB").resize(cs, Image.BILINEAR), dtype=np.float32)
    comp = comp * (1 - cov) + fgc * cov
    s = imaging.ssim(im.convert("L").resize(cs, Image.BOX), Image.fromarray(comp.astype(np.uint8)).convert("L"))
    return {"mask": (mdata, mfilt, mparms, W, H), "bg": (bg_jpg, bs, color), "fg": fg, "ssim": s,
            "bytes": len(mdata) + len(bg_jpg) + fg_bytes, "codec": codec}


def apply_mrc(pdf, obj, L):
    """Turn the image object into a Form XObject drawing background + masked foreground in the same
    unit square, so every page/form using it (and any text layer or annotation) stays untouched."""
    mdata, mfilt, mparms, W, H = L["mask"]
    mask = pikepdf.Stream(pdf, mdata)
    mask.write(mdata, filter=mfilt, decode_parms=mparms)
    mask.Type, mask.Subtype = Name.XObject, Name.Image
    mask.Width, mask.Height, mask.ImageMask, mask.BitsPerComponent = W, H, True, 1
    bg_jpg, (bw, bh), color = L["bg"]
    bg = pikepdf.Stream(pdf, bg_jpg, Type=Name.XObject, Subtype=Name.Image, Width=bw, Height=bh,
                        ColorSpace=Name.DeviceRGB if color else Name.DeviceGray, BitsPerComponent=8,
                        Filter=Name.DCTDecode)
    if L["fg"][0] == "solid":
        c = L["fg"][1]
        col = f"{c[0]:.3f} g" if len(c) == 1 else f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} rg"
        content = f"/BG Do {col} /FG Do".encode()
        fg = mask
    else:
        fg_jpg, (fw, fh) = L["fg"][1], L["fg"][2]
        fg = pikepdf.Stream(pdf, fg_jpg, Type=Name.XObject, Subtype=Name.Image, Width=fw, Height=fh,
                            ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode)
        fg.Mask = mask
        content = b"/BG Do /FG Do"
    obj.write(content)
    for k in list(obj.keys()):
        if k != "/Length":
            del obj[k]
    obj.Type, obj.Subtype = Name.XObject, Name.Form
    obj.BBox = [0, 0, 1, 1]
    obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(BG=bg, FG=fg))


# ---------------------------------------------------------------------------- invisible text layer

def _encodable(words):
    try:
        "".join(w[4] for w in words).encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def add_text_layer(pdf, page, words, cropbox):
    """Invisible (render mode 3) Helvetica text at the given word boxes (pdfwords coordinates:
    points, top-left origin, relative to the unrotated CropBox)."""
    if not words:
        return
    font = pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                                Encoding=Name.WinAnsiEncoding))
    res = page.obj.get("/Resources")
    if res is None:
        page.obj.Resources = res = pikepdf.Dictionary()
    if "/Font" not in res:
        res.Font = pikepdf.Dictionary()
    res.Font.PWHidden = font
    cx0, cy1 = cropbox[0], cropbox[3]
    ops = ["BT 3 Tr"]
    for x0, y0, x1, y1, t, *_ in words:
        s = t.encode("cp1252", "replace")
        h = max(1.0, y1 - y0)
        size = h * 0.9
        natural = 0.5 * size * max(1, len(s))   # rough Helvetica advance
        tz = max(10.0, min(400.0, 100.0 * (x1 - x0) / max(natural, 1e-3)))
        esc = s.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").decode("latin-1")
        ops.append(f"/PWHidden {size:.2f} Tf {tz:.1f} Tz 1 0 0 1 {cx0 + x0:.2f} {cy1 - y1 + 0.2 * h:.2f} Tm ({esc}) Tj")
    ops.append("ET")
    page.contents_add(pdf.make_stream(("q " + "\n".join(ops) + " Q").encode("latin-1")), prepend=False)


# ---------------------------------------------------------------------------- per-page raster fallback

def _vector_cost(page):
    """Compressed size of the page's content streams + form XObjects (what rasterising removes)."""
    seen, total = set(), 0

    def visit(streams, res, depth=0):
        nonlocal total
        for s in streams:
            try:
                total += len(zlib.compress(s.read_bytes(), 6))
            except Exception:
                pass
        if res is None or depth > 8:
            return
        for _, xo in (res.get("/XObject") or {}).items():
            if isinstance(xo, pikepdf.Stream) and xo.get("/Subtype") == "/Form" and xo.objgen not in seen:
                seen.add(xo.objgen)
                visit([xo], xo.get("/Resources"), depth + 1)

    c = page.obj.get("/Contents")
    visit(list(c) if isinstance(c, pikepdf.Array) else [c] if c is not None else [], page.obj.get("/Resources"))
    return total


def raster_fallback(pdf, wdoc, i, P, min_cost=400_000):
    """Rasterise page i only if its vector content is heavy and the raster is < 50 % of it. Text is
    kept as an invisible layer (search/copy still work); annotations, links and forms are untouched."""
    page = pdf.pages[i]
    cost = _vector_cost(page)
    if cost < min_cost:
        return None
    wp = wdoc[i]
    words = wp.get_text("words")
    if words and not _encodable(words):
        return None   # the hidden layer is WinAnsi-only: keep pages with other scripts vector
    img = wp.render(dpi=max(P.color_dpi, 150), rotated=False, output="pil", annots=False, forms=False)
    jpg = codecs.jpeg(img, min(95, P.jpeg_quality + 10))
    if len(jpg) > 0.5 * cost:
        return None
    cb = [float(v) for v in page.obj.get("/CropBox", page.mediabox)]
    w, h = cb[2] - cb[0], cb[3] - cb[1]
    xo = pikepdf.Stream(pdf, jpg, Type=Name.XObject, Subtype=Name.Image, Width=img.width, Height=img.height,
                        ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode)
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Rf=xo))
    page.obj.Contents = pdf.make_stream(f"q {w:.4f} 0 0 {h:.4f} {cb[0]:.4f} {cb[1]:.4f} cm /Rf Do Q".encode())
    add_text_layer(pdf, page, words, cb)
    return {"page": i, "vector_bytes": cost, "raster_bytes": len(jpg)}


# ---------------------------------------------------------------------------- main pipeline

def _is_signed(pdf):
    af = pdf.Root.get("/AcroForm")
    if af is None:
        return False
    try:
        if int(af.get("/SigFlags", 0)) & 1:
            return True
    except Exception:
        pass
    stack = list(af.get("/Fields", []))
    while stack:
        f = stack.pop()
        if not isinstance(f, pikepdf.Dictionary):
            continue
        if f.get("/FT") == Name.Sig and f.get("/V") is not None:
            return True
        stack.extend(f.get("/Kids", []))
    return False


def _pages_using(pl, ogs):
    return sorted({u[2] for og in ogs for u in pl.get(og, [])})


def _run(src_bytes, P, opts, protect, progress):
    """One optimisation pass. Returns (out_bytes, details)."""
    import pdfwords

    pdf = pikepdf.open(io.BytesIO(src_bytes), password=opts["password"] or "")
    wdoc = pdfwords.open(src_bytes, password=opts["password"])
    det = {"images": [], "pages_rasterized": [], "ocr_pages": [], "counts": {}, "changed_pages": set(),
           "image_pages": {}}
    counts = det["counts"]
    try:
        n = len(pdf.pages)
        if opts["dedupe"]:
            kinds = dedupe_streams(pdf)
            counts["deduped_streams"] = sum(kinds.values())
            counts["deduped_fonts"] = kinds.get("font", 0)
            counts["deduped_images"] = kinds.get("image", 0)
        counts["cleanup"] = cleanup(pdf, opts["remove_thumbnails"], opts["drop_xmp"], opts["drop_info"])
        if P.icc_to_device_above:
            counts["icc_replaced"] = replace_big_icc(pdf, P.icc_to_device_above, P.icc_cmyk_only)
        pl = placements(pdf, wdoc)
        quality = {}
        for i in range(n):
            try:
                quality[i] = wdoc[i].text_quality()
            except Exception:
                quality[i] = {"chars": 0, "needs_ocr": True, "invisible_ratio": 0.0}
        mrc_mode = opts["mrc"]
        total = len(pl)
        for k, (og, uses) in enumerate(pl.items()):
            if progress:
                progress("images", k, total)
            obj = pdf.get_object(og)
            if not isinstance(obj, pikepdf.Stream):
                continue
            pages = sorted({u[2] for u in uses})
            if og in protect:
                det["images"].append({"object": f"{og[0]} {og[1]} R", "pages": pages, "action": "kept",
                                      "reason": "protected after page render check"})
                continue
            eff = min(u[0] for u in uses)
            cover = max(u[1] for u in uses)
            scan_like = all(quality[p]["chars"] == 0 or quality[p].get("invisible_ratio", 0) > 0.8
                            or quality[p].get("needs_ocr") for p in pages)
            want_mrc = P.lossy and len(uses) == 1 and cover > 0.85 and eff >= 150 and (
                mrc_mode is True or (mrc_mode == "auto" and P.mrc and scan_like))
            if want_mrc and "/SMask" not in obj and "/Mask" not in obj \
                    and int(obj.get("/BitsPerComponent", 8)) == 8 \
                    and obj.get("/ColorSpace") in (Name.DeviceRGB, Name.DeviceGray):
                raw_len = len(obj.read_raw_bytes())
                try:
                    im = PdfImage(obj).as_pil_image()
                except Exception:
                    im = None
                L = mrc_layers(im, eff, P) if im is not None and im.mode in ("L", "RGB") else None
                if L and L["ssim"] >= P.mrc_min_ssim and L["bytes"] < 0.8 * raw_len:
                    w0, h0 = int(obj.Width), int(obj.Height)
                    apply_mrc(pdf, obj, L)
                    det["images"].append({"object": f"{og[0]} {og[1]} R", "pages": pages, "width": w0, "height": h0,
                                          "effective_dpi": round(eff, 1), "bytes_before": raw_len,
                                          "bytes_after": L["bytes"], "action": "mrc", "mask_codec": L["codec"],
                                          "ssim": round(L["ssim"], 4), "reason": ""})
                    det["changed_pages"].update(pages)
                    det["image_pages"][og] = pages
                    continue
                counts["mrc_rejected"] = counts.get("mrc_rejected", 0) + 1
            d = recompress_image(obj, eff, P, opts)
            d["pages"] = pages
            det["images"].append(d)
            if d["action"] != "kept":
                det["changed_pages"].update(pages)
                det["image_pages"][og] = pages
        # images never drawn by page content (annotation appearances, patterns, ...): lossless only
        lossless = replace(P, lossy=False)
        for o in list(pdf.objects):
            if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image" and o.objgen not in pl \
                    and o.objgen not in protect and not bool(o.get("/ImageMask", False)):
                d = recompress_image(o, None, lossless, opts)
                if d["action"] != "kept":
                    d["pages"] = []
                    det["images"].append(d)
        limit = opts["max_raster_fallback"]
        if limit != 0 and P.lossy and "/StructTreeRoot" not in pdf.Root:
            for i in range(n):
                if limit > 0 and len(det["pages_rasterized"]) >= limit:
                    break
                if progress:
                    progress("raster-fallback", i, n)
                if i in opts.get("protect_pages", ()):
                    continue
                r = raster_fallback(pdf, wdoc, i, P)
                if r:
                    det["pages_rasterized"].append(r)
                    det["changed_pages"].add(i)
        if opts["ocr"]:
            spec = "auto" if opts["ocr"] is True else opts["ocr"]
            for i in range(n):
                q = quality[i]
                if not (q["chars"] == 0 and q.get("needs_ocr")):
                    continue
                if progress:
                    progress("ocr", i, n)
                words = wdoc[i].get_text("words", ocr=spec)
                if words:
                    page = pdf.pages[i]
                    cb = [float(v) for v in page.obj.get("/CropBox", page.mediabox)]
                    add_text_layer(pdf, page, words, cb)
                    det["ocr_pages"].append(i)
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        if progress:
            progress("write", 0, 1)
        pikepdf.settings.set_flate_compression_level(9)
        out = io.BytesIO()
        pdf.save(out, compress_streams=True, recompress_flate=True,
                 stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate, linearize=opts["linearize"])
        return out.getvalue(), det
    finally:
        pikepdf.settings.set_flate_compression_level(-1)
        wdoc.close()
        pdf.close()


def _page_ssims(src_bytes, out_bytes, pages, password, dpi=150):
    import pdfwords

    res = []
    with pdfwords.open(src_bytes, password=password) as a, pdfwords.open(out_bytes) as b:
        for i in pages:
            ia = a[i].render(dpi=dpi, grayscale=True, output="pil")
            ib = b[i].render(dpi=dpi, grayscale=True, output="pil")
            res.append({"page": i, "ssim": round(imaging.ssim(ia, ib), 4)})
    return res


def optimize(source, output=None, preset="balanced", *, password=None, target_dpi=None, jpeg_quality=None,
             grayscale="auto", mrc="auto", ocr=False, strip_metadata=None, remove_thumbnails=True, dedupe=True,
             min_ssim=None, max_raster_fallback=None, verify=True, linearize=False, progress=None):
    """Shrink a PDF while keeping its structure. Returns an OptimizeReport (``.data`` = output bytes).

    source: path, bytes or file object. output: path to write (None: only return the bytes).
    preset: "lossless" | "balanced" | "max" (see PRESETS / docs/SHRINK.md).
    target_dpi: colour/gray image resolution (default 150 balanced, 100 max); images are only
        downsampled when drawn at > 1.3x this. jpeg_quality: 1-95 (default 72 / 55).
    grayscale: "auto" (convert images that are effectively gray) | True (all images) | False.
    mrc: "auto" (scanned pages in lossy presets) | True (any full-page image) | False.
    ocr: False | True ("auto" engine) | engine name / pdfwords OCR spec: add an invisible text layer
        to pages without text (needs pdfwords[ocr]).
    strip_metadata: None (preset: max drops the XMP packet) | True (drop XMP and the Info dict) | False.
    remove_thumbnails: drop embedded page thumbnails. dedupe: merge byte-identical streams.
    min_ssim: per-image quality floor (default 0.90 balanced, 0.85 max).
    max_raster_fallback: max pages that may be rasterised when their vector content is heavy and a
        raster is < 50 % of it (default: 0 for lossless/balanced, unlimited for max; -1 = unlimited).
    verify: render changed pages with PDFium before/after; images on pages below the preset's
        min_page_ssim are restored (one retry).
    progress: callable(stage, done, total).
    Never larger than the input: if the result is not smaller, the original bytes are returned
    (``kept_original=True``). Signed and encrypted PDFs are returned unchanged."""
    t0 = time.time()
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r} (choose from {', '.join(PRESETS)})")
    P = PRESETS[preset]
    if target_dpi:
        P = replace(P, color_dpi=int(target_dpi), mono_dpi=max(P.mono_dpi, int(target_dpi)))
    if jpeg_quality:
        P = replace(P, jpeg_quality=int(jpeg_quality), mrc_bg_quality=min(P.mrc_bg_quality, int(jpeg_quality)))
    if min_ssim is not None:
        P = replace(P, min_ssim=float(min_ssim))
    if grayscale not in ("auto", True, False):
        raise ValueError("grayscale must be 'auto', True or False")
    if mrc not in ("auto", True, False):
        raise ValueError("mrc must be 'auto', True or False")
    if hasattr(source, "read"):
        src_bytes = source.read()
        name = getattr(source, "name", "")
    elif isinstance(source, (bytes, bytearray, memoryview)):
        src_bytes, name = bytes(source), ""
    else:
        name = os.fspath(source)
        with open(name, "rb") as f:
            src_bytes = f.read()
    opts = {"password": password, "grayscale": grayscale, "mrc": mrc, "ocr": ocr, "dedupe": dedupe,
            "remove_thumbnails": remove_thumbnails, "linearize": linearize,
            "drop_xmp": P.drop_xmp if strip_metadata is None else bool(strip_metadata),
            "drop_info": bool(strip_metadata),
            "max_raster_fallback": P.raster_fallback if max_raster_fallback is None else int(max_raster_fallback)}
    rep = OptimizeReport(preset=preset, bytes_before=len(src_bytes), codecs=codecs.available())

    def finish(data, kept, reason=""):
        rep.data, rep.bytes_after, rep.kept_original, rep.reason = data, len(data), kept, reason
        rep.seconds = round(time.time() - t0, 3)
        if output is not None:
            rep.output = os.fspath(output)
            with open(rep.output, "wb") as f:
                f.write(data)
        return rep

    with pikepdf.open(io.BytesIO(src_bytes), password=password or "") as probe:
        rep.pages = len(probe.pages)
        if probe.is_encrypted:
            return finish(src_bytes, True, "encrypted: left unchanged (saving would drop or alter the encryption)")
        if _is_signed(probe):
            return finish(src_bytes, True, "digitally signed: left unchanged (rewriting would invalidate signatures)")
    del name

    protect = set()
    for attempt in range(2):
        out, det = _run(src_bytes, P, opts, protect, progress)
        checks = []
        if verify and det["changed_pages"]:
            if progress:
                progress("verify", 0, 1)
            checks = _page_ssims(src_bytes, out, sorted(det["changed_pages"]), password)
            bad = {c["page"] for c in checks if c["ssim"] < P.min_page_ssim}
            if bad and attempt == 0:
                protect |= {og for og, pages in det["image_pages"].items() if bad & set(pages)}
                opts = dict(opts, protect_pages=set(opts.get("protect_pages", ())) | bad)
                continue
        break
    rep.images, rep.pages_rasterized, rep.ocr_pages = det["images"], det["pages_rasterized"], det["ocr_pages"]
    rep.page_checks, rep.counts = checks, det["counts"]
    rep.counts["images_reencoded"] = sum(1 for d in det["images"] if d.get("action") not in ("kept", None))
    if len(out) >= len(src_bytes):
        return finish(src_bytes, True, "result was not smaller")
    return finish(out, False)
