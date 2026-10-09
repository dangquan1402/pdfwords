# SPDX-License-Identifier: Apache-2.0
"""Put page text into table cells whose boxes come from elsewhere (a layout / table model)."""
from __future__ import annotations


def _scale(page, image_size, rotated):
    if not image_size:
        return 1.0, 1.0
    r = page.rect if rotated else (0.0, 0.0, page._w, page._h)
    w, h = image_size
    return w / max(r[2], 1e-9), h / max(r[3], 1e-9)


def _cover(a, b):
    """Fraction of box a inside box b."""
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    if area <= 1e-12:
        cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
        return 1.0 if b[0] <= cx <= b[2] and b[1] <= cy <= b[3] else 0.0
    return max(ix, 0) * max(iy, 0) / area


def _reading_order(words):
    """Group words into lines (vertical overlap with the line's running band), lines top-down,
    words left-to-right."""
    lines = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        h = w[3] - w[1]
        for ln in lines:
            top, bot = ln[0]
            if min(bot, w[3]) - max(top, w[1]) >= 0.5 * min(h, bot - top):
                ln[1].append(w)
                ln[0] = (min(top, w[1]), max(bot, w[3]))
                break
        else:
            lines.append([(w[1], w[3]), [w]])
    lines.sort(key=lambda ln: ln[0][0])
    return [sorted(ln[1], key=lambda w: w[0]) for ln in lines]


def assign_cells(page, cells, image_size=None, rotated=True, min_overlap=0.5):
    sx, sy = _scale(page, image_size, rotated)
    boxes = [(c[0] / sx, c[1] / sy, c[2] / sx, c[3] / sy) for c in cells]
    words = page.get_text("words", rotated=rotated, clip=False)
    per = [[] for _ in boxes]
    for w in words:
        best, k = 0.0, -1
        for i, b in enumerate(boxes):
            if b[0] > w[2] or b[2] < w[0] or b[1] > w[3] or b[3] < w[1]:
                continue
            c = _cover(w, b)
            if c > best:
                best, k = c, i
        if k >= 0 and best >= min_overlap:
            per[k].append(w)
    out = []
    for cell, ws in zip(cells, per):
        lines = _reading_order(ws)
        flat = [(w[0] * sx, w[1] * sy, w[2] * sx, w[3] * sy, w[4]) for ln in lines for w in ln]
        out.append({"bbox": tuple(cell), "text": "\n".join(" ".join(w[4] for w in ln) for ln in lines),
                    "words": flat})
    return out
