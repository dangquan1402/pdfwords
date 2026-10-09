# SPDX-License-Identifier: Apache-2.0
"""Tagged PDF: the structure tree (logical reading order and roles).

    page.get_struct_tree()                     # [{"role": "H1", "mcids": [3], "children": [...], ...}]
    page.get_text("words", sort="struct")      # author's reading order (falls back to xycut)
    page.get_text("dict", roles=True)          # blocks/spans carry "role" (P, H1, LI, TD, ...) and "mcid"

Chars are tied to structure elements by marked-content ids (MCID) of their text objects.
Text that is not in the tree (artifacts such as running headers, untagged content) keeps its
column-aware (xycut) position among the tagged blocks.
"""
from __future__ import annotations

import ctypes as _ct

import pypdfium2.raw as R

# structure types that delimit a block of text (PDF 32000 14.8.4 + PDF 2.0 additions)
BLOCK_ROLES = {"P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "Title", "LI", "Lbl", "LBody", "Caption", "TD", "TH",
               "BlockQuote", "Code", "Note", "Figure", "Formula", "TOCI", "Index", "BibEntry", "Quote", "FENote",
               "Aside", "Sub"}
_INLINE = {"Span", "Link", "Annot", "Reference", "Em", "Strong", "Ruby", "RB", "RT", "RP", "Warichu", "WT", "WP",
           "Form"}


def _s16(fn, *args):
    n = fn(*args, None, 0)
    if n <= 2:
        return ""
    buf = _ct.create_string_buffer(n)
    fn(*args, buf, n)
    return buf.raw[:n - 2].decode("utf-16-le", "replace")


# common non-standard names (e.g. from word processors) -> standard roles; others are kept as-is
# (PDFium does not expose the document's /RoleMap)
_ALIASES = {**{f"Heading{i}": f"H{i}" for i in range(1, 7)}, **{f"Heading {i}": f"H{i}" for i in range(1, 7)},
            "Para": "P", "Paragraph": "P", "Normal": "P", "Body Text": "P", "List": "L", "ListItem": "LI",
            "Footnote": "Note", "Image": "Figure", "Artifact": "Artifact"}


def _elem(e, depth, counter, rolemap):
    typ = _s16(R.FPDF_StructElement_GetType, e)
    d = {"role": rolemap.get(typ) or _ALIASES.get(typ, typ), "type": typ, "id": counter[0]}
    counter[0] += 1
    for key, fn in (("title", R.FPDF_StructElement_GetTitle), ("alt", R.FPDF_StructElement_GetAltText),
                    ("actual_text", R.FPDF_StructElement_GetActualText), ("lang", R.FPDF_StructElement_GetLang)):
        v = _s16(fn, e)
        if v:
            d[key] = v
    kids, mcids = [], []
    n = R.FPDF_StructElement_CountChildren(e)
    for i in range(n):
        c = R.FPDF_StructElement_GetChildAtIndex(e, i)
        if c and depth < 64:
            kids.append(_elem(c, depth + 1, counter, rolemap))
        else:
            m = R.FPDF_StructElement_GetChildMarkedContentID(e, i)
            if m >= 0:
                mcids.append(m)
                kids.append({"mcid": m})
    if not n:
        cnt = R.FPDF_StructElement_GetMarkedContentIdCount(e) if hasattr(R, "FPDF_StructElement_GetMarkedContentIdCount") else -1
        if cnt > 0:
            for i in range(cnt):
                m = R.FPDF_StructElement_GetMarkedContentIdAtIndex(e, i)
                if m >= 0:
                    mcids.append(m)
                    kids.append({"mcid": m})
        else:
            m = R.FPDF_StructElement_GetMarkedContentID(e)
            if m >= 0:
                mcids.append(m)
                kids.append({"mcid": m})
    d["mcids"] = mcids
    d["children"] = [k for k in kids if "mcid" not in k]
    d["_kids"] = kids     # interleaved order of MCIDs and child elements
    return d


def struct_tree(page, raw_kids=False):
    """The page's structure elements as nested dicts (empty list for untagged pages)."""
    pg = page._page
    t = R.FPDF_StructTree_GetForPage(pg.raw)
    if not t:
        return []
    try:
        counter = [0]
        out = []
        for i in range(R.FPDF_StructTree_CountChildren(t)):
            e = R.FPDF_StructTree_GetChildAtIndex(t, i)
            if e:
                out.append(_elem(e, 0, counter, {}))
    finally:
        R.FPDF_StructTree_Close(t)
    if not raw_kids:
        _strip(out)
    return out


def _strip(nodes):
    for n in nodes:
        n.pop("_kids", None)
        _strip(n["children"])


