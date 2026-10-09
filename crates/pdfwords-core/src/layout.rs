// SPDX-License-Identifier: Apache-2.0
//! Stage 2: pen-tracking state machine (glyphs -> blocks/lines/chars).
//! Same algorithm and thresholds as the Python backend (`python/pdfwords/layout.py`).

use crate::glyphs::{FontInfo, Glyph, PageGlyphs, TEXT_FONT_SUPERSCRIPT};
use unicode_bidi::BidiClass;
use unicode_general_category::{get_general_category, GeneralCategory};

/// Grouping thresholds. Distances are in multiples of the font size unless noted.
/// Defaults were derived by `tools/tune_thresholds.py` (see `docs/THRESHOLDS.md`) and must
/// stay identical to `LayoutParams` in the Python package.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LayoutParams {
    /// forward gap >= this inserts a word space (below: kerning)
    pub word_gap: f64,
    /// forward gap >= this ends the line (table cells, columns)
    pub column_gap: f64,
    /// baseline shift < this keeps the glyph on the same line
    pub baseline_tol: f64,
    /// baseline shift > this starts a new block
    pub block_gap: f64,
    /// same char drawn again closer than this -> overprint (fake bold), dropped
    pub dup_dist: f64,
    /// lowest gap accepted as a word break when PDFium reports a generated space
    pub gen_space_min: f64,
    /// line start right of the previous line start by more than this (points) -> new block
    pub indent_pt: f64,
    /// baseline raised by more than this -> superscript flag
    pub superscript_rise: f64,
    /// XY-cut: min vertical gutter (points)
    pub xycut_gap_x: f64,
    /// XY-cut: min horizontal gap (points)
    pub xycut_gap_y: f64,
}

impl Default for LayoutParams {
    fn default() -> Self {
        LayoutParams {
            word_gap: 0.14,
            column_gap: 0.85,
            baseline_tol: 0.85,
            block_gap: 1.5,
            dup_dist: 0.1,
            gen_space_min: -0.46,
            indent_pt: 4.1,
            superscript_rise: 0.21,
            xycut_gap_x: 12.5,
            xycut_gap_y: 2.0,
        }
    }
}

pub type BBox = [f64; 4];

#[derive(Clone, Copy, Debug)]
pub struct Char {
    pub c: char,
    pub ox: f64,
    pub oy: f64,
    pub bbox: BBox,
    pub size: f64,
    pub font: u32,
    pub color: u32,
    pub synthetic: bool,
    pub bidi: u8,
}

#[derive(Clone, Debug)]
pub struct Line {
    pub chars: Vec<Char>,
    pub dir: (f64, f64),
    pub wmode: i32,
    pub bbox: BBox,
    pub visual_rtl: bool,
}

#[derive(Clone, Debug)]
pub struct Block {
    pub lines: Vec<Line>,
    pub bbox: BBox,
    pub number: usize,
}

fn is_bullet(c: char) -> bool {
    matches!(
        c,
        '*' | '\u{00b7}'
            | '\u{2022}'
            | '\u{2023}'
            | '\u{2043}'
            | '\u{204c}'
            | '\u{204d}'
            | '\u{2219}'
            | '\u{25c9}'
            | '\u{25cb}'
            | '\u{25cf}'
            | '\u{25d8}'
            | '\u{25e6}'
            | '\u{2619}'
            | '\u{261a}'
            | '\u{261b}'
            | '\u{261c}'
            | '\u{261d}'
            | '\u{261e}'
            | '\u{261f}'
            | '\u{2765}'
            | '\u{2767}'
            | '\u{29be}'
            | '\u{29bf}'
            | '\u{25aa}'
            | '\u{25ab}'
            | '\u{25a0}'
            | '\u{25a1}'
            | '\u{2013}'
            | '\u{2014}'
            | '-'
    )
}

#[inline]
fn space_allowed_after(last: Option<char>) -> bool {
    match last {
        None | Some(' ') => false,
        Some(c) => {
            let o = c as u32;
            o < 0x700 || (0x2000..=0x20CF).contains(&o)
        }
    }
}

