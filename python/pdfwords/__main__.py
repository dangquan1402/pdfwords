# SPDX-License-Identifier: Apache-2.0
"""pdfwords command line.

    pdfwords file.pdf [--mode words|dict|rawdict|blocks|text] [--pages 0,2-4] [--sort xycut]
    pdfwords redact in.pdf -o out.pdf --search "text" [--rect x0,y0,x1,y1] [--fill 0,0,0] ...
    pdfwords insert-text in.pdf -o out.pdf --text "..." (--point x,y | --rect x0,y0,x1,y1) ...
    pdfwords contents in.pdf [--pages 0]
    pdfwords debug file.pdf [--page 0] [--show words,lines,blocks,order,links] [-o out.png]
    pdfwords links | annots | toc | quality file.pdf [--pages ...]
    pdfwords search file.pdf "needle" [--regex] [--quads]
    pdfwords pdftext file.pdf [--json] [--sort] [--keep_hyphens] [--page_range 0,5-10] ...
    pdfwords render file.pdf [--pages 0,2-4] [--dpi 150] [--fmt jpeg|png|webp] [--out-dir out/] [--workers 8]
    pdfwords render file.pdf --pages 0 --overlay words,blocks -o debug.png
    pdfwords export file.pdf [--format markdown|html|xhtml|xml|hocr|alto] [--pages ...] [-o out.md]
    pdfwords chunks file.pdf [--max-chars 2000] [--overlap 0]      # JSON lines with page+bbox provenance
    pdfwords objects file.pdf [--pages 0] [--kind images|drawings|struct]
    pdfwords mcp [--root DIR]                                      # MCP server (stdio) for LLM agents
"""
import argparse
import json
import sys

import pdfwords

SUBCOMMANDS = ("extract", "redact", "insert-text", "contents", "debug", "links", "annots", "toc", "quality",
               "search", "pdftext", "render", "export", "chunks", "objects", "mcp")


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
        try:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()
        except BrokenPipeError:   # e.g. `pdfwords ... | head`: stop quietly
            import os
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def extract(argv):
    ap = argparse.ArgumentParser(prog="pdfwords")
    ap.add_argument("pdf")
    ap.add_argument("--mode", default="words", choices=["text", "words", "blocks", "dict", "rawdict"])
    ap.add_argument("--pages", help="0-based, e.g. 0,2-4")
    ap.add_argument("--sort", default="none", choices=["none", "simple", "xycut", "struct"],
                    help="struct: tagged-PDF structure order (untagged text keeps xycut order)")
    ap.add_argument("--roles", action="store_true", help="dict/rawdict: add structure roles (P, H1, LI, ...)")
    ap.add_argument("--backend", default=None, choices=["rust", "python"], help="default: rust if built")
    ap.add_argument("--rotated", action="store_true", help="coordinates on the displayed (rotated) page")
    ap.add_argument("--ligatures", action="store_true", help="re-compose ligature glyphs into U+FB0x chars")
    ap.add_argument("--dehyphenate", action="store_true")
    ap.add_argument("--links", action="store_true", help="dict/rawdict: split spans at links, add span 'url'")
    ap.add_argument("--flatten", action="store_true", help="render form values/annotations into the text first")
    ap.add_argument("--workers", type=int, default=None, help="extract pages in N processes")
    ap.add_argument("--password")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    sort = {"none": False, "simple": True, "xycut": "xycut", "struct": "struct"}[a.sort]
    kw = dict(sort=sort, rotated=a.rotated, ligatures=a.ligatures)
    if a.mode in ("dict", "rawdict"):
        kw["links"] = a.links
        if a.roles:
            kw["roles"] = True
    else:
        kw["dehyphenate"] = a.dehyphenate
    with pdfwords.open(a.pdf, a.password, backend=a.backend, flatten=a.flatten) as doc:
        pages = _pages(a.pages, len(doc))
        res = []
        for i, r in doc.iter_pages(a.mode, pages, a.workers, **kw):
            pg = doc[i]
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


