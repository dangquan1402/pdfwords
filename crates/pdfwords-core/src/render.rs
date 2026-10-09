// SPDX-License-Identifier: Apache-2.0
//! Page rendering into a caller-owned buffer (same geometry rules as the Python package).
//!
//! * Pixel size = round(points x scale) (not ceil, which stretches pages by one pixel when
//!   floating-point noise lands just above an integer).
//! * A clip selects a window of the full-page raster at integer pixel offsets.
//! * `rotated = true` renders the page as displayed (honours /Rotate); `false` renders the
//!   unrotated page, matching the default (unrotated) text coordinates.
use crate::ffi::{Pdfium, FPDF_PAGE, IFSDK_PAUSE};
use std::os::raw::{c_int, c_ulong, c_void};
use std::time::{Duration, Instant};

const FPDF_ANNOT: c_int = 0x01;
const FPDF_GRAYSCALE: c_int = 0x08;
const FPDF_REVERSE_BYTE_ORDER: c_int = 0x10;
const FPDF_RENDER_NO_SMOOTHTEXT: c_int = 0x1000;
const FPDF_RENDER_NO_SMOOTHIMAGE: c_int = 0x2000;
const FPDF_RENDER_NO_SMOOTHPATH: c_int = 0x4000;
const FPDFBITMAP_GRAY: c_int = 1;
const FPDFBITMAP_BGR: c_int = 2;
const FPDFBITMAP_BGRA: c_int = 4;
const FPDF_RENDER_TOBECONTINUED: c_int = 1;
const FPDF_RENDER_FAILED: c_int = 3;

#[derive(Clone, Debug)]
pub struct RenderOptions {
    /// pixels per point (dpi / 72), x and y
    pub scale: (f64, f64),
    /// window in output-space points (x0, y0, x1, y1), top-left origin
    pub clip: Option<[f64; 4]>,
    pub rotated: bool,
    pub alpha: bool,
    pub grayscale: bool,
    pub annots: bool,
    pub antialias: bool,
    /// 0xAARRGGBB
    pub background: u32,
    pub timeout: Option<Duration>,
}

impl Default for RenderOptions {
    fn default() -> Self {
        RenderOptions {
            scale: (150.0 / 72.0, 150.0 / 72.0),
            clip: None,
            rotated: true,
            alpha: false,
            grayscale: false,
            annots: true,
            antialias: true,
            background: 0xFFFF_FFFF,
            timeout: None,
        }
    }
}

/// Raster geometry: full-page raster size, the window rendered, effective scale.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub full_w: i32,
    pub full_h: i32,
    pub sx: f64,
    pub sy: f64,
    pub px0: i32,
    pub py0: i32,
    pub w: i32,
    pub h: i32,
    /// PDFium rotate argument (quarter turns clockwise relative to the displayed page)
    pub rotate: i32,
    /// bytes per pixel: 1 (gray), 3 (RGB) or 4 (RGBA)
    pub channels: usize,
}

