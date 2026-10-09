#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Text-extraction benchmark: pdfwords vs other Python PDF libraries (whichever are installed).

    python tools/fetch_test_pdfs.py               # downloads the third-party test PDFs
    python tools/bench_suite.py [--runs 3] [--json out.json] [--md out.md]

Metrics (see docs/BENCHMARKS.md):
  speed        ms/page for plain text and for words, best of N runs, per document
  word F1      tokens of each engine's plain text vs the exact ground truth of held-out
               synthetic documents (tools/make_synthetic.py with seeds 1-3, NOT the seed the
               pdfwords thresholds were tuned on), multiset precision/recall
  order        reading order on the same synthetic pages (prose, lists, two columns, tables):
               fraction of concordant word pairs (1.0 = ground-truth order; Kendall-tau style)
  agreement    word multiset F1 against PyMuPDF's words on real documents (agreement, not truth)
Every engine is used through its own public API with default settings plus the "sorted" /
reading-order option it offers, and is only ever executed (never read or copied).
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
DATA = os.path.join(ROOT, "tests", "data")


# ---------------------------------------------------------------- engines
class Engine:
    name = ""

    def open(self, path):
        raise NotImplementedError

    def close(self, doc):
        pass


class Pdfwords(Engine):
    def __init__(self, backend=None, sort="xycut"):
        import pdfwords
        self.m, self.backend, self.sort = pdfwords, backend, sort
        self.name = "pdfwords" + (f" ({backend})" if backend else "") + ("" if sort == "xycut" else f" sort={sort}")
        self.version = pdfwords.__version__

    def open(self, path):
        return self.m.open(path, backend=self.backend)

    def n(self, d):
        return len(d)

    def text(self, d, i):
        return d[i].get_text("text", sort=self.sort)

    def words(self, d, i):
        return [w[4] for w in d[i].get_text("words", sort=self.sort)]

    def close(self, d):
        d.close()


class PyMuPDF(Engine):
    def __init__(self, sort=False):
        import pymupdf
        self.m, self.sort = pymupdf, sort
        self.name = "PyMuPDF" + (" sort=True" if sort else "")
        self.version = pymupdf.__version__

    def open(self, path):
        return self.m.open(path)

    def n(self, d):
        return len(d)

    def text(self, d, i):
        return d[i].get_text("text", sort=self.sort)

    def words(self, d, i):
        return [w[4] for w in d[i].get_text("words", sort=self.sort)]

    def close(self, d):
        d.close()


class Pdfplumber(Engine):
    name = "pdfplumber"

    def __init__(self):
        import pdfplumber
        self.m, self.version = pdfplumber, pdfplumber.__version__

    def open(self, path):
        return self.m.open(path)

    def n(self, d):
        return len(d.pages)

    def text(self, d, i):
        return d.pages[i].extract_text() or ""

    def words(self, d, i):
        return [w["text"] for w in d.pages[i].extract_words()]

    def close(self, d):
        d.close()


class Pdftext(Engine):
    def __init__(self, sort=False):
        from pdftext import extraction
        import importlib.metadata as md
        self.m, self.sort, self.version = extraction, sort, md.version("pdftext")
        self.name = "pdftext" + (" sort=True" if sort else "")

    def open(self, path):
        import pypdfium2
        return (path, len(pypdfium2.PdfDocument(path)))

    def n(self, d):
        return d[1]

    def text(self, d, i):
        return self.m.paginated_plain_text_output(d[0], sort=self.sort, page_range=[i])[0]

    def all_text(self, d):     # pdftext is much faster per document than per page
        return self.m.paginated_plain_text_output(d[0], sort=self.sort)

    def words(self, d, i):
        return self.text(d, i).split()


class PdfOxide(Engine):
    name = "pdf_oxide"

    def __init__(self):
        import pdf_oxide
        import importlib.metadata as md
        self.m, self.version = pdf_oxide, md.version("pdf_oxide")

    def open(self, path):
        return self.m.PdfDocument(path)

    def n(self, d):
        return d.page_count() if callable(d.page_count) else d.page_count

    def text(self, d, i):
        return d.extract_text(i)

    def words(self, d, i):
        return [w.text for w in d.extract_words(i)]


