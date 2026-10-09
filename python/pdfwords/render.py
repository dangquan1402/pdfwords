# SPDX-License-Identifier: Apache-2.0
"""Page rendering on PDFium (the same engine and the same coordinates as the text).

    img = page.render(dpi=150)                       # numpy uint8 (H, W, 3), RGB
    img = page.render(dpi=300, grayscale=True)       # (H, W), about 2x faster
    img = page.render(max_side=1600, output="pil")   # PIL.Image
    png = page.render(dpi=150, output="png")         # encoded bytes (png | jpeg | webp | tiff)
    pix = page.get_pixmap(dpi=150)                   # PyMuPDF-style Pixmap
    x, y = page.pixel_to_pdf(px, py, dpi=150)        # map pixels <-> get_text() coordinates

Design notes (see docs/RENDERING.md):
* Pixel size = round(points * scale). pypdfium2's PdfPage.render() uses ceil(), which turns
  792 pt x 150/72 = 1650.0000000002 into 1651 px and stretches the page by one pixel.
* PDFium renders straight into the output buffer (numpy array or bytearray); there is no
  intermediate PdfBitmap and no copy. A caller-owned array can be reused with out=.
* A clip renders the sub-rectangle of the full-page raster at integer pixel offsets, so clips
  and tiles are pixel-identical to the corresponding region of a full render.
* AcroForm field values are drawn (FPDF_FFLDraw) from a second, form-enabled handle when the
  document has forms; timeout= uses PDFium's progressive renderer and aborts between objects.
"""
from __future__ import annotations

import ctypes as _ct
import math
import time

__all__ = ["RenderTimeout", "Pixmap", "render", "geometry", "pixel_to_pdf", "pdf_to_pixel", "render_tiles",
           "DEFAULT_MAX_PIXELS", "encode"]

# refuse output images above this many pixels (decompression-bomb style guard); None disables
DEFAULT_MAX_PIXELS = 1 << 28   # 268 M pixels, e.g. 16384 x 16384
_FORMATS = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP", "tiff": "TIFF", "tif": "TIFF",
            "bmp": "BMP", "ppm": "PPM", "pnm": "PPM", "gif": "GIF"}
_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "TIFF": "tif", "BMP": "bmp", "PPM": "ppm", "GIF": "gif"}


class RenderTimeout(TimeoutError):
    """Rendering a page took longer than `timeout` seconds and was aborted."""


class _Geom:
    """Pixel geometry of one render: full-page raster size, the clip window in it, the scale."""
    __slots__ = ("full_w", "full_h", "sx", "sy", "px0", "py0", "w", "h", "rotate", "rotated", "pw", "ph", "page_rot")

    def as_dict(self):
        return {k: getattr(self, k) for k in ("full_w", "full_h", "sx", "sy", "px0", "py0", "w", "h", "rotated")}


def _rint(v):
    # round half up (Python's round() is banker's rounding); the epsilon absorbs float noise
    return int(math.floor(v + 0.5 + 1e-9))


def geometry(page, dpi=None, scale=None, size=None, max_side=None, clip=None, rotated=True):
    """Compute the raster geometry used by render() (and pixel_to_pdf / pdf_to_pixel)."""
    given = sum(x is not None for x in (dpi, scale, size, max_side))
    if given > 1:
        raise ValueError("pass only one of dpi=, scale=, size=, max_side=")
    w, h, rot = page._w, page._h, page.rotation
    # page size in the output space: displayed page (rotated=True) or unrotated page
    pw, ph = (h, w) if (rotated and rot in (90, 270)) else (w, h)
    if size is not None:
        if isinstance(size, (int, float)):
            size = (size, None)
        sw, sh = size
        if sw is None and sh is None:
            raise ValueError("size=(None, None)")
        if sw is None:
            sx = sy = sh / ph
        elif sh is None:
            sx = sy = sw / pw
        else:
            sx, sy = sw / pw, sh / ph
    elif max_side is not None:
        sx = sy = float(max_side) / max(pw, ph)
    else:
        s = scale if scale is not None else (dpi if dpi is not None else 150) / 72.0
        if isinstance(s, (tuple, list)):
            sx, sy = map(float, s)
        else:
            sx = sy = float(s)
    if sx <= 0 or sy <= 0:
        raise ValueError("scale must be positive")
    g = _Geom()
    g.pw, g.ph, g.page_rot, g.rotated = pw, ph, rot, bool(rotated)
    g.full_w = max(1, _rint(pw * sx))
    g.full_h = max(1, _rint(ph * sy))
    if size is not None and size[0] is not None and size[1] is not None:
        g.full_w, g.full_h = int(size[0]), int(size[1])
    # effective scale = integer raster size / page size (what PDFium actually draws)
    g.sx, g.sy = g.full_w / pw, g.full_h / ph
    # FPDF_RenderPageBitmap rotates relative to the displayed page: undo /Rotate for rotated=False
    g.rotate = 0 if rotated else ((360 - rot) % 360) // 90
    if clip is not None and clip is not True:
        x0, y0, x1, y1 = map(float, clip[:4])
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        g.px0, g.py0 = _rint(x0 * g.sx), _rint(y0 * g.sy)
        g.w = max(1, _rint(x1 * g.sx) - g.px0)
        g.h = max(1, _rint(y1 * g.sy) - g.py0)
    else:
        g.px0 = g.py0 = 0
        g.w, g.h = g.full_w, g.full_h
    return g


