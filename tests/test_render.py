# SPDX-License-Identifier: Apache-2.0
"""Rendering: sizing, clip/tiles, rotation, alpha/gray, forms/annots, outputs, coordinate
helpers, pixmap shim, document-level image export, pdf2image compat, Rust parity."""
import io
import os

import pytest

import pdfwords
from conftest import FIXTURES
from _featurepdf import feature_pdf

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

W9 = os.path.join(FIXTURES, "irs_w9.pdf")
ROT = os.path.join(FIXTURES, "rot90.pdf")
SIMPLE = os.path.join(FIXTURES, "simple_text.pdf")


@pytest.fixture(scope="module")
def w9():
    with pdfwords.open(W9) as d:
        yield d


def test_round_sizing(w9):
    p = w9[0]
    assert p.rect[2:] == pytest.approx((611.976, 791.968), abs=1e-3)
    a = p.render(dpi=150)
    assert a.shape == (1650, 1275, 3) and a.dtype == np.uint8
    from pdfwords.render import _rint
    # US Letter: 792 * 150 / 72 = 1650.0000000002 -> 1650 (pypdfium2's ceil() gives 1651)
    assert _rint(792 * (150 / 72)) == 1650 and _rint(612 * (150 / 72)) == 1275
    assert p.render(dpi=72).shape == (792, 612, 3)
    assert p.render(scale=2).shape == (1584, 1224, 3)
    assert p.render(max_side=1000).shape == (1000, 773, 3)
    assert p.render(size=(300, None)).shape == (388, 300, 3)
    assert p.render(size=(None, 100)).shape == (100, 77, 3)
    assert p.render(size=(200, 100)).shape == (100, 200, 3)
    with pytest.raises(ValueError):
        p.render(dpi=100, scale=2)


def test_default_dpi_and_background(w9):
    a = w9[0].render()
    assert a.shape[:2] == (1650, 1275)
    assert tuple(a[0, 0]) == (255, 255, 255)
    b = w9[0].render(dpi=20, background=(0, 0, 255))
    assert tuple(b[0, 0]) == (0, 0, 255)
    c = w9[0].render(dpi=20, background=(1.0, 0.0, 0.0))
    assert tuple(c[0, 0]) == (255, 0, 0)


def test_clip_matches_full_render(w9):
    p = w9[0]
    for kw in ({"grayscale": True}, {"antialias": False}, {}):
        full = p.render(dpi=150, **kw)
        clip = (100.3, 100.7, 300.2, 200.9)
        g = p.render_geometry(150, clip=clip)
        sub = p.render(dpi=150, clip=clip, **kw)
        ref = full[g["py0"]:g["py0"] + g["h"], g["px0"]:g["px0"] + g["w"]]
        assert sub.shape == ref.shape
        if kw:      # bit-identical; colour anti-aliased glyphs may differ right at the left edge
            assert np.array_equal(sub, ref)
        else:
            assert (sub != ref).any(axis=-1).mean() < 0.02


def test_tiles_stitch(w9):
    p = w9[0]
    full = p.render(dpi=100, grayscale=True)
    out = np.zeros_like(full)
    n = 0
    for (x, y), t in p.render_tiles(100, tile=300, grayscale=True):
        assert t.shape[0] <= 300 and t.shape[1] <= 300
        out[y:y + t.shape[0], x:x + t.shape[1]] = t
        n += 1
    assert n == 4 * 3 and np.array_equal(out, full)


def test_rotation_and_word_alignment():
    with pdfwords.open(ROT) as d:
        p = d[0]
        assert p.rotation == 90
        disp = p.render(dpi=72)
        unrot = p.render(dpi=72, rotated=False)
        assert disp.shape == (300, 500, 3) and unrot.shape == (500, 300, 3)
        bg = disp.mean()
        for img, rotated in ((disp, True), (unrot, False)):
            for w in p.get_text("words", rotated=rotated):
                x0, y0, x1, y1 = (int(round(v)) for v in p.bbox_to_pixel(w[:4], 72, rotated=rotated))
                assert img[y0:y1, x0:x1].mean() < bg - 20, (rotated, w)   # ink under every word


