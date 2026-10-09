# SPDX-License-Identifier: Apache-2.0
"""pdf2image-compatible API on PDFium: no poppler install, no subprocesses, no GPL binaries.

    from pdfwords.compat.pdf2image import convert_from_path   # instead of: from pdf2image import ...
    images = convert_from_path("file.pdf", dpi=200)            # list of PIL images

Same signatures and return values as pdf2image 1.17 (convert_from_path, convert_from_bytes,
pdfinfo_from_path, pdfinfo_from_bytes). Differences (see docs/RENDERING.md):
* pages are rendered by PDFium, sized round(points * dpi / 72), and always within the CropBox
  (pdftoppm's default is the MediaBox; use_cropbox is accepted and ignored);
* thread_count uses processes (PDFium is not thread-safe); poppler_path / use_pdftocairo /
  strict are accepted and ignored;
* output_folder writes the files and returns images opened from them (like pdf2image), named
  "{output_file}0001-{page:0Nd}.{ext}" with N the digit count of the page count, or the paths
  with paths_only=True.
"""
from __future__ import annotations

import os
import uuid

__all__ = ["convert_from_path", "convert_from_bytes", "pdfinfo_from_path", "pdfinfo_from_bytes",
           "PDFInfoNotInstalledError", "PDFPageCountError", "PDFSyntaxError", "PDFPopplerTimeoutError"]


class PDFInfoNotInstalledError(Exception):
    pass


class PDFPageCountError(Exception):
    pass


class PDFSyntaxError(Exception):
    pass


class PDFPopplerTimeoutError(Exception):
    pass


_EXT = {"ppm": "ppm", "png": "png", "jpeg": "jpg", "jpg": "jpg", "tiff": "tif", "tif": "tif", "webp": "webp",
        "bmp": "bmp", "gif": "gif"}
_PIL = {"ppm": "PPM", "png": "PNG", "jpg": "JPEG", "tif": "TIFF", "webp": "WEBP", "bmp": "BMP", "gif": "GIF"}


def _open(src, userpw, ownerpw):
    from .. import open as _open
    try:
        return _open(src, userpw or ownerpw)
    except Exception as e:   # pypdfium2.PdfiumError
        raise PDFPageCountError(f"Unable to get page count. {e}") from e


def _size_kw(size):
    if size is None:
        return {}
    if isinstance(size, (int, float)):        # pdftoppm -scale-to: longest side
        return {"max_side": int(size)}
    w, h = (tuple(size) + (None, None))[:2]
    if w is None and h is None:
        return {}
    return {"size": (w, h)}


def _job(args):
    src, pw, pages, kw, save = args
    from .. import open as _open
    out = []
    with _open(src, pw) as d:
        for i in pages:
            out.append(_one(d[i], kw, save))
    return out


def _one(page, kw, save):
    img = page.render(output="pil", **kw)
    if save is None:
        return img
    folder, prefix, ext, digits, opts = save
    path = os.path.join(folder, f"{prefix}{page.number + 1:0{digits}d}.{ext}")
    if _PIL[ext] == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    img.save(path, _PIL[ext], **opts)
    return path


def convert_from_path(pdf_path, dpi=200, output_folder=None, first_page=None, last_page=None, fmt="ppm",
                      jpegopt=None, thread_count=1, userpw=None, ownerpw=None, use_cropbox=False, strict=False,
                      transparent=False, single_file=False, output_file=None, poppler_path=None,
                      grayscale=False, size=None, paths_only=False, use_pdftocairo=False, timeout=None,
                      hide_annotations=False):
    """Render a PDF to a list of PIL images (or file paths with output_folder + paths_only)."""
    if isinstance(pdf_path, os.PathLike):
        pdf_path = os.fspath(pdf_path)
    return _convert(pdf_path, dpi, output_folder, first_page, last_page, fmt, jpegopt, thread_count, userpw,
                    ownerpw, transparent, single_file, output_file, grayscale, size, paths_only, timeout,
                    hide_annotations)


def convert_from_bytes(pdf_file, dpi=200, output_folder=None, first_page=None, last_page=None, fmt="ppm",
                       jpegopt=None, thread_count=1, userpw=None, ownerpw=None, use_cropbox=False, strict=False,
                       transparent=False, single_file=False, output_file=None, poppler_path=None,
                       grayscale=False, size=None, paths_only=False, use_pdftocairo=False, timeout=None,
                       hide_annotations=False):
    """Like convert_from_path, for PDF bytes."""
    return _convert(bytes(pdf_file), dpi, output_folder, first_page, last_page, fmt, jpegopt, thread_count,
                    userpw, ownerpw, transparent, single_file, output_file, grayscale, size, paths_only, timeout,
                    hide_annotations)