def pixel_to_pdf(page, px, py, **kw):
    """Pixel position in an image made by render(**kw) -> (x, y) in get_text() coordinates
    (displayed page for rotated=True (default), unrotated page for rotated=False)."""
    g = geometry(page, **kw)
    return ((px + g.px0) / g.sx, (py + g.py0) / g.sy)


def pdf_to_pixel(page, x, y, **kw):
    """(x, y) in get_text() coordinates -> pixel position in an image made by render(**kw)."""
    g = geometry(page, **kw)
    return (x * g.sx - g.px0, y * g.sy - g.py0)


def _channels(alpha, grayscale):
    if grayscale:
        return 1
    return 4 if alpha else 3


def _color_argb(bg, alpha):
    """background: (r, g, b[, a]) as 0..255 ints or 0..1 floats; None = white (transparent
    with alpha=True). Returns PDFium's 0xAARRGGBB."""
    if bg is None:
        bg = (255, 255, 255, 0) if alpha else (255, 255, 255)
    if isinstance(bg, (int, float)):
        bg = (bg, bg, bg)
    bg = tuple(bg)
    if bg and all(isinstance(c, float) for c in bg) and max(bg) <= 1.0:
        bg = tuple(c * 255 for c in bg)
    bg = tuple(max(0, min(255, int(round(c)))) for c in bg)
    r, g, b = bg[:3]
    a = bg[3] if len(bg) > 3 else 255
    return (a << 24) | (r << 16) | (g << 8) | b


def _alloc(g, ch, out, use_numpy):
    n = g.w * g.h * ch
    if out is not None:
        try:
            import numpy as np
            if isinstance(out, np.ndarray):
                want = (g.h, g.w) if ch == 1 else (g.h, g.w, ch)
                if out.shape != want or out.dtype != np.uint8 or not out.flags.c_contiguous or not out.flags.writeable:
                    raise ValueError(f"out= must be a writeable C-contiguous uint8 array of shape {want}")
                return out, out.ctypes.data
        except ImportError:
            pass
        mv = memoryview(out)
        if mv.readonly or mv.nbytes < n:
            raise ValueError(f"out= must be a writeable buffer of at least {n} bytes")
        return out, _ct.addressof((_ct.c_ubyte * n).from_buffer(out))
    if use_numpy:
        import numpy as np
        arr = np.empty((g.h, g.w) if ch == 1 else (g.h, g.w, ch), dtype=np.uint8)
        return arr, arr.ctypes.data
    buf = bytearray(n)
    return buf, _ct.addressof((_ct.c_ubyte * n).from_buffer(buf))


def _render_page(raw_page, formenv, g, ch, addr, flags, argb, timeout):
    import pypdfium2.raw as R
    fmt = {1: R.FPDFBitmap_Gray, 3: R.FPDFBitmap_BGR, 4: R.FPDFBitmap_BGRA}[ch]
    bm = R.FPDFBitmap_CreateEx(g.w, g.h, fmt, _ct.c_void_p(addr), g.w * ch)
    if not bm:
        raise MemoryError(f"PDFium could not create a {g.w}x{g.h} bitmap")
    try:
        R.FPDFBitmap_FillRect(bm, 0, 0, g.w, g.h, argb)
        args = (-g.px0, -g.py0, g.full_w, g.full_h, g.rotate, flags)
        if timeout is None:
            R.FPDF_RenderPageBitmap(bm, raw_page, *args)
        else:
            deadline = time.monotonic() + float(timeout)
            pause = R.IFSDK_PAUSE(version=1)
            cb = type(pause.NeedToPauseNow)(lambda _p: int(time.monotonic() >= deadline))
            pause.NeedToPauseNow = cb
            st = R.FPDF_RenderPageBitmap_Start(bm, raw_page, *args, _ct.byref(pause))
            while st == R.FPDF_RENDER_TOBECONTINUED:
                if time.monotonic() >= deadline:
                    R.FPDF_RenderPage_Close(raw_page)
                    raise RenderTimeout(f"rendering took longer than {timeout} s")
                st = R.FPDF_RenderPage_Continue(raw_page, _ct.byref(pause))
            R.FPDF_RenderPage_Close(raw_page)
            if st == R.FPDF_RENDER_FAILED:
                raise RuntimeError("PDFium failed to render the page")
        if formenv is not None:
            R.FPDF_FFLDraw(formenv, bm, raw_page, *args)
    finally:
        R.FPDFBitmap_Destroy(bm)


