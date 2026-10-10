# SPDX-License-Identifier: Apache-2.0
"""Image encoders for the optimiser.

Always available (Pillow): baseline/progressive JPEG with optimised Huffman tables, Flate, and
CCITT Group 4 for 1-bit images (Pillow's bundled libtiff).

Optional, auto-detected:
- ``mozjpeg-lossless-optimization`` (BSD-3, wheels): lossless JPEG re-optimisation with MozJPEG
  (smaller Huffman tables + progressive scans, pixels unchanged). Part of ``pdfwords[shrink]``.
- a MozJPEG ``cjpeg`` executable (``PDFWORDS_CJPEG`` or ``cjpeg`` on PATH reporting "mozjpeg"):
  trellis-quantised lossy JPEG, ~5-10 % smaller than libjpeg at the same quality.
- the ``jbig2`` executable from jbig2enc (Apache-2.0, ``PDFWORDS_JBIG2`` or PATH): lossless
  JBIG2 generic-region coding for 1-bit images and MRC masks, usually 30-50 % smaller than G4.
"""
from __future__ import annotations

import functools
import io
import os
import shutil
import subprocess
import tempfile

from PIL import Image


@functools.lru_cache(maxsize=None)
def _mozjpeg_lossless():
    try:
        import mozjpeg_lossless_optimization as m
        return m
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _cjpeg():
    exe = os.environ.get("PDFWORDS_CJPEG") or shutil.which("cjpeg")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        if "mozjpeg" in (r.stdout + r.stderr).lower():
            return exe
    except Exception:
        pass
    return None


@functools.lru_cache(maxsize=None)
def _jbig2():
    exe = os.environ.get("PDFWORDS_JBIG2") or shutil.which("jbig2")
    return exe if exe and os.path.exists(exe) else None


def available():
    """Which optional encoders were found: {"mozjpeg_lossless", "mozjpeg_cjpeg", "jbig2"} -> bool."""
    return {"mozjpeg_lossless": _mozjpeg_lossless() is not None, "mozjpeg_cjpeg": _cjpeg() is not None,
            "jbig2": _jbig2() is not None, "ccitt_g4": True}


def jpeg(im, quality, gray=False):
    """Lossy JPEG of a PIL image (MozJPEG cjpeg when available, else Pillow optimize+progressive)."""
    im = im.convert("L" if gray else "RGB")
    exe = _cjpeg()
    if exe:
        b = io.BytesIO()
        im.save(b, "PPM")
        try:
            r = subprocess.run([exe, "-quality", str(int(quality)), "-optimize", "-progressive"],
                               input=b.getvalue(), capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
                return r.stdout
        except Exception:
            pass
    b = io.BytesIO()
    im.save(b, "JPEG", quality=int(quality), optimize=True, progressive=True)
    data = b.getvalue()
    m = _mozjpeg_lossless()
    if m is not None:
        try:
            opt = m.optimize(data)
            if opt and len(opt) < len(data):
                data = opt
        except Exception:
            pass
    return data


def jpeg_lossless(data):
    """Pixel-identical re-optimisation of an existing JPEG stream, or None if unavailable/worse."""
    m = _mozjpeg_lossless()
    if m is None:
        return None
    try:
        out = m.optimize(data)
    except Exception:
        return None
    return out if out and len(out) < len(data) else None


def ccitt_g4(im1):
    """1-bit PIL image -> (raw CCITT G4 data, black_is_1)."""
    b = io.BytesIO()
    im1.save(b, "TIFF", compression="group4", strip_size=1 << 30)
    t = Image.open(io.BytesIO(b.getvalue()))
    off, cnt = t.tag_v2[273], t.tag_v2[279]
    off = off[0] if isinstance(off, tuple) else off
    cnt = cnt[0] if isinstance(cnt, tuple) else cnt
    return b.getvalue()[off:off + cnt], t.tag_v2.get(262, 0) == 1


def jbig2_generic(im1):
    """1-bit PIL image -> lossless JBIG2 generic-region stream (no globals), or None."""
    exe = _jbig2()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.png")
        im1.save(p)
        try:
            r = subprocess.run([exe, "-p", p], capture_output=True, cwd=d, timeout=120)
        except Exception:
            return None
        return r.stdout if r.returncode == 0 and r.stdout else None
