// SPDX-License-Identifier: Apache-2.0
//! Minimal dynamically-loaded PDFium C API (only what pdfwords needs).
//!
//! The library is opened with `dlopen` at runtime. When used from Python we open the very
//! same `libpdfium` that pypdfium2 already loaded, so both share one PDFium instance
//! (documents/pages opened by pypdfium2 can be passed to us as raw handles).
#![allow(non_snake_case, non_camel_case_types)]

use libloading::Library;
use std::os::raw::{c_char, c_double, c_float, c_int, c_uint, c_ulong, c_void};

pub type FPDF_DOCUMENT = *mut c_void;
pub type FPDF_PAGE = *mut c_void;
pub type FPDF_TEXTPAGE = *mut c_void;
pub type FPDF_PAGEOBJECT = *mut c_void;
pub type FPDF_FONT = *mut c_void;

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct FS_RECTF {
    pub left: c_float,
    pub top: c_float,
    pub right: c_float,
    pub bottom: c_float,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct FS_MATRIX {
    pub a: c_float,
    pub b: c_float,
    pub c: c_float,
    pub d: c_float,
    pub e: c_float,
    pub f: c_float,
}

macro_rules! pdfium_api {
    ($( $name:ident : fn($($arg:ty),*) $(-> $ret:ty)? ;)*) => {
        /// Table of PDFium function pointers.
        pub struct Pdfium {
            _lib: Library,
            $( pub $name: unsafe extern "C" fn($($arg),*) $(-> $ret)?, )*
        }
        impl Pdfium {
            /// Load PDFium from a shared library path and resolve all symbols.
            pub fn load(path: &str) -> Result<Pdfium, String> {
                unsafe {
                    let lib = Library::new(path).map_err(|e| format!("cannot load {path}: {e}"))?;
                    $(
                        let $name = *lib
                            .get::<unsafe extern "C" fn($($arg),*) $(-> $ret)?>(concat!(stringify!($name), "\0").as_bytes())
                            .map_err(|e| format!("missing symbol {}: {e}", stringify!($name)))?;
                    )*
                    Ok(Pdfium { _lib: lib, $($name,)* })
                }
            }
        }
    };
}

pdfium_api! {
    FPDF_InitLibrary: fn();
    FPDF_LoadDocument: fn(*const c_char, *const c_char) -> FPDF_DOCUMENT;
    FPDF_CloseDocument: fn(FPDF_DOCUMENT);
    FPDF_GetPageCount: fn(FPDF_DOCUMENT) -> c_int;
    FPDF_LoadPage: fn(FPDF_DOCUMENT, c_int) -> FPDF_PAGE;
    FPDF_ClosePage: fn(FPDF_PAGE);
    FPDFPage_GetRotation: fn(FPDF_PAGE) -> c_int;
    FPDFPage_GetCropBox: fn(FPDF_PAGE, *mut c_float, *mut c_float, *mut c_float, *mut c_float) -> c_int;
    FPDFPage_GetMediaBox: fn(FPDF_PAGE, *mut c_float, *mut c_float, *mut c_float, *mut c_float) -> c_int;
    FPDF_GetPageBoundingBox: fn(FPDF_PAGE, *mut FS_RECTF) -> c_int;
    FPDFText_LoadPage: fn(FPDF_PAGE) -> FPDF_TEXTPAGE;
    FPDFText_ClosePage: fn(FPDF_TEXTPAGE);
    FPDFText_CountChars: fn(FPDF_TEXTPAGE) -> c_int;
    FPDFText_GetUnicode: fn(FPDF_TEXTPAGE, c_int) -> c_uint;
    FPDFText_IsGenerated: fn(FPDF_TEXTPAGE, c_int) -> c_int;
    FPDFText_GetCharOrigin: fn(FPDF_TEXTPAGE, c_int, *mut c_double, *mut c_double) -> c_int;
    FPDFText_GetLooseCharBox: fn(FPDF_TEXTPAGE, c_int, *mut FS_RECTF) -> c_int;
    FPDFText_GetTextObject: fn(FPDF_TEXTPAGE, c_int) -> FPDF_PAGEOBJECT;
    FPDFText_GetMatrix: fn(FPDF_TEXTPAGE, c_int, *mut FS_MATRIX) -> c_int;
    FPDFText_GetFontSize: fn(FPDF_TEXTPAGE, c_int) -> c_double;
    FPDFText_GetFontInfo: fn(FPDF_TEXTPAGE, c_int, *mut c_void, c_ulong, *mut c_int) -> c_ulong;
    FPDFText_GetFillColor: fn(FPDF_TEXTPAGE, c_int, *mut c_uint, *mut c_uint, *mut c_uint, *mut c_uint) -> c_int;
    FPDFTextObj_GetFontSize: fn(FPDF_PAGEOBJECT, *mut c_float) -> c_int;
    FPDFTextObj_GetFont: fn(FPDF_PAGEOBJECT) -> FPDF_FONT;
    FPDFFont_GetBaseFontName: fn(FPDF_FONT, *mut c_char, usize) -> usize;
    FPDFFont_GetFlags: fn(FPDF_FONT) -> c_int;
    FPDFFont_GetWeight: fn(FPDF_FONT) -> c_int;
    FPDFFont_GetAscent: fn(FPDF_FONT, c_float, *mut c_float) -> c_int;
    FPDFFont_GetDescent: fn(FPDF_FONT, c_float, *mut c_float) -> c_int;
    FPDFFont_GetItalicAngle: fn(FPDF_FONT, *mut c_int) -> c_int;
    FPDFFont_GetIsEmbedded: fn(FPDF_FONT) -> c_int;
}

// PDFium handles are plain pointers; the library itself is process-global.
unsafe impl Send for Pdfium {}
unsafe impl Sync for Pdfium {}
