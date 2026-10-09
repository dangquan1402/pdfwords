# SPDX-License-Identifier: Apache-2.0
"""Markdown and LLM-ready chunks with page + bbox provenance.

    md = pdfwords.to_markdown("paper.pdf")                       # one Markdown string
    pages = pdfwords.to_markdown(doc, page_chunks=True)           # per page: {"text", "metadata", "elements", ...}
    chunks = pdfwords.chunks(doc, max_chars=2000)                  # heading-aware chunks for RAG
    page.get_text("markdown")                                     # one page

Every element (heading, paragraph, list item, code, table, image) keeps its page number and
bounding box (PDF points, top-left origin, page as displayed).

How structure is recognised (independent implementation, see docs/MARKDOWN.md):
* headings: tagged-PDF roles (H, H1..H6, Title) first, then the outline (bookmark titles),
  then font statistics (sizes clearly above the body size, ranked; short bold blocks);
* lists: bullet / number / letter markers at line starts, nesting from indentation;
* code: blocks set entirely in monospaced fonts;
* tables: tagged Table elements, or detected tables (page.find_tables) when available;
* running headers/footers and page numbers: text repeated in the top/bottom margins of
  many pages, and number-only lines there, are dropped;
* bold / italic / monospace / links become **x**, _x_, `x`, [x](url).
"""
from __future__ import annotations

import re
from collections import Counter