def debug(argv):
    from pdfwords.debug import LAYERS, DEFAULT_LAYERS, overlay, legend
    ap = argparse.ArgumentParser(prog="pdfwords debug", description="draw extracted boxes over the rendered page")
    ap.add_argument("pdf")
    ap.add_argument("-p", "--page", type=int, default=0)
    ap.add_argument("--show", default=",".join(DEFAULT_LAYERS), help=f"comma list of: {', '.join(LAYERS)}")
    ap.add_argument("--scale", type=float, default=2.0, help="render scale (2.0 = 144 dpi)")
    ap.add_argument("--sort", default="xycut", choices=["none", "simple", "xycut"])
    ap.add_argument("--overlay", "-o", dest="out", default=None, help="output image (default: <pdf>-p<N>.png)")
    a = ap.parse_args(argv)
    sort = {"none": False, "simple": True, "xycut": "xycut"}[a.sort]
    out = a.out or f"{a.pdf.rsplit('.', 1)[0]}-p{a.page}.png"
    with pdfwords.open(a.pdf) as doc:
        overlay(doc[a.page], show=a.show, scale=a.scale, sort=sort).save(out)
    _out(json.dumps({"output": out, "legend": legend([s for s in a.show.split(",") if s])}))


def render(argv):
    import os
    ap = argparse.ArgumentParser(prog="pdfwords render", description="render pages to images with PDFium")
    ap.add_argument("pdf")
    ap.add_argument("--pages", help="0-based, e.g. 0,2-4 (default: all)")
    res = ap.add_mutually_exclusive_group()
    res.add_argument("--dpi", type=float, default=None, help="resolution (default 150)")
    res.add_argument("--scale", type=float, default=None, help="pixels per point (dpi / 72)")
    res.add_argument("--max-side", type=int, default=None, help="fit the longest side to this many pixels")
    res.add_argument("--size", default=None, help="WxH pixels; W or H may be empty to keep the aspect ratio")
    ap.add_argument("--fmt", default="jpeg", choices=["jpeg", "png", "webp", "tiff", "bmp", "ppm"])
    ap.add_argument("--quality", type=int, default=90, help="JPEG/WebP quality")
    ap.add_argument("--out-dir", default=None, help="directory for the images (default: next to the PDF)")
    ap.add_argument("--name", default="{stem}-{page:04d}.{ext}", help="file name pattern ({stem} {page} {page1} {ext})")
    ap.add_argument("--workers", type=int, default=None, help="render in N processes")
    ap.add_argument("--gray", action="store_true", help="8-bit grayscale (about 2x faster)")
    ap.add_argument("--alpha", action="store_true", help="transparent background (png/webp/tiff)")
    ap.add_argument("--no-annots", action="store_true", help="do not draw annotations")
    ap.add_argument("--no-forms", action="store_true", help="do not draw form field values")
    ap.add_argument("--no-antialias", action="store_true", help="crisp, non-smoothed rendering")
    ap.add_argument("--unrotated", action="store_true", help="ignore /Rotate (match unrotated text coordinates)")
    ap.add_argument("--clip", default=None, help="x0,y0,x1,y1 in points (top-left origin)")
    ap.add_argument("--timeout", type=float, default=None, help="seconds per page")
    ap.add_argument("--password")
    ap.add_argument("--overlay", default=None, help="draw extraction layers, e.g. words,blocks,order,links")
    ap.add_argument("-o", "--out", default=None, help="output file for a single page (--overlay or one page)")
    a = ap.parse_args(argv)
    kw = dict(grayscale=a.gray, alpha=a.alpha, annots=not a.no_annots, forms=not a.no_forms,
              antialias=not a.no_antialias, rotated=not a.unrotated, timeout=a.timeout)
    if a.clip:
        kw["clip"] = _floats(a.clip, 4)
    if a.scale:
        kw["scale"] = a.scale
    elif a.max_side:
        kw["max_side"] = a.max_side
    elif a.size:
        w, _, h = a.size.lower().partition("x")
        kw["size"] = (int(w) if w else None, int(h) if h else None)
    dpi = a.dpi if a.dpi or kw.keys() & {"scale", "max_side", "size"} else 150
    with pdfwords.open(a.pdf, a.password) as doc:
        pages = _pages(a.pages, len(doc))
        if a.overlay or a.out:
            if len(pages) != 1:
                raise SystemExit("--overlay / -o render a single page: pass --pages N")
            page = doc[pages[0]]
            out = a.out or f"{os.path.splitext(a.pdf)[0]}-p{pages[0]}.{'png' if a.overlay else a.fmt}"
            if a.overlay:
                from pdfwords.debug import overlay
                if a.unrotated or a.clip:
                    raise SystemExit("--overlay draws on the displayed, unclipped page")
                rkw = {k: v for k, v in kw.items() if k in ("grayscale", "annots", "forms", "antialias", "timeout")}
                img = overlay(page, show=a.overlay, scale=dpi / 72.0 if dpi else 2.0, **rkw)
                img.save(out)
            else:
                ext = os.path.splitext(out)[1].lstrip(".").lower() or a.fmt
                with open(out, "wb") as f:
                    f.write(page.render(dpi, output=ext, quality=a.quality, **kw))
            _out(json.dumps({"output": out}))
            return
        out_dir = a.out_dir or (os.path.dirname(os.path.abspath(a.pdf)))
        paths = doc.to_images(pages, dpi, workers=a.workers, fmt=a.fmt, quality=a.quality, out_dir=out_dir,
                              name=a.name, **kw)
    _out(json.dumps({"outputs": paths}, ensure_ascii=False))