class Pypdfium2(Engine):
    name = "pypdfium2 (raw)"

    def __init__(self):
        import pypdfium2
        self.m, self.version = pypdfium2, pypdfium2.version.PYPDFIUM_INFO.version

    def open(self, path):
        return self.m.PdfDocument(path)

    def n(self, d):
        return len(d)

    def text(self, d, i):
        tp = d[i].get_textpage()
        return tp.get_text_bounded()

    def words(self, d, i):
        return self.text(d, i).split()

    def close(self, d):
        d.close()


def engines(names=None):
    makers = [("pdfwords", lambda: Pdfwords()), ("pdfwords-python", lambda: Pdfwords("python")),
              ("pdftext", lambda: Pdftext()), ("pdftext-sort", lambda: Pdftext(True)),
              ("pymupdf", lambda: PyMuPDF()), ("pymupdf-sort", lambda: PyMuPDF(True)),
              ("pdfplumber", Pdfplumber), ("pdf_oxide", PdfOxide), ("pypdfium2", Pypdfium2)]
    out = []
    for key, mk in makers:
        if names and key not in names:
            continue
        try:
            e = mk()
            if key == "pdfwords-python" and "python" not in e.m.available_backends():
                continue
            out.append(e)
        except Exception as ex:   # not installed
            print(f"[skip] {key}: {type(ex).__name__}: {ex}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- metrics
def best_of(fn, runs):
    best = float("inf")
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def speed(e, path, runs):
    d = e.open(path)
    n = e.n(d)
    e.close(d)

    def run(kind):
        doc = e.open(path)
        if kind == "text" and hasattr(e, "all_text"):
            e.all_text(doc)
        else:
            for i in range(n):
                getattr(e, kind)(doc, i)
        e.close(doc)
    run("text")   # warm-up
    return {k: round(1000 * best_of(lambda: run(k), runs) / n, 2) for k in ("text", "words")}, n


def _match(tokens, gt):
    """Greedy multiset match of output tokens to ground-truth words (text, order)."""
    pool = collections.defaultdict(collections.deque)
    for text, order in gt:
        pool[text].append(order)
    seq = []
    for t in tokens:
        if pool.get(t):
            seq.append(pool[t].popleft())
    return seq


def concordance(seq):
    """Fraction of concordant pairs in seq (1.0 = sorted)."""
    n = len(seq)
    if n < 2:
        return 1.0
    # merge-sort inversion count
    def sort(a):
        if len(a) < 2:
            return a, 0
        m = len(a) // 2
        l, x = sort(a[:m])
        r, y = sort(a[m:])
        out, inv, i, j = [], x + y, 0, 0
        while i < len(l) and j < len(r):
            if l[i] <= r[j]:
                out.append(l[i]); i += 1
            else:
                out.append(r[j]); j += 1; inv += len(l) - i
        out += l[i:] + r[j:]
        return out, inv
    _, inv = sort(list(seq))
    return 1.0 - inv / (n * (n - 1) / 2)


HELDOUT_SEEDS = (1, 2, 3)


def heldout():
    """Synthetic documents from seeds never used for tuning (generated on first use)."""
    out = os.path.join(DATA, "heldout")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import make_synthetic
    files = []
    for s in HELDOUT_SEEDS:
        stem = os.path.join(out, f"synthetic_seed{s}")
        if not os.path.exists(stem + ".json"):
            make_synthetic.make(s, out, f"synthetic_seed{s}")
        files.append(stem)
    return files


def synthetic(e, stems=None):
    per_kind = collections.defaultdict(lambda: [0, 0, 0, []])
    for stem in stems or heldout():
        _synthetic_one(e, stem, per_kind)
    out = {}
    tm = tt = tg = 0
    for kind, (m, t, g, conc) in per_kind.items():
        out[kind] = {"f1": round(2 * m / (t + g), 4), "order": round(sum(conc) / len(conc), 4)}
        tm, tt, tg = tm + m, tt + t, tg + g
    allc = [c for v in per_kind.values() for c in v[3]]
    out["all"] = {"f1": round(2 * tm / (tt + tg), 4), "order": round(sum(allc) / len(allc), 4)}
    return out


def _synthetic_one(e, stem, per_kind):
    gt = json.load(open(stem + ".json", encoding="utf-8"))
    d = e.open(stem + ".pdf")
    for i, pg in enumerate(gt["pages"]):
        words = [(w[0], w[3]) for w in pg["words"]]
        toks = e.text(d, i).split()
        seq = _match(toks, words)
        k = per_kind[pg["kind"]]
        k[0] += len(seq); k[1] += len(toks); k[2] += len(words); k[3].append(concordance(seq))
    e.close(d)


def agreement(e, ref, path):
    d, r = e.open(path), ref.open(path)
    m = t = g = 0
    for i in range(e.n(d)):
        a = collections.Counter(e.words(d, i))
        b = collections.Counter(ref.words(r, i))
        m += sum((a & b).values()); t += sum(a.values()); g += sum(b.values())
    e.close(d); ref.close(r)
    return round(2 * m / max(t + g, 1), 4)


def corpus():
    files = sorted(glob.glob(os.path.join(DATA, "*.pdf"))) + [os.path.join(FIX, "irs_w9.pdf"),
                                                               os.path.join(FIX, "synthetic.pdf")]
    return [f for f in files if os.path.exists(f)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--engines", help="comma-separated keys (default: all installed)")
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args()
    es = engines(a.engines.split(",") if a.engines else None)
    files = corpus()
    res = {"python": sys.version.split()[0], "platform": sys.platform, "engines": {}}
    ref = next((x for x in es if x.name == "PyMuPDF"), None)
    for e in es:
        r = {"version": e.version, "speed": {}, "agreement": {}}
        for f in files:
            name = os.path.basename(f)
            try:
                r["speed"][name], n = speed(e, f, a.runs)
                r["speed"][name]["pages"] = n
                if ref is not None and ref is not e and name != "synthetic.pdf":
                    r["agreement"][name] = agreement(e, ref, f)
            except Exception as ex:
                r["speed"][name] = {"error": f"{type(ex).__name__}: {ex}"[:200]}
        r["synthetic"] = synthetic(e)
        res["engines"][e.name] = r
        print(json.dumps({e.name: r}), file=sys.stderr, flush=True)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1)
    md = to_md(res, files)
    if a.md:
        with open(a.md, "w") as fh:
            fh.write(md)
    print(md)


def to_md(res, files):
    names = [os.path.basename(f) for f in files]
    es = res["engines"]
    out = [f"Python {res['python']} on {res['platform']}; best of N runs, ms/page (lower is better).\n",
           "### Speed: plain text, ms/page\n", "| engine | " + " | ".join(names) + " |",
           "|---|" + "---:|" * len(names)]
    for k, r in es.items():
        out.append(f"| {k} {r['version']} | " + " | ".join(
            str(r["speed"].get(n, {}).get("text", "-")) for n in names) + " |")
    out += ["", "### Speed: words with boxes, ms/page\n", "| engine | " + " | ".join(names) + " |",
            "|---|" + "---:|" * len(names)]
    for k, r in es.items():
        out.append(f"| {k} | " + " | ".join(str(r["speed"].get(n, {}).get("words", "-")) for n in names) + " |")
    kinds = ["prose", "list", "two_column", "table", "all"]
    out += ["", "### Held-out synthetic ground truth (seeds 1-3): word F1 / reading-order concordance\n",
            "| engine | " + " | ".join(kinds) + " |", "|---|" + "---:|" * len(kinds)]
    for k, r in es.items():
        s = r["synthetic"]
        out.append(f"| {k} | " + " | ".join(f"{s[x]['f1']:.3f} / {s[x]['order']:.3f}" if x in s else "-"
                                             for x in kinds) + " |")
    real = [n for n in names if n != "synthetic.pdf"]
    out += ["", "### Word agreement with PyMuPDF (multiset F1)\n", "| engine | " + " | ".join(real) + " |",
            "|---|" + "---:|" * len(real)]
    for k, r in es.items():
        if r["agreement"]:
            out.append(f"| {k} | " + " | ".join(str(r["agreement"].get(n, "-")) for n in real) + " |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