def _flags(alpha, grayscale, annots, antialias, extra=0):
    import pypdfium2.raw as R
    f = extra
    if annots:
        f |= R.FPDF_ANNOT
    if grayscale:
        f |= R.FPDF_GRAYSCALE
    else:   # RGB(A) byte order instead of PDFium's native BGR(A)
        f |= R.FPDF_REVERSE_BYTE_ORDER
    if not antialias:
        f |= R.FPDF_RENDER_NO_SMOOTHTEXT | R.FPDF_RENDER_NO_SMOOTHIMAGE | R.FPDF_RENDER_NO_SMOOTHPATH
    return f


def _page_handles(page, forms):
    """(raw FPDF_PAGE, form handle or None): the form-enabled copy of the page when the document
    has an AcroForm and forms=True (flattened documents already carry the values)."""
    doc = page.parent
    if forms and not doc._flatten:
        if page._cache.get("has_forms") is None:
            import pypdfium2.raw as R
            page._fresh()
            page._cache["has_forms"] = R.FPDF_GetFormType(doc._pdf.raw) != R.FORMTYPE_NONE
        if page._cache["has_forms"]:
            fp = page._cache.get("form_page")
            if fp is None:
                fp = page._cache["form_page"] = doc._forms_pdf()[page.number]
            return fp.raw, fp.formenv.raw if fp.formenv else None
    return page._page.raw, None


def encode(img, fmt="png", quality=90, **save_kw):
    """Encode a numpy array / PIL image as png | jpeg | webp | tiff | bmp | ppm bytes (Pillow).
    PNG uses compress_level=1 unless given (encoding dominates the cost otherwise)."""
    import io
    pil = _to_pil(img)
    f = _FORMATS.get(fmt.lower().lstrip("."))
    if f is None:
        raise ValueError(f"unknown image format {fmt!r}")
    if f == "JPEG" and pil.mode in ("RGBA", "LA"):
        pil = pil.convert("RGB")
    if f in ("JPEG", "WEBP"):
        save_kw.setdefault("quality", quality)
    if f == "PNG":
        save_kw.setdefault("compress_level", 1)
    bio = io.BytesIO()
    pil.save(bio, f, **save_kw)
    return bio.getvalue()


def _to_pil(img, size=None, ch=None):
    from PIL import Image
    if isinstance(img, Image.Image):
        return img
    if isinstance(img, (bytes, bytearray, memoryview)):
        w, h = size
        mode = {1: "L", 3: "RGB", 4: "RGBA"}[ch]
        return Image.frombuffer(mode, (w, h), bytes(img) if isinstance(img, memoryview) else img, "raw", mode, 0, 1)
    return Image.fromarray(img)


def render(page, dpi=None, *, scale=None, size=None, max_side=None, clip=None, rotated=True, alpha=False,
           grayscale=False, annots=True, forms=True, background=None, antialias=True, output="numpy",
           timeout=None, out=None, max_pixels=DEFAULT_MAX_PIXELS, quality=90, flags=0):
    g = geometry(page, dpi=dpi, scale=scale, size=size, max_side=max_side, clip=clip, rotated=rotated)
    if max_pixels is not None and g.w * g.h > max_pixels:
        raise ValueError(f"output would be {g.w}x{g.h} = {g.w * g.h} pixels > max_pixels={max_pixels}; "
                         "lower the resolution, use clip=/render_tiles(), or pass max_pixels=None")
    ch = _channels(alpha, grayscale)
    output = (output or "numpy").lower()
    enc = _FORMATS.get(output)
    use_numpy = output == "numpy"
    buf, addr = _alloc(g, ch, out, use_numpy)
    argb = _color_argb(background, alpha)
    if not grayscale:   # FillRect ignores FPDF_REVERSE_BYTE_ORDER: pass the colour as 0xAABBGGRR
        argb = (argb & 0xFF00FF00) | ((argb & 0xFF) << 16) | ((argb >> 16) & 0xFF)
    raw_page, formenv = _page_handles(page, forms)
    _render_page(raw_page, formenv, g, ch, addr, _flags(alpha, grayscale, annots, antialias, flags), argb, timeout)
    if output == "numpy":
        return buf
    if output == "bytes":
        return bytes(buf)
    if output == "pil":
        return _to_pil(buf, (g.w, g.h), ch)
    if enc:
        return encode(_to_pil(buf, (g.w, g.h), ch), output, quality=quality)
    raise ValueError(f"unknown output {output!r}: numpy | pil | bytes | png | jpeg | webp | tiff")


