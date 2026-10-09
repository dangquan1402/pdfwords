# SPDX-License-Identifier: Apache-2.0
"""Export formats: markdown, html (positioned), xhtml (semantic), xml (chars), hOCR, ALTO.

    page.get_text("hocr")                       # one page
    pdfwords.export("file.pdf", "alto", pages="0-4", dpi=300)   # whole document as one string
    pdfwords export file.pdf --format hocr -o file.hocr          # CLI

hOCR and ALTO coordinates are pixels of a page image rendered at `dpi` (default 72, i.e.
points), so the output lines up with page.render(dpi=...) for OCR/archive toolchains.
Coordinates are of the displayed page (rotation applied), as in rendered images.
"""
from __future__ import annotations

import html as _html

from . import __version__

FORMATS = ("markdown", "html", "xhtml", "xml", "hocr", "alto")


def _e(s):
    return _html.escape(s, quote=True)


def _f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _hex(c):
    return f"#{c & 0xFFFFFF:06x}"


# ---------------------------------------------------------------------- html (positioned)
def _html_page(page, sort):
    d = page.get_text("dict", sort=sort, rotated=True, links=True)
    w, h = d["width"], d["height"]
    out = [f'<div id="page{page.number}" class="page" style="position:relative;width:{_f(w)}pt;height:{_f(h)}pt;'
           'border:1px solid #ccc;margin:1em auto;overflow:hidden">']
    for b in d["blocks"]:
        for ln in b["lines"]:
            x0, y0 = ln["bbox"][0], ln["bbox"][1]
            parts = []
            for sp in ln["spans"]:
                fam = "monospace" if sp["flags"] & 8 else ("serif" if sp["flags"] & 4 else "sans-serif")
                style = f"font-family:'{_e(sp['font'])}',{fam};font-size:{_f(sp['size'])}pt;color:{_hex(sp['color'])}"
                t = _e(sp["text"])
                if sp["flags"] & 16:
                    t = f"<b>{t}</b>"
                if sp["flags"] & 2:
                    t = f"<i>{t}</i>"
                if sp.get("url"):
                    t = f'<a href="{_e(sp["url"])}">{t}</a>'
                parts.append(f'<span style="{style}">{t}</span>')
            out.append(f'<p style="position:absolute;white-space:pre;margin:0;padding:0;top:{_f(y0)}pt;'
                       f'left:{_f(x0)}pt">{"".join(parts)}</p>')
    out.append("</div>")
    return "\n".join(out)


