// SPDX-License-Identifier: Apache-2.0
//! PyO3 binding: one call per page does PDFium extraction + grouping natively and builds the
//! PyMuPDF-shaped Python objects (tuples / dicts) directly.
use pdfwords_core::glyphs::PageGlyphs;
use pdfwords_core::layout::{make_spans, Block};
use pdfwords_core::output::{self, Xf};
use pdfwords_core::{extract_page, layout_page, LayoutParams, Pdfium, Sort};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::Instant;

static PDFIUM: OnceLock<Pdfium> = OnceLock::new();

fn pdfium() -> PyResult<&'static Pdfium> {
    PDFIUM
        .get()
        .ok_or_else(|| PyRuntimeError::new_err("pdfwords._native.init(libpdfium_path) not called"))
}

/// Load the PDFium shared library. Pass the path of the libpdfium that pypdfium2 already
/// loaded, so both share one PDFium instance (same dlopen handle).
#[pyfunction]
fn init(lib_path: &str) -> PyResult<()> {
    if PDFIUM.get().is_some() {
        return Ok(());
    }
    let p = Pdfium::load(lib_path).map_err(PyRuntimeError::new_err)?;
    let _ = PDFIUM.set(p);
    Ok(())
}

fn parse_sort(sort: &Bound<'_, PyAny>) -> PyResult<Sort> {
    if let Ok(s) = sort.extract::<String>() {
        return match s.as_str() {
            "xycut" => Ok(Sort::XyCut),
            "simple" => Ok(Sort::Simple),
            "none" | "" => Ok(Sort::None),
            _ => Err(PyValueError::new_err(format!("unknown sort {s:?}"))),
        };
    }
    Ok(if sort.is_truthy()? {
        Sort::Simple
    } else {
        Sort::None
    })
}

/// Glyphs of one page (extracted natively). Layout/formatting happens per get_text call.
#[pyclass(module = "pdfwords._native", frozen)]
struct RsPage {
    pg: PageGlyphs,
}

impl RsPage {
    fn xf(&self, rotated: bool) -> Xf {
        Xf {
            rotation: self.pg.rotation,
            w: self.pg.width,
            h: self.pg.height,
            on: rotated,
        }
    }
}

fn bbox_tuple<'py>(py: Python<'py>, b: [f64; 4]) -> PyResult<Bound<'py, PyTuple>> {
    PyTuple::new(py, b)
}

