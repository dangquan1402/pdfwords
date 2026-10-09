# SPDX-License-Identifier: Apache-2.0
"""Best-of-N benchmark: pdfwords (rust / python) vs PyMuPDF vs pdftext, ms per page.
Needs `pip install pymupdf pdftext` (benchmark-only).   python benchmarks/bench_backends.py [N]"""
import json, os, sys, time

import pymupdf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import fetch_test_pdfs  # noqa: E402
import pdfwords  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
NAMES = ["arxiv_attention.pdf", "arxiv_resnet.pdf", "irs_w9.pdf", "table_camelot.pdf", "simple_text.pdf"]


def best(fn):
    t = []
    for _ in range(N):
        s = time.perf_counter(); fn(); t.append(time.perf_counter() - s)
    return min(t)


def pw(path, backend, mode):
    def run():
        with pdfwords.open(path, backend=backend) as d:
            for p in d:
                p.get_text(mode)
    return run


def pw_np(path):
    def run():
        with pdfwords.open(path, backend="rust") as d:
            for p in d:
                p.words_array()
    return run


def mu(path, mode):
    def run():
        d = pymupdf.open(path)
        for p in d:
            p.get_text(mode)
        d.close()
    return run


def pdftext_run(path):
    from pdftext.extraction import dictionary_output
    return lambda: dictionary_output(path)


def breakdown(path):
    """Native stage times (rust) + Python object conversion, best of N, ms total for doc."""
    rs = pdfwords._rust()
    bestd = None
    for _ in range(N):
        with pdfwords.open(path, backend="rust") as d:
            acc = {}
            for i in range(len(d)):
                r = rs.bench_page(d._addr, i)
                for k, v in r.items():
                    acc[k] = acc.get(k, 0) + v
            # conversion: get_text("words") on a page whose glyphs are already extracted
            conv = 0
            for p in d:
                p._rs(False)
                s = time.perf_counter_ns(); p.get_text("words"); conv += time.perf_counter_ns() - s
            acc["layout+words+pyconv_ns"] = conv
        tot = acc["load_page_ns"] + acc["load_text_ns"] + acc["chars_ns"] + acc["close_ns"] + acc["layout_ns"] + acc["words_ns"]
        if bestd is None or tot < bestd[0]:
            bestd = (tot, acc)
    a = bestd[1]
    ms = lambda k: round(a[k] / 1e6, 2)  # noqa: E731
    out = {k[:-3]: ms(k) for k in ("load_page_ns", "load_text_ns", "chars_ns", "close_ns", "layout_ns", "words_ns")}
    out["py_conversion"] = round((a["layout+words+pyconv_ns"] - a["layout_ns"] - a["words_ns"]) / 1e6, 2)
    out["n_glyphs"], out["n_words"] = a["n_glyphs"], a["n_words"]
    return out


def main():
    rows = []
    for name in NAMES:
        path = fetch_test_pdfs.path_of(name)
        n = len(pymupdf.open(path))
        r = {"pdf": name, "pages": n}
        cases = {
            "rust_words": pw(path, "rust", "words"),
            "rust_words_numpy": pw_np(path),
            "rust_rawdict": pw(path, "rust", "rawdict"),
            "python_words": pw(path, "python", "words"),
            "python_rawdict": pw(path, "python", "rawdict"),
            "pymupdf_words": mu(path, "words"),
            "pymupdf_rawdict": mu(path, "rawdict"),
            "pdftext": pdftext_run(path),
        }
        for k, fn in cases.items():
            fn()  # warm-up
            r[k] = round(best(fn) * 1e3 / n, 2)  # ms/page
        r["breakdown_ms_total"] = breakdown(path)
        print(json.dumps(r), flush=True)
        rows.append(r)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "bench_backends.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(rows, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
