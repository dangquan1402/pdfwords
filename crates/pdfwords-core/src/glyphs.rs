// SPDX-License-Identifier: Apache-2.0
//! Stage 1: PDFium chars -> `Glyph` records in one native loop per page.
//!
//! Per char: unicode, origin, loose box, text-object handle (4 cheap FFI calls).
//! Matrix, font info, font size and fill colour are queried once per *text object*.

use crate::ffi::*;
use regex::Regex;
use std::collections::HashMap;
use std::os::raw::{c_char, c_double, c_float, c_int, c_uint};
use std::ptr;
use std::sync::OnceLock;
use std::time::Instant;

pub const FD_FIXED_PITCH: i32 = 1 << 0;
pub const FD_SERIF: i32 = 1 << 1;
pub const FD_ITALIC: i32 = 1 << 6;
pub const FD_FORCE_BOLD: i32 = 1 << 18;

pub const TEXT_FONT_SUPERSCRIPT: u32 = 1;
pub const TEXT_FONT_ITALIC: u32 = 2;
pub const TEXT_FONT_SERIFED: u32 = 4;
pub const TEXT_FONT_MONOSPACED: u32 = 8;
pub const TEXT_FONT_BOLD: u32 = 16;

struct Res {
    subset: Regex,
    bold: Regex,
    italic: Regex,
    mono: Regex,
    sans: Regex,
    serif: Regex,
}

fn res() -> &'static Res {
    static R: OnceLock<Res> = OnceLock::new();
    R.get_or_init(|| Res {
        subset: Regex::new(r"^[A-Z]{6}\+").unwrap(),
        bold: Regex::new(r"(?i)bold|black|heavy|-bd|\bbd\b|-blk|medi|^cmbx|^cmbsy|^cmmib|^cmb\d").unwrap(),
        italic: Regex::new(r"(?i)italic|oblique|-it\b|-bdi|-ital|slant|kursiv").unwrap(),
        mono: Regex::new(r"(?i)mono|courier|consol|code|typewriter|sftt|cmtt|menlo").unwrap(),
        sans: Regex::new(r"(?i)sans|arial|helvetica|verdana|calibri|segoe|tahoma|gothic|grotesk|frutiger|futura|cmss").unwrap(),
        serif: Regex::new(r"(?i)times|roman|serif|georgia|garamond|nimbusrom|minion|cambria|palatino|book|cmr|cmmi|sil").unwrap(),
    })
}

#[derive(Clone, Debug)]
pub struct FontInfo {
    pub name: String,
    pub pdf_flags: i32,
    pub weight: i32,
    pub italic_angle: i32,
    pub ascender: f64,
    pub descender: f64,
    pub embedded: bool,
    /// PyMuPDF-compatible style flags (without superscript)
    pub flags: u32,
    /// ascender/descender normalised so boxes are >= 1 font size tall
    pub asc_n: f64,
    pub desc_n: f64,
}

impl FontInfo {
    pub fn new(
        name: String,
        pdf_flags: i32,
        weight: i32,
        ascender: f64,
        descender: f64,
        italic_angle: i32,
        embedded: bool,
    ) -> Self {
        let r = res();
        let n = name.as_str();
        let f = pdf_flags;
        let mut flags = 0;
        if (f & FD_ITALIC) != 0 || r.italic.is_match(n) || italic_angle < -1 {
            flags |= TEXT_FONT_ITALIC;
        }
        if r.mono.is_match(n) || ((f & FD_FIXED_PITCH) != 0 && !r.sans.is_match(n)) {
            flags |= TEXT_FONT_MONOSPACED;
        }
        // PyMuPDF output marks practically every embedded font "serifed" (observed); mirror that.
        if embedded || r.serif.is_match(n) || ((f & FD_SERIF) != 0 && !r.sans.is_match(n)) {
            flags |= TEXT_FONT_SERIFED;
        }
        if (f & FD_FORCE_BOLD) != 0 || r.bold.is_match(n) || weight >= 750 {
            flags |= TEXT_FONT_BOLD;
        }
        let (mut a, mut d) = (ascender, descender);
        if a <= 0.0 && d >= 0.0 {
            a = 0.8;
            d = -0.2;
        }
        let h = a - d;
        if h > 0.0 && h < 1.0 {
            a /= h;
            d /= h;
        }
        FontInfo {
            name,
            pdf_flags,
            weight,
            italic_angle,
            ascender,
            descender,
            embedded,
            flags,
            asc_n: a,
            desc_n: d,
        }
    }
}