def render_tiles(page, dpi=None, tile=2048, *, scale=None, size=None, max_side=None, rotated=True, **kw):
    """Yield ((x, y), image) tiles of at most tile x tile pixels covering the full-page raster;
    (x, y) is the tile's top-left pixel. Tiles are pixel-identical to the same region of a
    full render, so they can be stitched (or processed) independently - for huge pages / DPIs."""
    tw, th = (tile, tile) if isinstance(tile, int) else tile
    g = geometry(page, dpi=dpi, scale=scale, size=size, max_side=max_side, rotated=rotated)
    sx, sy = g.sx, g.sy
    for y in range(0, g.full_h, th):
        for x in range(0, g.full_w, tw):
            x1, y1 = min(x + tw, g.full_w), min(y + th, g.full_h)
            # clip in points that maps exactly back onto these pixel edges
            clip = (x / sx, y / sy, x1 / sx, y1 / sy)
            yield (x, y), render(page, scale=(sx, sy), clip=clip, rotated=rotated, **kw)


class Pixmap:
    """Minimal PyMuPDF-style pixmap (get_pixmap): width, height, n, alpha, stride, samples,
    tobytes(fmt), save(path), pil_image(), numpy() / np.asarray(pix)."""

    def __init__(self, buf, width, height, n, alpha, xres=72, yres=72, x=0, y=0):
        self._buf = buf
        self.width, self.height, self.n, self.alpha = width, height, n, int(bool(alpha))
        self.xres, self.yres, self.x, self.y = xres, yres, x, y

    w = property(lambda self: self.width)
    h = property(lambda self: self.height)
    stride = property(lambda self: self.width * self.n)
    irect = property(lambda self: (self.x, self.y, self.x + self.width, self.y + self.height))
    size = property(lambda self: self.width * self.height * self.n)

    @property
    def samples(self):
        return bytes(self._buf) if not isinstance(self._buf, bytes) else self._buf

    @property
    def samples_mv(self):
        return memoryview(self._buf).cast("B")

    @property
    def colorspace(self):
        return "gray" if self.n - self.alpha == 1 else "rgb"

    def numpy(self):
        import numpy as np
        a = np.frombuffer(self.samples_mv, dtype=np.uint8)
        return a.reshape((self.height, self.width)) if self.n == 1 else a.reshape((self.height, self.width, self.n))

    def __array__(self, dtype=None, copy=None):
        a = self.numpy()
        return a.astype(dtype) if dtype is not None else a

    def pixel(self, x, y):
        o = (y * self.width + x) * self.n
        return tuple(self.samples_mv[o:o + self.n])

    def pil_image(self):
        return _to_pil(bytes(self.samples_mv), (self.width, self.height), self.n)

    def tobytes(self, output="png", jpg_quality=95):
        return encode(self.pil_image(), output, quality=jpg_quality)

    def save(self, filename, output=None, jpg_quality=95):
        import os
        fmt = output or os.path.splitext(str(filename))[1].lstrip(".") or "png"
        with open(filename, "wb") as f:
            f.write(self.tobytes(fmt, jpg_quality))

    def __repr__(self):
        return f"Pixmap({self.colorspace}, {self.irect}, {self.alpha})"