# ---------------------------------------------------------------------- xhtml (semantic)
def _xhtml_elements(els):
    out = []
    in_list = False
    for e in els:
        attrs = f' data-page="{e["page"]}" data-bbox="{" ".join(_f(v) for v in e["bbox"])}"'
        if e["type"] != "list_item" and in_list:
            out.append("</ul>")
            in_list = False
        if e["type"] == "heading":
            out.append(f'<h{e["level"]}{attrs}>{_e(e["text"])}</h{e["level"]}>')
        elif e["type"] == "list_item":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li{attrs}>{_e(e['text'])}</li>")
        elif e["type"] == "code":
            out.append(f"<pre{attrs}><code>{_e(e['text'])}</code></pre>")
        elif e["type"] == "table":
            rows = e.get("rows") or []
            trs = []
            for i, r in enumerate(rows):
                tag = "th" if i == 0 and e.get("header", True) else "td"
                trs.append("<tr>" + "".join(f"<{tag}>{_e(c or '')}</{tag}>" for c in r) + "</tr>")
            out.append(f"<table{attrs}>" + "".join(trs) + "</table>")
        elif e["type"] == "image":
            out.append(f'<img{attrs} alt="{_e(e.get("alt") or "image")}" src="{_e(e.get("path", ""))}"/>')
        else:
            out.append(f"<p{attrs}>{_e(e['text'])}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# ---------------------------------------------------------------------- xml (chars)
def _xml_page(page, sort):
    d = page.get_text("rawdict", sort=sort, rotated=True)
    out = [f'<page id="page{page.number}" width="{_f(d["width"])}" height="{_f(d["height"])}">']
    for b in d["blocks"]:
        out.append(f'<block bbox="{" ".join(_f(v) for v in b["bbox"])}">')
        for ln in b["lines"]:
            out.append(f'<line bbox="{" ".join(_f(v) for v in ln["bbox"])}" wmode="{ln["wmode"]}" '
                       f'dir="{_f(ln["dir"][0])} {_f(ln["dir"][1])}">')
            for sp in ln["spans"]:
                out.append(f'<font name="{_e(sp["font"])}" size="{_f(sp["size"])}" flags="{sp["flags"]}" '
                           f'color="{_hex(sp["color"])}">')
                for ch in sp["chars"]:
                    x0, y0, x1, y1 = ch["bbox"]
                    out.append(f'<char bbox="{_f(x0)} {_f(y0)} {_f(x1)} {_f(y1)}" x="{_f(ch["origin"][0])}" '
                               f'y="{_f(ch["origin"][1])}" c="{_e(ch["c"])}"/>')
                out.append("</font>")
            out.append("</line>")
        out.append("</block>")
    out.append("</page>")
    return "\n".join(out)


# ---------------------------------------------------------------------- hOCR
def _px(v, s):
    return int(round(v * s))


def _hocr_page(page, sort, dpi):
    s = dpi / 72.0
    d = page.get_text("dict", sort=sort, rotated=True)
    words = page.get_text("words", sort=sort, rotated=True)
    by_line = {}
    for w in words:
        by_line.setdefault((w[5], w[6]), []).append(w)
    W, H = d["width"], d["height"]
    pid = page.number + 1
    out = [f'<div class="ocr_page" id="page_{pid}" title="bbox 0 0 {_px(W, s)} {_px(H, s)}; ppageno {page.number}; '
           f'scan_res {int(dpi)} {int(dpi)}">']
    for b in d["blocks"]:
        bn = b["number"]
        bb = " ".join(str(_px(v, s)) for v in b["bbox"])
        out.append(f'<div class="ocr_carea" id="block_{pid}_{bn + 1}" title="bbox {bb}">')
        out.append(f'<p class="ocr_par" id="par_{pid}_{bn + 1}" title="bbox {bb}">')
        for li, ln in enumerate(b["lines"]):
            lb = " ".join(str(_px(v, s)) for v in ln["bbox"])
            size = max((sp["size"] for sp in ln["spans"]), default=0)
            out.append(f'<span class="ocr_line" id="line_{pid}_{bn + 1}_{li + 1}" title="bbox {lb}; '
                       f'x_size {_f(size * s)}; x_descenders 0; x_ascenders 0">')
            for w in by_line.get((bn, li), []):
                wb = " ".join(str(_px(v, s)) for v in w[:4])
                out.append(f'<span class="ocrx_word" id="word_{pid}_{bn + 1}_{li + 1}_{w[7] + 1}" '
                           f'title="bbox {wb}; x_wconf 100">{_e(w[4])}</span>')
            out.append("</span>")
        out.append("</p>\n</div>")
    out.append("</div>")
    return "\n".join(out)


def _hocr_doc(bodies, title):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
            '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n<head>\n'
            f"<title>{_e(title)}</title>\n"
            '<meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>\n'
            f'<meta name="ocr-system" content="pdfwords {__version__}"/>\n'
            '<meta name="ocr-capabilities" content="ocr_page ocr_carea ocr_par ocr_line ocrx_word"/>\n'
            "</head>\n<body>\n" + "\n".join(bodies) + "\n</body>\n</html>\n")


# ---------------------------------------------------------------------- ALTO v4
def _alto_page(page, sort, dpi, styles):
    s = dpi / 72.0
    d = page.get_text("rawdict", sort=sort, rotated=True)
    pid = page.number + 1
    W, H = _px(d["width"], s), _px(d["height"], s)
    out = [f'<Page ID="page_{pid}" PHYSICAL_IMG_NR="{pid}" WIDTH="{W}" HEIGHT="{H}">',
           f'<PrintSpace HPOS="0" VPOS="0" WIDTH="{W}" HEIGHT="{H}">']

    def geo(bb):
        x0, y0, x1, y1 = (v * s for v in bb)
        return f'HPOS="{_f(x0)}" VPOS="{_f(y0)}" WIDTH="{_f(x1 - x0)}" HEIGHT="{_f(y1 - y0)}"'
    for b in d["blocks"]:
        bn = b["number"] + 1
        out.append(f'<TextBlock ID="block_{pid}_{bn}" {geo(b["bbox"])}>')
        for li, ln in enumerate(b["lines"]):
            out.append(f'<TextLine ID="line_{pid}_{bn}_{li + 1}" {geo(ln["bbox"])}>')
            # words: chars split at whitespace (exact boxes), style of the word's first char
            words = []
            for sp in ln["spans"]:
                key = (sp["font"], round(sp["size"], 1), sp["flags"] & 18)
                sid = styles.setdefault(key, f"font{len(styles)}")
                for ch in sp["chars"]:
                    if not ch["c"].strip():
                        words.append(None)
                    elif words and words[-1] is not None:
                        words[-1][0].append(ch)
                    else:
                        words.append([[ch], sid])
            wn = 0
            for w in words:
                if w is None:
                    continue
                chs, sid = w
                bb = (min(c["bbox"][0] for c in chs), min(c["bbox"][1] for c in chs),
                      max(c["bbox"][2] for c in chs), max(c["bbox"][3] for c in chs))
                if wn:
                    out.append("<SP/>")
                wn += 1
                out.append(f'<String ID="string_{pid}_{bn}_{li + 1}_{wn}" CONTENT="{_e("".join(c["c"] for c in chs))}" '
                           f'{geo(bb)} STYLEREFS="{sid}"/>')
            out.append("</TextLine>")
        out.append("</TextBlock>")
    out.append("</PrintSpace>\n</Page>")
    return "\n".join(out)


