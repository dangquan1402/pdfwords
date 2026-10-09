# SPDX-License-Identifier: Apache-2.0
"""Visual debugging: draw what pdfwords sees on top of a rendered page (needs Pillow).

    from pdfwords.debug import overlay
    overlay(doc[0], show=("words", "blocks", "order", "links")).save("p0.png")

CLI:  pdfwords debug file.pdf --page 0 --show words,lines,blocks,order,links -o out.png
"""
from __future__ import annotations

LAYERS = ("chars", "words", "lines", "spans", "blocks", "order", "links", "annots", "widgets", "cells")
DEFAULT_LAYERS = ("words", "blocks", "order", "links")
COLORS = {
    "chars": (150, 150, 150), "words": (30, 110, 255), "lines": (0, 170, 80), "spans": (0, 190, 190),
    "blocks": (230, 40, 40), "order": (255, 140, 0), "links": (200, 0, 200), "annots": (220, 180, 0),
    "widgets": (120, 60, 200), "cells": (0, 120, 120),
}


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # Pillow < 10.1
        return ImageFont.load_default()


def overlay(page, show=DEFAULT_LAYERS, scale=2.0, sort="xycut", cells=None, width=1):
    """Render `page` (as displayed) and draw the requested layers. Returns a PIL.Image.
    show: any of chars, words, lines, spans, blocks, order (reading order arrows), links,
          annots, widgets, cells (pass cells=[bbox, ...] in page coordinates)."""
    from PIL import Image, ImageDraw
    show = tuple(s.strip() for s in (show.split(",") if isinstance(show, str) else show) if s.strip())
    bad = [s for s in show if s not in LAYERS]
    if bad:
        raise ValueError(f"unknown layer(s) {bad}; choose from {', '.join(LAYERS)}")
    pg = page._page
    img = pg.render(scale=scale, may_draw_forms=True).to_pil().convert("RGB")
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    fnt = _font(max(10, int(6 * scale)))

    def R(bb):
        x0, y0, x1, y1 = (v * scale for v in bb)
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    def box(bb, key, fill_alpha=0, w=width):
        c = COLORS[key]
        dr.rectangle(R(bb), outline=c + (255,), fill=c + (fill_alpha,) if fill_alpha else None, width=w)

    need_dict = any(s in show for s in ("chars", "spans", "lines", "blocks", "order"))
    d = page.get_text("rawdict", sort=sort, rotated=True) if need_dict else None
    if "chars" in show:
        for b in d["blocks"]:
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    for ch in sp["chars"]:
                        box(ch["bbox"], "chars")
    if "spans" in show:
        for b in d["blocks"]:
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    box(sp["bbox"], "spans")
    if "words" in show:
        for w in page.get_text("words", rotated=True):
            box(w[:4], "words", fill_alpha=28)
    if "lines" in show:
        for b in d["blocks"]:
            for ln in b["lines"]:
                box(ln["bbox"], "lines")
    if "blocks" in show:
        for i, b in enumerate(d["blocks"]):
            box(b["bbox"], "blocks", w=width + 1)
            x0, y0 = R(b["bbox"])[:2]
            dr.text((x0 + 2, y0 + 1), str(i), fill=COLORS["blocks"] + (255,), font=fnt)
    if "order" in show:
        pts = [((b["bbox"][0] + b["bbox"][2]) / 2 * scale, (b["bbox"][1] + b["bbox"][3]) / 2 * scale)
               for b in d["blocks"]]
        c = COLORS["order"] + (230,)
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            dr.line([x0, y0, x1, y1], fill=c, width=max(1, int(scale)))
            # arrow head
            import math
            a = math.atan2(y1 - y0, x1 - x0)
            L = 6 * scale
            dr.polygon([(x1, y1), (x1 - L * math.cos(a - 0.4), y1 - L * math.sin(a - 0.4)),
                        (x1 - L * math.cos(a + 0.4), y1 - L * math.sin(a + 0.4))], fill=c)
        for i, (x, y) in enumerate(pts):
            r = 7 * scale
            dr.ellipse([x - r, y - r, x + r, y + r], fill=COLORS["order"] + (255,))
            dr.text((x - r / 2, y - r / 1.3), str(i), fill=(255, 255, 255, 255), font=fnt)
    if "links" in show:
        for lk in page.get_links(web=True, rotated=True):
            for r in lk.get("rects") or [lk["from"]]:
                box(r, "links", fill_alpha=40, w=width + 1)
    if "annots" in show:
        for a in page.annots(rotated=True):
            if a["rect"] and a["type"] not in ("Link", "Widget", "Popup"):
                box(a["rect"], "annots", fill_alpha=40, w=width + 1)
                dr.text((a["rect"][0] * scale, a["rect"][1] * scale - 12 * scale / 2), a["type"],
                        fill=COLORS["annots"] + (255,), font=fnt)
    if "widgets" in show:
        for wdg in page.widgets(rotated=True):
            box(wdg["rect"], "widgets", fill_alpha=30, w=width + 1)
    if "cells" in show and cells:
        for c in cells:
            box(c, "cells", w=width + 1)
    return Image.alpha_composite(base, layer).convert("RGB")


def legend(show):
    return ", ".join(f"{s}={'#%02x%02x%02x' % COLORS[s]}" for s in show)