def test_pixel_coordinate_helpers(w9):
    p = w9[0]
    x, y = p.pixel_to_pdf(300, 450, dpi=150)
    assert p.pdf_to_pixel(x, y, dpi=150) == pytest.approx((300, 450))
    assert p.pdf_to_pixel(72, 72, dpi=144) == pytest.approx((144, 144), abs=0.01)
    assert p.pdf_to_pixel(110, 210, dpi=72, clip=(100, 200, 300, 400)) == pytest.approx((10, 10), abs=0.05)
    assert p.pixel_to_pdf(0, 0, dpi=72, clip=(100, 200, 300, 400)) == pytest.approx((100, 200), abs=0.05)


def test_alpha_gray_and_outputs(w9):
    p = w9[0]
    rgba = p.render(dpi=36, alpha=True)
    assert rgba.shape[2] == 4 and rgba[0, 0, 3] == 0 and rgba[..., 3].max() == 255
    g = p.render(dpi=36, grayscale=True)
    assert g.ndim == 2 and g.shape == rgba.shape[:2]
    pil = p.render(dpi=36, output="pil")
    assert pil.size == (306, 396) and pil.mode == "RGB"
    raw = p.render(dpi=36, output="bytes")
    assert isinstance(raw, bytes) and len(raw) == 306 * 396 * 3
    assert np.array_equal(np.frombuffer(raw, np.uint8).reshape(396, 306, 3), p.render(dpi=36))
    for fmt, sig in (("png", b"\x89PNG"), ("jpeg", b"\xff\xd8"), ("webp", b"RIFF")):
        assert p.render(dpi=36, output=fmt)[:4].startswith(sig)
    buf = np.empty((396, 306, 3), np.uint8)
    assert p.render(dpi=36, out=buf) is buf
    with pytest.raises(ValueError):
        p.render(dpi=36, out=np.empty((10, 10, 3), np.uint8))
    with pytest.raises(ValueError):
        p.render(dpi=7200)            # > max_pixels
    assert p.render(dpi=2, max_pixels=None).shape == (22, 17, 3)


def test_forms_and_annots_drawn():
    with pdfwords.open(feature_pdf()) as d:
        p = d[0]
        with_forms = p.render(dpi=72)
        no_forms = p.render(dpi=72, forms=False)
        bare = p.render(dpi=72, forms=False, annots=False)
        assert (with_forms != no_forms).any() and (no_forms != bare).any()
    with pdfwords.open(feature_pdf(), flatten=True) as d:   # flattened: values are page content
        assert d[0].render(dpi=72, forms=False, annots=False).shape == with_forms.shape


def test_timeout(w9):
    with pytest.raises(pdfwords.RenderTimeout):
        w9[0].render(dpi=72, timeout=0)
    a = w9[0].render(dpi=72, timeout=30)     # progressive render == normal render
    assert np.array_equal(a, w9[0].render(dpi=72))


def test_thumbnail_and_pixmap(w9):
    p = w9[0]
    assert max(p.thumbnail(128).shape[:2]) == 128
    pix = p.get_pixmap()
    assert (pix.width, pix.height, pix.n, pix.alpha) == (612, 792, 3, 0)
    assert len(pix.samples) == 612 * 792 * 3 and pix.stride == 612 * 3
    assert Image.open(io.BytesIO(pix.tobytes("png"))).size == (612, 792)
    assert np.asarray(pix).shape == (792, 612, 3)
    assert pix.pixel(0, 0) == (255, 255, 255)
    assert p.get_pixmap(dpi=144).width == 1224
    assert p.get_pixmap(matrix=(2, 0, 0, 2, 0, 0)).width == 1224
    gray = p.get_pixmap(colorspace="gray", clip=(0, 0, 100, 50))
    assert (gray.n, gray.width, gray.height, gray.colorspace) == (1, 100, 50, "gray")
    assert p.get_pixmap(alpha=True).n == 4


