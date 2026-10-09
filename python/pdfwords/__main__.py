# SPDX-License-Identifier: Apache-2.0
"""pdfwords command line.

    pdfwords file.pdf [--mode words|dict|rawdict|blocks|text] [--pages 0,2-4] [--sort xycut]
    pdfwords redact in.pdf -o out.pdf --search "text" [--rect x0,y0,x1,y1] [--fill 0,0,0] ...
    pdfwords insert-text in.pdf -o out.pdf --text "..." (--point x,y | --rect x0,y0,x1,y1) ...
    pdfwords contents in.pdf [--pages 0]
"""
import argparse
import json
import sys

import pdfwords

SUBCOMMANDS = ("extract", "redact", "insert-text", "contents")


def _pages(spec, n):
    if not spec:
        return list(range(n))
    idx = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        idx.extend(range(int(lo), int(hi or lo) + 1))
    return [i if i >= 0 else n + i for i in idx]


def _floats(s, n=None):
    v = tuple(float(x) for x in s.replace(" ", "").split(","))
    if n and len(v) != n:
        raise argparse.ArgumentTypeError(f"expected {n} comma-separated numbers, got {s!r}")
    return v


def _color(s):
    if s is None or s.lower() in ("none", "no", ""):
        return None
    return _floats(s)


def _out(s, path=None):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
    else:
        if hasattr(sys.stdout, "reconfigure"):  # Windows pipes default to the ANSI code page
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(s + "\n")


def extract(argv):
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
        res = []
        for i in _pages(a.pages, len(doc)):
            pg = doc[i]
            r = pg.get_text(a.mode, sort=sort, rotated=a.rotated, ligatures=a.ligatures, dehyphenate=a.dehyphenate)
            res.append({"page": i, "rotation": pg.rotation, "rect": pg.rect, "content": r})
    if a.mode == "text":
        s = "\f".join(r["content"] for r in res)
    else:
        s = json.dumps(res, ensure_ascii=False)
    _out(s, a.out)


def redact(argv):
    ap = argparse.ArgumentParser(prog="pdfwords redact", description="True redaction: removes the text/images/"
                                 "vector art under the areas from the content streams, then draws fill boxes.")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--search", action="append", default=[], help="text to redact (repeatable, case-insensitive)")
    ap.add_argument("--regex", action="store_true", help="treat --search values as regular expressions")
    ap.add_argument("--rect", action="append", default=[], type=lambda s: _floats(s, 4),
                    help="x0,y0,x1,y1 in page coordinates (top-left origin, unrotated page); repeatable")
    ap.add_argument("--pages", help="0-based pages, e.g. 0,2-4 (default: all)")
    ap.add_argument("--fill", default="0,0,0", help="box colour r,g,b in 0..1, or 'none' (default black)")
    ap.add_argument("--text", help="overlay text drawn in each box")
    ap.add_argument("--images", default="pixels", choices=["none", "remove", "pixels"])
    ap.add_argument("--graphics", default="covered", choices=["none", "covered", "touched"])
    ap.add_argument("--keep-annots", action="store_true", help="keep annotations overlapping the areas")
    ap.add_argument("--scrub", action="store_true", help="also remove metadata, XMP, JavaScript, attachments")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    if not a.search and not a.rect:
        ap.error("give at least one --search or --rect")
    img = {"none": 0, "remove": 1, "pixels": 2}[a.images]
    gfx = {"none": 0, "covered": 1, "touched": 2}[a.graphics]
    fill = _color(a.fill)
    summary = []
    with pdfwords.open(a.pdf) as doc:
        for i in _pages(a.pages, len(doc)):
            pg = doc[i]
            areas = [tuple(r) for r in a.rect]
            for s in a.search:
                areas += pg.search_for(s, regex=a.regex)
            if not areas:
                continue
            for r in areas:
                pg.add_redact_annot(r, text=a.text, fill=fill)
            pg.apply_redactions(images=img, graphics=gfx, annotations=not a.keep_annots, verify=not a.no_verify)
            rep = dict(pg.redaction_report or {})
            rep.pop("collateral_chars", None)
            rep.pop("leftover_chars", None)
            rep["areas"] = len(areas)
            summary.append({"page": i, **rep})
        if a.scrub:
            doc.scrub()
        doc.save(a.out, garbage=1, deflate=True)
    _out(json.dumps({"output": a.out, "pages": summary}, ensure_ascii=False))


def insert_text(argv):
    ap = argparse.ArgumentParser(prog="pdfwords insert-text")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--text", required=True, help="text; use \\n for line breaks")
    ap.add_argument("--page", type=int, default=0)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--point", type=lambda s: _floats(s, 2), help="x,y baseline start (top-left origin)")
    g.add_argument("--rect", type=lambda s: _floats(s, 4), help="x0,y0,x1,y1 text box (word-wrapped)")
    ap.add_argument("--fontsize", type=float, default=11)
    ap.add_argument("--fontname", default="helv", help="base-14 code: helv tiro cour hebo heit tibo ... ")
    ap.add_argument("--fontfile", help="TrueType/OpenType font to embed (needed for non-Latin-1 text)")
    ap.add_argument("--color", default="0,0,0")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--align", default="left", choices=["left", "center", "right", "justify"])
    a = ap.parse_args(argv)
    text = a.text.replace("\\n", "\n")
    kw = dict(fontsize=a.fontsize, fontname=a.fontname, fontfile=a.fontfile, color=_color(a.color), rotate=a.rotate)
    with pdfwords.open(a.pdf) as doc:
        pg = doc[a.page]
        if a.point:
            res = {"lines": pg.insert_text(a.point, text, **kw)}
        else:
            rc = pg.insert_textbox(a.rect, text, align=["left", "center", "right", "justify"].index(a.align), **kw)
            res = {"unused_height": rc}
            if rc < 0:
                _out(json.dumps({"error": "text does not fit", "missing_height": -rc}))
                sys.exit(1)
        doc.save(a.out, deflate=True)
    _out(json.dumps({"output": a.out, **res}))


def contents(argv):
    ap = argparse.ArgumentParser(prog="pdfwords contents", description="print decoded page content streams")
    ap.add_argument("pdf")
    ap.add_argument("--pages")
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf) as doc:
        out = sys.stdout.buffer
        for i in _pages(a.pages, len(doc)):
            out.write(b"%% page %d, streams %s\n" % (i, str(doc[i].get_contents()).encode()))
            out.write(doc[i].read_contents() + b"\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        return {"extract": extract, "redact": redact, "insert-text": insert_text, "contents": contents}[cmd](rest)
    return extract(argv)


if __name__ == "__main__":
    main()