#[pymethods]
impl RsPage {
    #[getter]
    fn width(&self) -> f64 {
        self.pg.width
    }
    #[getter]
    fn height(&self) -> f64 {
        self.pg.height
    }
    #[getter]
    fn rotation(&self) -> i32 {
        self.pg.rotation
    }
    /// CropBox in PDF user space (l, b, r, t)
    #[getter]
    fn crop(&self) -> (f64, f64, f64, f64) {
        let c = self.pg.crop;
        (c[0], c[1], c[2], c[3])
    }
    #[getter]
    fn n_glyphs(&self) -> usize {
        self.pg.glyphs.len()
    }
    /// PDFium timings of the extraction call, nanoseconds.
    #[getter]
    fn timings<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        let t = self.pg.timings;
        d.set_item("load_page_ns", t.load_page_ns)?;
        d.set_item("load_text_ns", t.load_text_ns)?;
        d.set_item("chars_ns", t.chars_ns)?;
        d.set_item("close_ns", t.close_ns)?;
        Ok(d)
    }

    /// Same contract as pdfwords.Page.get_text for option in text|words|blocks|dict|rawdict.
    #[pyo3(signature = (option="text", sort=None, clip=None, rotated=false, dehyphenate=false, delimiters=None, extended=false))]
    #[allow(clippy::too_many_arguments)]
    fn get_text<'py>(
        &self,
        py: Python<'py>,
        option: &str,
        sort: Option<Bound<'py, PyAny>>,
        clip: Option<(f64, f64, f64, f64)>,
        rotated: bool,
        dehyphenate: bool,
        delimiters: Option<String>,
        extended: bool,
    ) -> PyResult<PyObject> {
        let sort = match sort {
            Some(s) => parse_sort(&s)?,
            None => Sort::None,
        };
        let clip = clip.map(|c| [c.0, c.1, c.2, c.3]);
        let blocks = layout_page(&self.pg, sort, clip, &LayoutParams::default());
        let xf = self.xf(rotated);
        match option.to_ascii_lowercase().as_str() {
            "words" => {
                let ws = output::words(
                    &blocks,
                    xf,
                    dehyphenate,
                    delimiters.as_deref().unwrap_or(""),
                );
                let mut items: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(ws.len());
                for w in ws {
                    items.push(PyTuple::new(
                        py,
                        [
                            w.bbox[0].into_pyobject(py)?.into_any(),
                            w.bbox[1].into_pyobject(py)?.into_any(),
                            w.bbox[2].into_pyobject(py)?.into_any(),
                            w.bbox[3].into_pyobject(py)?.into_any(),
                            PyString::new(py, &w.text).into_any(),
                            w.block.into_pyobject(py)?.into_any(),
                            w.line.into_pyobject(py)?.into_any(),
                            w.wno.into_pyobject(py)?.into_any(),
                        ],
                    )?);
                }
                Ok(PyList::new(py, items)?.into_any().unbind())
            }
            "blocks" => {
                let mut items: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(blocks.len());
                for b in &blocks {
                    let r = xf.rc(b.bbox);
                    let t = output::block_text(b, dehyphenate);
                    items.push(PyTuple::new(
                        py,
                        [
                            r[0].into_pyobject(py)?.into_any(),
                            r[1].into_pyobject(py)?.into_any(),
                            r[2].into_pyobject(py)?.into_any(),
                            r[3].into_pyobject(py)?.into_any(),
                            PyString::new(py, &t).into_any(),
                            b.number.into_pyobject(py)?.into_any(),
                            0i32.into_pyobject(py)?.into_any(),
                        ],
                    )?);
                }
                Ok(PyList::new(py, items)?.into_any().unbind())
            }
            "text" => {
                let mut s = String::new();
                for b in &blocks {
                    s.push_str(&output::block_text(b, dehyphenate));
                }
                Ok(PyString::new(py, &s).into_any().unbind())
            }
            "dict" | "rawdict" => Ok(self
                .dict(
                    py,
                    &blocks,
                    option.eq_ignore_ascii_case("rawdict"),
                    xf,
                    rotated,
                    extended,
                )?
                .into_any()
                .unbind()),
            other => Err(PyValueError::new_err(format!(
                "unsupported option {other:?}"
            ))),
        }
    }

    /// Blocks in pdftext's schema (pdfwords.compat.pdftext): lists for bboxes, interned font
    /// dicts {name, flags, size, weight}, char_start_idx/char_end_idx, rotation in radians,
    /// PDFium-generated spaces as their own spans with an empty font, a "\n" span ending every
    /// line but the page's last. Spans also carry "_base" (baseline coordinate across the line),
    /// "_sup" (superscript font flag) and "_vertical" for the Python post-pass.
    #[pyo3(signature = (sort=None, keep_chars=false))]
    fn pdftext_blocks<'py>(
        &self,
        py: Python<'py>,
        sort: Option<Bound<'py, PyAny>>,
        keep_chars: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let sort = match sort {
            Some(s) => parse_sort(&s)?,
            None => Sort::None,
        };
        let blocks = layout_page(&self.pg, sort, None, &LayoutParams::default());
        let xf = self.xf(true);
        let fonts = &self.pg.fonts;
        let k = |s: &str| PyString::intern(py, s);
        let empty_font = PyDict::new(py);
        empty_font.set_item(k("name"), "")?;
        empty_font.set_item(k("flags"), 0)?;
        empty_font.set_item(k("size"), 1.0f64)?;
        empty_font.set_item(k("weight"), -1)?;
        let mut font_cache: HashMap<(u32, u64), Bound<'py, PyDict>> = HashMap::new();
        let n_lines: usize = blocks.iter().map(|b| b.lines.len()).sum();
        let mut li = 0usize;
        let out = PyList::empty(py);
        let rbox = |b: [f64; 4]| -> [f64; 4] { xf.rc(b) };
        for b in &blocks {
            let lines = PyList::empty(py);
            for ln in &b.lines {
                li += 1;
                let mut dir = ln.dir;
                if self.pg.rotation != 0 {
                    let p0 = xf.pt(0.0, 0.0);
                    let p1 = xf.pt(dir.0, dir.1);
                    dir = (p1.0 - p0.0, p1.1 - p0.1);
                }
                let rotation = if dir == (1.0, 0.0) {
                    0.0
                } else {
                    (-dir.1).atan2(dir.0).rem_euclid(2.0 * std::f64::consts::PI)
                };
                let vertical = dir.1.abs() > dir.0.abs();
                let spans = PyList::empty(py);
                let mut lbox = [
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ];
                let mut last_idx: i32 = -1;
                let mut last_box = [0.0f64; 4];
                let mut last_base = 0.0f64;
                let mut any = false;
                for sp in make_spans(ln, fonts, &LayoutParams::default()) {
                    let f = &fonts[sp.font as usize];
                    let chars = &ln.chars[sp.start..sp.end];
                    if chars.is_empty() {
                        continue;
                    }
                    let fkey = (sp.font, sp.size.to_bits());
                    let font = match font_cache.get(&fkey) {
                        Some(d) => d.clone(),
                        None => {
                            let d = PyDict::new(py);
                            d.set_item(k("name"), &f.name)?;
                            d.set_item(k("flags"), f.pdf_flags)?;
                            d.set_item(k("size"), sp.size)?;
                            d.set_item(k("weight"), f.weight)?;
                            font_cache.insert(fkey, d.clone());
                            d
                        }
                    };
                    let o = xf.pt(chars[0].ox, chars[0].oy);
                    let base = if vertical { o.0 } else { o.1 };
                    // split into runs of real chars / PDFium-generated chars
                    let mut start = 0usize;
                    while start < chars.len() {
                        let gen = chars[start].synthetic;
                        let mut end = start + 1;
                        while end < chars.len() && chars[end].synthetic == gen {
                            end += 1;
                        }
                        let run = &chars[start..end];
                        let mut text = String::with_capacity(run.len());
                        let mut bb = [
                            f64::INFINITY,
                            f64::INFINITY,
                            f64::NEG_INFINITY,
                            f64::NEG_INFINITY,
                        ];
                        let cl = if keep_chars {
                            Some(PyList::empty(py))
                        } else {
                            None
                        };
                        let mut i0 = -1i32;
                        let mut i1 = -1i32;
                        for (j, c) in run.iter().enumerate() {
                            let mut idx = c.idx;
                            if idx < 0 {
                                // a generated char sits between its real neighbours
                                let nxt = chars[start + j + 1..]
                                    .iter()
                                    .find(|x| x.idx >= 0)
                                    .map(|x| x.idx);
                                idx = match nxt {
                                    Some(n) if last_idx + 1 >= n => last_idx.max(0),
                                    _ => last_idx + 1,
                                };
                            }
                            last_idx = last_idx.max(idx);
                            if i0 < 0 {
                                i0 = idx;
                            }
                            i1 = idx;
                            text.push(c.c);
                            let r = rbox(c.bbox);
                            bb = [
                                bb[0].min(r[0]),
                                bb[1].min(r[1]),
                                bb[2].max(r[2]),
                                bb[3].max(r[3]),
                            ];
                            last_box = r;
                            if let Some(cl) = &cl {
                                let cd = PyDict::new(py);
                                cd.set_item(k("bbox"), PyList::new(py, r)?)?;
                                let mut buf = [0u8; 4];
                                cd.set_item(k("char"), c.c.encode_utf8(&mut buf) as &str)?;
                                cd.set_item(k("rotation"), rotation)?;
                                cd.set_item(k("font"), if gen { &empty_font } else { &font })?;
                                cd.set_item(k("char_idx"), idx)?;
                                cl.append(cd)?;
                            }
                        }
                        lbox = [
                            lbox[0].min(bb[0]),
                            lbox[1].min(bb[1]),
                            lbox[2].max(bb[2]),
                            lbox[3].max(bb[3]),
                        ];
                        let d = PyDict::new(py);
                        d.set_item(k("bbox"), PyList::new(py, bb)?)?;
                        d.set_item(k("text"), &text)?;
                        d.set_item(k("font"), if gen { &empty_font } else { &font })?;
                        d.set_item(k("rotation"), rotation)?;
                        d.set_item(k("char_start_idx"), i0)?;
                        d.set_item(k("char_end_idx"), i1)?;
                        d.set_item(k("url"), "")?;
                        d.set_item(k("superscript"), false)?;
                        d.set_item(k("subscript"), false)?;
                        if let Some(cl) = cl {
                            d.set_item(k("chars"), cl)?;
                        }
                        d.set_item(k("_base"), base)?;
                        d.set_item(k("_sup"), (sp.flags & 1) != 0)?;
                        d.set_item(k("_vertical"), vertical)?;
                        spans.append(d)?;
                        last_base = base;
                        any = true;
                        start = end;
                    }
                }
                if !any {
                    continue;
                }
                if li < n_lines {
                    // PDFium's generated "\r\n" ends the line
                    let x = if vertical {
                        (last_box[0] + last_box[2]) / 2.0
                    } else {
                        last_box[2]
                    };
                    let y = if vertical { last_box[3] } else { last_base };
                    let nl = last_idx + 1;
                    let d = PyDict::new(py);
                    d.set_item(k("bbox"), PyList::new(py, [x, y, x, y])?)?;
                    d.set_item(k("text"), "\n")?;
                    d.set_item(k("font"), &empty_font)?;
                    d.set_item(k("rotation"), rotation)?;
                    d.set_item(k("char_start_idx"), nl)?;
                    d.set_item(k("char_end_idx"), nl + 1)?;
                    d.set_item(k("url"), "")?;
                    d.set_item(k("superscript"), false)?;
                    d.set_item(k("subscript"), false)?;
                    if keep_chars {
                        let cl = PyList::empty(py);
                        for (j, ch) in ["\r", "\n"].iter().enumerate() {
                            let cd = PyDict::new(py);
                            cd.set_item(k("bbox"), PyList::new(py, [x, y, x, y])?)?;
                            cd.set_item(k("char"), *ch)?;
                            cd.set_item(k("rotation"), rotation)?;
                            cd.set_item(k("font"), &empty_font)?;
                            cd.set_item(k("char_idx"), nl + j as i32)?;
                            cl.append(cd)?;
                        }
                        d.set_item(k("chars"), cl)?;
                    }
                    d.set_item(k("_base"), y)?;
                    d.set_item(k("_sup"), false)?;
                    d.set_item(k("_vertical"), vertical)?;
                    spans.append(d)?;
                    lbox = [
                        lbox[0].min(x),
                        lbox[1].min(y),
                        lbox[2].max(x),
                        lbox[3].max(y),
                    ];
                }
                let ld = PyDict::new(py);
                ld.set_item(k("bbox"), PyList::new(py, lbox)?)?;
                ld.set_item(k("spans"), spans)?;
                lines.append(ld)?;
            }
            if lines.is_empty() {
                continue;
            }
            let mut bb = [
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            ];
            for ln in lines.iter() {
                let r: [f64; 4] = ln.get_item("bbox")?.extract()?;
                bb = [
                    bb[0].min(r[0]),
                    bb[1].min(r[1]),
                    bb[2].max(r[2]),
                    bb[3].max(r[3]),
                ];
            }
            let bd = PyDict::new(py);
            bd.set_item(k("bbox"), PyList::new(py, bb)?)?;
            bd.set_item(k("lines"), lines)?;
            out.append(bd)?;
        }
        Ok(out)
    }

    /// Bulk words for numpy: (coords f64[N*4] bytes, ids i32[N*3] bytes (block,line,word), texts list)
    #[pyo3(signature = (sort=None, clip=None, rotated=false))]
    fn words_arrays<'py>(
        &self,
        py: Python<'py>,
        sort: Option<Bound<'py, PyAny>>,
        clip: Option<(f64, f64, f64, f64)>,
        rotated: bool,
    ) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>, Bound<'py, PyList>)> {
        let sort = match sort {
            Some(s) => parse_sort(&s)?,
            None => Sort::None,
        };
        let blocks = layout_page(
            &self.pg,
            sort,
            clip.map(|c| [c.0, c.1, c.2, c.3]),
            &LayoutParams::default(),
        );
        let ws = output::words(&blocks, self.xf(rotated), false, "");
        let mut coords: Vec<u8> = Vec::with_capacity(ws.len() * 32);
        let mut ids: Vec<u8> = Vec::with_capacity(ws.len() * 12);
        for w in &ws {
            for v in w.bbox {
                coords.extend_from_slice(&v.to_ne_bytes());
            }
            for v in [w.block as i32, w.line as i32, w.wno as i32] {
                ids.extend_from_slice(&v.to_ne_bytes());
            }
        }
        let texts = PyList::new(py, ws.iter().map(|w| PyString::new(py, &w.text)))?;
        Ok((PyBytes::new(py, &coords), PyBytes::new(py, &ids), texts))
    }
}