def mcid_order(tree):
    """{mcid: (rank, block element id, block role, path of roles)} in logical (tree) order."""
    out = {}

    def rec(node, block, path):
        role = node["role"]
        path = path + (role,)
        if role in BLOCK_ROLES or (block is None and role not in _INLINE and not node["children"]):
            block = (node["id"], role)
        for k in node["_kids"]:
            if "mcid" in k:
                if k["mcid"] not in out:
                    b = block or (node["id"], role)
                    out[k["mcid"]] = (len(out), b[0], b[1], path)
            else:
                rec(k, block, path)

    for n in tree:
        rec(n, None, ())
    return out


_FAST = []


def _fast_fns():
    """Raw-pointer prototypes (pointers as ints: no per-call ctypes object wrapping)."""
    if not _FAST:
        addr = lambda f: _ct.cast(f, _ct.c_void_p).value
        get_obj = _ct.CFUNCTYPE(_ct.c_void_p, _ct.c_void_p, _ct.c_int)(addr(R.FPDFText_GetTextObject))
        get_mcid = _ct.CFUNCTYPE(_ct.c_int, _ct.c_void_p)(addr(R.FPDFPageObj_GetMarkedContentID))
        _FAST.extend((get_obj, get_mcid))
    return _FAST


def char_mcids(page, idxs):
    """MCID of the text object of each PDFium char index (-1: none / artifact)."""
    pg = page._page
    tp = R.FPDFText_LoadPage(pg.raw)
    get_obj, get_mcid = _fast_fns()
    tpa = _ct.cast(tp, _ct.c_void_p).value
    try:
        cache = {None: -1}
        out = []
        for i in idxs:
            if i is None or i < 0:
                out.append(-1)
                continue
            k = get_obj(tpa, i)
            m = cache.get(k)
            if m is None:
                m = cache[k] = get_mcid(k)
            out.append(m)
        return out
    finally:
        R.FPDFText_ClosePage(tp)


def annotate(page, d):
    """Give rawdict `d` (extended=True: chars carry "idx") per-span "mcid"/"role" and per-block
    "role"; split blocks whose lines belong to different block-level elements. Returns the
    {mcid: info} order map (empty when the page is untagged)."""
    tree = struct_tree(page, raw_kids=True)
    order = mcid_order(tree) if tree else {}
    if not order:
        return order
    chars = [ch for b in d["blocks"] for ln in b["lines"] for sp in ln["spans"] for ch in sp["chars"]]
    mc = char_mcids(page, [ch.get("idx", -1) for ch in chars])
    for ch, m in zip(chars, mc):
        ch["mcid"] = m
    new_blocks = []
    for b in d["blocks"]:
        groups = []
        for ln in b["lines"]:
            votes = {}
            for sp in ln["spans"]:
                sv = {}
                for ch in sp["chars"]:
                    info = order.get(ch["mcid"])
                    if info is not None and ch["c"].strip():
                        votes[info[1]] = votes.get(info[1], 0) + 1
                        sv[ch["mcid"]] = sv.get(ch["mcid"], 0) + 1
                m = max(sv, key=sv.get) if sv else -1
                sp["mcid"] = m
                sp["role"] = order[m][2] if m in order else ""
            key = max(votes, key=votes.get) if votes else None
            if groups and (groups[-1][0] == key or key is None):
                groups[-1][1].append(ln)
            else:
                groups.append([key, [ln]])
        for key, lines in groups:
            nb = dict(b)
            nb["lines"] = lines
            nb["bbox"] = _union([ln["bbox"] for ln in lines])
            ranks = [order[ch["mcid"]][0] for ln in lines for sp in ln["spans"] for ch in sp["chars"]
                     if ch["mcid"] in order]
            nb["_rank"] = min(ranks) if ranks else None
            role = ""
            if key is not None:
                role = next(order[ch["mcid"]][2] for ln in lines for sp in ln["spans"] for ch in sp["chars"]
                            if ch["mcid"] in order and order[ch["mcid"]][1] == key)
            nb["role"] = role
            new_blocks.append(nb)
    d["blocks"] = new_blocks
    return order


def _union(bbs):
    return (min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs))


def struct_sort(d):
    """Reorder annotated blocks: tagged blocks by tree rank, untagged ones keep their slots."""
    blocks = d["blocks"]
    tagged = sorted((b for b in blocks if b.get("_rank") is not None), key=lambda b: b["_rank"])
    it = iter(tagged)
    d["blocks"] = [next(it) if b.get("_rank") is not None else b for b in blocks]
    for i, b in enumerate(d["blocks"]):
        b["number"] = i
        b.pop("_rank", None)
    return d