#[inline]
pub fn is_rtl(c: char) -> bool {
    c >= '\u{0590}' && matches!(unicode_bidi::bidi_class(c), BidiClass::R | BidiClass::AL)
}

#[inline]
fn is_mn(c: char) -> bool {
    (c as u32) >= 0x300 && get_general_category(c) == GeneralCategory::NonspacingMark
}

#[inline]
#[allow(clippy::too_many_arguments)]
pub fn char_box(
    px: f64,
    py: f64,
    qx: f64,
    qy: f64,
    dx: f64,
    dy: f64,
    size: f64,
    f: &FontInfo,
) -> BBox {
    let (asc, dsc) = (f.asc_n, f.desc_n);
    if dy == 0.0 && dx > 0.0 {
        return [px, py - asc * size, qx, py - dsc * size];
    }
    let (ux, uy) = (dy, -dx);
    let (ax, ay) = (ux * asc * size, uy * asc * size);
    let (bx, by) = (ux * dsc * size, uy * dsc * size);
    let xs = [px + ax, px + bx, qx + ax, qx + bx];
    let ys = [py + ay, py + by, qy + ay, qy + by];
    [
        xs.iter().cloned().fold(f64::INFINITY, f64::min),
        ys.iter().cloned().fold(f64::INFINITY, f64::min),
        xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        ys.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
    ]
}

#[inline]
pub fn union(a: Option<BBox>, b: BBox) -> BBox {
    match a {
        None => b,
        Some(a) => [
            a[0].min(b[0]),
            a[1].min(b[1]),
            a[2].max(b[2]),
            a[3].max(b[3]),
        ],
    }
}

// Python's min()/max() keep the first argument on ties and never produce -0.0 surprises;
// f64::min/max are equivalent for finite non-NaN input.

