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
    #[pyo3(signature = (option="text", sort=None, clip=None, rotated=false, dehyphenate=false, delimiters=None))]
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
                )?
                .into_any()
                .unbind()),
            other => Err(PyValueError::new_err(format!(
                "unsupported option {other:?}"
            ))),
        }
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
                    if raw {
                        let cl = PyList::empty(py);
                        for c in chars {
                            let cd = PyDict::new(py);
                            cd.set_item(k("origin"), xf.pt(c.ox, c.oy))?;
                            cd.set_item(k("bbox"), bbox_tuple(py, xf.rc(c.bbox))?)?;
                            let mut buf = [0u8; 4];
                            cd.set_item(k("c"), c.c.encode_utf8(&mut buf) as &str)?;
                            cd.set_item(k("synthetic"), c.synthetic)?;
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