def _per_page(argv, prog, fn, extra=None):
    ap = argparse.ArgumentParser(prog=f"pdfwords {prog}")
    ap.add_argument("pdf")
    ap.add_argument("--pages")
    ap.add_argument("--rotated", action="store_true")
    if extra:
        extra(ap)
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf) as doc:
        res = [{"page": i, **fn(doc[i], a)} for i in _pages(a.pages, len(doc))]
    _out(json.dumps(res, ensure_ascii=False))


def links(argv):
    _per_page(argv, "links", lambda p, a: {"links": p.get_links(web=not a.no_web, rotated=a.rotated)},
              lambda ap: ap.add_argument("--no-web", action="store_true", help="only link annotations"))


def annots(argv):
    _per_page(argv, "annots", lambda p, a: {"annots": p.annots(rotated=a.rotated),
                                             "widgets": p.widgets(rotated=a.rotated)})


def quality(argv):
    ap = argparse.ArgumentParser(prog="pdfwords quality", description="text-layer quality / needs-OCR report")
    ap.add_argument("pdf")
    ap.add_argument("--pages")
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf) as doc:
        rep = doc.text_quality(_pages(a.pages, len(doc)))
    _out(json.dumps({"needs_ocr": [r["page"] for r in rep if r["needs_ocr"]], "pages": rep}, ensure_ascii=False))


def toc(argv):
    ap = argparse.ArgumentParser(prog="pdfwords toc")
    ap.add_argument("pdf")
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf) as doc:
        _out(json.dumps(doc.get_toc(), ensure_ascii=False))


def search(argv):
    ap = argparse.ArgumentParser(prog="pdfwords search")
    ap.add_argument("pdf")
    ap.add_argument("needle")
    ap.add_argument("--pages")
    ap.add_argument("--regex", action="store_true")
    ap.add_argument("--case", action="store_true", help="case-sensitive")
    ap.add_argument("--quads", action="store_true")
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf) as doc:
        res = []
        for i in _pages(a.pages, len(doc)):
            hits = doc[i].search_for(a.needle, regex=a.regex, ignore_case=not a.case, quads=a.quads)
            if hits:
                res.append({"page": i, "hits": hits})
    _out(json.dumps(res))


