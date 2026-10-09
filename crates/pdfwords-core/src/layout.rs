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
    /// PDFium char index (content-stream order); -1 for inserted spaces
    pub idx: i32,
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
    // current line holds RTL chars: weak/neutral chars then follow the RTL rules
    let mut line_rtl = false;
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
                idx: g.idx,
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
                    } else if bidi != 0 || (line_rtl && bidi_kind(g.c) != BidiKind::L) {
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
            line_rtl = false;
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
                idx: -1,
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
            idx: g.idx,
        });
        last_c = Some(g.c);
        last_bidi = bidi;
        line_rtl |= bidi != 0;
        prev_origin = (g.px, g.py);
        pen = (g.qx, g.qy);
    }

    let blocks = merge_vertical(blocks);
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

fn is_cjk(c: char) -> bool {
    let o = c as u32;
    (0x2E80..=0x9FFF).contains(&o)
        || (0xAC00..=0xD7AF).contains(&o)
        || (0xF900..=0xFAFF).contains(&o)
        || (0xFE30..=0xFE4F).contains(&o)
        || (0xFF00..=0xFFEF).contains(&o)
        || (0x20000..=0x3FFFF).contains(&o)
}

fn real_chars(line: &Line) -> impl Iterator<Item = &Char> {
    line.chars.iter().filter(|c| !c.synthetic && c.c != ' ')
}

/// `b` (one CJK glyph) sits directly below the last glyph of `a` (one glyph or a vertical line).
fn stacks(a: &Line, b: &Line) -> bool {
    let na = real_chars(a).count();
    if na == 0 || real_chars(b).count() != 1 || !(a.wmode == 1 || na == 1) {
        return false;
    }
    if a.dir.0 < 0.999 || b.dir.0 < 0.999 {
        return false;
    }
    let ca = real_chars(a).last().unwrap();
    let cb = real_chars(b).next().unwrap();
    if !(is_cjk(ca.c) && is_cjk(cb.c)) {
        return false;
    }
    let s = ca.size.max(cb.size).max(1.0);
    let dy = cb.oy - ca.oy;
    (cb.ox - ca.ox).abs() < 0.3 * s && 0.5 * s < dy && dy < 1.8 * s
}

fn vertical_block(b: &Block) -> bool {
    b.lines.iter().all(|l| l.wmode == 1)
}