pub fn build_blocks(pg: &PageGlyphs, clip: Option<BBox>, params: &LayoutParams) -> Vec<Block> {
    let (word_gap, column_gap, baseline_tol) =
        (params.word_gap, params.column_gap, params.baseline_tol);
    let (block_gap, dup_dist) = (params.block_gap, params.dup_dist);
    let fonts = &pg.fonts;
    let mut blocks: Vec<Block> = Vec::new();
    let mut have_line = false;
    let mut pen = (0.0f64, 0.0f64);
    let mut prev_origin = (0.0f64, 0.0f64);
    let mut last_c: Option<char> = None;
    let mut last_bidi: u8 = 0;
    let mut start_x = 0.0f64;
    let mut starts_with_bullet = false;

    for g in pg.glyphs.iter() {
        let g: &Glyph = g;
        let f = &fonts[g.font as usize];
        let mut bx = char_box(g.px, g.py, g.qx, g.qy, g.dx, g.dy, g.size, f);
        if let Some(cl) = clip {
            if bx[2] < cl[0] || bx[0] > cl[2] || bx[3] < cl[1] || bx[1] > cl[3] {
                continue;
            }
        }
        let size = if g.size != 0.0 { g.size } else { 1.0 };
        let bidi: u8 = if is_rtl(g.c) { 1 } else { 0 };
        if have_line && (g.cont || is_mn(g.c)) {
            let mut origin = (g.px, g.py);
            if g.cont {
                bx = char_box(pen.0, pen.1, pen.0, pen.1, g.dx, g.dy, g.size, f);
                origin = pen;
            }
            let line = blocks.last_mut().unwrap().lines.last_mut().unwrap();
            line.chars.push(Char {
                c: g.c,
                ox: origin.0,
                oy: origin.1,
                bbox: bx,
                size: g.size,
                font: g.font,
                color: g.color,
                synthetic: false,
                bidi,
            });
            last_c = Some(g.c);
            continue;
        }

        let mut start_block = true;
        let mut start_line = true;
        let mut insert_space = false;
        if have_line {
            let ldir = blocks.last().unwrap().lines.last().unwrap().dir;
            if ldir.0 * g.dx + ldir.1 * g.dy >= 0.999 {
                if last_c == Some(g.c)
                    && (g.px - prev_origin.0).hypot(g.py - prev_origin.1) / size < dup_dist
                {
                    continue;
                }
                let ls = size;
                let (ddx, ddy) = (g.px - pen.0, g.py - pen.1);
                let spacing = (g.dx * ddx + g.dy * ddy) / size;
                let shift = (-g.dy * ddx + g.dx * ddy) / ls;
                start_block = false;
                if shift.abs() < baseline_tol {
                    if bidi != last_bidi {
                        start_line = false;
                    } else if bidi != 0 {
                        let adv = (g.qx - g.px).hypot(g.qy - g.py) / size;
                        let lsp = (g.dx * (g.px - prev_origin.0) + g.dy * (g.py - prev_origin.1))
                            / size
                            + adv;
                        let line = blocks.last_mut().unwrap().lines.last_mut().unwrap();
                        if lsp.abs() < word_gap {
                            start_line = false;
                        } else if spacing.abs() < word_gap {
                            start_line = false;
                            line.visual_rtl = true;
                        } else if -column_gap < lsp && lsp < 0.0 {
                            insert_space = space_allowed_after(last_c);
                            start_line = false;
                        } else if -column_gap < spacing && spacing < 0.0 {
                            start_line = false;
                        } else if 0.0 < spacing && spacing < column_gap {
                            line.visual_rtl = true;
                            insert_space = space_allowed_after(last_c);
                            start_line = false;
                        }
                    } else if g.gen_space && params.gen_space_min < spacing && spacing < column_gap
                    {
                        insert_space = space_allowed_after(last_c);
                        start_line = false;
                    } else if spacing.abs() < word_gap || (-column_gap < spacing && spacing < 0.0) {
                        start_line = false;
                    } else if 0.0 < spacing && spacing < column_gap {
                        insert_space = space_allowed_after(last_c);
                        start_line = false;
                    }
                } else if shift.abs() <= block_gap {
                    if (g.px - start_x) > params.indent_pt && !starts_with_bullet {
                        start_block = true;
                    }
                } else {
                    start_block = true;
                }
            }
        }

        if start_block || blocks.is_empty() {
            blocks.push(Block {
                lines: Vec::new(),
                bbox: [0.0; 4],
                number: 0,
            });
            have_line = false;
        }
        let blk = blocks.last_mut().unwrap();
        if start_line || !have_line {
            blk.lines.push(Line {
                chars: Vec::new(),
                dir: (g.dx, g.dy),
                wmode: 0,
                bbox: [0.0; 4],
                visual_rtl: false,
            });
            have_line = true;
            start_x = g.px;
            starts_with_bullet = is_bullet(g.c);
        }
        let line = blk.lines.last_mut().unwrap();
        if insert_space && g.c != ' ' {
            let sb = char_box(pen.0, pen.1, g.px, g.py, g.dx, g.dy, g.size, f);
            line.chars.push(Char {
                c: ' ',
                ox: pen.0,
                oy: pen.1,
                bbox: sb,
                size: g.size,
                font: g.font,
                color: g.color,
                synthetic: true,
                bidi,
            });
        }
        line.chars.push(Char {
            c: g.c,
            ox: g.px,
            oy: g.py,
            bbox: bx,
            size: g.size,
            font: g.font,
            color: g.color,
            synthetic: false,
            bidi,
        });
        last_c = Some(g.c);
        last_bidi = bidi;
        prev_origin = (g.px, g.py);
        pen = (g.qx, g.qy);
    }

    let mut out: Vec<Block> = Vec::with_capacity(blocks.len());
    for mut b in blocks.into_iter() {
        b.lines.retain(|l| !l.chars.is_empty());
        let mut bb: Option<BBox> = None;
        for ln in b.lines.iter_mut() {
            if ln.visual_rtl || (ln.chars.iter().any(|c| c.bidi != 0) && looks_visual(ln)) {
                ln.chars = visual_to_logical(&ln.chars);
            }
            let (mut x0, mut y0, mut x1, mut y1) = (
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            );
            for ch in &ln.chars {
                let a = ch.bbox;
                if a[0] < x0 {
                    x0 = a[0];
                }
                if a[1] < y0 {
                    y0 = a[1];
                }
                if a[2] > x1 {
                    x1 = a[2];
                }
                if a[3] > y1 {
                    y1 = a[3];
                }
            }
            ln.bbox = [x0, y0, x1, y1];
            bb = Some(union(bb, ln.bbox));
        }
        if let Some(bb) = bb {
            b.bbox = bb;
            out.push(b);
        }
    }
    for (i, b) in out.iter_mut().enumerate() {
        b.number = i;
    }
    out
}

