# SPDX-License-Identifier: Apache-2.0
"""Rendering benchmark: pdfwords.Page.render vs pypdfium2 PdfPage.render, PyMuPDF get_pixmap
and pdf2image (poppler), ms/page for whole documents (open included), RGB numpy output.
Also reports output size agreement with PyMuPDF and a blurred-SSIM fidelity score.

    python tools/bench_render.py [--dpi 72,150,300] [--repeat 3] [pdf ...]

PyMuPDF (AGPL) and pdf2image/poppler (GPL binaries) are only used here as black-box
references; they are never imported by pdfwords."""
import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))
import pdfwords  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = [os.path.join(HERE, "..", "tests", "data", f) for f in ("arxiv_attention.pdf", "arxiv_resnet.pdf",
                                                                   "table_camelot.pdf")]
DEFAULT.append(os.path.join(HERE, "..", "tests", "fixtures", "irs_w9.pdf"))


def r_pdfwords(path, dpi, **kw):
    with pdfwords.open(path) as d:
        return [p.render(dpi=dpi, **kw) for p in d]


def r_pypdfium2(path, dpi):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    doc.init_forms()
    out = [p.render(scale=dpi / 72, rev_byteorder=True, may_draw_forms=True).to_numpy().copy() for p in doc]
    doc.close()
    return out


def r_pymupdf(path, dpi):
    import pymupdf
    doc = pymupdf.open(path)
    out = []
    for p in doc:
        pm = p.get_pixmap(dpi=dpi)
        out.append(np.frombuffer(pm.samples, np.uint8).reshape(pm.h, pm.w, pm.n).copy())
    doc.close()
    return out


def r_pdf2image(path, dpi):
    from pdf2image import convert_from_path
    return [np.asarray(im) for im in convert_from_path(path, dpi=dpi)]


def _blur(a, k=3):
    # box blur (k x k) via integral image: ignores anti-aliasing differences between engines
    a = a.astype(np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    c = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def ssim(a, b):
    """Global SSIM of 3x3 box-blurred grayscale images (same size)."""
    x, y = _blur(a), _blur(b)
    mx, my = x.mean(), y.mean()
    vx, vy, cxy = x.var(), y.var(), ((x - mx) * (y - my)).mean()
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))


def best(fn, path, dpi, repeat):
    t = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(path, dpi)
        t.append(time.perf_counter() - t0)
    return min(t) * 1000 / len(out), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*")
    ap.add_argument("--dpi", default="72,150,300")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args()
    pdfs = [p for p in (a.pdfs or DEFAULT) if os.path.exists(p)]
    engines = {"pdfwords": r_pdfwords, "pdfwords gray": lambda p, d: r_pdfwords(p, d, grayscale=True),
               "pypdfium2": r_pypdfium2}
    for name, fn in (("pymupdf", r_pymupdf), ("pdf2image", r_pdf2image)):
        try:
            fn(pdfs[0], 10)
            engines[name] = fn
        except Exception as e:   # not installed
            print(f"# {name} unavailable: {e}", file=sys.stderr)
    rows = []
    for dpi in [int(x) for x in a.dpi.split(",")]:
        print(f"\n### {dpi} DPI (ms/page, best of {a.repeat})\n")
        print("| PDF (pages) | " + " | ".join(engines) + " | size = PyMuPDF (pdfwords / pypdfium2) | SSIM vs PyMuPDF (pdfwords / pypdfium2) |")
        print("|---" * (len(engines) + 3) + "|")
        for path in pdfs:
            res = {n: best(fn, path, dpi, a.repeat) for n, fn in engines.items()}
            cells = [f"{res[n][0]:.1f}" for n in engines]
            same, sim = "–", "–"
            if "pymupdf" in res:
                ref = res["pymupdf"][1]
                ok = [sum(x.shape[:2] == r.shape[:2] for x, r in zip(res[n][1], ref)) for n in ("pdfwords", "pypdfium2")]
                same = f"{ok[0]}/{len(ref)} / {ok[1]}/{len(ref)}"
                ss = []
                for n in ("pdfwords", "pypdfium2"):
                    v = [ssim(x, r) for x, r in zip(res[n][1], ref) if x.shape[:2] == r.shape[:2]]
                    ss.append(f"{np.mean(v):.3f}" if v else "n/a (size)")
                sim = " / ".join(ss)
            name = f"{os.path.basename(path)[:-4]} ({len(res['pdfwords'][1])})"
            print(f"| {name} | " + " | ".join(cells) + f" | {same} | {sim} |")
            rows.append({"pdf": name, "dpi": dpi, **{n: res[n][0] for n in engines}})
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
