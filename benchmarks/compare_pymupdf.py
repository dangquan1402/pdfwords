# SPDX-License-Identifier: Apache-2.0
"""Compare pdfwords against PyMuPDF (black-box output comparison): word match, bbox IoU,
structure, flags, speed. Requires `pip install pymupdf` (AGPL; benchmark-only dependency).

    python benchmarks/compare_pymupdf.py [--legacy-params] [pdf ...]
    PDFWORDS_BACKEND=python python benchmarks/compare_pymupdf.py

--legacy-params evaluates the pre-0.1 hand-set thresholds (python backend) for comparison.
Writes benchmarks/results/pymupdf_compare[_legacy].json"""
import dataclasses, difflib, json, os, statistics, sys, time

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import fetch_test_pdfs  # noqa: E402
import pdfwords  # noqa: E402
import pdfwords.layout  # noqa: E402

LEGACY = dict(word_gap=0.15, column_gap=0.8, baseline_tol=0.8, block_gap=1.5, dup_dist=0.1,
              gen_space_min=-0.2, indent_pt=0.5, superscript_rise=0.1, xycut_gap_x=6.0, xycut_gap_y=1.0)
NAMES = ["arxiv_attention.pdf", "arxiv_resnet.pdf", "irs_w9.pdf", "rot90.pdf", "simple_text.pdf", "table_camelot.pdf"]


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    I = ix * iy; U = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - I
    return I / U if U > 0 else (1.0 if a == b else 0.0)


def best(fn, n=3):
    ts = []
    for _ in range(n):
        t = time.perf_counter(); fn(); ts.append(time.perf_counter() - t)
    return min(ts)


LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def fold(t):
    """PDFium always decomposes ligature glyphs, PyMuPDF output keeps U+FB0x when the ToUnicode map
    says so -- unrecoverable through PDFium's API, so compare ligature-folded text."""
    return "".join(LIG.get(c, c) for c in t)


def raw_chars(d):
    out = []
    for b in d["blocks"]:
        if b.get("type", 0):
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                for c in s["chars"]:
                    if not c["c"].isspace():
                        for k, ch in enumerate(fold(c["c"])):
                            bb = tuple(c["bbox"]) if k == 0 else (c["bbox"][2], c["bbox"][1], c["bbox"][2], c["bbox"][3])
                            out.append((ch, bb, s["flags"], s["font"], round(s["size"], 1), s["color"]))
    return out


