# SPDX-License-Identifier: Apache-2.0
"""Shared (text, metadata) iterator behind the LangChain and LlamaIndex adapters."""
from __future__ import annotations

import os

MODES = ("page", "single", "chunks", "markdown_page", "markdown")


def iter_texts(source, mode="page", *, pages=None, password=None, max_chars=2000, sort="xycut", **kw):
    """Yield (text, metadata) pairs.
    mode: page (plain text per page) | single (whole document) | markdown_page (Markdown per page)
          | markdown (whole document as Markdown) | chunks (heading-aware chunks with provenance)."""
    import pdfwords
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    src_name = os.fspath(source) if isinstance(source, (str, os.PathLike)) else ""
    with pdfwords.open(source, password) as doc:
        n = len(doc)
        base = {"source": src_name, "total_pages": n}
        md = doc._pdf.get_metadata_dict(skip_empty=True)
        for k in ("Title", "Author", "Subject"):
            if md.get(k):
                base[k.lower()] = md[k]
        if mode == "page":
            for i, t in doc.iter_pages("text", pages=pages, sort=sort):
                yield t, dict(base, page=i)
        elif mode == "single":
            yield "\n\f".join(t for _, t in doc.iter_pages("text", pages=pages, sort=sort)), dict(base)
        elif mode == "markdown_page":
            for c in pdfwords.to_markdown(doc, pages, page_chunks=True, **kw):
                yield c["text"], dict(base, page=c["metadata"]["page"])
        elif mode == "markdown":
            yield pdfwords.to_markdown(doc, pages, **kw), dict(base)
        else:
            for k, c in enumerate(pdfwords.chunks(doc, pages, max_chars=max_chars, **kw)):
                yield c["text"], dict(base, chunk=k, page=c["pages"][0] if c["pages"] else 0, pages=c["pages"],
                                      headings=c["headings"],
                                      provenance=[{"page": p["page"], "bbox": list(p["bbox"]), "type": p["type"]}
                                                  for p in c["provenance"]])