def _convert(src, dpi, output_folder, first_page, last_page, fmt, jpegopt, thread_count, userpw, ownerpw,
             transparent, single_file, output_file, grayscale, size, paths_only, timeout, hide_annotations):
    import time
    from ..render import RenderTimeout
    fmt = (fmt or "ppm").lower()
    if fmt not in _EXT:
        raise ValueError(f"unsupported fmt {fmt!r}")
    ext = _EXT[fmt]
    with _open(src, userpw, ownerpw) as d:
        n = len(d)
    first = max(1, first_page or 1)
    last = min(n, last_page or n)
    if single_file:
        last = first
    pages = list(range(first - 1, last))
    if not pages:
        return []
    kw = dict(dpi=dpi, grayscale=grayscale, annots=not hide_annotations,
              alpha=bool(transparent) and ext in ("png", "tif", "webp"))
    kw.update(_size_kw(size))
    if kw.keys() & {"size", "max_side"}:
        kw.pop("dpi")
    save = None
    if output_folder is not None:
        os.makedirs(output_folder, exist_ok=True)
        prefix = output_file if output_file is not None else str(uuid.uuid4())
        if not single_file:
            prefix += "0001-"     # pdf2image: "{output_file}{thread index:04d}-{page}"
        opts = {}
        if ext == "jpg":
            jo = dict(jpegopt or {})
            opts = {"quality": int(jo.get("quality", 75)), "progressive": str(jo.get("progressive", "n")).lower()
                    in ("y", "yes", "true", "1"), "optimize": str(jo.get("optimize", "n")).lower()
                    in ("y", "yes", "true", "1")}
        elif ext == "png":
            opts = {"compress_level": 6}
        digits = 1 if single_file else len(str(n))
        save = (os.fspath(output_folder), prefix, ext, digits, opts)
    deadline = time.monotonic() + timeout if timeout else None
    try:
        if not thread_count or thread_count <= 1 or len(pages) < 2:
            from .. import open as _o
            res = []
            with _o(src, userpw or ownerpw) as d:
                for i in pages:
                    if deadline is not None:
                        kw["timeout"] = max(0.001, deadline - time.monotonic())
                    res.append(_one(d[i], kw, save))
        else:
            import concurrent.futures as cf
            k = min(thread_count, len(pages))
            chunks = [pages[i::k] for i in range(k)]
            with cf.ProcessPoolExecutor(k) as ex:
                futs = [ex.submit(_job, (src, userpw or ownerpw, c, kw, save)) for c in chunks]
                parts = [f.result(timeout=timeout) for f in futs]
            res = [None] * len(pages)
            for k_, c in enumerate(chunks):
                for j, r in zip(c, parts[k_]):
                    res[j - pages[0]] = r
    except (RenderTimeout, TimeoutError) as e:
        raise PDFPopplerTimeoutError(f"Run poppler timeout. {e}") from e
    if save is not None and single_file:
        # pdf2image names a single file "{output_file}.{ext}" (pdftoppm -singlefile)
        old = res[0]
        new = os.path.join(save[0], f"{save[1]}.{ext}")
        os.replace(old, new)
        res = [new]
    if save is not None and not paths_only:
        from PIL import Image
        res = [Image.open(p) for p in res]
    return res


def pdfinfo_from_path(pdf_path, userpw=None, ownerpw=None, poppler_path=None, rawdates=False, timeout=None,
                      first_page=None, last_page=None):
    """Subset of poppler's pdfinfo as a dict: Pages, Title, Author, Subject, Keywords, Creator,
    Producer, CreationDate, ModDate, Encrypted, Page size, Page rot, PDF version."""
    return _info(os.fspath(pdf_path), userpw, ownerpw)


def pdfinfo_from_bytes(pdf_file, userpw=None, ownerpw=None, poppler_path=None, rawdates=False, timeout=None,
                       first_page=None, last_page=None):
    return _info(bytes(pdf_file), userpw, ownerpw)


def _info(src, userpw, ownerpw):
    with _open(src, userpw, ownerpw) as d:
        md = d._pdf.get_metadata_dict(skip_empty=False)
        p = d[0] if len(d) else None
        info = {k: md.get(k, "") for k in ("Title", "Subject", "Keywords", "Author", "Creator", "Producer",
                                            "CreationDate", "ModDate")}
        info["Pages"] = len(d)
        info["Encrypted"] = "yes" if d._password else "no"
        if p is not None:
            w, h = p._w, p._h
            info["Page size"] = f"{w:g} x {h:g} pts"
            info["Page rot"] = str(p.rotation)
        v = d._pdf.get_version()
        info["PDF version"] = f"{v // 10}.{v % 10}" if v else ""
        return info