impl Geometry {
    pub fn stride(&self) -> usize {
        self.w as usize * self.channels
    }
    pub fn len(&self) -> usize {
        self.stride() * self.h as usize
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

fn rint(v: f64) -> i32 {
    (v + 0.5 + 1e-9).floor() as i32
}

/// Geometry for an unrotated page of `width` x `height` points with /Rotate `rotation`.
pub fn geometry(width: f64, height: f64, rotation: i32, o: &RenderOptions) -> Geometry {
    let rot = rotation.rem_euclid(360);
    let (pw, ph) = if o.rotated && (rot == 90 || rot == 270) {
        (height, width)
    } else {
        (width, height)
    };
    let full_w = rint(pw * o.scale.0).max(1);
    let full_h = rint(ph * o.scale.1).max(1);
    let (sx, sy) = (full_w as f64 / pw, full_h as f64 / ph);
    let rotate = if o.rotated {
        0
    } else {
        ((360 - rot) % 360) / 90
    };
    let (px0, py0, w, h) = match o.clip {
        Some(c) => {
            let (x0, x1) = (c[0].min(c[2]), c[0].max(c[2]));
            let (y0, y1) = (c[1].min(c[3]), c[1].max(c[3]));
            let (px0, py0) = (rint(x0 * sx), rint(y0 * sy));
            (
                px0,
                py0,
                (rint(x1 * sx) - px0).max(1),
                (rint(y1 * sy) - py0).max(1),
            )
        }
        None => (0, 0, full_w, full_h),
    };
    let channels = if o.grayscale {
        1
    } else if o.alpha {
        4
    } else {
        3
    };
    Geometry {
        full_w,
        full_h,
        sx,
        sy,
        px0,
        py0,
        w,
        h,
        rotate,
        channels,
    }
}

unsafe extern "C" fn past_deadline(p: *mut IFSDK_PAUSE) -> c_int {
    let deadline = &*((*p).user as *const Instant);
    (Instant::now() > *deadline) as c_int
}

/// Render a loaded page into `buf` (packed rows, `geometry().len()` bytes): RGB, RGBA or gray.
///
/// # Safety
/// `page` must be a valid page handle of the PDFium library `p`, and no other thread may call
/// into PDFium concurrently.
pub unsafe fn render_loaded_page(
    p: &Pdfium,
    page: FPDF_PAGE,
    g: &Geometry,
    o: &RenderOptions,
    buf: &mut [u8],
) -> Result<(), String> {
    if buf.len() < g.len() {
        return Err(format!("buffer too small: {} < {}", buf.len(), g.len()));
    }
    let fmt = match g.channels {
        1 => FPDFBITMAP_GRAY,
        4 => FPDFBITMAP_BGRA,
        _ => FPDFBITMAP_BGR,
    };
    let bm = (p.FPDFBitmap_CreateEx)(
        g.w,
        g.h,
        fmt,
        buf.as_mut_ptr() as *mut c_void,
        g.stride() as c_int,
    );
    if bm.is_null() {
        return Err(format!("cannot create a {}x{} bitmap", g.w, g.h));
    }
    let mut flags = 0;
    if o.annots {
        flags |= FPDF_ANNOT;
    }
    if o.grayscale {
        flags |= FPDF_GRAYSCALE;
    } else {
        flags |= FPDF_REVERSE_BYTE_ORDER;
    }
    if !o.antialias {
        flags |= FPDF_RENDER_NO_SMOOTHTEXT | FPDF_RENDER_NO_SMOOTHIMAGE | FPDF_RENDER_NO_SMOOTHPATH;
    }
    // FillRect ignores FPDF_REVERSE_BYTE_ORDER: swap red and blue for RGB(A) output
    let bg = if o.grayscale {
        o.background
    } else {
        let c = o.background;
        (c & 0xFF00_FF00) | ((c & 0xFF) << 16) | ((c >> 16) & 0xFF)
    };
    (p.FPDFBitmap_FillRect)(bm, 0, 0, g.w, g.h, bg as c_ulong);
    let mut res = Ok(());
    match o.timeout {
        None => (p.FPDF_RenderPageBitmap)(
            bm, page, -g.px0, -g.py0, g.full_w, g.full_h, g.rotate, flags,
        ),
        Some(t) => {
            let deadline = Instant::now() + t;
            let mut pause = IFSDK_PAUSE {
                version: 1,
                NeedToPauseNow: Some(past_deadline),
                user: &deadline as *const Instant as *mut c_void,
            };
            let mut st = (p.FPDF_RenderPageBitmap_Start)(
                bm, page, -g.px0, -g.py0, g.full_w, g.full_h, g.rotate, flags, &mut pause,
            );
            while st == FPDF_RENDER_TOBECONTINUED {
                if Instant::now() > deadline {
                    res = Err(format!("rendering took longer than {t:?}"));
                    break;
                }
                st = (p.FPDF_RenderPage_Continue)(page, &mut pause);
            }
            (p.FPDF_RenderPage_Close)(page);
            if res.is_ok() && st == FPDF_RENDER_FAILED {
                res = Err("PDFium failed to render the page".into());
            }
        }
    }
    (p.FPDFBitmap_Destroy)(bm);
    res
}

/// Load page `index` of `doc`, render it and return (geometry, pixels).
///
/// # Safety
/// As for [`render_loaded_page`]; `doc` must be a valid document handle.
pub unsafe fn render_page(
    p: &Pdfium,
    doc: crate::ffi::FPDF_DOCUMENT,
    index: i32,
    o: &RenderOptions,
) -> Result<(Geometry, Vec<u8>), String> {
    let page = (p.FPDF_LoadPage)(doc, index);
    if page.is_null() {
        return Err(format!("cannot load page {index}"));
    }
    let (l, b, r, t) = crate::glyphs::page_box(p, page);
    let rotation = (p.FPDFPage_GetRotation)(page).rem_euclid(4) * 90;
    let g = geometry((r - l).abs(), (t - b).abs(), rotation, o);
    let mut buf = vec![0u8; g.len()];
    let res = render_loaded_page(p, page, &g, o, &mut buf);
    (p.FPDF_ClosePage)(page);
    res.map(|_| (g, buf))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_not_ceil() {
        let o = RenderOptions::default();
        let g = geometry(612.0, 792.0, 0, &o);
        assert_eq!((g.full_w, g.full_h), (1275, 1650));
        let g = geometry(612.0, 792.0, 90, &o);
        assert_eq!((g.full_w, g.full_h, g.rotate), (1650, 1275, 0));
        let o2 = RenderOptions {
            rotated: false,
            ..o.clone()
        };
        let g = geometry(612.0, 792.0, 90, &o2);
        assert_eq!((g.full_w, g.full_h, g.rotate), (1275, 1650, 3));
        let o3 = RenderOptions {
            clip: Some([100.0, 100.0, 300.0, 200.0]),
            ..o
        };
        let g = geometry(612.0, 792.0, 0, &o3);
        assert_eq!((g.px0, g.py0, g.w, g.h), (208, 208, 417, 209));
    }
}