impl RsPage {
    fn dict<'py>(
        &self,
        py: Python<'py>,
        blocks: &[Block],
        raw: bool,
        xf: Xf,
        rotated: bool,
        extended: bool,
    ) -> PyResult<Bound<'py, PyDict>> {
        let fonts = &self.pg.fonts;
        let (w, h) = if rotated && (self.pg.rotation == 90 || self.pg.rotation == 270) {
            (self.pg.height, self.pg.width)
        } else {
            (self.pg.width, self.pg.height)
        };
        let res = PyDict::new(py);
        res.set_item("width", w)?;
        res.set_item("height", h)?;
        let pblocks = PyList::empty(py);
        // interned key strings
        let k = |s: &str| PyString::intern(py, s);
        for b in blocks {
            let lines = PyList::empty(py);
            for ln in &b.lines {
                let spans = PyList::empty(py);
                for sp in make_spans(ln, fonts, &LayoutParams::default()) {
                    let f = &fonts[sp.font as usize];
                    let chars = &ln.chars[sp.start..sp.end];
                    let d = PyDict::new(py);
                    d.set_item(k("size"), sp.size)?;
                    d.set_item(k("flags"), sp.flags)?;
                    d.set_item(k("bidi"), chars[0].bidi as i32)?;
                    d.set_item(
                        k("char_flags"),
                        if chars.iter().any(|c| c.synthetic) {
                            0
                        } else {
                            16
                        },
                    )?;
                    d.set_item(k("font"), &f.name)?;
                    d.set_item(k("color"), sp.color)?;
                    d.set_item(k("alpha"), 255)?;
                    d.set_item(k("ascender"), f.ascender)?;
                    d.set_item(k("descender"), f.descender)?;
                    if extended {
                        d.set_item(k("weight"), f.weight)?;
                        d.set_item(k("pdf_flags"), f.pdf_flags)?;
                    }
                    if raw {
                        let cl = PyList::empty(py);
                        for c in chars {
                            let cd = PyDict::new(py);
                            cd.set_item(k("origin"), xf.pt(c.ox, c.oy))?;
                            cd.set_item(k("bbox"), bbox_tuple(py, xf.rc(c.bbox))?)?;
                            let mut buf = [0u8; 4];
                            cd.set_item(k("c"), c.c.encode_utf8(&mut buf) as &str)?;
                            cd.set_item(k("synthetic"), c.synthetic)?;
                            if extended {
                                cd.set_item(k("idx"), c.idx)?;
                            }
                            cl.append(cd)?;
                        }
                        d.set_item(k("chars"), cl)?;
                    } else {
                        let t: String = chars.iter().map(|c| c.c).collect();
                        d.set_item(k("text"), t)?;
                    }
                    d.set_item(k("origin"), xf.pt(chars[0].ox, chars[0].oy))?;
                    d.set_item(k("bbox"), bbox_tuple(py, xf.rc(sp.bbox))?)?;
                    spans.append(d)?;
                }
                let mut dir = ln.dir;
                if rotated && self.pg.rotation != 0 {
                    let p0 = xf.pt(0.0, 0.0);
                    let p1 = xf.pt(dir.0, dir.1);
                    dir = (p1.0 - p0.0, p1.1 - p0.1);
                }
                let ld = PyDict::new(py);
                ld.set_item(k("spans"), spans)?;
                ld.set_item(k("wmode"), ln.wmode)?;
                ld.set_item(k("dir"), dir)?;
                ld.set_item(k("bbox"), bbox_tuple(py, xf.rc(ln.bbox))?)?;
                lines.append(ld)?;
            }
            let bd = PyDict::new(py);
            bd.set_item(k("type"), 0)?;
            bd.set_item(k("number"), b.number)?;
            bd.set_item(k("flags"), 0)?;
            bd.set_item(k("bbox"), bbox_tuple(py, xf.rc(b.bbox))?)?;
            bd.set_item(k("lines"), lines)?;
            pblocks.append(bd)?;
        }
        res.set_item("blocks", pblocks)?;
        Ok(res)
    }
}

