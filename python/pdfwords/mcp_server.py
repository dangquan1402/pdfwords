# SPDX-License-Identifier: Apache-2.0
"""MCP server: PDF reading tools for LLM agents (pip install "pdfwords[mcp]").

    pdfwords mcp [--root DIR ...]       # stdio transport (Claude Desktop, IDEs, agents)

Read-only tools: pdf_info, extract_text, to_markdown, search, get_links, get_tables,
render_page (PNG). Files must lie inside one of the --root directories (default: the current
working directory); paths outside are refused.
"""
from __future__ import annotations

import json
import os

MAX_CHARS = 200_000
_ROOTS = [os.getcwd()]


def set_roots(roots):
    _ROOTS[:] = [os.path.realpath(r) for r in roots] or [os.getcwd()]


def _path(path):
    p = os.path.realpath(os.path.expanduser(path))
    if not any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in _ROOTS):
        raise PermissionError(f"{path!r} is outside the allowed roots {_ROOTS}")
    if not os.path.isfile(p):
        raise FileNotFoundError(path)
    return p


def _cut(s, max_chars):
    if len(s) > max_chars:
        return s[:max_chars] + f"\n\n[... truncated: {len(s) - max_chars} more characters; request fewer pages]"
    return s


def _pages(spec):
    return spec if spec not in ("", None) else None


def pdf_info(path: str) -> str:
    """Page count, metadata, outline (table of contents), tagged-PDF flag and the pages that
    need OCR (no usable text layer)."""
    import pdfwords
    with pdfwords.open(_path(path)) as d:
        info = {"pages": len(d), "metadata": d.metadata, "toc": d.get_toc()[:200],
                "tagged": bool(len(d) and d[0].get_struct_tree()), "needs_ocr": d.needs_ocr(),
                "page_size": d[0].rect[2:] if len(d) else None}
    return json.dumps(info, ensure_ascii=False)


def extract_text(path: str, pages: str = "", mode: str = "text", sort: str = "xycut",
                 max_chars: int = MAX_CHARS) -> str:
    """Text of the given pages ("0,2-4", 0-based; empty = all). mode: text | words | blocks |
    markdown. sort: xycut (column-aware) | struct (tagged PDFs) | none."""
    import pdfwords
    srt = {"none": False, "": False}.get(sort, sort)
    with pdfwords.open(_path(path)) as d:
        if mode == "markdown":
            return _cut(pdfwords.to_markdown(d, _pages(pages), page_separators=True), max_chars)
        out = []
        for i, r in d.iter_pages(mode, pages=_pages(pages), sort=srt):
            out.append(f"--- page {i} ---\n" + (r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)))
    return _cut("\n".join(out), max_chars)


def to_markdown(path: str, pages: str = "", max_chars: int = MAX_CHARS) -> str:
    """Markdown with headings, lists, tables, links (page comments mark page starts)."""
    return extract_text(path, pages, "markdown", max_chars=max_chars)


def search(path: str, query: str, regex: bool = False, pages: str = "", hit_max: int = 200) -> str:
    """Find text (case-insensitive; across line breaks): [{page, bbox, context}]."""
    import pdfwords
    out = []
    with pdfwords.open(_path(path)) as d:
        for i in pdfwords._page_list(_pages(pages), len(d)):
            p = d[i]
            for r in p.search_for(query, regex=regex, hit_max=hit_max - len(out)):
                ctx = " ".join(w[4] for w in p.get_text("words") if w[1] < r[3] + 2 and w[3] > r[1] - 2)
                out.append({"page": i, "bbox": [round(v, 2) for v in r], "context": ctx[:300]})
            if len(out) >= hit_max:
                break
    return json.dumps(out, ensure_ascii=False)


def get_links(path: str, page: int = 0) -> str:
    """Links on a page (URIs and internal targets) with their rectangles."""
    import pdfwords
    with pdfwords.open(_path(path)) as d:
        return json.dumps(d[page].get_links(web=True), ensure_ascii=False, default=str)


def get_tables(path: str, page: int = 0) -> str:
    """Tables on a page as Markdown (tagged tables, or detected ones)."""
    import pdfwords
    from .markdown import _page_tables
    with pdfwords.open(_path(path)) as d:
        p = d[page]
        ts = _page_tables(p, bool(p.get_struct_tree()), True)
        return "\n\n".join(t["md"] for t in ts) or "(no tables found)"


def render_page_png(path: str, page: int = 0, dpi: int = 100) -> bytes:
    import pdfwords
    with pdfwords.open(_path(path)) as d:
        return d[page].render(dpi=min(int(dpi), 300), output="png")


def build_server(roots=None):
    if roots:
        set_roots(roots)
    try:
        from mcp.server.mcpserver import MCPServer as Server, Image
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server, Image
    srv = Server("pdfwords", instructions="Read PDF files: text with reading order, Markdown, search, links, "
                                          "tables and page images. Pages are 0-based.")
    for fn in (pdf_info, extract_text, to_markdown, search, get_links, get_tables):
        srv.tool()(fn)

    def render_page(path: str, page: int = 0, dpi: int = 100):
        """Render a page to a PNG image (dpi <= 300)."""
        return Image(data=render_page_png(path, page, dpi), format="png")
    srv.tool()(render_page)
    return srv


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="pdfwords mcp", description="MCP server (stdio) with PDF reading tools")
    ap.add_argument("--root", action="append", default=[], help="directory the tools may read (repeatable; "
                                                                 "default: current directory)")
    a = ap.parse_args(argv)
    build_server(a.root).run()
