# SPDX-License-Identifier: Apache-2.0
"""pdfwords.optimize / Document.optimize / `pdfwords shrink` on a small generated corpus.

Everything here is generated on the fly (synthetic photos and scans) or comes from the
public-domain fixtures; no third-party images are committed."""
import io
import json
import os
import shutil
import subprocess
from collections import Counter

import pytest

pikepdf = pytest.importorskip("pikepdf")
np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

import pdfwords  # noqa: E402
from pdfwords.__main__ import main as cli  # noqa: E402
from pdfwords.shrink import _codecs, _imaging  # noqa: E402

from conftest import FIXTURES  # noqa: E402

W9 = os.path.join(FIXTURES, "irs_w9.pdf")
SIMPLE = os.path.join(FIXTURES, "simple_text.pdf")


# ------------------------------------------------------------------ helpers
def _read(path):
    with open(path, "rb") as f:
        return f.read()


def words(src):
    with pdfwords.open(src) as d:
        return Counter(w[4] for p in d for w in p.get_text("words"))


def recall(a, b):
    wa, wb = words(a), words(b)
    total = sum(wa.values())
    return sum((wa & wb).values()) / total if total else 1.0


def page_ssim(a, b, dpi=150):
    with pdfwords.open(a) as x, pdfwords.open(b) as y:
        out = []
        for i in range(len(x)):
            ia = x[i].render(dpi=dpi, grayscale=True, output="pil")
            ib = y[i].render(size=ia.size, grayscale=True, output="pil")
            out.append(_imaging.ssim(ia, ib))
    return min(out)


def assert_valid(data):
    """qpdf's structural check (pikepdf = libqpdf; plus the qpdf CLI when installed) and PDFium."""
    with pikepdf.open(io.BytesIO(data)) as p:
        check = getattr(p, "check_pdf_syntax", None) or p.check     # renamed in pikepdf 10
        problems = check()
        assert problems == [], problems
    if shutil.which("qpdf"):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "check.pdf")
            with open(p, "wb") as f:
                f.write(data)
            r = subprocess.run(["qpdf", "--check", p], capture_output=True)
        assert r.returncode in (0, 3), r.stdout + r.stderr   # 3 = warnings only
    with pdfwords.open(data) as d:
        for page in d:
            assert page.render(dpi=20).size > 0


