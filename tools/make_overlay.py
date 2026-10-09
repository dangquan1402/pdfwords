#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render a page with pdfwords' word (blue) and block (red) boxes and the XY-cut reading order.

    pip install pillow
    python tools/make_overlay.py tests/fixtures/irs_w9.pdf 0 docs/overlay_w9.png
"""
import sys

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

import pdfwords


def overlay(path, pno=0, out="overlay.png", scale=1.6, crop=None):
    pdf = pdfium.PdfDocument(path)
    img = pdf[pno].render(scale=scale).to_pil().convert("RGB")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    pg = pdfwords.open(path)[pno]
    s = lambda b: [b[0] * scale, b[1] * scale, b[2] * scale, b[3] * scale]  # noqa: E731
    for w in pg.get_text("words", rotated=True):
        dr.rectangle(s(w[:4]), outline=(30, 90, 255, 255), fill=(30, 90, 255, 40), width=1)
    for b in pg.get_text("blocks", sort="xycut", rotated=True):
        r = s(b[:4])
        dr.rectangle(r, outline=(230, 30, 30, 255), width=2)
        dr.text((r[0] + 2, r[1] - 11), str(b[5]), fill=(230, 30, 30, 255))
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    if crop:
        img = img.crop([int(c * scale) for c in crop])
    img.save(out, optimize=True)
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    overlay(a[0], int(a[1]) if len(a) > 1 else 0, a[2] if len(a) > 2 else "overlay.png",
            crop=tuple(map(float, a[3].split(","))) if len(a) > 3 else None)
