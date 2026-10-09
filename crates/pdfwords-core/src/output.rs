// SPDX-License-Identifier: Apache-2.0
//! Output helpers shared by all bindings (words / blocks / text, coordinate transform).
use crate::layout::{BBox, Block, Line};

/// Maps unrotated top-left coordinates to the displayed (rotated) page if requested.
#[derive(Clone, Copy)]
pub struct Xf {
    pub rotation: i32,
    pub w: f64,
    pub h: f64,
    pub on: bool,
}

impl Xf {
    #[inline]
    pub fn pt(&self, x: f64, y: f64) -> (f64, f64) {
        if !self.on || self.rotation == 0 {
            return (x, y);
        }
        match self.rotation {
            90 => (self.h - y, x),
            180 => (self.w - x, self.h - y),
            _ => (y, self.w - x),
        }
    }
    #[inline]
    pub fn rc(&self, b: BBox) -> BBox {
        if !self.on || self.rotation == 0 {
            return b;
        }
        let a = self.pt(b[0], b[1]);
        let c = self.pt(b[2], b[3]);
        [a.0.min(c.0), a.1.min(c.1), a.0.max(c.0), a.1.max(c.1)]
    }
}

pub struct Word {
    pub bbox: BBox,
    pub text: String,
    pub block: usize,
    pub line: usize,
    pub wno: usize,
}

#[inline]
pub fn is_ws(c: char) -> bool {
    matches!(
        c,
        ' ' | '\t' | '\n' | '\r' | '\u{00a0}' | '\u{2000}'
            ..='\u{200a}' | '\u{202f}' | '\u{205f}' | '\u{3000}'
    )
}

pub fn line_text(ln: &Line) -> String {
    ln.chars.iter().map(|c| c.c).collect()
}

fn ends_hyphenated(t: &str) -> bool {
    let mut it = t.chars().rev();
    matches!((it.next(), it.next()), (Some('-'), Some(p)) if p.is_alphabetic())
}

pub fn block_text(b: &Block, dehyphenate: bool) -> String {
    let mut s = String::new();
    for ln in &b.lines {
        let t = line_text(ln);
        if dehyphenate && ends_hyphenated(&t) {
            s.push_str(&t[..t.len() - 1]);
        } else {
            s.push_str(&t);
            s.push('\n');
        }
    }
    s
}

pub fn words(blocks: &[Block], xf: Xf, dehyphenate: bool, delimiters: &str) -> Vec<Word> {
    let mut out: Vec<Word> = Vec::new();
    for b in blocks {
        for (li, ln) in b.lines.iter().enumerate() {
            let mut wn = 0usize;
            let mut start: Option<usize> = None;
            let n = ln.chars.len();
            for i in 0..=n {
                let brk = i == n || {
                    let c = ln.chars[i].c;
                    is_ws(c) || (!delimiters.is_empty() && delimiters.contains(c))
                };
                if brk {
                    if let Some(s) = start.take() {
                        let cs = &ln.chars[s..i];
                        let mut bb = cs[0].bbox;
                        let mut text = String::with_capacity(i - s);
                        for c in cs {
                            bb = [
                                bb[0].min(c.bbox[0]),
                                bb[1].min(c.bbox[1]),
                                bb[2].max(c.bbox[2]),
                                bb[3].max(c.bbox[3]),
                            ];
                            text.push(c.c);
                        }
                        out.push(Word {
                            bbox: xf.rc(bb),
                            text,
                            block: b.number,
                            line: li,
                            wno: wn,
                        });
                        wn += 1;
                    }
                } else if start.is_none() {
                    start = Some(i);
                }
            }
        }
    }
    if dehyphenate {
        let mut merged: Vec<Word> = Vec::with_capacity(out.len());
        for w in out {
            if let Some(last) = merged.last_mut() {
                if ends_hyphenated(&last.text)
                    && last.block == w.block
                    && last.line + 1 == w.line
                    && w.wno == 0
                {
                    last.text.pop();
                    last.text.push_str(&w.text);
                    continue;
                }
            }
            merged.push(w);
        }
        return merged;
    }
    out
}
