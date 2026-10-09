// SPDX-License-Identifier: Apache-2.0
//! pdfwords-core: PyMuPDF-style text extraction (chars/words/lines/blocks with bounding
//! boxes) on top of PDFium. Pure Rust, no Python; bindings live in separate crates.
pub mod ffi;
pub mod glyphs;
pub mod layout;
pub mod order;
pub mod output;
pub mod render;

pub use ffi::Pdfium;
pub use glyphs::{extract_page, open_document, FontInfo, Glyph, PageGlyphs, Timings};
pub use layout::{build_blocks, Block, Char, LayoutParams, Line};
pub use render::{render_page, Geometry, RenderOptions};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sort {
    None,
    Simple,
    XyCut,
}

/// Group a page's glyphs into blocks and apply the requested reading order.
pub fn layout_page(
    pg: &PageGlyphs,
    sort: Sort,
    clip: Option<[f64; 4]>,
    params: &LayoutParams,
) -> Vec<Block> {
    let blocks = build_blocks(pg, clip, params);
    let mut blocks = match sort {
        Sort::None => blocks,
        Sort::Simple => order::sort_simple(blocks),
        Sort::XyCut => order::sort_xycut(blocks, params.xycut_gap_x, params.xycut_gap_y, false),
    };
    for (i, b) in blocks.iter_mut().enumerate() {
        b.number = i;
    }
    blocks
}