/// Vertical writing (CJK, Identity-V fonts): see `_merge_vertical` in the Python package.
fn merge_vertical(mut blocks: Vec<Block>) -> Vec<Block> {
    if !blocks
        .iter()
        .flat_map(|b| b.lines.iter())
        .any(|l| l.chars.first().is_some_and(|c| is_cjk(c.c)))
    {
        return blocks;
    }
    let mut merged = false;
    // (block index, line index) of the previous kept line
    let mut prev: Option<(usize, usize)> = None;
    for bi in 0..blocks.len() {
        let lines = std::mem::take(&mut blocks[bi].lines);
        for ln in lines.into_iter() {
            if ln.chars.is_empty() {
                continue;
            }
            if let Some((pb, pl)) = prev {
                let ok = {
                    let p = if pb == bi {
                        &blocks[bi].lines[pl]
                    } else {
                        &blocks[pb].lines[pl]
                    };
                    stacks(p, &ln)
                };
                if ok {
                    let p = &mut blocks[pb].lines[pl];
                    p.chars.extend(ln.chars);
                    p.wmode = 1;
                    merged = true;
                    continue;
                }
            }
            blocks[bi].lines.push(ln);
            prev = Some((bi, blocks[bi].lines.len() - 1));
        }
    }
    if !merged {
        return blocks;
    }
    let mut out: Vec<Block> = Vec::with_capacity(blocks.len());
    for b in blocks.into_iter() {
        if b.lines.is_empty() {
            continue;
        }
        if let Some(last) = out.last_mut() {
            if vertical_block(last) && vertical_block(&b) {
                let c0 = last.lines.last().unwrap().chars[0];
                let c1 = b.lines[0].chars[0];
                let s = c0.size.max(c1.size).max(1.0);
                if (c0.oy - c1.oy).abs() <= 1.5 * s && (c0.ox - c1.ox).abs() <= 2.5 * s {
                    last.lines.extend(b.lines);
                    continue;
                }
            }
        }
        out.push(b);
    }
    for b in out.iter_mut() {
        if vertical_block(b) {
            b.lines.sort_by(|x, y| {
                y.chars[0]
                    .ox
                    .partial_cmp(&x.chars[0].ox)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        }
    }
    out
}

fn looks_visual(line: &Line) -> bool {
    let rtl: Vec<&Char> = line.chars.iter().filter(|c| c.bidi != 0).collect();
    rtl.len() > 1 && rtl[rtl.len() - 1].ox > rtl[0].ox
}

/// Bidi class used by the UAX #9-lite resolution: strong L / strong R / number / neutral.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum BidiKind {
    L,
    R,
    N,
    O,
}

pub fn bidi_kind(c: char) -> BidiKind {
    match unicode_bidi::bidi_class(c) {
        BidiClass::R | BidiClass::AL => BidiKind::R,
        BidiClass::L => BidiKind::L,
        BidiClass::EN | BidiClass::AN => BidiKind::N,
        _ => BidiKind::O,
    }
}

/// UAX #9-lite on a VISUAL sequence (see `bidi_levels` in the Python package): returns the
/// embedding levels and the paragraph level (1 = RTL when strong RTL chars >= LTR ones).
pub fn bidi_levels(kinds: &[BidiKind]) -> (Vec<u8>, u8) {
    use BidiKind::*;
    let n = kinds.len();
    let nr = kinds.iter().filter(|k| **k == R).count();
    let nl = kinds.iter().filter(|k| **k == L).count();
    let base: u8 = if nr > 0 && nr >= nl { 1 } else { 0 };
    let mut left: Vec<Option<BidiKind>> = vec![None; n];
    let mut right: Vec<Option<BidiKind>> = vec![None; n];
    let mut last = None;
    for i in 0..n {
        left[i] = last;
        if matches!(kinds[i], L | R) {
            last = Some(kinds[i]);
        }
    }
    last = None;
    for i in (0..n).rev() {
        right[i] = last;
        if matches!(kinds[i], L | R) {
            last = Some(kinds[i]);
        }
    }
    let mut strong: Vec<Option<BidiKind>> = vec![None; n];
    for i in 0..n {
        strong[i] = match kinds[i] {
            L | R => Some(kinds[i]),
            N => {
                if left[i] == Some(R)
                    || right[i] == Some(R)
                    || (base == 1 && (left[i].is_none() || right[i].is_none()))
                {
                    Some(R)
                } else {
                    Some(L)
                }
            }
            O => None,
        };
    }
    let sos = if base == 1 { R } else { L };
    let mut lev = vec![0u8; n];
    for i in 0..n {
        lev[i] = match kinds[i] {
            O => {
                let mut j = i as isize - 1;
                while j >= 0 && kinds[j as usize] == O {
                    j -= 1;
                }
                let a = if j >= 0 {
                    strong[j as usize].unwrap_or(sos)
                } else {
                    sos
                };
                let mut k = i + 1;
                while k < n && kinds[k] == O {
                    k += 1;
                }
                let b = if k < n { strong[k].unwrap_or(sos) } else { sos };
                let d = if a == b { a } else { sos };
                if d == R {
                    1
                } else if base == 1 {
                    2
                } else {
                    0
                }
            }
            N => {
                if base == 1 || strong[i] == Some(R) {
                    2
                } else {
                    0
                }
            }
            R => 1,
            L => {
                if base == 1 {
                    2
                } else {
                    0
                }
            }
        };
    }
    (lev, base)
}

/// UAX #9 rule L2 (an involution: maps visual -> logical as well as logical -> visual).
pub fn reorder<T: Clone>(items: &[T], levels: &[u8]) -> Vec<T> {
    let mut items: Vec<T> = items.to_vec();
    let mut lev: Vec<u8> = levels.to_vec();
    let max = lev.iter().copied().max().unwrap_or(0);
    let n = items.len();
    for k in (1..=max).rev() {
        let mut i = 0;
        while i < n {
            if lev[i] >= k {
                let mut j = i;
                while j < n && lev[j] >= k {
                    j += 1;
                }
                items[i..j].reverse();
                lev[i..j].reverse();
                i = j;
            } else {
                i += 1;
            }
        }
    }
    items
}

fn mirror(c: char) -> char {
    match c {
        '(' => ')',
        ')' => '(',
        '[' => ']',
        ']' => '[',
        '{' => '}',
        '}' => '{',
        '<' => '>',
        '>' => '<',
        '\u{ab}' => '\u{bb}',
        '\u{bb}' => '\u{ab}',
        '\u{2039}' => '\u{203a}',
        '\u{203a}' => '\u{2039}',
        _ => c,
    }
}

/// Visual (left -> right) chars -> logical order (UAX #9-lite levels + L2 + L4 mirroring).
fn visual_to_logical(chars: &[Char]) -> Vec<Char> {
    let mut vis: Vec<Char> = chars.to_vec();
    vis.sort_by(|a, b| a.ox.partial_cmp(&b.ox).unwrap_or(std::cmp::Ordering::Equal));
    let kinds: Vec<BidiKind> = vis.iter().map(|c| bidi_kind(c.c)).collect();
    let (lev, _) = bidi_levels(&kinds);
    for (c, l) in vis.iter_mut().zip(lev.iter()) {
        if l % 2 == 1 {
            c.c = mirror(c.c);
        }
    }
    reorder(&vis, &lev)
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

#[cfg(test)]
mod bidi_tests {
    use super::*;

    fn v2l(vis: &str) -> String {
        let chars: Vec<char> = vis.chars().collect();
        let kinds: Vec<BidiKind> = chars.iter().map(|c| bidi_kind(*c)).collect();
        let (lev, _) = bidi_levels(&kinds);
        let m: Vec<char> = chars
            .iter()
            .zip(lev.iter())
            .map(|(c, l)| if l % 2 == 1 { mirror(*c) } else { *c })
            .collect();
        reorder(&m, &lev).into_iter().collect()
    }

    #[test]
    fn visual_to_logical_cases() {
        assert_eq!(v2l("םולש"), "שלום");
        assert_eq!(v2l("123 םולש"), "שלום 123");
        assert_eq!(v2l("abc םולש def"), "abc שלום def");
        assert_eq!(v2l("ןושאר 2 קרפ"), "פרק 2 ראשון");
        assert_eq!(v2l("Hello, םולש!"), "Hello, שלום!");
        assert_eq!(v2l("(םולש)"), "(שלום)");
    }
}
