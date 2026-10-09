# SPDX-License-Identifier: Apache-2.0
"""Drop-in replacement for pdftext's extraction API (datalab-to/pdftext, Apache-2.0), backed by
pdfwords' layout engine.

    from pdfwords.compat.pdftext import plain_text_output, dictionary_output, table_output
    # instead of: from pdftext.extraction import ...

Same functions, arguments and output schema as pdftext 0.7:

  page  {page, bbox, width, height, rotation, blocks, refs}
  block {bbox, lines}            line {bbox, spans}
  span  {bbox, text, font{name, flags, size, weight}, rotation (radians), char_start_idx,
         char_end_idx, url, superscript, subscript[, chars]}
  char  {bbox, char, rotation, font, char_idx}
  refs  [Reference(idx, page, coord)] with .ref "page-{page}-{idx}" and .url "#page-..."

Coordinates are pdftext's: points, top-left origin, on the page as displayed (rotation applied);
bboxes are lists. Each line ends with a "\\n" span (empty font, weight -1) like pdftext's
PDFium line breaks, except the page's last line.

Differences, by design: blocks/lines/spans come from pdfwords' grouping (column- and
line-aware) rather than pdftext's median-gap heuristics, so groupings can differ; char boxes
are pdfwords' (ascender/descender) boxes, with quote_loosebox=False still giving apostrophes
PDFium's tight box; superscript/subscript use a baseline-shift test. `sort` also accepts
"xycut" (column-aware reading order) in addition to True/False.
Written independently against pdftext's public API and output; no pdftext code is copied.
"""
from __future__ import annotations

import concurrent.futures as _cf
import math
import unicodedata as _ud
from dataclasses import dataclass
from typing import Dict, List, Optional

import pypdfium2 as pdfium

import pdfwords as _pw

__all__ = ["plain_text_output", "paginated_plain_text_output", "dictionary_output", "table_output",
           "Reference", "PageReference", "PdfPasswordError", "sort_blocks"]

WORKER_PAGE_THRESHOLD = 10   # same default as pdftext: at least this many pages per worker
_EMPTY_FONT = {"name": "", "flags": 0, "size": 1.0, "weight": -1}
_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\ufb05": "st", "\ufb06": "st"}


class PdfPasswordError(pdfium.PdfiumError):
    """The PDF is encrypted and the password is missing or wrong."""


@dataclass
class Reference:
    idx: int
    page: int
    coord: List[float]

    @property
    def ref(self):
        return f"page-{self.page}-{self.idx}"

    @property
    def url(self):
        return f"#{self.ref}"


class PageReference:
    def __init__(self):
        self.page_ref_map: Dict[int, List[Reference]] = {}

    def get_refs(self, page: int) -> List[Reference]:
        return self.page_ref_map.get(page, [])

    def check_ref(self, page: int, coord) -> Optional[Reference]:
        for r in self.page_ref_map.get(page, []):
            if r.coord == coord:
                return r
        return None

    def add_ref(self, page: int, coord) -> Reference:
        refs = self.page_ref_map.setdefault(page, [])
        r = self.check_ref(page, coord)
        if r is None:
            r = Reference(idx=len(refs), page=page, coord=coord)
            refs.append(r)
        return r


# ---------------------------------------------------------------------- text helpers
def _clean(text):
    """pdftext's text normalisation: odd spaces -> " ", CR/LF -> "\\n", ligatures expanded,
    control/format characters dropped."""
    if text.isascii() and text.isprintable():
        return text
    text = text.replace("\r\n", "\n")
    out = []
    for c in text:
        if c in "\u00a0\ufeff\ufffe":
            out.append(" ")
        elif c == "\r":
            out.append("\n")
        elif c in _LIGATURES:
            out.append(_LIGATURES[c])
        elif c in "\n\t\f " or _ud.category(c)[0] != "C":
            out.append(c)
    return "".join(out)