/// One character as emitted by the content stream.
#[derive(Clone, Copy, Debug)]
pub struct Glyph {
    pub c: char,
    pub px: f64,
    pub py: f64,
    pub qx: f64,
    pub qy: f64,
    pub dx: f64,
    pub dy: f64,
    pub size: f64,
    pub font: u32,
    pub color: u32,
    pub hyphen: bool,
    pub cont: bool,
    pub gen_space: bool,
    pub idx: i32,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Timings {
    pub load_page_ns: u64,
    pub load_text_ns: u64,
    pub chars_ns: u64,
    pub close_ns: u64,
}

pub struct PageGlyphs {
    /// unrotated CropBox width/height (points)
    pub width: f64,
    pub height: f64,
    /// /Rotate in degrees (0, 90, 180, 270)
    pub rotation: i32,
    pub glyphs: Vec<Glyph>,
    pub fonts: Vec<FontInfo>,
    pub n_chars: i32,
    pub timings: Timings,
}

/// CropBox -> MediaBox -> page bounding box -> US Letter.  Returns (l, b, r, t).
unsafe fn crop_box(p: &Pdfium, page: FPDF_PAGE) -> (f64, f64, f64, f64) {
    let (mut l, mut b, mut r, mut t) = (0f32, 0f32, 0f32, 0f32);
    if (p.FPDFPage_GetCropBox)(page, &mut l, &mut b, &mut r, &mut t) != 0
        || (p.FPDFPage_GetMediaBox)(page, &mut l, &mut b, &mut r, &mut t) != 0
    {
        return (l as f64, b as f64, r as f64, t as f64);
    }
    let mut rc = FS_RECTF::default();
    if (p.FPDF_GetPageBoundingBox)(page, &mut rc) != 0 {
        return (
            rc.left as f64,
            rc.bottom as f64,
            rc.right as f64,
            rc.top as f64,
        );
    }
    (0.0, 0.0, 612.0, 792.0)
}

unsafe fn font_info(p: &Pdfium, font: FPDF_FONT) -> FontInfo {
    if font.is_null() {
        return FontInfo::new(String::new(), 0, 0, 0.8, -0.2, 0, true);
    }
    let embedded = (p.FPDFFont_GetIsEmbedded)(font) != 0;
    let mut buf = [0 as c_char; 256];
    let n = (p.FPDFFont_GetBaseFontName)(font, buf.as_mut_ptr(), 256);
    let mut name = String::new();
    if n > 0 {
        let bytes: Vec<u8> = buf
            .iter()
            .take_while(|&&c| c != 0)
            .map(|&c| c as u8)
            .collect();
        name = String::from_utf8_lossy(&bytes).into_owned();
    }
    name = res().subset.replace(&name, "").into_owned();
    let mut flags = (p.FPDFFont_GetFlags)(font);
    if flags <= 0 {
        flags = 0;
    }
    let weight = (p.FPDFFont_GetWeight)(font);
    let (mut a, mut d): (c_float, c_float) = (0.0, 0.0);
    let asc = if (p.FPDFFont_GetAscent)(font, 1.0, &mut a) != 0 {
        a as f64
    } else {
        0.8
    };
    let dsc = if (p.FPDFFont_GetDescent)(font, 1.0, &mut d) != 0 {
        d as f64
    } else {
        -0.2
    };
    let mut ia: c_int = 0;
    let ia = if (p.FPDFFont_GetItalicAngle)(font, &mut ia) != 0 {
        ia
    } else {
        0
    };
    FontInfo::new(name, flags, weight, asc, dsc, ia, embedded)
}

struct ObjState {
    font: u32,
    size: f64,
    dx: f64,
    dy: f64,
    color: u32,
    adx: f64,
    ady: f64,
    asc_desc: f64,
}

/// Extract glyphs from an already-loaded page (`page`) and text page (`tp`).
/// Extract glyphs (with fonts and pen geometry) from a loaded page and its text page.
///
/// # Safety
/// `p` must be the PDFium library that created the handle(s) passed in, the handles must be
/// valid and open, and no other thread may call into PDFium concurrently.
#[allow(clippy::map_entry)]
pub unsafe fn glyphs_from_textpage(
    p: &Pdfium,
    page: FPDF_PAGE,
    tp: FPDF_TEXTPAGE,
    ligatures: bool,
) -> PageGlyphs {
    let (l, b, r, t) = crop_box(p, page);
    let (ox, oy) = (l, t);
    let width = (r - l).abs();
    let height = (t - b).abs();
    let rotation = ((p.FPDFPage_GetRotation)(page).rem_euclid(4)) * 90;
    let n = (p.FPDFText_CountChars)(tp);
    let mut out: Vec<Glyph> = Vec::with_capacity(n.max(0) as usize);
    let mut fonts: Vec<FontInfo> = Vec::new();
    let mut font_idx: HashMap<usize, u32> = HashMap::new();
    let mut objs: HashMap<usize, ObjState> = HashMap::new();
    let (mut x, mut y): (c_double, c_double) = (0.0, 0.0);
    let mut rect = FS_RECTF::default();
    let mut m = FS_MATRIX::default();
    let mut gen_space = false;

    for i in 0..n {
        let mut code = (p.FPDFText_GetUnicode)(tp, i);
        if code == 32 || code == 13 || code == 10 {
            if (p.FPDFText_IsGenerated)(tp, i) == 1 {
                gen_space = gen_space || code == 32;
                continue;
            }
            if code != 32 {
                continue;
            }
        }
        let mut hyphen = false;
        if code == 2 {
            code = 0x2D;
            hyphen = true;
        } else if code == 0 || code >= 0xFFFE || (0xD800..=0xDFFF).contains(&code) {
            code = 0xFFFD;
        }
        let c = char::from_u32(code).unwrap_or('\u{FFFD}');
        let obj = (p.FPDFText_GetTextObject)(tp, i);
        let key = obj as usize;
        if !objs.contains_key(&key) {
            (p.FPDFText_GetMatrix)(tp, i, &mut m);
            let (fi, tf);
            if !obj.is_null() {
                let mut fs: c_float = 0.0;
                (p.FPDFTextObj_GetFontSize)(obj, &mut fs);
                tf = fs as f64;
                let font = (p.FPDFTextObj_GetFont)(obj);
                let fk = font as usize;
                fi = match font_idx.get(&fk) {
                    Some(&ix) => ix,
                    None => {
                        let ix = fonts.len() as u32;
                        fonts.push(font_info(p, font));
                        font_idx.insert(fk, ix);
                        ix
                    }
                };
            } else {
                tf = (p.FPDFText_GetFontSize)(tp, i);
                fi = fonts.len() as u32;
                fonts.push(FontInfo::new(String::new(), 0, 400, 0.8, -0.2, 0, true));
            }
            let (a, bb, cc, d) = (m.a as f64, m.b as f64, m.c as f64, m.d as f64);
            let det = (a * d - bb * cc).abs();
            let size = if det > 0.0 { tf * det.sqrt() } else { tf };
            let mut nrm = a.hypot(bb);
            if nrm == 0.0 {
                nrm = 1.0;
            }
            let (dx, dy) = (a / nrm, -bb / nrm);
            let (mut cr, mut cg, mut cb, mut ca): (c_uint, c_uint, c_uint, c_uint) = (0, 0, 0, 0);
            let color = if (p.FPDFText_GetFillColor)(tp, i, &mut cr, &mut cg, &mut cb, &mut ca) != 0
            {
                (cr << 16) | (cg << 8) | cb
            } else {
                0
            };
            let f = &fonts[fi as usize];
            objs.insert(
                key,
                ObjState {
                    font: fi,
                    size,
                    dx,
                    dy,
                    color,
                    adx: dx.abs(),
                    ady: dy.abs(),
                    asc_desc: f.ascender - f.descender,
                },
            );
        }
        let st = &objs[&key];
        (p.FPDFText_GetCharOrigin)(tp, i, &mut x, &mut y);
        (p.FPDFText_GetLooseCharBox)(tp, i, &mut rect);
        let px = x - ox;
        let py = oy - y;
        let adv = if st.ady < 1e-3 {
            (rect.right as f64 - rect.left as f64).abs()
        } else if st.adx < 1e-3 {
            (rect.top as f64 - rect.bottom as f64).abs()
        } else {
            let w = (rect.right as f64 - rect.left as f64).abs();
            let hh = (rect.top as f64 - rect.bottom as f64).abs();
            let h = st.size * st.asc_desc;
            if st.adx >= st.ady {
                ((w - h * st.ady) / st.adx).max(0.0)
            } else {
                ((hh - h * st.adx) / st.ady).max(0.0)
            }
        };
        let qx = px + adv * st.dx;
        let qy = py + adv * st.dy;
        let mut g = Glyph {
            c,
            px,
            py,
            qx,
            qy,
            dx: st.dx,
            dy: st.dy,
            size: st.size,
            font: st.font,
            color: st.color,
            hyphen,
            cont: false,
            gen_space,
            idx: i,
        };
        gen_space = false;
        if let Some(prev) = out.last() {
            if prev.px == px
                && prev.py == py
                && prev.qx == qx
                && prev.qy == qy
                && prev.font == g.font
            {
                g.cont = true;
            }
        }
        out.push(g);
    }
    fix_overhang(&mut out);
    let glyphs = if ligatures {
        recombine_ligatures(out)
    } else {
        out
    };
    PageGlyphs {
        width,
        height,
        rotation,
        glyphs,
        fonts,
        n_chars: n,
        timings: Timings::default(),
    }
}

/// Loose box = origin + glyph-bbox width (too wide for italic overhang); clamp to next origin.
fn fix_overhang(g: &mut [Glyph]) {
    for i in 0..g.len().saturating_sub(1) {
        let b = g[i + 1];
        let a = &mut g[i];
        if a.dy == 0.0
            && a.dx > 0.0
            && b.py == a.py
            && b.dx > 0.0
            && a.px < b.px
            && b.px < a.qx
            && !b.cont
            && !b.gen_space
        {
            a.qx = b.px;
        }
    }
}

fn lig_for(s: &str) -> Option<char> {
    Some(match s {
        "ff" => '\u{FB00}',
        "fi" => '\u{FB01}',
        "fl" => '\u{FB02}',
        "ffi" => '\u{FB03}',
        "ffl" => '\u{FB04}',
        "st" => '\u{FB06}',
        _ => return None,
    })
}

fn recombine_ligatures(glyphs: Vec<Glyph>) -> Vec<Glyph> {
    let mut res = Vec::with_capacity(glyphs.len());
    let n = glyphs.len();
    let mut i = 0;
    while i < n {
        let mut j = i + 1;
        while j < n && glyphs[j].cont {
            j += 1;
        }
        if j - i > 1 {
            let txt: String = glyphs[i..j].iter().map(|g| g.c).collect();
            if let Some(l) = lig_for(&txt) {
                let mut g = glyphs[i];
                g.c = l;
                res.push(g);
            } else {
                res.extend_from_slice(&glyphs[i..j]);
            }
        } else {
            res.push(glyphs[i]);
        }
        i = j;
    }
    res
}

/// Load page `index` of `doc`, extract glyphs, close everything. Records timings.
///
/// # Safety
/// `p` must be the PDFium library that created the handle(s) passed in, the handles must be
/// valid and open, and no other thread may call into PDFium concurrently.
pub unsafe fn extract_page(
    p: &Pdfium,
    doc: FPDF_DOCUMENT,
    index: i32,
    ligatures: bool,
) -> Result<PageGlyphs, String> {
    let t0 = Instant::now();
    let page = (p.FPDF_LoadPage)(doc, index);
    if page.is_null() {
        return Err(format!("cannot load page {index}"));
    }
    let t1 = Instant::now();
    let tp = (p.FPDFText_LoadPage)(page);
    if tp.is_null() {
        (p.FPDF_ClosePage)(page);
        return Err(format!("cannot load text of page {index}"));
    }
    (p.FPDFText_CountChars)(tp);
    let t2 = Instant::now();
    let mut pg = glyphs_from_textpage(p, page, tp, ligatures);
    let t3 = Instant::now();
    (p.FPDFText_ClosePage)(tp);
    (p.FPDF_ClosePage)(page);
    let t4 = Instant::now();
    pg.timings = Timings {
        load_page_ns: (t1 - t0).as_nanos() as u64,
        load_text_ns: (t2 - t1).as_nanos() as u64,
        chars_ns: (t3 - t2).as_nanos() as u64,
        close_ns: (t4 - t3).as_nanos() as u64,
    };
    Ok(pg)
}

/// Open a document by path (standalone use, e.g. from Swift/Kotlin/CLI).
///
/// # Safety
/// `p` must be the PDFium library that created the handle(s) passed in, the handles must be
/// valid and open, and no other thread may call into PDFium concurrently.
pub unsafe fn open_document(
    p: &Pdfium,
    path: &str,
    password: Option<&str>,
) -> Result<FPDF_DOCUMENT, String> {
    (p.FPDF_InitLibrary)(); // no-op if already initialised
    let cpath = std::ffi::CString::new(path).map_err(|e| e.to_string())?;
    let cpw = password.map(|s| std::ffi::CString::new(s).unwrap());
    let doc = (p.FPDF_LoadDocument)(
        cpath.as_ptr(),
        cpw.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
    );
    if doc.is_null() {
        Err(format!("cannot open {path}"))
    } else {
        Ok(doc)
    }
}
