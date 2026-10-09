# SPDX-License-Identifier: Apache-2.0
"""Time pdftext's API vs pdfwords.compat.pdftext on the same files (best of N runs).

    python tools/bench_pdftext.py --impl pdfwords tests/data/*.pdf
    python tools/bench_pdftext.py --impl pdftext  tests/data/*.pdf   # in an env with pdftext
Prints one JSON line per file: seconds for plain_text_output, dictionary_output (default
options), dictionary_output(disable_links=True) and dictionary_output(keep_chars=True).
"""
import argparse
import json
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", choices=["pdftext", "pdfwords"], required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("pdfs", nargs="+")
    a = ap.parse_args()
    if a.impl == "pdftext":
        from pdftext.extraction import dictionary_output, plain_text_output
    else:
        from pdfwords.compat.pdftext import dictionary_output, plain_text_output
    cases = {"plain": lambda f: plain_text_output(f), "dict": lambda f: dictionary_output(f),
             "dict_nolinks": lambda f: dictionary_output(f, disable_links=True),
             "dict_chars": lambda f: dictionary_output(f, keep_chars=True)}
    for f in a.pdfs:
        cases["plain"](f)   # warm-up (imports, font caches)
        res = {"file": f.rsplit("/", 1)[-1], "impl": a.impl}
        for name, fn in cases.items():
            best = float("inf")
            for _ in range(a.runs):
                t = time.perf_counter()
                fn(f)
                best = min(best, time.perf_counter() - t)
            res[name] = round(best, 4)
        print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