fn looks_visual(line: &Line) -> bool {
    let rtl: Vec<&Char> = line.chars.iter().filter(|c| c.bidi != 0).collect();
    rtl.len() > 1 && rtl[rtl.len() - 1].ox > rtl[0].ox
}

fn visual_to_logical(chars: &[Char]) -> Vec<Char> {
    let mut sorted: Vec<Char> = chars.to_vec();
    sorted.sort_by(|a, b| a.ox.partial_cmp(&b.ox).unwrap_or(std::cmp::Ordering::Equal));
    let mut runs: Vec<(Option<bool>, Vec<Char>)> = Vec::new();
    let mut cur: Vec<Char> = Vec::new();
    let mut cur_rtl: Option<bool> = None;
    for c in sorted {
        let r = if !c.c.is_whitespace() {
            Some(c.bidi != 0)
        } else {
            cur_rtl
        };
        if !cur.is_empty() && r != cur_rtl {
            runs.push((cur_rtl, std::mem::take(&mut cur)));
        }
        cur.push(c);
        cur_rtl = r;
    }
    if !cur.is_empty() {
        runs.push((cur_rtl, cur));
    }
    let mut res = Vec::with_capacity(chars.len());
    for (rtl, run) in runs.into_iter().rev() {
        if rtl == Some(true) {
            res.extend(run.into_iter().rev());
        } else {
            res.extend(run);
        }
    }
    res
}

#[inline]
pub fn is_superscript(line: &Line, ch: &Char, rise: f64) -> bool {
    if line.dir.0 < 0.999 {
        return false;
    }
    ch.oy < line.chars[0].oy - ch.size * rise
}

pub struct Span {
    pub start: usize,
    pub end: usize,
    pub font: u32,
    pub size: f64,
    pub flags: u32,
    pub color: u32,
    pub bbox: BBox,
}

/// Python `round(x, 3)` equivalent for grouping keys (correctly rounded decimal).
fn round3_key(x: f64) -> String {
    format!("{:.3}", x)
}

pub fn make_spans(line: &Line, fonts: &[FontInfo], params: &LayoutParams) -> Vec<Span> {
    let mut spans: Vec<Span> = Vec::new();
    let mut key: Option<(&str, f64, u32, u32)> = None;
    for (i, ch) in line.chars.iter().enumerate() {
        let f = &fonts[ch.font as usize];
        let flags = f.flags
            | if is_superscript(line, ch, params.superscript_rise) {
                TEXT_FONT_SUPERSCRIPT
            } else {
                0
            };
        let same = match key {
            Some((n, s, fl, co)) => {
                n == f.name.as_str()
                    && fl == flags
                    && co == ch.color
                    && (s == ch.size || round3_key(s) == round3_key(ch.size))
            }
            None => false,
        };
        if !same {
            spans.push(Span {
                start: i,
                end: i + 1,
                font: ch.font,
                size: ch.size,
                flags,
                color: ch.color,
                bbox: ch.bbox,
            });
            key = Some((f.name.as_str(), ch.size, flags, ch.color));
        } else {
            let sp = spans.last_mut().unwrap();
            sp.end = i + 1;
            sp.bbox = union(Some(sp.bbox), ch.bbox);
        }
    }
    spans
}
