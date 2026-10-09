# SPDX-License-Identifier: Apache-2.0
"""OCR fallback with the same output schema as the text layer.

    page.get_text("words", ocr="auto")      # OCR only if page.needs_ocr() (no usable text layer)
    page.get_text("dict", ocr=True)         # always OCR
    doc = pdfwords.open("scan.pdf", ocr="auto")   # default for every get_text() of the document
    page.get_text("text", ocr={"engine": "tesseract", "dpi": 300, "lang": "eng+deu"})
    page.ocr()                              # -> rawdict (PDF points), cached per page and settings

Engines (all optional; "auto" picks the first available in this order):
    "ocrmac"     Apple Vision via ocrmac (macOS; MIT)
    "rapidocr"   RapidOCR (rapidocr or rapidocr_onnxruntime; Apache-2.0)
    "tesseract"  Tesseract via pytesseract (Apache-2.0; needs the tesseract binary)
    callable     f(PIL.Image) -> [(text, confidence 0-1 | None, (x0, y0, x1, y1) in pixels), ...]
                 or dicts with those keys; runs of text are split into words at spaces.

The page is rendered upright (as displayed) at `dpi`, recognised, and mapped back to PDF
coordinates (unrotated page for rotated=False, like the text layer). Words become chars with
evenly split boxes; spans carry font "OCR" and "conf" (mean confidence 0-1); lines and blocks
come from the engine (Tesseract) or from line geometry (others).
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

ENGINES = ("ocrmac", "rapidocr", "tesseract")
DEFAULTS = {"engine": "auto", "dpi": 300, "lang": None, "min_conf": 0.0}


class OcrUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------- engines -> runs
# A "run": {"text", "conf" (0-1 or None), "bbox" (px), "block", "line"} (block/line ids optional)

def _tesseract(img, lang=None, config="", **_):
    try:
        import pytesseract
    except ImportError as e:
        raise OcrUnavailable("pip install pytesseract (and install the tesseract binary)") from e
    d = pytesseract.image_to_data(img, lang=lang or "eng", config=config, output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        if d["level"][i] != 5 or not t.strip():
            continue
        c = float(d["conf"][i])
        out.append({"text": t, "conf": c / 100 if c >= 0 else None,
                    "bbox": (d["left"][i], d["top"][i], d["left"][i] + d["width"][i], d["top"][i] + d["height"][i]),
                    "block": (d["block_num"][i], d["par_num"][i]),
                    "line": (d["block_num"][i], d["par_num"][i], d["line_num"][i])})
    return out


def _rapidocr(img, lang=None, **kw):
    import numpy as np
    arr = np.asarray(img.convert("RGB"))
    try:
        from rapidocr import RapidOCR        # rapidocr >= 2
        eng = _cached("rapidocr", lambda: RapidOCR(**kw))
        res = eng(arr)
        boxes, txts, scores = res.boxes, res.txts, res.scores
        items = list(zip(boxes if boxes is not None else [], txts or [], scores or []))
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise OcrUnavailable("pip install rapidocr onnxruntime (or rapidocr_onnxruntime)") from e
        eng = _cached("rapidocr_onnxruntime", lambda: RapidOCR(**kw))
        res, _ = eng(arr)
        items = [(r[0], r[1], r[2]) for r in (res or [])]
    out = []
    for box, text, score in items:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"text": text, "conf": float(score), "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def _ocrmac(img, lang=None, recognition_level="accurate", **kw):
    try:
        from ocrmac import ocrmac
    except ImportError as e:
        raise OcrUnavailable("pip install ocrmac (macOS only)") from e
    langs = [lang] if isinstance(lang, str) else lang
    res = ocrmac.OCR(img, recognition_level=recognition_level, language_preference=langs, **kw).recognize()
    w, h = img.size
    out = []
    for text, conf, (x, y, bw, bh) in res:     # normalised, origin bottom-left
        out.append({"text": text, "conf": float(conf),
                    "bbox": (x * w, (1 - y - bh) * h, (x + bw) * w, (1 - y) * h)})
    return out


_ENGINE_FNS = {"tesseract": _tesseract, "rapidocr": _rapidocr, "ocrmac": _ocrmac}
_CACHE = {}


def _cached(key, make):
    if key not in _CACHE:
        _CACHE[key] = make()
    return _CACHE[key]


def available_engines():
    """Engines that can run here, in "auto" preference order."""
    import importlib.util
    import shutil
    import sys
    out = []
    if sys.platform == "darwin" and importlib.util.find_spec("ocrmac"):
        out.append("ocrmac")
    if importlib.util.find_spec("rapidocr") or importlib.util.find_spec("rapidocr_onnxruntime"):
        out.append("rapidocr")
    if importlib.util.find_spec("pytesseract") and shutil.which("tesseract"):
        out.append("tesseract")
    return out


def _engine(engine):
    if callable(engine):
        return engine
    if engine in (None, "auto"):
        av = available_engines()
        if not av:
            raise OcrUnavailable("no OCR engine available: pip install pytesseract (+ tesseract), "
                                 "rapidocr onnxruntime, or ocrmac (macOS); or pass a callable")
        engine = av[0]
    if engine not in _ENGINE_FNS:
        raise ValueError(f"unknown OCR engine {engine!r}; choose from {ENGINES} or pass a callable")
    return _ENGINE_FNS[engine]


def _norm_runs(raw):
    out = []
    for r in raw or []:
        if isinstance(r, dict):
            out.append({"text": str(r["text"]), "conf": r.get("conf"), "bbox": tuple(map(float, r["bbox"])),
                        "block": r.get("block"), "line": r.get("line")})
        else:
            t, c, bb = r
            out.append({"text": str(t), "conf": c, "bbox": tuple(map(float, bb)), "block": None, "line": None})
    return [r for r in out if r["text"].strip() and r["bbox"][2] > r["bbox"][0] and r["bbox"][3] > r["bbox"][1]]


def _split_words(run):
    """Split a run of text at spaces; boxes proportional to character counts."""
    t = run["text"]
    x0, y0, x1, y1 = run["bbox"]
    n = len(t)
    if " " not in t.strip() or n == 0:
        return [dict(run, text=t.strip())]
    out = []
    unit = (x1 - x0) / n
    i = 0
    for part in t.split(" "):
        if part:
            out.append(dict(run, text=part, bbox=(x0 + i * unit, y0, x0 + (i + len(part)) * unit, y1)))
        i += len(part) + 1
    return out


def _group_lines(words):
    """Words without engine line ids -> lines (vertical overlap + horizontal proximity)."""
    lines = []
    for w in sorted(words, key=lambda w: ((w["bbox"][1] + w["bbox"][3]) / 2, w["bbox"][0])):
        h = w["bbox"][3] - w["bbox"][1]
        for ln in reversed(lines[-4:]):
            lb = ln["bbox"]
            ov = min(lb[3], w["bbox"][3]) - max(lb[1], w["bbox"][1])
            if ov >= 0.5 * min(h, lb[3] - lb[1]) and w["bbox"][0] - lb[2] <= 3 * h and w["bbox"][2] >= lb[0] - 3 * h:
                ln["words"].append(w)
                ln["bbox"] = (min(lb[0], w["bbox"][0]), min(lb[1], w["bbox"][1]),
                              max(lb[2], w["bbox"][2]), max(lb[3], w["bbox"][3]))
                break
        else:
            lines.append({"words": [w], "bbox": w["bbox"]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["bbox"][0])
    return lines


def _group_blocks(lines):
    """Lines -> blocks: consecutive lines (top-down) close vertically and overlapping horizontally."""
    blocks = []
    for ln in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        lb = ln["bbox"]
        h = lb[3] - lb[1]
        for b in reversed(blocks[-6:]):
            bb = b["bbox"]
            last = b["lines"][-1]["bbox"]
            if (0 <= lb[1] - last[3] <= 0.9 * h and min(bb[2], lb[2]) - max(bb[0], lb[0]) > 0
                    and abs((last[3] - last[1]) - h) <= 0.35 * h):
                b["lines"].append(ln)
                b["bbox"] = (min(bb[0], lb[0]), min(bb[1], lb[1]), max(bb[2], lb[2]), max(bb[3], lb[3]))
                break
        else:
            blocks.append({"lines": [ln], "bbox": lb})
    return blocks


# ---------------------------------------------------------------------- page OCR -> rawdict
def _to_pdf(page, dpi, rotated):
    """Pixel (of an upright render at dpi) -> output coordinates."""
    s = 72.0 / dpi
    if rotated or page.rotation == 0:
        return lambda x, y: (x * s, y * s)
    r, w, h = page.rotation, page._w, page._h

    def f(x, y):
        X, Y = x * s, y * s       # displayed page
        if r == 90:
            return Y, h - X
        if r == 180:
            return w - X, h - Y
        return w - Y, X           # 270
    return f


def _box(f, bb):
    a, b = f(bb[0], bb[1]), f(bb[2], bb[3])
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def ocr_rawdict(page, engine="auto", dpi=300, lang=None, *, rotated=False, min_conf=0.0, sort=False, **engine_kw):
    """OCR the page -> rawdict (same schema as page.get_text("rawdict"))."""
    fn = _engine(engine)
    img = page.render(dpi=dpi, rotated=True, output="pil", annots=True, forms=True)
    if callable(engine) and engine not in _ENGINE_FNS.values():
        raw = fn(img)
    else:
        raw = fn(img, lang=lang, **engine_kw)
    runs = [r for r in _norm_runs(raw) if r["conf"] is None or r["conf"] >= min_conf]
    words = [w for r in runs for w in _split_words(r)]
    if words and all(w.get("line") is not None for w in words):
        by = {}
        for w in words:
            by.setdefault(w["line"], []).append(w)
        lines = []
        for k, ws in by.items():
            ws.sort(key=lambda w: w["bbox"][0])
            bb = (min(w["bbox"][0] for w in ws), min(w["bbox"][1] for w in ws),
                  max(w["bbox"][2] for w in ws), max(w["bbox"][3] for w in ws))
            lines.append({"words": ws, "bbox": bb, "block": ws[0].get("block")})
        bmap = {}
        for ln in lines:
            bmap.setdefault(ln["block"], []).append(ln)
        blocks = [{"lines": ls, "bbox": (min(l["bbox"][0] for l in ls), min(l["bbox"][1] for l in ls),
                                         max(l["bbox"][2] for l in ls), max(l["bbox"][3] for l in ls))}
                  for ls in bmap.values()]
    else:
        blocks = _group_blocks(_group_lines(words))
    f = _to_pdf(page, dpi, rotated)
    w_out, h_out = (page.rect[2], page.rect[3]) if rotated else (page._w, page._h)
    out_blocks = []
    for b in blocks:
        lines_out = []
        for ln in b["lines"]:
            chars = []
            confs = []
            for k, w in enumerate(ln["words"]):
                bb = _box(f, w["bbox"])
                if k:
                    prev = chars[-1]["bbox"]
                    gap = (prev[2], bb[1], max(prev[2], bb[0]), bb[3])
                    chars.append({"origin": (gap[0], bb[3]), "bbox": gap, "c": " ", "synthetic": True})
                n = len(w["text"])
                # boxes in the reading direction of the upright image, mapped back
                for i, c in enumerate(w["text"]):
                    px = w["bbox"][0] + (w["bbox"][2] - w["bbox"][0]) * i / n
                    px1 = w["bbox"][0] + (w["bbox"][2] - w["bbox"][0]) * (i + 1) / n
                    cb = _box(f, (px, w["bbox"][1], px1, w["bbox"][3]))
                    chars.append({"origin": (cb[0], cb[3]), "bbox": cb, "c": c, "synthetic": False})
                if w.get("conf") is not None:
                    confs.append(w["conf"])
            lbb = _box(f, ln["bbox"])
            size = round((ln["bbox"][3] - ln["bbox"][1]) * 72.0 / dpi, 2)
            d = _dir(page, rotated)
            span = {"size": size, "flags": 0, "bidi": 0, "char_flags": 0, "font": "OCR", "color": 0, "alpha": 255,
                    "ascender": 1.0, "descender": 0.0, "conf": round(sum(confs) / len(confs), 4) if confs else None,
                    "origin": (lbb[0], lbb[3]), "bbox": lbb, "chars": chars}
            lines_out.append({"spans": [span], "wmode": 0, "dir": d, "bbox": lbb})
        bb = _box(f, b["bbox"])
        out_blocks.append({"type": 0, "number": 0, "flags": 0, "bbox": bb, "lines": lines_out})
    if sort:
        out_blocks = _sort_blocks(out_blocks, sort)
    for i, b in enumerate(out_blocks):
        b["number"] = i
    return {"width": w_out, "height": h_out, "blocks": out_blocks}


def _dir(page, rotated):
    if rotated or page.rotation == 0:
        return (1.0, 0.0)
    return {90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}[page.rotation]


def _sort_blocks(blocks, sort):
    from .order import sort_xycut
    if sort == "xycut" or sort == "struct":
        wrapped = [SimpleNamespace(bbox=b["bbox"], lines=[SimpleNamespace(dir=b["lines"][0]["dir"])] if b["lines"] else [],
                                   d=b) for b in blocks]
        return [w.d for w in sort_xycut(wrapped)]
    return sorted(blocks, key=lambda b: (b["bbox"][3], b["bbox"][0]))


def resolve(ocr):
    """get_text(ocr=...) -> (mode, settings) with mode in (None, "auto", "always")."""
    if ocr in (None, False):
        return None, None
    if ocr is True or ocr == "always":
        return "always", dict(DEFAULTS)
    if ocr == "auto":
        return "auto", dict(DEFAULTS)
    if isinstance(ocr, str) or callable(ocr):     # an engine name / callable: always
        return "always", dict(DEFAULTS, engine=ocr)
    if isinstance(ocr, dict):
        s = dict(DEFAULTS, **ocr)
        mode = s.pop("mode", "always")
        return mode, s
    raise ValueError(f"bad ocr={ocr!r}")


def page_ocr(page, settings, rotated, sort):
    """Cached OCR rawdict for a page (deep-copied, so callers may mutate it)."""
    eng = settings.get("engine", "auto")
    key = ("ocr", eng if isinstance(eng, str) else id(eng), settings.get("dpi"), settings.get("lang"),
           settings.get("min_conf"), rotated, sort)
    cache = page.parent._ocr_cache
    k = (page.number,) + key
    if k not in cache:
        extra = {kk: v for kk, v in settings.items() if kk not in ("engine", "dpi", "lang", "min_conf")}
        cache[k] = ocr_rawdict(page, eng, settings.get("dpi", 300), settings.get("lang"), rotated=rotated,
                               min_conf=settings.get("min_conf", 0.0), sort=sort, **extra)
    return copy.deepcopy(cache[k])