def sort_blocks(blocks, tolerance=1.25):
    """Rows by rounded top edge, left-to-right within a row (pdftext's simple order)."""
    groups = {}
    for b in blocks:
        groups.setdefault(round(b["bbox"][1] / tolerance) * tolerance, []).append(b)
    out = []
    for _k, g in sorted(groups.items()):
        out.extend(sorted(g, key=lambda b: b["bbox"][0]))
    return out


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


# ---------------------------------------------------------------------- page builder
def _open(pdf_path, flatten_pdf, password):
    try:
        return _pw.open(pdf_path, password, flatten=flatten_pdf)
    except pdfium.PdfiumError as e:
        if getattr(e, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
            raise PdfPasswordError("PDF is encrypted; pass the correct password via the password argument.") from e
        raise


def _scripts(line_spans):
    """Mark superscript/subscript spans: smaller than the line's main text and with a shifted
    baseline (horizontal lines only)."""
    real = [s for s in line_spans if s["text"].strip()]
    if len(real) < 2:
        return
    main = max(real, key=lambda s: (len(s["text"].strip()) * s["font"]["size"], s["font"]["size"]))
    msize, mbase = main["font"]["size"], main["_base"]
    for s in real:
        if s is main or s["_vertical"]:
            continue
        t = s["text"].strip()
        if len(t) > 6 or s["font"]["size"] > 0.85 * msize:
            continue
        shift = mbase - s["_base"]          # y grows downwards: positive = raised
        if shift >= 0.15 * msize or (s.get("_sup", s.get("_supflag")) and shift > 0):
            s["superscript"] = True
        elif -shift >= 0.08 * msize:
            s["subscript"] = True


def _tight_box(page, idx, xf):
    import ctypes
    import pypdfium2.raw as R
    tp = page._compat_tp
    l, r, b, t = (ctypes.c_double() for _ in range(4))
    if idx < 0 or not R.FPDFText_GetCharBox(tp.raw, idx, l, r, b, t):
        return None
    return list(xf.rect(l.value, b.value, r.value, t.value))


def _page_links(page, with_links):
    from ..annots import link_url, LINK_GOTO
    link_rects = []
    if with_links:
        for lk in page.get_links(rotated=True):
            if lk.get("kind") == LINK_GOTO and lk.get("page") is not None and lk["page"] >= 0:
                if lk.get("to") is not None:   # pdftext's position: top-left of a 2pt box around it
                    target = ("ref", lk["page"], [float(round(lk["to"][0] - 1)), float(round(lk["to"][1] - 1))])
                elif lk["page"] == page.number:
                    continue           # pdftext: no self-links without a position
                else:
                    target = ("ref", lk["page"], [0.0, 0.0])
            else:
                target = link_url(lk)
                if not target:
                    continue
            rects = [_pw._quad_rect(q) for q in lk["quads"]] if lk.get("quads") else [lk["from"]]
            link_rects.extend((r, target) for r in rects)
    return link_rects


def _split_links(spans, link_rects):
    """Split spans (with chars) where the covering link changes."""
    from ..annots import char_urls
    out = []
    for s in spans:
        sb = s["bbox"]
        chars = s.get("chars")
        if not chars or not any(r[0] <= sb[2] and r[2] >= sb[0] and r[1] <= sb[3] and r[3] >= sb[1]
                                for r, _u in link_rects):
            out.append(s)
            continue
        urls = char_urls([c["bbox"] for c in chars], link_rects, texts=[c["char"] for c in chars])
        start = 0
        for k in range(1, len(chars) + 1):
            if k == len(chars) or urls[k] != urls[start]:
                cs = chars[start:k]
                ns = dict(s)
                ns.update(bbox=_union([c["bbox"] for c in cs]), text="".join(c["char"] for c in cs),
                          char_start_idx=cs[0]["char_idx"], char_end_idx=cs[-1]["char_idx"], url=urls[start],
                          chars=cs)
                out.append(ns)
                start = k
    return out


def _build_page(page, keep_chars, quote_loosebox, with_links, sort):
    """One page in pdftext's schema (links as pending ("ref", page, coord) when internal)."""
    from ..annots import Transform
    from ..chars import crop_box
    rot = page.rotation
    link_rects = _page_links(page, with_links)
    if page._rust and quote_loosebox:     # no PDFium page object needed from Python
        crop = page._rs(False).crop
        xf = None
    else:
        pg = page._page
        crop = crop_box(pg)
        xf = Transform(pg.raw, crop, rot, rotated=True)
    if page._rust:
        need_chars = keep_chars or bool(link_rects) or not quote_loosebox
        blocks = page._rs(False).pdftext_blocks("xycut" if sort == "xycut" else False, need_chars)
        tp = None
        if not quote_loosebox:
            tp = page._compat_tp = page._page.get_textpage()
        try:
            for b in blocks:
                for ln in b["lines"]:
                    spans = ln["spans"]
                    if link_rects:
                        spans = ln["spans"] = _split_links(spans, link_rects)
                    _scripts(spans)
                    for s in spans:
                        t = s.pop("_base"), s.pop("_sup"), s.pop("_vertical")  # noqa: F841
                        txt = s["text"]
                        if not (txt.isascii() and txt.isprintable()):
                            s["text"] = _clean(txt)
                        if tp is not None and "'" in txt:
                            for c in s["chars"]:
                                if c["char"] == "'":
                                    c["bbox"] = _tight_box(page, c["char_idx"], xf) or c["bbox"]
                        if not keep_chars:
                            s.pop("chars", None)
        finally:
            if tp is not None:
                tp.close()
                page._compat_tp = None
        return _finish_page(page, crop, rot, blocks, sort)
    d = page.get_text("rawdict", rotated=True, clip=False, extended=True,
                      sort="xycut" if sort == "xycut" else False)

    tp = None
    if not quote_loosebox:
        tp = page._compat_tp = pg.get_textpage()
    try:
        blocks = []
        fonts = {}
        n_lines = sum(len(b["lines"]) for b in d["blocks"])
        li = 0
        for b in d["blocks"]:
            out_lines = []
            for ln in b["lines"]:
                li += 1
                dx, dy = ln["dir"]
                rotation = math.atan2(-dy, dx) % (2 * math.pi) if (dx, dy) != (1.0, 0.0) else 0.0
                vertical = abs(dy) > abs(dx)
                spans = []
                last_idx = -1
                for sp in ln["spans"]:
                    fkey = (sp["font"], sp.get("pdf_flags", 0), sp["size"], sp.get("weight", 400))
                    font = fonts.get(fkey)
                    if font is None:   # interned per page, like pdftext
                        font = fonts[fkey] = {"name": fkey[0], "flags": fkey[1], "size": float(fkey[2]),
                                              "weight": fkey[3]}
                    raw = sp["chars"]
                    if not raw:
                        continue
                    sb = sp["bbox"]
                    if (quote_loosebox and not any(c["synthetic"] for c in raw)
                            and not any(r[0] <= sb[2] and r[2] >= sb[0] and r[1] <= sb[3] and r[3] >= sb[1]
                                        for r, _u in link_rects)):
                        # fast path: one span, PDFium indices as they are
                        txt = "".join([c["c"] for c in raw])
                        i0, i1 = raw[0]["idx"], raw[-1]["idx"]
                        last_idx = max(last_idx, i1)
                        ns = {"bbox": list(sb), "text": txt, "font": font, "rotation": rotation,
                              "char_start_idx": i0, "char_end_idx": i1, "url": "", "superscript": False,
                              "subscript": False, "_txt": txt, "_base": sp["origin"][0 if vertical else 1],
                              "_vertical": vertical, "_supflag": bool(sp["flags"] & 1)}
                        if keep_chars:
                            ns["chars"] = [{"bbox": list(c["bbox"]), "char": c["c"], "rotation": rotation,
                                            "font": font, "char_idx": c["idx"]} for c in raw]
                        else:
                            ns["_lastbox"] = raw[-1]["bbox"]
                        spans.append(ns)
                        continue
                    chars = []
                    for k, ch in enumerate(raw):
                        idx = ch["idx"]
                        if idx < 0:   # synthetic space: PDFium's generated char sits between neighbours
                            nxt = next((c["idx"] for c in raw[k + 1:] if c["idx"] >= 0), None)
                            idx = last_idx + 1 if nxt is None or last_idx + 1 < nxt else last_idx
                        last_idx = max(last_idx, idx)
                        bb = list(ch["bbox"])
                        if not quote_loosebox and ch["c"] == "'" and ch["idx"] >= 0:
                            bb = _tight_box(page, ch["idx"], xf) or bb
                        # PDFium-generated spaces carry no font (pdftext: own span, weight -1)
                        cf = _EMPTY_FONT if ch.get("synthetic") else font
                        chars.append({"bbox": bb, "char": ch["c"], "rotation": rotation, "font": cf,
                                      "char_idx": idx})
                    # split at font (generated chars) and link boundaries
                    urls = [""] * len(chars)
                    if link_rects and chars:
                        from ..annots import char_urls
                        urls = char_urls([c["bbox"] for c in chars], link_rects, texts=[c["char"] for c in chars])
                    groups = []
                    for c, u in zip(chars, urls):
                        if groups and groups[-1][1] == u and groups[-1][2] is c["font"]:
                            groups[-1][0].append(c)
                        else:
                            groups.append(([c], u, c["font"]))
                    for cs, url, cfont in groups:
                        txt = "".join(c["char"] for c in cs)
                        spans.append({
                            "bbox": _union([c["bbox"] for c in cs]), "text": txt, "font": cfont,
                            "rotation": rotation, "char_start_idx": cs[0]["char_idx"],
                            "char_end_idx": cs[-1]["char_idx"], "url": url, "superscript": False,
                            "subscript": False, "chars": cs,
                            "_txt": txt, "_base": sp["origin"][0 if vertical else 1], "_vertical": vertical,
                            "_supflag": bool(sp["flags"] & 1)})
                if not spans:
                    continue
                _scripts(spans)
                if li < n_lines:      # PDFium-style line break span
                    lb = spans[-1]["chars"][-1]["bbox"] if "chars" in spans[-1] else spans[-1]["_lastbox"]
                    x = lb[2] if not vertical else (lb[0] + lb[2]) / 2
                    y = spans[-1]["_base"] if not vertical else lb[3]
                    nl_idx = last_idx + 1      # PDFium's generated "\r\n" pair
                    spans.append({"bbox": [x, y, x, y], "text": "\r\n", "font": _EMPTY_FONT,
                                  "rotation": rotation, "char_start_idx": nl_idx, "char_end_idx": nl_idx + 1,
                                  "url": "", "superscript": False, "subscript": False,
                                  "chars": [{"bbox": [x, y, x, y], "char": c, "rotation": rotation,
                                             "font": _EMPTY_FONT, "char_idx": nl_idx + k}
                                            for k, c in enumerate("\r\n")]})
                for s in spans:
                    for k in ("_txt", "_base", "_vertical", "_supflag", "_lastbox"):
                        s.pop(k, None)
                    s["text"] = _clean(s["text"])
                    if not keep_chars:
                        s.pop("chars", None)
                out_lines.append({"bbox": _union([s["bbox"] for s in spans]), "spans": spans})
            if out_lines:
                blocks.append({"bbox": _union([ln["bbox"] for ln in out_lines]), "lines": out_lines})
    finally:
        if tp is not None:
            tp.close()
            page._compat_tp = None

    return _finish_page(page, crop, rot, blocks, sort)


def _finish_page(page, crop, rot, blocks, sort):
    l, b0, r, t = crop
    w, h = math.ceil(abs(r - l)), math.ceil(abs(t - b0))
    if sort is True:
        blocks = sort_blocks(blocks)
    page_d = {"page": page.number, "bbox": (l, b0, r, t), "width": w, "height": h, "rotation": rot,
              "blocks": blocks}
    if rot in (90, 270):
        page_d["width"], page_d["height"] = h, w
        page_d["bbox"] = [b0, l, t, r]
    return page_d


def _worker(args):
    src, password, flatten, pages, opts = args
    with _open(src, flatten, password) as doc:
        return [_build_page(doc[i], **opts) for i in pages]


def _get_pages(pdf_path, page_range, flatten_pdf, quote_loosebox, workers, password, keep_chars,
               with_links, sort):
    doc = _open(pdf_path, flatten_pdf, password)
    try:
        n = len(doc)
        pages = list(range(n)) if page_range is None else list(page_range)
        bad = [p for p in pages if not 0 <= p < n]
        if bad:
            raise ValueError(f"Invalid page number(s) {bad}; document has {n} pages (0-indexed).")
        opts = dict(keep_chars=keep_chars, quote_loosebox=quote_loosebox, with_links=with_links, sort=sort)
        if workers is not None:
            workers = min(workers, len(pages) // WORKER_PAGE_THRESHOLD)
        if not workers or workers <= 1:
            return [_build_page(doc[i], **opts) for i in pages]
        src = doc._current_source()
    finally:
        doc.close()
    size = math.ceil(len(pages) / workers)
    chunks = [pages[i:i + size] for i in range(0, len(pages), size)]
    with _cf.ProcessPoolExecutor(len(chunks)) as ex:
        parts = list(ex.map(_worker, [(src, password, flatten_pdf, c, opts) for c in chunks]))
    return [p for part in parts for p in part]


def _resolve_refs(pages):
    refs = PageReference()
    for p in pages:
        for b in p["blocks"]:
            for ln in b["lines"]:
                for s in ln["spans"]:
                    u = s["url"]
                    if isinstance(u, tuple):
                        s["url"] = refs.add_ref(u[1], u[2]).url
    for p in pages:
        p["refs"] = refs.get_refs(p["page"])


# ---------------------------------------------------------------------- public API
def dictionary_output(pdf_path, sort=False, page_range=None, keep_chars=False, flatten_pdf=False,
                      quote_loosebox=True, disable_links=False, workers=None, password=None):
    pages = _get_pages(pdf_path, page_range, flatten_pdf, quote_loosebox, workers, password,
                       keep_chars, not disable_links, sort)
    if disable_links:
        for p in pages:
            p["refs"] = []
    else:
        _resolve_refs(pages)
    return pages


def _page_text(blocks, sort, hyphens):
    if sort is True:
        blocks = sort_blocks([{"bbox": b[:4], "t": b[4]} for b in blocks])
        texts = [b["t"] for b in blocks]
    else:
        texts = [b[4] for b in blocks]
    out = []
    for t in texts:
        lines = [_clean(x).rstrip() for x in t.rstrip("\n").split("\n")]
        out.append("\n".join(lines).rstrip() + "\n\n")
    return "".join(out).strip()


def paginated_plain_text_output(pdf_path, sort=False, hyphens=False, page_range=None, flatten_pdf=False,
                                quote_loosebox=True, workers=None, password=None) -> List[str]:
    """Plain text per page. hyphens=False joins words broken by a line-end hyphen."""
    doc = _open(pdf_path, flatten_pdf, password)
    try:
        n = len(doc)
        pages = list(range(n)) if page_range is None else list(page_range)
        bad = [p for p in pages if not 0 <= p < n]
        if bad:
            raise ValueError(f"Invalid page number(s) {bad}; document has {n} pages (0-indexed).")
        if workers is not None:
            workers = min(workers, len(pages) // WORKER_PAGE_THRESHOLD)
        kw = dict(rotated=True, clip=False, dehyphenate=not hyphens, sort="xycut" if sort == "xycut" else False)
        res = doc.extract("blocks", pages, workers if workers and workers > 1 else None, **kw)
    finally:
        doc.close()
    return [_page_text(b, sort, hyphens) for b in res]


def plain_text_output(pdf_path, sort=False, hyphens=False, page_range=None, flatten_pdf=False,
                      quote_loosebox=True, workers=None, password=None) -> str:
    return "\n".join(paginated_plain_text_output(pdf_path, sort=sort, hyphens=hyphens, page_range=page_range,
                                                 flatten_pdf=flatten_pdf, quote_loosebox=quote_loosebox,
                                                 workers=workers, password=password))


def _table_fragments(table, page, img_size, table_thresh=0.8):
    """Text fragments of one table: runs of chars on a line separated by gaps narrower than
    ~0.8 x the line height; bboxes in image pixels relative to the table's top-left corner."""
    sx = img_size[0] / page["width"] if page["width"] else 1.0
    sy = img_size[1] / page["height"] if page["height"] else 1.0
    tx0, ty0, tx1, ty1 = table
    frags = []
    for b in page["blocks"]:
        for ln in b["lines"]:
            lb = ln["bbox"]
            lbs = [lb[0] * sx, lb[1] * sy, lb[2] * sx, lb[3] * sy]
            area = (lbs[2] - lbs[0]) * (lbs[3] - lbs[1])
            ix = min(lbs[2], tx1) - max(lbs[0], tx0)
            iy = min(lbs[3], ty1) - max(lbs[1], ty0)
            if area <= 0 or ix <= 0 or iy <= 0 or ix * iy / area < table_thresh:
                continue
            cur, box = None, None
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    c = ch["char"]
                    if c in "\r\n":
                        if cur and cur.strip():
                            frags.append({"text": cur, "bbox": box})
                        cur, box = None, None
                        continue
                    bb = [ch["bbox"][0] * sx, ch["bbox"][1] * sy, ch["bbox"][2] * sx, ch["bbox"][3] * sy]
                    hgt = max(bb[3] - bb[1], 1e-6)
                    if cur is not None:
                        if abs(page["rotation"]) in (90, 270) or ch["rotation"]:
                            gap = max(bb[1] - box[3], box[1] - bb[3])
                        else:
                            gap = bb[0] - box[2]
                        if (c.strip() or gap > 0) and gap > 0.8 * hgt:
                            if cur.strip():
                                frags.append({"text": cur, "bbox": box})
                            cur, box = None, None
                    if cur is None:
                        cur, box = c, bb
                    else:
                        cur += c
                        box = [min(box[0], bb[0]), min(box[1], bb[1]), max(box[2], bb[2]), max(box[3], bb[3])]
            if cur is not None and cur.strip():
                frags.append({"text": cur, "bbox": box})
    for f in frags:
        f["text"] = f["text"].strip()
        x0, y0, x1, y1 = f["bbox"]
        f["bbox"] = [x0 - tx0, y0 - ty0, x1 - tx0, y1 - ty0]
    return sort_blocks(frags)


def table_output(pdf_path, table_inputs, page_range=None, flatten_pdf=False, quote_loosebox=True,
                 workers=None, pages=None, password=None):
    """Text fragments inside caller-given table boxes. table_inputs: one
    {"tables": [[x0, y0, x1, y1], ...], "img_size": [w, h]} per page, boxes in image pixels."""
    if not pages:
        pages = dictionary_output(pdf_path, page_range=page_range, flatten_pdf=flatten_pdf,
                                  quote_loosebox=quote_loosebox, workers=workers, keep_chars=True, password=password)
    else:
        for p in pages:
            for b in p["blocks"]:
                for ln in b["lines"]:
                    for s in ln["spans"]:
                        if "chars" not in s:
                            raise ValueError("table_output requires pages extracted with keep_chars=True")
    if len(pages) != len(table_inputs):
        raise ValueError("Number of pages and table inputs must match")
    out = []
    for p, ti in zip(pages, table_inputs):
        tables, img = ti["tables"], ti["img_size"]
        if not all(len(t) == 4 for t in tables):
            raise ValueError("Tables must be a list of 4 ints representing the bounding box of the table")
        if len(img) != 2 or not all(v > 0 for v in img):
            raise ValueError("img_size must be a list of 2 positive ints representing the image dimensions width, height")
        out.append([_table_fragments(t, p, img) for t in tables])
    return out