def pdftext_cli(argv):
    """Same flags as pdftext's CLI."""
    from pdfwords.compat import pdftext as pt
    ap = argparse.ArgumentParser(prog="pdfwords pdftext", description="pdftext-compatible output")
    ap.add_argument("pdf_path")
    ap.add_argument("--out_path", default=None)
    ap.add_argument("--json", action="store_true", help="dictionary output instead of plain text")
    ap.add_argument("--sort", action="store_true")
    ap.add_argument("--keep_hyphens", action="store_true")
    ap.add_argument("--page_range", default=None, help="e.g. 0,5-10")
    ap.add_argument("--flatten_pdf", action="store_true")
    ap.add_argument("--keep_chars", action="store_true")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--password", default=None)
    a = ap.parse_args(argv)
    pr = None
    if a.page_range:
        pr = []
        for part in a.page_range.split(","):
            lo, _, hi = part.partition("-")
            pr.extend(range(int(lo), int(hi or lo) + 1))
    kw = dict(page_range=pr, flatten_pdf=a.flatten_pdf, workers=a.workers, password=a.password)
    if a.json:
        pages = pt.dictionary_output(a.pdf_path, sort=a.sort, keep_chars=a.keep_chars, disable_links=True, **kw)
        s = json.dumps(pages, ensure_ascii=False)
    else:
        s = pt.plain_text_output(a.pdf_path, sort=a.sort, hyphens=a.keep_hyphens, **kw)
    _out(s, a.out_path)


def export_cli(argv):
    from pdfwords.exporters import FORMATS
    ap = argparse.ArgumentParser(prog="pdfwords export", description="Markdown / HTML / XHTML / XML / hOCR / ALTO")
    ap.add_argument("pdf")
    ap.add_argument("-f", "--format", default="markdown", choices=FORMATS + ("md",))
    ap.add_argument("--pages")
    ap.add_argument("--sort", default="auto", help="auto (struct if tagged, else xycut) | xycut | struct")
    ap.add_argument("--dpi", type=float, default=72, help="hOCR / ALTO pixel units")
    ap.add_argument("--no-headers-footers", action="store_true", help="markdown: keep running headers/footers")
    ap.add_argument("--no-tables", action="store_true", help="markdown: no table detection")
    ap.add_argument("--image-dir", help="markdown: write page images here and link them")
    ap.add_argument("--page-separators", action="store_true", help="markdown: <!-- page N --> markers")
    ap.add_argument("--password")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    fmt = "markdown" if a.format == "md" else a.format
    kw = {}
    if fmt in ("markdown", "xhtml"):
        kw = dict(headers_footers=not a.no_headers_footers, tables=not a.no_tables)
        if fmt == "markdown":
            kw.update(page_separators=a.page_separators)
            if a.image_dir:
                kw.update(images=True, image_dir=a.image_dir)
    with pdfwords.open(a.pdf, a.password) as doc:
        s = pdfwords.export(doc, fmt, _pages(a.pages, len(doc)), sort=a.sort, dpi=a.dpi, **kw)
    _out(s, a.out)


def chunks_cli(argv):
    ap = argparse.ArgumentParser(prog="pdfwords chunks", description="heading-aware chunks (JSON lines)")
    ap.add_argument("pdf")
    ap.add_argument("--pages")
    ap.add_argument("--max-chars", type=int, default=2000)
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--overlap", type=int, default=0)
    ap.add_argument("--split-level", type=int, default=2)
    ap.add_argument("--password")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    with pdfwords.open(a.pdf, a.password) as doc:
        cs = pdfwords.chunks(doc, _pages(a.pages, len(doc)), max_chars=a.max_chars, min_chars=a.min_chars,
                             overlap=a.overlap, split_level=a.split_level)
    _out("\n".join(json.dumps(c, ensure_ascii=False) for c in cs), a.out)


def objects_cli(argv):
    _per_page(argv, "objects", lambda p, a: {a.kind: {
        "images": lambda: p.get_images(rotated=a.rotated, hashes=True),
        "drawings": lambda: p.get_drawings(rotated=a.rotated),
        "struct": lambda: p.get_struct_tree()}[a.kind]()},
        lambda ap: ap.add_argument("--kind", default="images", choices=["images", "drawings", "struct"]))


def mcp_cli(argv):
    from pdfwords.mcp_server import main as m
    m(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        return {"extract": extract, "redact": redact, "insert-text": insert_text, "contents": contents,
                "debug": debug, "links": links, "annots": annots, "toc": toc, "quality": quality,
                "search": search, "pdftext": pdftext_cli, "render": render, "export": export_cli,
                "chunks": chunks_cli, "objects": objects_cli, "mcp": mcp_cli}[cmd](rest)
    return extract(argv)


if __name__ == "__main__":
    main()
