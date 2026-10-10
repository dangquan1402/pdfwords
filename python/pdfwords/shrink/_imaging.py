# SPDX-License-Identifier: Apache-2.0
"""Small numpy/Pillow image helpers for the optimiser: SSIM, local (Sauvola) thresholding,
box filters and hole-filling resize. No SciPy / scikit-image dependency."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


def _box_sum(a, r):
    """Sum over a (2r+1)x(2r+1) window (edge-replicated), via an integral image."""
    p = np.pad(np.asarray(a, dtype=np.float64), r, mode="edge")
    c = np.zeros((p.shape[0] + 1, p.shape[1] + 1))
    c[1:, 1:] = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]


def box_mean(a, r):
    return _box_sum(a, r) / float((2 * r + 1) ** 2)


def ssim(a, b, max_side=1024):
    """Structural similarity of two PIL images (compared as 8-bit gray, same size), using the
    standard constants and a 7x7 uniform window with sample covariance (as scikit-image's default)."""
    a, b = a.convert("L"), b.convert("L")
    if b.size != a.size:
        b = b.resize(a.size, Image.BILINEAR)
    if max(a.size) > max_side:
        k = max_side / max(a.size)
        s = (max(8, int(a.width * k)), max(8, int(a.height * k)))
        a, b = a.resize(s, Image.BILINEAR), b.resize(s, Image.BILINEAR)
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if min(x.shape) < 7:
        return 1.0 if np.array_equal(x, y) else float(1 - np.abs(x - y).mean() / 255)
    r, n = 3, 49
    cov_norm = n / (n - 1.0)
    ux, uy = box_mean(x, r), box_mean(y, r)
    uxx, uyy, uxy = box_mean(x * x, r), box_mean(y * y, r), box_mean(x * y, r)
    vx = cov_norm * (uxx - ux * ux)
    vy = cov_norm * (uyy - uy * uy)
    vxy = cov_norm * (uxy - ux * uy)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    p = r  # crop the border like scikit-image does
    return float(s[p:-p, p:-p].mean())


def gray_like(im, tol=12):
    """True when an RGB image is effectively gray (99.5th percentile of chroma below tol)."""
    if im.mode == "L":
        return True
    t = im.convert("RGB")
    t.thumbnail((384, 384))
    a = np.asarray(t, dtype=np.int16)
    chroma = a.max(axis=2) - a.min(axis=2)
    return bool(np.percentile(chroma, 99.5) < tol)


def few_colors(im, limit=256):
    """True for line art / screenshots (few distinct colours): keep those lossless."""
    t = im.copy()
    t.thumbnail((256, 256))
    colors = t.getcolors(1 << 16)
    return colors is not None and len(colors) < limit


def _smooth_mean(a, r):
    """Local mean over a window of about 2r px: BOX-downsample by r, then bilinear upsample (fast)."""
    h, w = a.shape
    im = Image.fromarray(a.astype(np.float32))
    small = im.resize((max(1, w // r), max(1, h // r)), Image.BOX)
    return np.asarray(small.resize((w, h), Image.BILINEAR), dtype=np.float32)


def sauvola_mask(gray, dpi, k=0.2, max_level=190):
    """Ink mask (True = ink) of an 8-bit gray array with Sauvola's local threshold (local mean
    and deviation over ~25 px at 300 dpi, computed on a coarse grid)."""
    g = gray.astype(np.float32)
    r = max(4, int(12 * dpi / 300))
    m = _smooth_mean(g, r)
    s = np.sqrt(np.maximum(_smooth_mean(g * g, r) - m * m, 0))
    th = m * (1 + k * (s / 128.0 - 1))
    mask = (g < th) & (g < max_level)
    # drop isolated sensor-noise specks (ink pixels with no ink neighbour)
    im = Image.fromarray((mask * 28).astype(np.uint8))
    neighbours = np.asarray(im.filter(ImageFilter.Kernel((3, 3), [1] * 9, scale=1)))
    return mask & (neighbours > 28)


def dilate(mask, iterations):
    """Binary dilation with a square structuring element."""
    if iterations <= 0:
        return mask
    out = mask.copy()
    for _ in range(iterations):          # separable 3x3 max, repeated
        t = out.copy()
        t[1:, :] |= out[:-1, :]
        t[:-1, :] |= out[1:, :]
        out = t.copy()
        out[:, 1:] |= t[:, :-1]
        out[:, :-1] |= t[:, 1:]
    return out


def norm_resize(values, weight, size):
    """Weighted area-average resize (normalised convolution): pixels with weight 0 (e.g. text
    covered by the mask) are filled from their neighbours. values: HxWxC float, weight: HxW."""
    num = [np.asarray(Image.fromarray((values[..., c] * weight).astype(np.float32)).resize(size, Image.BOX))
           for c in range(values.shape[2])]
    den = np.asarray(Image.fromarray(weight.astype(np.float32)).resize(size, Image.BOX))
    out = np.stack(num, axis=2) / np.maximum(den, 1e-6)[..., None]
    holes = den < 1e-3
    if holes.any():
        known = ~holes
        fill = out.copy()
        small = (max(1, size[0] // 8), max(1, size[1] // 8))
        for _ in range(8):
            if not holes.any():
                break
            bn = np.stack([np.asarray(Image.fromarray((fill[..., c] * known).astype(np.float32))
                                      .resize(small, Image.BOX).resize(size, Image.BILINEAR))
                           for c in range(values.shape[2])], 2)
            bd = np.asarray(Image.fromarray(known.astype(np.float32)).resize(small, Image.BOX)
                            .resize(size, Image.BILINEAR))
            est = bn / np.maximum(bd, 1e-6)[..., None]
            newly = holes & (bd > 1e-3)
            fill[newly] = est[newly]
            known = known | newly
            holes = holes & ~newly
        fill[holes] = 255
        out = fill
    return np.clip(out, 0, 255).astype(np.uint8)