def photo_jpeg(w=2000, h=1500, quality=98, seed=1):
    """Smooth colour gradients with light grain (photo-like, compresses like a real photo)."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([x * 255 / w, y * 255 / h, 128 + 100 * np.sin(x / 90.0) * np.cos(y / 70.0)], 2)
    img = np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    b = io.BytesIO()
    Image.fromarray(img).save(b, "JPEG", quality=quality)
    return b.getvalue(), w, h


def make_photo_pdf(path):
    """One Letter page: a 2000x1500 q98 JPEG drawn at 6x4.5 in (333 dpi), real text, a URI link,
    a bookmark."""
    jpg, w, h = photo_jpeg()
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    img = pikepdf.Stream(pdf, jpg, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=w, Height=h,
                         ColorSpace=pikepdf.Name.DeviceRGB, BitsPerComponent=8, Filter=pikepdf.Name.DCTDecode)
    content = (b"q 432 0 0 324 90 380 cm /Im0 Do Q "
               b"BT /F1 14 Tf 90 340 Td (Holiday photo caption alpha beta gamma) Tj ET "
               b"BT /F1 11 Tf 90 300 Td (Visit example.org for the full album) Tj ET")
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
                              Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font),
                                                           XObject=pikepdf.Dictionary(Im0=img)),
                              Contents=pdf.make_stream(content))
    pdf.pages.append(pikepdf.Page(page))
    link = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=[90, 295, 330, 312],
                              Border=[0, 0, 0], A=pikepdf.Dictionary(S=pikepdf.Name.URI,
                                                                     URI=pikepdf.String("https://example.org/")))
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(link)]))
    with pdf.open_outline() as ol:
        ol.root.append(pikepdf.OutlineItem("Photo", 0))
    pdf.save(path)
    return path


def make_scan_pdf(path, pages=1, dpi=300, color=False):
    """Image-only 'scan': W-9 pages rendered at 300 dpi with noise, blur-free, saved as q92 JPEG."""
    pdf = pikepdf.new()
    rng = np.random.default_rng(7)
    with pdfwords.open(W9) as d:
        for i in range(pages):
            im = d[i].render(dpi=dpi, output="pil")
            a = np.asarray(im.convert("RGB" if color else "L"), dtype=np.float32)
            a = a * 0.92 + 14 + rng.normal(0, 5, a.shape)        # paper tint + sensor noise
            if color:
                a[..., 2] -= 10
            im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=92)
            w, h = im.size
            pw, ph = w * 72 / dpi, h * 72 / dpi
            img = pikepdf.Stream(pdf, b.getvalue(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
                                 Width=w, Height=h, BitsPerComponent=8, Filter=pikepdf.Name.DCTDecode,
                                 ColorSpace=pikepdf.Name.DeviceRGB if color else pikepdf.Name.DeviceGray)
            page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, pw, ph],
                                      Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img)),
                                      Contents=pdf.make_stream(f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /Im0 Do Q".encode()))
            pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)
    return path


def make_bilevel_pdf(path):
    """A 1-bit text image stored with Flate (as many scanners/fax tools do)."""
    with pdfwords.open(W9) as d:
        im = d[0].render(dpi=200, grayscale=True, output="pil")
    im1 = im.point(lambda v: 255 if v > 160 else 0).convert("1")
    w, h = im1.size
    pdf = pikepdf.new()
    img = pikepdf.Stream(pdf, im1.tobytes(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=w,
                         Height=h, BitsPerComponent=1, ColorSpace=pikepdf.Name.DeviceGray)
    pw, ph = w * 72 / 200, h * 72 / 200
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, pw, ph],
                              Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img)),
                              Contents=pdf.make_stream(f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /Im0 Do Q".encode()))
    pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)   # pikepdf flate-compresses the raw 1-bit samples
    return path


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("shrink")
    return {"photo": make_photo_pdf(str(d / "photo.pdf")), "scan": make_scan_pdf(str(d / "scan.pdf")),
            "scan_color": make_scan_pdf(str(d / "scan_color.pdf"), color=True),
            "bilevel": make_bilevel_pdf(str(d / "bilevel.pdf")), "dir": d}


# ------------------------------------------------------------------ text PDFs: lossless, 100 % recall
@pytest.mark.parametrize("preset", ["lossless", "balanced", "max"])
def test_text_pdf_keeps_every_word_and_form(preset, tmp_path):
    out = str(tmp_path / "w9.pdf")
    rep = pdfwords.optimize(W9, out, preset)
    assert rep.bytes_after <= rep.bytes_before
    assert rep.bytes_after < rep.bytes_before * 0.95          # object streams + recompression
    assert recall(W9, out) == 1.0
    assert page_ssim(W9, out) > 0.99
    with pdfwords.open(W9) as a, pdfwords.open(out) as b:
        assert [len(p.widgets()) for p in a] == [len(p.widgets()) for p in b]          # form fields
        assert [w["field_name"] for w in a[0].widgets()] == [w["field_name"] for w in b[0].widgets()]
        assert [e["role"] for e in a[0].get_struct_tree()] == [e["role"] for e in b[0].get_struct_tree()]
    assert_valid(rep.data)


def test_never_grows(tmp_path):
    first = pdfwords.optimize(SIMPLE, None, "balanced")
    again = pdfwords.optimize(first.data, None, "balanced")
    assert again.bytes_after <= again.bytes_before
    if again.kept_original:
        assert again.data == first.data and again.reason
    # a file that cannot get smaller comes back byte-identical
    src = _read(SIMPLE)
    rep = pdfwords.optimize(src, None, "lossless", dedupe=False, remove_thumbnails=False)
    assert rep.bytes_after <= len(src)


def test_signed_pdf_is_left_alone(tmp_path):
    pdf = pikepdf.open(SIMPLE)
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), SigFlags=3)
    p = str(tmp_path / "signed.pdf")
    pdf.save(p)
    rep = pdfwords.optimize(p, str(tmp_path / "out.pdf"))
    assert rep.kept_original and "signed" in rep.reason
    assert _read(str(tmp_path / "out.pdf")) == _read(p)


# ------------------------------------------------------------------ photos: big savings, structure intact
def test_photo_page_shrinks_and_keeps_text_links_outline(corpus, tmp_path):
    src = corpus["photo"]
    out = str(tmp_path / "photo_balanced.pdf")
    seen = []
    rep = pdfwords.optimize(src, out, "balanced", progress=lambda *a: seen.append(a))
    assert rep.reduction > 0.7, rep.to_dict()
    jpeg = [d for d in rep.images if d["action"] == "jpeg"]
    assert jpeg and jpeg[0]["new_width"] < 2000
    assert abs(jpeg[0]["effective_dpi"] - 333.3) < 2               # from pdfwords get_images
    assert jpeg[0]["ssim"] >= 0.9
    assert page_ssim(src, out) > 0.93
    assert recall(src, out) == 1.0
    with pdfwords.open(out) as d:
        assert any("example.org" in str(x) for x in d[0].get_links())
        assert d.get_toc() and d.get_toc()[0][1] == "Photo"
    assert rep.page_checks and rep.page_checks[0]["ssim"] >= 0.85
    assert {s[0] for s in seen} >= {"images", "write"}
    assert_valid(rep.data)


def test_presets_order_and_options(corpus):
    src = corpus["photo"]
    sizes = {p: pdfwords.optimize(src, None, p).bytes_after for p in ("lossless", "balanced", "max")}
    assert sizes["max"] < sizes["balanced"] < sizes["lossless"] <= os.path.getsize(src)
    gray = pdfwords.optimize(src, None, "balanced", grayscale=True)
    d = [x for x in gray.images if x["action"] == "jpeg"][0]
    assert d["grayscale"] is True and gray.bytes_after < sizes["balanced"]
    low = pdfwords.optimize(src, None, "balanced", target_dpi=72, jpeg_quality=40)
    assert low.bytes_after < sizes["balanced"]
    strict = pdfwords.optimize(src, None, "balanced", min_ssim=0.9999)
    assert all(x["action"] != "jpeg" for x in strict.images)       # nothing passes the SSIM floor


def test_document_optimize_and_report_json(corpus, tmp_path):
    with pdfwords.open(corpus["photo"]) as doc:
        rep = doc.optimize(str(tmp_path / "d.pdf"), preset="max")
    d = rep.to_dict()
    json.dumps(d)                                                   # serialisable, no bytes inside
    assert d["bytes_after"] == os.path.getsize(tmp_path / "d.pdf") and 0 < d["reduction"] < 1
    assert "data" not in d and d["codecs"]["ccitt_g4"] is True


# ------------------------------------------------------------------ scans: MRC, bilevel
@pytest.mark.parametrize("jbig2", [False, True])
def test_scan_uses_mrc(corpus, tmp_path, monkeypatch, jbig2):
    if jbig2 and not _codecs._jbig2():
        pytest.skip("jbig2enc not installed")
    if not jbig2:
        monkeypatch.setattr(_codecs, "_jbig2", lambda: None)        # the always-available G4 path
    for key in ("scan", "scan_color"):
        src = corpus[key]
        out = str(tmp_path / f"{key}_{jbig2}.pdf")
        rep = pdfwords.optimize(src, out, "balanced")
        assert [d["action"] for d in rep.images] == ["mrc"], rep.images
        assert rep.images[0]["mask_codec"] == ("jbig2" if jbig2 else "g4")
        assert rep.reduction > 0.8, rep.reduction
        assert page_ssim(src, out) > 0.9
        assert_valid(rep.data)


def test_scan_without_mrc_is_jpeg(corpus):
    rep = pdfwords.optimize(corpus["scan"], None, "balanced", mrc=False)
    assert [d["action"] for d in rep.images] == ["jpeg"]
    assert rep.reduction > 0.3


def test_scan_lossless_preset_never_lossy(corpus):
    rep = pdfwords.optimize(corpus["scan"], None, "lossless")
    assert all(d["action"] in ("kept", "jpeg-lossless") for d in rep.images)
    assert page_ssim(corpus["scan"], rep.data) > 0.999


def test_bilevel_flate_image_becomes_ccitt(corpus, monkeypatch):
    monkeypatch.setattr(_codecs, "_jbig2", lambda: None)
    rep = pdfwords.optimize(corpus["bilevel"], None, "balanced")
    assert [d["action"] for d in rep.images] == ["g4"]
    assert rep.reduction > 0.1      # a clean render: Flate is already decent, G4 still wins
    assert page_ssim(corpus["bilevel"], rep.data) > 0.999           # lossless
    assert_valid(rep.data)


def test_ocr_layer_makes_scan_searchable(corpus):
    pytest.importorskip("pytesseract")
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    rep = pdfwords.optimize(corpus["scan"], None, "balanced", ocr=True)
    assert rep.ocr_pages == [0]
    with pdfwords.open(rep.data) as d:
        text = d[0].get_text().lower()
    assert "taxpayer" in text or "identification" in text


# ------------------------------------------------------------------ CLI
def test_cli_shrink(corpus, tmp_path, capsys):
    out, rep = str(tmp_path / "cli.pdf"), str(tmp_path / "r.json")
    cli(["shrink", corpus["photo"], "-o", out, "--preset", "balanced", "--report", rep])
    r = json.loads(_read(rep))
    assert r["bytes_after"] == os.path.getsize(out) < r["bytes_before"]
    assert "smaller" in capsys.readouterr().err


def test_bad_arguments():
    with pytest.raises(ValueError):
        pdfwords.optimize(SIMPLE, None, "tiny")
    with pytest.raises(ValueError):
        pdfwords.optimize(SIMPLE, None, grayscale="yes")


def test_ssim_matches_reference_formula():
    rng = np.random.default_rng(0)
    a = Image.fromarray(rng.integers(0, 255, (64, 80), dtype=np.uint8))
    assert _imaging.ssim(a, a) == pytest.approx(1.0)
    skm = pytest.importorskip("skimage.metrics")
    b = Image.fromarray(np.clip(np.asarray(a, dtype=np.int16) + rng.integers(-20, 20, (64, 80)), 0, 255)
                        .astype(np.uint8))
    ref = skm.structural_similarity(np.asarray(a), np.asarray(b), data_range=255)
    assert _imaging.ssim(a, b) == pytest.approx(ref, abs=1e-6)