def compare(path):
    md, pd = pymupdf.open(path), pdfwords.open(path, backend=os.environ.get('PDFWORDS_BACKEND'))
    W1, W2, C1, C2 = [], [], [], []
    nb1 = nb2 = nl1 = nl2 = 0
    for i in range(md.page_count):
        w1 = [(fold(w[4]), w[:4], (i, w[5]), (i, w[5], w[6])) for w in md[i].get_text("words")]
        w2 = [(fold(w[4]), w[:4], (i, w[5]), (i, w[5], w[6])) for w in pd[i].get_text("words")]
        W1 += w1; W2 += w2
        nb1 += sum(1 for b in md[i].get_text("blocks") if b[6] == 0 and b[4].strip()); nb2 += sum(1 for b in pd[i].get_text("blocks") if b[4].strip())
        nl1 += len({w[3] for w in w1}); nl2 += len({w[3] for w in w2})
        C1 += raw_chars(md[i].get_text("rawdict")); C2 += raw_chars(pd[i].get_text("rawdict"))
    sm = difflib.SequenceMatcher(None, [w[0] for w in W1], [w[0] for w in W2], autojunk=False)
    pairs = [(i1 + k, j1 + k) for t, i1, i2, j1, j2 in sm.get_opcodes() if t == "equal" for k in range(i2 - i1)]
    ious = [iou(W1[a][1], W2[b][1]) for a, b in pairs]
    # structure: do consecutive matched words agree on "same block" / "same line"?
    sb = sl = tot = 0
    for (a0, b0), (a1, b1) in zip(pairs, pairs[1:]):
        tot += 1
        sb += (W1[a0][2] == W1[a1][2]) == (W2[b0][2] == W2[b1][2])
        sl += (W1[a0][3] == W1[a1][3]) == (W2[b0][3] == W2[b1][3])
    cm = difflib.SequenceMatcher(None, [c[0] for c in C1], [c[0] for c in C2], autojunk=False)
    cp = [(i1 + k, j1 + k) for t, i1, i2, j1, j2 in cm.get_opcodes() if t == "equal" for k in range(i2 - i1)]
    ciou = [iou(C1[a][1], C2[b][1]) for a, b in cp]
    flag_ok = sum(C1[a][2] == C2[b][2] for a, b in cp)
    font_ok = sum(C1[a][3] == C2[b][3] for a, b in cp)
    size_ok = sum(abs(C1[a][4] - C2[b][4]) <= 0.1 for a, b in cp)
    col_ok = sum(C1[a][5] == C2[b][5] for a, b in cp)

    def run_pw(mode):
        with pdfwords.open(path, backend=os.environ.get('PDFWORDS_BACKEND')) as d:
            for p in d: p.get_text(mode)

    def run_mu(mode):
        with pymupdf.open(path) as d:
            for p in d: p.get_text(mode)
    r = {
        "pdf": os.path.basename(path), "pages": md.page_count,
        "words_pymupdf": len(W1), "words_pdfwords": len(W2),
        "word_seq_similarity": round(sm.ratio(), 4),
        "word_recall": round(len(pairs) / max(1, len(W1)), 4),
        "word_bbox_iou_median": round(statistics.median(ious), 4) if ious else None,
        "word_bbox_iou>0.9": round(sum(x > 0.9 for x in ious) / max(1, len(ious)), 4),
        "blocks_pymupdf": nb1, "blocks_pdfwords": nb2, "lines_pymupdf": nl1, "lines_pdfwords": nl2,
        "same_block_agreement": round(sb / max(1, tot), 4), "same_line_agreement": round(sl / max(1, tot), 4),
        "char_recall": round(len(cp) / max(1, len(C1)), 4),
        "char_bbox_iou_median": round(statistics.median(ciou), 4) if ciou else None,
        "span_flags_agree": round(flag_ok / max(1, len(cp)), 4), "font_name_agree": round(font_ok / max(1, len(cp)), 4),
        "size_agree": round(size_ok / max(1, len(cp)), 4), "color_agree": round(col_ok / max(1, len(cp)), 4),
        "t_pdfwords_words": round(best(lambda: run_pw("words")), 4),
        "t_pdfwords_rawdict": round(best(lambda: run_pw("rawdict")), 4),
        "t_pymupdf_words": round(best(lambda: run_mu("words")), 4),
        "t_pymupdf_rawdict": round(best(lambda: run_mu("rawdict")), 4),
    }
    return r


if __name__ == "__main__":
    args = sys.argv[1:]
    legacy = "--legacy-params" in args
    args = [a for a in args if a != "--legacy-params"]
    if legacy:
        os.environ["PDFWORDS_BACKEND"] = "python"
        pdfwords.layout.DEFAULT_PARAMS = dataclasses.replace(pdfwords.layout.DEFAULT_PARAMS, **LEGACY)
    pdfs = args or [p for p in (fetch_test_pdfs.path_of(n) for n in NAMES) if p]
    rows = []
    for p in pdfs:
        r = compare(p); rows.append(r)
        print(json.dumps(r), flush=True)
    W = sum(r["words_pymupdf"] for r in rows)
    print("weighted word recall:", round(sum(r["word_recall"] * r["words_pymupdf"] for r in rows) / W, 5), "over", W, "words")
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = os.path.join(HERE, "results", "pymupdf_compare%s.json" % ("_legacy" if legacy else ""))
    json.dump(rows, open(out, "w"), indent=1)