def _alto_doc(pages_xml, styles, name, dpi):
    st = []
    for (font, size, fl), sid in styles.items():
        fs = " ".join(x for x, bit in (("bold", 16), ("italics", 2)) if fl & bit)
        st.append(f'<TextStyle ID="{sid}" FONTFAMILY="{_e(font)}" FONTSIZE="{_f(size)}"'
                  + (f' FONTSTYLE="{fs}"' if fs else "") + "/>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://www.loc.gov/standards/alto/ns-v4# '
            'http://www.loc.gov/alto/v4/alto-4-2.xsd">\n'
            "<Description>\n<MeasurementUnit>pixel</MeasurementUnit>\n"
            f"<sourceImageInformation><fileName>{_e(name)}</fileName></sourceImageInformation>\n"
            "<Processing ID=\"proc_1\"><processingSoftware><softwareName>pdfwords</softwareName>"
            f"<softwareVersion>{__version__}</softwareVersion></processingSoftware>"
            f"<processingStepSettings>dpi={int(dpi)}</processingStepSettings></Processing>\n"
            "</Description>\n<Styles>\n" + "\n".join(st) + "\n</Styles>\n<Layout>\n" + "\n".join(pages_xml)
            + "\n</Layout>\n</alto>\n")


# ---------------------------------------------------------------------- entry points
def _sort(sort):
    return "xycut" if sort in (None, True, "auto") else sort


def page_export(page, fmt, sort=False, clip=True, rotated=False, dpi=72):
    fmt = {"md": "markdown"}.get(fmt, fmt)
    srt = _sort(sort) if sort else "xycut"
    if fmt == "markdown":
        from .markdown import Context, page_elements, _join_md, _harmonise_levels
        ctx = Context(page.parent, [page.number], sort="auto" if sort in (False, None, True) else sort,
                      headers_footers=False)
        els = page_elements(page, ctx)
        _harmonise_levels([e for e in els if e["type"] == "heading"])
        for e in els:
            if e["type"] == "image":
                from .markdown import _image_md
                e["md"] = _image_md(e, page, None, 72, "")
        return _join_md(els)
    if fmt == "html":
        return _html_page(page, srt)
    if fmt == "xhtml":
        from .markdown import Context, page_elements, _harmonise_levels
        ctx = Context(page.parent, [page.number], headers_footers=False)
        els = page_elements(page, ctx)
        _harmonise_levels([e for e in els if e["type"] == "heading"])
        return f'<div id="page{page.number}">\n{_xhtml_elements(els)}\n</div>'
    if fmt == "xml":
        return _xml_page(page, srt)
    if fmt == "hocr":
        return _hocr_doc([_hocr_page(page, srt, dpi)], page.parent.name or "pdfwords")
    if fmt == "alto":
        styles = {}
        body = _alto_page(page, srt, dpi, styles)
        return _alto_doc([body], styles, page.parent.name, dpi)
    raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")


def export(src, fmt="markdown", pages=None, *, sort="auto", dpi=72, password=None, **kw):
    """Whole-document export as one string. fmt: markdown | html | xhtml | xml | hocr | alto.
    kw: passed to to_markdown for markdown / xhtml (headers_footers, tables, images, ...)."""
    from . import Document, _page_list, open as _open
    fmt = {"md": "markdown"}.get(fmt.lower(), fmt.lower())
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    doc = src if isinstance(src, Document) else _open(src, password)
    try:
        pl = _page_list(pages, len(doc))
        srt = _sort(sort)
        title = doc.name or "document"
        if fmt == "markdown":
            from .markdown import to_markdown
            return to_markdown(doc, pl, sort=sort, **kw)
        if fmt == "xhtml":
            from .markdown import to_markdown
            pcs = to_markdown(doc, pl, sort=sort, page_chunks=True, **kw)
            body = "\n".join(f'<div id="page{c["metadata"]["page"]}" class="page">\n{_xhtml_elements(c["elements"])}\n'
                             "</div>" for c in pcs)
            return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml">\n<head><meta charset="utf-8"/>'
                    f"<title>{_e(title)}</title></head>\n<body>\n{body}\n</body>\n</html>\n")
        if fmt == "html":
            body = "\n".join(_html_page(doc[i], srt) for i in pl)
            return (f'<!DOCTYPE html>\n<html>\n<head><meta charset="utf-8"/><title>{_e(title)}</title></head>\n'
                    f'<body style="background:#eee">\n{body}\n</body>\n</html>\n')
        if fmt == "xml":
            body = "\n".join(_xml_page(doc[i], srt) for i in pl)
            return f'<?xml version="1.0" encoding="UTF-8"?>\n<document name="{_e(title)}">\n{body}\n</document>\n'
        if fmt == "hocr":
            return _hocr_doc([_hocr_page(doc[i], srt, dpi) for i in pl], title)
        styles = {}
        bodies = [_alto_page(doc[i], srt, dpi, styles) for i in pl]
        return _alto_doc(bodies, styles, title, dpi)
    finally:
        if doc is not src:
            doc.close()
