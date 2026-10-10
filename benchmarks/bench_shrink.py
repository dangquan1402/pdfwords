# SPDX-License-Identifier: Apache-2.0
"""Benchmark pdfwords.optimize presets on a directory of PDFs.

    python benchmarks/bench_shrink.py CORPUS_DIR [--presets lossless,balanced,max] [--json out.json]

For each file and preset: output size, reduction, page SSIM vs the original (PDFium renders at
150 dpi, gray; mean and min), word recall (multiset of pdfwords words, original vs output) and
seconds per page. Optionally compares Ghostscript (--gs, a *reference only*: AGPL, not used by
pdfwords) and qpdf (--qpdf) when the executables are on PATH.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter

import pdfwords
from pdfwords.shrink import _imaging


def compare(orig, out, dpi=150):
    with pdfwords.open(orig) as a, pdfwords.open(out) as b:
        ss = []
        for i in range(len(a)):
            ra = a[i].render(dpi=dpi, grayscale=True, output="pil")
            rb = b[i].render(size=ra.size, grayscale=True, output="pil")
            ss.append(_imaging.ssim(ra, rb, max_side=1 << 30))
        wa = Counter(w[4] for p in a for w in p.get_text("words"))
        wb = Counter(w[4] for p in b for w in p.get_text("words"))
    total = sum(wa.values())
    return {"ssim_mean": sum(ss) / len(ss), "ssim_min": min(ss),
            "word_recall": (sum((wa & wb).values()) / total) if total else None, "words": total}


def run_external(cmd):
    t = time.time()
    subprocess.run(cmd, check=True, capture_output=True)
    return time.time() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--presets", default="lossless,balanced,max")
    ap.add_argument("--gs", action="store_true", help="also run Ghostscript /ebook and /screen (reference)")
    ap.add_argument("--qpdf", action="store_true", help="also run qpdf lossless")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = []
    tmp = tempfile.mkdtemp()
    for src in sorted(glob.glob(os.path.join(a.corpus, "*.pdf"))):
        name = os.path.splitext(os.path.basename(src))[0]
        size = os.path.getsize(src)
        with pdfwords.open(src) as d:
            pages = len(d)
        jobs = [(p, None) for p in a.presets.split(",")]
        if a.gs and shutil.which("gs"):
            jobs += [(f"gs_{s}", ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                                  f"-dPDFSETTINGS=/{s}", "-sOutputFile={out}", src]) for s in ("ebook", "screen")]
        if a.qpdf and shutil.which("qpdf"):
            jobs.append(("qpdf", ["qpdf", "--object-streams=generate", "--recompress-flate",
                                  "--compression-level=9", src, "{out}"]))
        for method, cmd in jobs:
            out = os.path.join(tmp, f"{name}.{method}.pdf")
            if cmd is None:
                rep = pdfwords.optimize(src, out, method)
                sec = rep.seconds
            else:
                sec = run_external([c.replace("{out}", out) for c in cmd])
            q = compare(src, out)
            row = {"file": name, "method": method, "pages": pages, "bytes_before": size,
                   "bytes_after": os.path.getsize(out), "reduction": 1 - os.path.getsize(out) / size,
                   "sec_per_page": sec / pages, **q}
            rows.append(row)
            print(f"{name:16s} {method:10s} {size/1e6:8.2f} MB -> {row['bytes_after']/1e6:8.2f} MB "
                  f"({row['reduction']:6.1%})  ssim {q['ssim_mean']:.3f}/{q['ssim_min']:.3f}  "
                  f"recall {q['word_recall'] if q['word_recall'] is not None else float('nan'):.3f}  "
                  f"{row['sec_per_page']:.2f} s/page", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