/// Extract the glyphs of page `index` from a PDFium FPDF_DOCUMENT handle (address).
/// The GIL is held for the whole call: PDFium is not thread-safe.
#[pyfunction]
#[pyo3(signature = (doc_addr, index, ligatures=false))]
fn load_page(doc_addr: usize, index: i32, ligatures: bool) -> PyResult<RsPage> {
    let p = pdfium()?;
    let pg = unsafe { extract_page(p, doc_addr as *mut std::ffi::c_void, index, ligatures) }
        .map_err(PyRuntimeError::new_err)?;
    Ok(RsPage { pg })
}

/// Benchmark helper: run extraction + layout + words for one page fully natively and return
/// timings in ns (no Python objects): load_page, load_text, chars, close, layout, words, n_words.
#[pyfunction]
fn bench_page<'py>(py: Python<'py>, doc_addr: usize, index: i32) -> PyResult<Bound<'py, PyDict>> {
    let p = pdfium()?;
    let pg = unsafe { extract_page(p, doc_addr as *mut std::ffi::c_void, index, false) }
        .map_err(PyRuntimeError::new_err)?;
    let t1 = Instant::now();
    let blocks = layout_page(
        &pg,
        Sort::None,
        Some([0.0, 0.0, pg.width, pg.height]),
        &LayoutParams::default(),
    );
    let t2 = Instant::now();
    let ws = output::words(
        &blocks,
        Xf {
            rotation: pg.rotation,
            w: pg.width,
            h: pg.height,
            on: false,
        },
        false,
        "",
    );
    let t3 = Instant::now();
    let d = PyDict::new(py);
    let t = pg.timings;
    d.set_item("load_page_ns", t.load_page_ns)?;
    d.set_item("load_text_ns", t.load_text_ns)?;
    d.set_item("chars_ns", t.chars_ns)?;
    d.set_item("close_ns", t.close_ns)?;
    d.set_item("layout_ns", (t2 - t1).as_nanos() as u64)?;
    d.set_item("words_ns", (t3 - t2).as_nanos() as u64)?;
    d.set_item("n_words", ws.len())?;
    d.set_item("n_glyphs", pg.glyphs.len())?;
    Ok(d)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(init, m)?)?;
    m.add_function(wrap_pyfunction!(load_page, m)?)?;
    m.add_function(wrap_pyfunction!(bench_page, m)?)?;
    m.add_class::<RsPage>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
