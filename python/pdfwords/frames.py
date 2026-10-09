"""Columnar output: one row per char / span / line / word / block for a whole document, as a
pyarrow Table, a Parquet file or a pandas DataFrame. pyarrow and pandas are optional:
``pip install pdfwords[arrow]`` (pyarrow) or ``pdfwords[tables]`` (pandas)."""

KINDS = ("chars", "spans", "lines", "words", "blocks")


def _doc(source, password=None):
    import pdfwords
    if isinstance(source, pdfwords.Document):
        return source, False
    return pdfwords.open(source, password), True


def records(source, kind="words", pages=None, **text_kw):
    """Columns (dict of equal-length lists) for `kind` in chars | spans | lines | words | blocks.
    `pages`: iterable of 0-based page numbers (default all). Extra keyword arguments go to
    Page.get_text (sort, clip, rotated, ocr, ligatures, ...).
    Every row has page, block, x0, y0, x1, y1 and text; lines add line; spans/chars add span
    and font, size, flags, color (chars add char, origin_x, origin_y); words add line, word."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    doc, close = _doc(source)
    cols = {}

    def add(**row):
        for k, v in row.items():
            cols.setdefault(k, []).append(v)

    try:
        for pno in (range(len(doc)) if pages is None else pages):
            page = doc[pno]
            if kind == "words":
                for x0, y0, x1, y1, t, b, ln, w in page.get_text("words", **text_kw):
                    add(page=pno, block=b, line=ln, word=w, x0=x0, y0=y0, x1=x1, y1=y1, text=t)
                continue
            if kind == "blocks":
                for x0, y0, x1, y1, t, b, typ in page.get_text("blocks", **text_kw):
                    if typ == 0:
                        add(page=pno, block=b, x0=x0, y0=y0, x1=x1, y1=y1, text=t)
                continue
            d = page.get_text("rawdict" if kind == "chars" else "dict", **text_kw)
            for bno, blk in enumerate(d["blocks"]):
                if blk.get("type", 0) != 0:
                    continue
                for lno, line in enumerate(blk["lines"]):
                    if kind == "lines":
                        t = "".join(s["text"] for s in line["spans"])
                        add(page=pno, block=bno, line=lno, x0=line["bbox"][0], y0=line["bbox"][1],
                            x1=line["bbox"][2], y1=line["bbox"][3], text=t,
                            dir_x=line["dir"][0], dir_y=line["dir"][1], wmode=line["wmode"])
                        continue
                    for sno, s in enumerate(line["spans"]):
                        common = dict(font=s["font"], size=s["size"], flags=s["flags"], color=s["color"])
                        if kind == "spans":
                            add(page=pno, block=bno, line=lno, span=sno, x0=s["bbox"][0], y0=s["bbox"][1],
                                x1=s["bbox"][2], y1=s["bbox"][3], text=s["text"], **common)
                            continue
                        for cno, ch in enumerate(s["chars"]):
                            add(page=pno, block=bno, line=lno, span=sno, char=cno, x0=ch["bbox"][0],
                                y0=ch["bbox"][1], x1=ch["bbox"][2], y1=ch["bbox"][3], text=ch["c"],
                                origin_x=ch["origin"][0], origin_y=ch["origin"][1], **common)
    finally:
        if close:
            doc.close()
    if not cols:  # empty document / pages: keep the schema
        for k in _SCHEMA[kind]:
            cols[k] = []
    return cols


_BASE = ("page", "block", "x0", "y0", "x1", "y1", "text")
_STYLE = ("font", "size", "flags", "color")
_SCHEMA = {
    "words": ("page", "block", "line", "word", "x0", "y0", "x1", "y1", "text"),
    "blocks": _BASE,
    "lines": ("page", "block", "line", "x0", "y0", "x1", "y1", "text", "dir_x", "dir_y", "wmode"),
    "spans": ("page", "block", "line", "span", "x0", "y0", "x1", "y1", "text") + _STYLE,
    "chars": ("page", "block", "line", "span", "char", "x0", "y0", "x1", "y1", "text", "origin_x", "origin_y")
    + _STYLE,
}


def _arrow_schema(pa, kind):
    types = {"text": pa.string(), "font": pa.string(), "size": pa.float64(), "flags": pa.int32(),
             "color": pa.int64(), "wmode": pa.int8(), "page": pa.int32()}
    return pa.schema([(k, types.get(k, pa.float64() if k[0] in "xyod" else pa.int32())) for k in _SCHEMA[kind]])


def to_arrow(source, kind="words", pages=None, **text_kw):
    """pyarrow.Table with the columns of `records` (page numbers are 0-based)."""
    try:
        import pyarrow as pa
    except ImportError as e:  # pragma: no cover
        raise ImportError("to_arrow needs pyarrow: pip install pdfwords[arrow]") from e
    cols = records(source, kind, pages, **text_kw)
    return pa.table(cols, schema=_arrow_schema(pa, kind))


def to_parquet(source, path, kind="words", pages=None, compression="zstd", **text_kw):
    """Write `kind` rows to a Parquet file (needs pyarrow); returns the path."""
    import pyarrow.parquet as pq
    pq.write_table(to_arrow(source, kind, pages, **text_kw), path, compression=compression)
    return path


def to_pandas(source, kind="words", pages=None, **text_kw):
    """pandas.DataFrame with the columns of `records` (pyarrow not required)."""
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover
        raise ImportError("to_pandas needs pandas: pip install pdfwords[tables]") from e
    return pd.DataFrame(records(source, kind, pages, **text_kw), columns=list(_SCHEMA[kind]))