def test_iter_and_to_images(tmp_path, w9):
    got = list(w9.iter_images("0-1", dpi=20))
    assert [i for i, _ in got] == [0, 1] and got[0][1].shape == (220, 170, 3)
    paths = w9.to_images([0, 1, 2], dpi=30, out_dir=tmp_path, workers=2)
    assert [os.path.basename(p) for p in paths] == ["irs_w9-0000.jpg", "irs_w9-0001.jpg", "irs_w9-0002.jpg"]
    assert Image.open(paths[1]).size == (255, 330)
    pngs = w9.to_images("-1", dpi=10, fmt="png", out_dir=tmp_path, name="p{page1}.{ext}", grayscale=True)
    assert os.path.basename(pngs[0]) == f"p{len(w9)}.png" and Image.open(pngs[0]).mode == "L"
    imgs = w9.to_images([0], dpi=10)
    assert imgs[0].size == (85, 110)
    with pytest.raises(ValueError):
        w9.to_images([0], out_dir=tmp_path, alpha=True)   # jpeg cannot hold alpha


def test_convert_from_path(tmp_path):
    imgs = pdfwords.convert_from_path(W9, dpi=50, first_page=2, last_page=3)
    assert len(imgs) == 2 and imgs[0].size == (425, 550) and imgs[0].mode == "RGB"
    with open(W9, "rb") as f:
        data = f.read()
    assert pdfwords.convert_from_bytes(data, dpi=50, last_page=1)[0].size == (425, 550)
    from pdfwords.compat import pdf2image as P
    assert P.convert_from_path(W9, size=300, last_page=1)[0].size == (232, 300)
    assert P.convert_from_path(W9, size=(300, None), last_page=1)[0].size == (300, 388)
    assert P.convert_from_path(W9, grayscale=True, last_page=1, dpi=20)[0].mode == "L"
    assert P.convert_from_path(W9, transparent=True, fmt="png", last_page=1, dpi=20)[0].mode == "RGBA"
    paths = P.convert_from_path(W9, dpi=20, output_folder=tmp_path, fmt="jpeg", paths_only=True, output_file="x",
                                last_page=2)
    assert [os.path.basename(p) for p in paths] == ["x0001-1.jpg", "x0001-2.jpg"]
    single = P.convert_from_path(W9, dpi=20, output_folder=tmp_path, fmt="png", single_file=True, output_file="s")
    assert single[0].size == (170, 220) and single[0].filename.endswith("s.png")
    par = P.convert_from_path(W9, dpi=20, thread_count=2)
    ser = P.convert_from_path(W9, dpi=20)
    assert [np.asarray(i).tobytes() for i in par] == [np.asarray(i).tobytes() for i in ser]
    info = P.pdfinfo_from_path(W9)
    assert info["Pages"] == 6 and info["Page size"].startswith("611.976 x 791.968")
    with pytest.raises(P.PDFPageCountError):
        P.convert_from_bytes(b"not a pdf")


@pytest.mark.skipif("rust" not in pdfwords.available_backends(), reason="native extension not built")
def test_rust_render_parity():
    nat = pdfwords._rust()
    for path in (W9, ROT):
        with pdfwords.open(path) as d:
            p = d[0]
            for kw in ({}, {"grayscale": True}, {"alpha": True}, {"rotated": False}, {"clip": (10, 20, 200, 300)}):
                a = p.render(dpi=100, forms=False, **kw)
                g = p.render_geometry(100, **kw)
                bg = 0x00FFFFFF if kw.get("alpha") else 0xFFFFFFFF
                w, h, ch, buf = nat.render_page(d._addr, 0, g["sx"], g["sy"], kw.get("clip"), kw.get("rotated", True),
                                                kw.get("alpha", False), kw.get("grayscale", False), True, True, bg, None)
                assert (h, w) == a.shape[:2] and np.array_equal(np.frombuffer(buf, np.uint8).reshape(a.shape), a)
