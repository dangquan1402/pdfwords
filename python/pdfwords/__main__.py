# SPDX-License-Identifier: Apache-2.0
"""pdfwords file.pdf [--mode words|dict|rawdict|blocks|text] [--pages 0,2-4] [--sort xycut]"""
import argparse
import json
import sys

import pdfwords


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pdfwords")
    ap.add_argument("pdf")
    ap.add_argument("--mode", default="words", choices=["text", "words", "blocks", "dict", "rawdict"])
    ap.add_argument("--pages", help="0-based, e.g. 0,2-4")
    ap.add_argument("--sort", default="none", choices=["none", "simple", "xycut"])
    ap.add_argument("--backend", default=None, choices=["rust", "python"], help="default: rust if built")
    ap.add_argument("--rotated", action="store_true", help="coordinates on the displayed (rotated) page")
    ap.add_argument("--ligatures", action="store_true", help="re-compose ligature glyphs into U+FB0x chars")
    ap.add_argument("--dehyphenate", action="store_true")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    sort = {"none": False, "simple": True, "xycut": "xycut"}[a.sort]
    with pdfwords.open(a.pdf, backend=a.backend) as doc:
        idx = range(len(doc))
        if a.pages:
            idx = []
            for part in a.pages.split(","):
                lo, _, hi = part.partition("-")
                idx.extend(range(int(lo), int(hi or lo) + 1))
        res = []
        for i in idx:
            pg = doc[i]
            r = pg.get_text(a.mode, sort=sort, rotated=a.rotated, ligatures=a.ligatures, dehyphenate=a.dehyphenate)
            res.append({"page": i, "rotation": pg.rotation, "rect": pg.rect, "content": r})
    if a.mode == "text":
        s = "\f".join(r["content"] for r in res)
    else:
        s = json.dumps(res, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(s)
    else:
        if hasattr(sys.stdout, "reconfigure"):  # Windows pipes default to the ANSI code page
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(s + "\n")


if __name__ == "__main__":
    main()