_BULLETS = "•◦▪▫‣⁃∙·●○■□►▶➢➤✓✔–—*-"
_BULLET_RX = re.compile(r"^\s*([" + re.escape(_BULLETS) + r"])\s+(?=\S)")
_ENUM_RX = re.compile(r"^\s*((?:\(?(?:\d{1,3}|[a-zA-Z]|[ivxlcdm]{1,6})[.)])|(?:\d{1,3}(?:\.\d{1,3})+\.?))\s+(?=\S)")
_PAGE_NO = re.compile(r"^\s*(?:page\s*)?[-–—(\[]?\s*(?:\d{1,4}|[ivxlcdm]{1,7})\s*[-–—)\]]?\s*"
                      r"(?:(?:of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
_HEADING_ROLES = {"Title": 1, "H": 2, "H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}
BOLD, ITALIC, MONO, SERIF, SUPER = 16, 2, 8, 4, 1


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", s.strip().lower()))


def _key(s):
    """Normalised heading key: section numbers ("3.1", "A.", "IV") and punctuation removed."""
    s = re.sub(r"^\s*(?:[0-9]+|[A-Z]|[IVXLC]+)(?:\.[0-9]+)*\.?\s+", "", s)
    return re.sub(r"[^0-9a-z]+", "", s.lower())


class Context:
    """Document-level statistics shared by all pages: body font size, heading size levels,
    outline titles, repeated header/footer lines."""

    def __init__(self, doc, pages, sort="auto", headers_footers=True, margin=0.1):
        self.doc, self.pages = doc, pages
        self.sort = sort
        self.margin = margin
        sizes = Counter()
        self.hf = set()
        keys = Counter()
        sample = pages if len(pages) <= 60 else pages[:: max(1, len(pages) // 60)]
        for pno in sample:
            p = doc[pno]
            d = p.get_text("dict", rotated=True)
            h = d["height"]
            seen = set()
            for b in d["blocks"]:
                for ln in b["lines"]:
                    if abs(ln["dir"][0]) < 0.99:
                        continue
                    for sp in ln["spans"]:
                        n = len(sp["text"].strip())
                        if n:
                            sizes[round(sp["size"] * 2) / 2] += n
                y0, y1 = b["bbox"][1], b["bbox"][3]
                if y1 <= h * margin or y0 >= h * (1 - margin):
                    for ln in b["lines"]:
                        t = "".join(sp["text"] for sp in ln["spans"])
                        k = (_norm(t), y0 < h / 2)
                        if k[0] and k not in seen:
                            seen.add(k)
                            keys[k] += 1
        if headers_footers and len(sample) >= 3:
            need = max(2, int(0.4 * len(sample) + 0.5))
            self.hf = {k for k, c in keys.items() if c >= need}
        self.body = sizes.most_common(1)[0][0] if sizes else 10.0
        total = sum(sizes.values()) or 1
        big = sorted((s for s, c in sizes.items() if s >= self.body * 1.15 and c / total < 0.2), reverse=True)
        # merge sizes within 5% into one level, keep the 4 largest levels
        levels = []
        for s in big:
            if not levels or s < levels[-1][-1] * 0.95:
                levels.append([s])
            else:
                levels[-1].append(s)
        self.levels = levels[:4]
        self.toc = {}
        try:
            for lvl, title, pg in doc.get_toc():
                k = _key(title)
                if len(k) >= 3:
                    self.toc.setdefault(k, min(lvl, 6))
        except Exception:
            pass

    def size_level(self, size):
        s = round(size * 2) / 2
        for i, grp in enumerate(self.levels):
            if s >= min(grp) * 0.97:
                return i + 1
        return 0


def _span_fmt(sp):
    f = sp.get("flags", 0)
    return (bool(f & BOLD) or sp.get("weight", 400) >= 600, bool(f & ITALIC), bool(f & MONO), sp.get("url", "") or "")


def _escape(t):
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", t)


def _wrap(text, bold, italic, mono, url):
    if not text.strip():
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if mono:
        core = "`" + core.replace("`", "'") + "`"
    else:
        core = _escape(core)
        if italic:
            core = f"_{core}_"
        if bold:
            core = f"**{core}**"
    if url:
        core = f"[{core}]({url.replace(' ', '%20').replace(')', '%29')})"
    return lead + core + trail


def _runs(lines, plain=False):
    """Format runs [(text, fmt)] of consecutive lines, joined with spaces (line-end hyphens
    of broken words removed), adjacent runs with equal formatting merged."""
    runs = []
    for ln in lines:
        spans = [sp for sp in ln["spans"] if sp["text"]]
        if not spans:
            continue
        first = spans[0]["text"].lstrip()
        if runs:
            pt = runs[-1][0].rstrip()
            if pt.endswith("-") and len(pt) > 1 and pt[-2].isalpha() and first[:1].islower():
                runs[-1][0] = pt[:-1]
            elif runs[-1][0] and not runs[-1][0].endswith(" "):
                runs[-1][0] += " "
        for k, sp in enumerate(spans):
            t = sp["text"].lstrip() if k == 0 else sp["text"]
            f = (False, False, False, "") if plain else _span_fmt(sp)
            if f[3].startswith("#"):
                f = (f[0], f[1], f[2], "")      # internal targets are not useful in Markdown
            if runs and runs[-1][1] == f:
                runs[-1][0] += t
            elif runs and not t.strip():
                runs[-1][0] += t
            else:
                runs.append([t, f])
    return runs


def _md_of(lines, plain=False):
    if plain:
        return " ".join("".join(t for t, _ in _runs(lines, True)).split())
    return re.sub(r"[ \t]+", " ", "".join(_wrap(t, *f) for t, f in _runs(lines))).strip()


def _line_md(ln, plain=False):
    return _md_of([ln], plain)


def _join_lines(lines):
    s = ""
    for t in lines:
        t = t.strip()
        if not t:
            continue
        if s.endswith("-") and len(s) > 1 and s[-2].isalpha() and t[:1].islower():
            s = s[:-1] + t
        elif s:
            s += " " + t
        else:
            s = t
    return s


def _dominant(b):
    c = Counter()
    for ln in b["lines"]:
        for sp in ln["spans"]:
            n = len(sp["text"].strip())
            if n:
                c[(round(sp["size"] * 2) / 2, _span_fmt(sp)[0], _span_fmt(sp)[2])] += n
    if not c:
        return 0.0, False, False, 0
    (size, bold, mono), _ = c.most_common(1)[0]
    total = sum(c.values())
    all_bold = sum(v for k, v in c.items() if k[1]) >= 0.9 * total
    all_mono = sum(v for k, v in c.items() if k[2]) >= 0.9 * total
    return size, all_bold, all_mono, total


def _text_of(b):
    return "\n".join("".join(sp["text"] for sp in ln["spans"]) for ln in b["lines"])


def _list_items(b):
    """Split a block into list items when its lines start with markers: [(marker, lines, indent)]."""
    items = []
    x0 = b["bbox"][0]
    for ln in b["lines"]:
        t = "".join(sp["text"] for sp in ln["spans"])
        m = _BULLET_RX.match(t) or _ENUM_RX.match(t)
        if m:
            items.append([m.group(1), [ln], ln["bbox"][0] - x0, m.end()])
        elif items:
            items[-1][1].append(ln)
        else:
            return None
    return items


def _strip_marker(ln, n):
    """Copy of line `ln` without its first n characters (the list marker)."""
    spans, left = [], n
    for sp in ln["spans"]:
        t = sp["text"]
        if left > 0:
            cut = min(left, len(t))
            t, left = t[cut:], left - cut
        if t:
            spans.append(dict(sp, text=t))
    return dict(ln, spans=spans)


def page_elements(page, ctx=None, *, tables=True, images=True, rotated=True, min_image=24.0):
    """Structured elements of one page, in reading order:
    {"type": heading|paragraph|list_item|code|table|image, "text" (plain), "md", "bbox", "page",
     "level" (headings / list nesting), "role" (tagged PDFs)}."""
    from .tagged import struct_tree, mcid_order
    doc = page.parent
    if ctx is None:
        ctx = Context(doc, [page.number], headers_footers=False)
    tree = struct_tree(page, raw_kids=True) if ctx.sort in ("auto", "struct") else []
    tagged = bool(tree)
    order = mcid_order(tree) if tagged else {}
    alts = {}
    if tagged:
        def _alts(n, alt):
            alt = n.get("alt") or alt
            for k in n["_kids"]:
                if "mcid" in k:
                    if alt:
                        alts[k["mcid"]] = alt
                else:
                    _alts(k, alt if n["role"] == "Figure" else None)
        for n in tree:
            _alts(n, None)
    sort = "struct" if tagged else ("xycut" if ctx.sort in ("auto", "struct") else ctx.sort)
    d = page.get_text("dict", sort=sort, links=True, extended=True, roles=tagged, rotated=rotated)
    h = d["height"]
    els = []
    tbls = []
    if tables:
        tbls = _page_tables(page, tagged, rotated)
    placed = set()
    for b in d["blocks"]:
        if b.get("type", 0) != 0 or not b["lines"]:
            continue
        txt = _text_of(b).strip()
        if not txt:
            continue
        y0, y1 = b["bbox"][1], b["bbox"][3]
        if all(abs(ln["dir"][0]) < 0.5 for ln in b["lines"]) and (b["bbox"][2] <= d["width"] * ctx.margin
                                                                  or b["bbox"][0] >= d["width"] * (1 - ctx.margin)):
            continue   # vertical text in the side margins (arXiv stamps, line numbers, ...)
        in_margin = y1 <= h * ctx.margin or y0 >= h * (1 - ctx.margin)
        if in_margin:
            if _PAGE_NO.match(txt) or all((_norm("".join(sp["text"] for sp in ln["spans"])), y0 < h / 2) in ctx.hf
                                          for ln in b["lines"]):
                continue
        hit = next((i for i, t in enumerate(tbls) if _inside(b["bbox"], t["bbox"])), None)
        if hit is not None:
            if hit not in placed:
                placed.add(hit)
                els.append(tbls[hit])
            continue
        new = _classify(b, ctx, page.number)
        if order:
            rk = [order[sp["mcid"]][0] for ln in b["lines"] for sp in ln["spans"] if sp.get("mcid", -1) in order]
            for e in new:
                e["_rank"] = min(rk) if rk else None
        els.extend(new)
    for i, t in enumerate(tbls):
        if i not in placed:
            els.append(t)
    if images:
        for im in page.get_images(rotated=rotated):
            x0, y0_, x1, y1_ = im["bbox"]
            if x1 - x0 < min_image or y1_ - y0_ < min_image:
                continue
            e = {"type": "image", "text": "", "md": "", "bbox": tuple(im["bbox"]), "page": page.number,
                 "image": im["number"]}
            m = im.get("mcid", -1)
            if m in alts:
                e["alt"] = e["text"] = alts[m]
            if m in order:   # tagged: the figure's place in the structure tree
                r = order[m][0]
                k = next((j for j, el in enumerate(els) if el.get("_rank") is not None and el["_rank"] > r), len(els))
            else:            # before the first element below the image
                k = next((j for j, el in enumerate(els) if el["bbox"][1] >= y0_ - 1 and el["type"] != "image"),
                         len(els))
            els.insert(k, e)
    for e in els:
        e.pop("_rank", None)
    return els


def _inside(a, b, tol=2.0):
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    return b[0] - tol <= cx <= b[2] + tol and b[1] - tol <= cy <= b[3] + tol


def _classify(b, ctx, pno):
    size, all_bold, all_mono, n = _dominant(b)
    txt = _text_of(b)
    bbox = tuple(b["bbox"])
    role = b.get("role", "")
    base = {"bbox": bbox, "page": pno}
    if role:
        base["role"] = role
    nlines = len(b["lines"])
    flat = " ".join(txt.split())
    # headings
    horizontal = all(abs(ln["dir"][0]) > 0.99 for ln in b["lines"])
    letters = sum(ch.isalpha() for ch in flat)
    has_url = any(sp.get("url") and not sp["url"].startswith("#") for ln in b["lines"] for sp in ln["spans"])
    level = _HEADING_ROLES.get(role, 0) if letters >= 1 else 0
    src = "role" if level else ""
    if not level and horizontal and letters >= 2 and not has_url and not role.startswith(("TD", "TH", "LI", "Lbl", "LBody")):
        tk = _key(flat)
        if tk in ctx.toc:
            level = ctx.toc[tk]
        elif nlines <= 3 and len(flat) <= 120 and not (flat.endswith(".") and len(flat) > 60):
            lv = ctx.size_level(size)
            # sizes just above the body text (small headings) also need bold, or one line
            # without a final period, to tell them from emphasised notes
            if lv and (size >= ctx.body * 1.3 or all_bold or (nlines == 1 and not flat.endswith("."))):
                level = lv
                src = "size"
            elif (all_bold and nlines == 1 and len(flat) <= 60 and not flat.endswith((".", ",", ":", ";"))
                  and abs(size - ctx.body) <= 0.75 and not _BULLET_RX.match(flat) and len(flat.split()) <= 8):
                level = min(len(ctx.levels) + 1, 6)
                src = "bold"
        if level and not src:
            src = "toc"
    if level:
        md_text = _md_of(b["lines"], plain=True)
        return [dict(base, type="heading", level=level, text=md_text, md="#" * level + " " + _escape(md_text),
                     source=src, size=size)]
    # code
    if all_mono and n >= 2:
        lines = []
        cw = max(size * 0.6, 1.0)
        x0 = b["bbox"][0]
        for ln in b["lines"]:
            ind = int(round((ln["bbox"][0] - x0) / cw))
            lines.append(" " * max(0, ind) + "".join(sp["text"] for sp in ln["spans"]).rstrip())
        code = "\n".join(lines)
        return [dict(base, type="code", text=code, md="```\n" + code + "\n```")]
    # lists
    items = _list_items(b) if (role in ("", "L", "LI", "LBody", "Lbl", "P")) else None
    if items:
        out = []
        for marker, lns, indent, cut in items:
            first = _strip_marker(lns[0], cut)
            body = _md_of([first] + lns[1:])
            plain = _md_of([first] + lns[1:], plain=True)
            lvl = 1 + int(max(0.0, indent) // max(size * 1.5, 6.0))
            ordered = marker[-1:] in ".)" and marker not in _BULLETS
            mk = (marker if marker.endswith(".") else marker.rstrip(")").lstrip("(") + ".") if ordered else "-"
            bb = (min(ln["bbox"][0] for ln in lns), min(ln["bbox"][1] for ln in lns),
                  max(ln["bbox"][2] for ln in lns), max(ln["bbox"][3] for ln in lns))
            out.append(dict(base, bbox=bb, type="list_item", level=lvl, text=plain,
                            md="  " * (lvl - 1) + f"{mk} {body}"))
        return out
    body = _md_of(b["lines"])
    plain = _md_of(b["lines"], plain=True)
    if re.match(r"^(#{1,6}\s|>|\d+[.)]\s|[-+*]\s)", body):
        body = "\\" + body
    return [dict(base, type="paragraph", text=plain, md=body)]


# ---------------------------------------------------------------------- tables
def _page_tables(page, tagged, rotated):
    out = []
    if tagged:
        out = tagged_tables(page, rotated)
    if not out and hasattr(page, "find_tables"):
        try:
            tabs = page.find_tables(rotated=rotated)
            if tabs:
                spans = _sized_spans(page, rotated)
            for t in tabs:
                if _has_heading_text(t.bbox, spans):
                    continue        # a boxed title area (form headers), not a data table
                out.append({"type": "table", "bbox": tuple(t.bbox), "page": page.number, "rows": t.extract(),
                            "md": t.to_markdown(), "text": t.to_text()})
        except Exception:   # detection is best effort
            out = []
    return out


def _sized_spans(page, rotated):
    """(bbox, size, chars) of the page's text spans plus the body font size (most chars)."""
    spans, hist = [], {}
    for b in page.get_text("dict", rotated=rotated)["blocks"]:
        for ln in b.get("lines", ()):
            for s in ln["spans"]:
                n = len(s["text"].strip())
                if n:
                    sz = round(s["size"], 1)
                    spans.append((s["bbox"], sz, n))
                    hist[sz] = hist.get(sz, 0) + n
    body = max(hist, key=hist.get) if hist else 0
    return spans, body


def _has_heading_text(bbox, spans):
    """True when the region holds text set clearly larger than the body text (a title)."""
    spans, body = spans
    if not body:
        return False
    x0, y0, x1, y1 = bbox
    for (a0, a1, a2, a3), sz, n in spans:
        if n >= 3 and sz > 1.4 * body and a0 >= x0 - 1 and a2 <= x1 + 1 and a1 >= y0 - 1 and a3 <= y1 + 1:
            return True
    return False


def table_markdown(rows, header=True):
    """GitHub-flavoured Markdown table from a list of rows (lists of cell strings / None)."""
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    cells = [[(" ".join(str(c).split()) if c is not None else "").replace("|", "\\|") for c in r] + [""] * (n - len(r))
             for r in rows]
    head = cells[0] if header else [""] * n
    body = cells[1:] if header else cells
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * n]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def tagged_tables(page, rotated=True):
    """Tables from the structure tree (Table > [THead/TBody/TFoot >] TR > TH/TD)."""
    from .tagged import struct_tree, char_mcids
    tree = struct_tree(page)
    tables = []

    def find(nodes):
        for n in nodes:
            if n["role"] == "Table":
                tables.append(n)
            else:
                find(n["children"])
    find(tree)
    if not tables:
        return []
    d = page.get_text("rawdict", extended=True, rotated=rotated)
    chars = [ch for b in d["blocks"] for ln in b["lines"] for sp in ln["spans"] for ch in sp["chars"]]
    mc = char_mcids(page, [c.get("idx", -1) for c in chars])
    by = {}
    for c, m in zip(chars, mc):
        if m >= 0:
            by.setdefault(m, []).append(c)

    def mcids(n):
        out = list(n["mcids"])
        for k in n["children"]:
            out += mcids(k)
        return out

    def rows_of(n):
        rows = []
        for k in n["children"]:
            if k["role"] == "TR":
                rows.append(k)
            elif k["role"] in ("THead", "TBody", "TFoot"):
                rows += rows_of(k)
        return rows
    out = []
    for t in tables:
        rows, allb = [], []
        header = False
        for r_i, tr in enumerate(rows_of(t)):
            row = []
            for cell in tr["children"]:
                if cell["role"] not in ("TD", "TH"):
                    continue
                if cell["role"] == "TH" and r_i == 0:
                    header = True
                cs = [c for m in mcids(cell) for c in by.get(m, [])]
                txt = " ".join("".join(c["c"] for c in cs).split())
                row.append(txt)
                allb += [c["bbox"] for c in cs if c["c"].strip()]
            if row:
                rows.append(row)
        if not rows or not allb:
            continue
        bbox = (min(b[0] for b in allb), min(b[1] for b in allb), max(b[2] for b in allb), max(b[3] for b in allb))
        md = table_markdown(rows, header=True)
        out.append({"type": "table", "bbox": bbox, "page": page.number, "rows": rows, "md": md,
                    "text": "\n".join("\t".join(r) for r in rows), "header": header, "source": "struct"})
    return out


# ---------------------------------------------------------------------- documents
def _open(src, password=None):
    from . import Document, open as _o
    if isinstance(src, Document):
        return src, False
    return _o(src, password), True


def _image_md(e, page, image_dir, dpi, stem, fmt="png"):
    alt = (e.get("alt") or "").replace("[", "(").replace("]", ")").replace("\n", " ")
    if image_dir is None:
        return f"![{alt or f'image p{page.number + 1}'}]()"
    import os
    os.makedirs(image_dir, exist_ok=True)
    name = f"{stem}-p{page.number + 1}-{e['image']}.{fmt}"
    path = os.path.join(image_dir, name)
    with open(path, "wb") as f:
        f.write(page.render(dpi=dpi, clip=e["bbox"], output=fmt))
    e["path"] = path
    return f"![{alt}]({path})"


def to_markdown(src, pages=None, *, page_chunks=False, sort="auto", headers_footers=True, tables=True,
                images=True, image_dir=None, dpi=150, page_separators=False, password=None):
    """Markdown of a document (path, bytes or Document).
    page_chunks=True: list of {"metadata": {...}, "text", "elements", "toc_items", "tables", "images"}.
    sort: "auto" (structure tree when tagged, else column-aware xycut) | "xycut" | "struct" | False.
    images: include image placeholders; image_dir: also write each image region there (rendered).
    page_separators: put a "<!-- page N -->" comment before each page."""
    import os
    from . import _page_list
    doc, close = _open(src, password)
    try:
        pages = _page_list(pages, len(doc))
        ctx = Context(doc, pages, sort=sort, headers_footers=headers_footers)
        stem = os.path.splitext(os.path.basename(doc.name))[0] if doc.name else "doc"
        toc = []
        try:
            toc = doc.get_toc()
        except Exception:
            pass
        md = doc._pdf.get_metadata_dict(skip_empty=True)
        per_page = [(pno, page_elements(doc[pno], ctx, tables=tables, images=images)) for pno in pages]
        # a "heading" found by font statistics that repeats (figure labels, running titles) is text
        cand = [(pno, e) for pno, els in per_page for e in els
                if e["type"] == "heading" and e.get("source") in ("size", "bold")]
        seen = Counter(e["text"] for _, e in cand)
        same_page = Counter((pno, e["text"]) for pno, e in cand)
        for pno, els in per_page:
            for e in els:
                if (e["type"] == "heading" and e.get("source") in ("size", "bold")
                        and (seen[e["text"]] > 2 or same_page[(pno, e["text"])] > 1)):
                    e.update(type="paragraph", md=_escape(e["text"]))
                    e.pop("level", None)
        _harmonise_levels([e for _, els in per_page for e in els if e["type"] == "heading"])
        chunks = []
        for pno, els in per_page:
            page = doc[pno]
            for e in els:
                if e["type"] == "image":
                    e["md"] = _image_md(e, page, image_dir, dpi, stem)
            text = _join_md(els)
            chunks.append({"metadata": {"file_path": doc.name, "page": pno, "page_count": len(doc),
                                        "title": md.get("Title", ""), "author": md.get("Author", ""),
                                        "width": page.rect[2], "height": page.rect[3]},
                           "text": text, "elements": els,
                           "toc_items": [t for t in toc if t[2] == pno + 1],
                           "tables": [e for e in els if e["type"] == "table"],
                           "images": [e for e in els if e["type"] == "image"]})
        if page_chunks:
            return chunks
        sep = "\n\n"
        out = []
        for c in chunks:
            if page_separators:
                out.append(f"<!-- page {c['metadata']['page'] + 1} -->")
            if c["text"]:
                out.append(c["text"])
        return sep.join(out) + "\n" if out else ""
    finally:
        if close:
            doc.close()


def _harmonise_levels(heads):
    """Make heading levels from different sources (outline, font size, tags) consistent:
    font-size headings get the outline level of outline headings set in the same size, a title
    larger than every outline heading becomes level 1, and used levels are made contiguous."""
    toc = [h for h in heads if h.get("source") == "toc"]
    if toc:
        by_size = {}
        for h in toc:
            by_size.setdefault(round(h["size"] * 2) / 2, Counter())[h["level"]] += 1
        top = max(round(h["size"] * 2) / 2 for h in toc)
        bigger = any(h.get("source") in ("size", "bold") and h["size"] > top * 1.05 for h in heads)
        for h in heads:
            src = h.get("source")
            if src == "toc":
                h["level"] += 1 if bigger else 0
            elif src in ("size", "bold"):
                k = round(h["size"] * 2) / 2
                if h["size"] > top * 1.05:
                    h["level"] = 1
                elif k in by_size:
                    h["level"] = by_size[k].most_common(1)[0][0] + (1 if bigger else 0)
    used = sorted({h["level"] for h in heads})
    remap = {lv: i + 1 for i, lv in enumerate(used)}
    for h in heads:
        h["level"] = min(6, remap[h["level"]])
        h["md"] = "#" * h["level"] + " " + _escape(h["text"])


def _join_md(els):
    out = []
    prev = None
    for e in els:
        if not e["md"]:
            continue
        if prev == "list_item" and e["type"] == "list_item":
            out[-1] += "\n" + e["md"]
        else:
            out.append(e["md"])
        prev = e["type"]
    return "\n\n".join(out)


def chunks(src, pages=None, *, max_chars=2000, min_chars=200, split_level=2, overlap=0, password=None, **kw):
    """Heading-aware chunks for retrieval: a new chunk starts at every heading of level <=
    split_level and whenever max_chars would be exceeded (elements are never split, except
    paragraphs longer than max_chars, which are cut at sentence boundaries).
    Each chunk: {"text" (markdown, prefixed with its parent headings "A > B" as context),
    "headings" (the heading path the chunk belongs to, including its own leading heading), "chunk" (index),
    "pages", "provenance": [{"page", "bbox", "type"}], "chars"}.
    overlap: number of trailing elements repeated at the start of the next chunk."""
    pcs = to_markdown(src, pages, page_chunks=True, password=password, **kw)
    path = []
    out = []
    cur = []

    def flush():
        if not cur:
            return
        body = _join_md(cur)
        parents = list(cur[0].get("_path", []))
        heads = parents + [cur[0]["text"]] if cur[0]["type"] == "heading" else parents
        prefix = " > ".join(parents) + "\n\n" if parents else ""
        out.append({"text": prefix + body, "headings": heads,
                    "pages": sorted({e["page"] for e in cur}),
                    "provenance": [{"page": e["page"], "bbox": e["bbox"], "type": e["type"]} for e in cur],
                    "chars": len(body)})

    for pc in pcs:
        for e in pc["elements"]:
            if not e["md"]:
                continue
            if e["type"] == "heading":
                lvl = e["level"]
                path = [p for p in path if p[0] < lvl] + [(lvl, e["text"])]
            pieces = [e]
            if e["type"] == "paragraph" and len(e["md"]) > max_chars:
                pieces = [dict(e, md=s, text=s) for s in _split_sentences(e["md"], max_chars)]
            for el in pieces:
                el = dict(el, _path=[t for _, t in path[:-1]] if el["type"] == "heading" else [t for _, t in path])
                size = sum(len(x["md"]) + 2 for x in cur)
                new_section = el["type"] == "heading" and el["level"] <= split_level
                if cur and ((new_section and size >= min_chars) or size + len(el["md"]) > max_chars):
                    flush()
                    keep = cur[-overlap:] if overlap else []
                    cur[:] = [dict(k, _path=el["_path"]) for k in keep if k["type"] != "heading"]
                cur.append(el)
    flush()
    for i, c in enumerate(out):
        c["chunk"] = i
    return out


def _split_sentences(text, max_chars):
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for p in parts:
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        if cur and len(cur) + 1 + len(p) > max_chars:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out