def get_pixmap(page, *, matrix=None, dpi=None, colorspace=None, clip=None, alpha=False, annots=True, **kw):
    """PyMuPDF-compatible shim: page.get_pixmap(matrix=Matrix(2, 2) | (sx, sy) | 2.0, dpi=,
    colorspace="rgb"|"gray", clip=(x0, y0, x1, y1), alpha=False, annots=True) -> Pixmap.
    Like PyMuPDF the default resolution is 72 dpi and coordinates are of the displayed page."""
    if dpi is not None:
        scale = dpi / 72.0
    elif matrix is None:
        scale = 1.0
    elif isinstance(matrix, (int, float)):
        scale = float(matrix)
    elif hasattr(matrix, "a") and hasattr(matrix, "d"):
        scale = (float(matrix.a), float(matrix.d))
    else:
        m = tuple(matrix)
        scale = (float(m[0]), float(m[3])) if len(m) == 6 else (float(m[0]), float(m[1]))
    cs = (getattr(colorspace, "name", colorspace) or "rgb").lower()
    gray = cs in ("gray", "grey", "csgray", "l")
    g = geometry(page, scale=scale, clip=clip)
    buf = render(page, scale=scale, clip=clip, alpha=alpha and not gray, grayscale=gray, annots=annots,
                 output="bytes", **kw)
    res = (72 * g.sx if not isinstance(scale, tuple) else 72 * scale[0])
    return Pixmap(buf, g.w, g.h, _channels(alpha and not gray, gray), alpha and not gray,
                  xres=round(res), yres=round(72 * g.sy), x=g.px0, y=g.py0)


# ---------------------------------------------------------------------- documents
def _name(pattern, stem, page, ext):
    return pattern.format(stem=stem, page=page, page1=page + 1, ext=ext)


def _stem(doc):
    import os
    return os.path.splitext(os.path.basename(doc.name))[0] if doc.name else "page"


def _render_job(args):
    """Worker: render (and optionally encode/write) a chunk of pages of its own document copy."""
    src, password, backend, flatten, pages, kw, enc = args
    from . import open as _open
    out = []
    with _open(src, password, backend=backend, flatten=flatten) as d:
        for i in pages:
            out.append((i, _emit(d[i], kw, enc, stem=enc and enc["stem"])))
    return out


def _emit(page, kw, enc, stem=None):
    if not enc:
        return render(page, **kw)
    import os
    fmt = enc["fmt"]
    data = render(page, output=fmt, quality=enc["quality"], **kw)
    path = os.path.join(enc["out_dir"], _name(enc["name"], stem or "page", page.number, enc["ext"]))
    with open(path, "wb") as f:
        f.write(data)
    return path


def iter_images(doc, pages=None, dpi=None, *, workers=None, _enc=None, **kw):
    """Yield (page_number, image) in page order with constant memory (one page at a time when
    serial; a process pool streaming chunks in order with workers > 1). kw: see Page.render."""
    from . import _page_list
    if dpi is not None:
        kw["dpi"] = dpi
    pages = _page_list(pages, len(doc))
    stem = _enc["stem"] if _enc else None
    if not workers or workers <= 1 or len(pages) < 2:
        for i in pages:
            yield i, _emit(doc[i], kw, _enc, stem)
        return
    import concurrent.futures as cf
    src = doc._current_source()
    password = doc._password if doc._gen == 0 else None
    workers = min(workers, len(pages))
    size = max(1, min(16, -(-len(pages) // (workers * 4))))
    chunks = [pages[i:i + size] for i in range(0, len(pages), size)]
    with cf.ProcessPoolExecutor(workers) as ex:
        futs = [ex.submit(_render_job, (src, password, doc.backend, doc._flatten, c, kw, _enc)) for c in chunks]
        for f in futs:
            yield from f.result()


def to_images(doc, pages=None, dpi=150, *, workers=None, fmt="jpeg", quality=90, out_dir=None,
              name="{stem}-{page:04d}.{ext}", **kw):
    """Render pages to image files in out_dir (returns their paths, in page order) or, with
    out_dir=None, to a list of images (output="numpy" | "pil" | "png" | ... via kw, default PIL).
    fmt: jpeg (default: PNG encoding costs ~4x the render) | png | webp | tiff.
    name: file name pattern with {stem}, {page} (0-based), {page1} (1-based) and {ext}."""
    if out_dir is None:
        kw.setdefault("output", "pil")
        return [img for _, img in iter_images(doc, pages, dpi, workers=workers, **kw)]
    import os
    f = _FORMATS.get(fmt.lower())
    if f is None:
        raise ValueError(f"unknown image format {fmt!r}")
    if f == "JPEG" and kw.get("alpha"):
        raise ValueError("JPEG has no alpha channel; use fmt='png' or 'webp' with alpha=True")
    os.makedirs(out_dir, exist_ok=True)
    enc = {"fmt": fmt.lower(), "quality": quality, "out_dir": os.fspath(out_dir), "name": name,
           "ext": "jpg" if f == "JPEG" else fmt.lower(), "stem": _stem(doc)}
    return [p for _, p in iter_images(doc, pages, dpi, workers=workers, _enc=enc, **kw)]
