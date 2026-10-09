// SPDX-License-Identifier: Apache-2.0
//! Stage 3: reading order. Mirrors `pdfwords/order.py`.
use crate::layout::{BBox, Block};
use std::cmp::Ordering;

fn cmp2(a: (f64, f64), b: (f64, f64)) -> Ordering {
    a.partial_cmp(&b).unwrap_or(Ordering::Equal)
}

/// PyMuPDF `sort=True`: lines by (y1, x0) inside blocks, blocks by (y1, x0).
pub fn sort_simple(mut blocks: Vec<Block>) -> Vec<Block> {
    for b in blocks.iter_mut() {
        b.lines
            .sort_by(|p, q| cmp2((p.bbox[3], p.bbox[0]), (q.bbox[3], q.bbox[0])));
    }
    blocks.sort_by(|p, q| cmp2((p.bbox[3], p.bbox[0]), (q.bbox[3], q.bbox[0])));
    blocks
}

type Item = (usize, BBox);

fn gaps(mut iv: Vec<(f64, f64)>, lo: f64, _hi: f64, min_gap: f64) -> Vec<(f64, f64)> {
    iv.sort_by(|a, b| cmp2(*a, *b));
    let mut out = Vec::new();
    let mut cur = lo;
    for (a, b) in iv {
        if a - cur >= min_gap && cur > lo {
            out.push((cur, a));
        }
        cur = cur.max(b);
    }
    out
}

fn bounds(items: &[Item]) -> BBox {
    let mut r = [
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    ];
    for (_, b) in items {
        r[0] = r[0].min(b[0]);
        r[1] = r[1].min(b[1]);
        r[2] = r[2].max(b[2]);
        r[3] = r[3].max(b[3]);
    }
    r
}

fn xcut(items: &[Item], min_gap: f64) -> Vec<(f64, f64)> {
    let b = bounds(items);
    gaps(
        items.iter().map(|(_, b)| (b[0], b[2])).collect(),
        b[0],
        b[2],
        min_gap,
    )
}

fn split_by(items: &[Item], g: &[(f64, f64)], x_axis: bool) -> Vec<Vec<Item>> {
    let (lo, hi) = if x_axis { (0, 2) } else { (1, 3) };
    let mut groups: Vec<Vec<Item>> = vec![Vec::new(); g.len() + 1];
    for it in items {
        let c = (it.1[lo] + it.1[hi]) / 2.0;
        let k = g.iter().filter(|gg| c > gg.0).count();
        groups[k].push(*it);
    }
    groups.into_iter().filter(|v| !v.is_empty()).collect()
}

fn sorted_yx(items: &[Item]) -> Vec<Item> {
    let mut v = items.to_vec();
    v.sort_by(|a, b| cmp2((a.1[1], a.1[0]), (b.1[1], b.1[0])));
    v
}

fn xycut(items: &[Item], mgx: f64, mgy: f64, rtl: bool, depth: u32) -> Vec<Item> {
    if items.len() <= 1 || depth > 50 {
        return sorted_yx(items);
    }
    let xg = xcut(items, mgx);
    if !xg.is_empty() {
        let mut cols = split_by(items, &xg, true);
        if rtl {
            cols.reverse();
        }
        let mut out = Vec::with_capacity(items.len());
        for c in cols {
            out.extend(xycut(&c, mgx, mgy, rtl, depth + 1));
        }
        return out;
    }
    let b = bounds(items);
    let yg = gaps(
        items.iter().map(|(_, b)| (b[1], b[3])).collect(),
        b[1],
        b[3],
        mgy,
    );
    if yg.is_empty() {
        let mut v = items.to_vec();
        v.sort_by(|a, b| {
            cmp2(
                (a.1[1].round_ties_even(), a.1[0]),
                (b.1[1].round_ties_even(), b.1[0]),
            )
        });
        return v;
    }
    let strips = split_by(items, &yg, false);
    let mut merged: Vec<Vec<Item>> = vec![strips[0].clone()];
    for s in strips.into_iter().skip(1) {
        let prev = merged.last().unwrap();
        let mut both: Vec<Item> = prev.clone();
        both.extend_from_slice(&s);
        if !xcut(prev, mgx).is_empty() && !xcut(&s, mgx).is_empty() && !xcut(&both, mgx).is_empty()
        {
            *merged.last_mut().unwrap() = both;
        } else {
            merged.push(s);
        }
    }
    if merged.len() == 1 && merged[0].len() == items.len() && xcut(items, mgx).is_empty() {
        return sorted_yx(items);
    }
    let mut out = Vec::with_capacity(items.len());
    for s in merged {
        if s.len() == items.len() {
            return sorted_yx(items);
        }
        out.extend(xycut(&s, mgx, mgy, rtl, depth + 1));
    }
    out
}

/// Column-aware XY-cut for horizontal blocks; rotated blocks appended by (x0, y0).
pub fn sort_xycut(blocks: Vec<Block>, min_gap_x: f64, min_gap_y: f64, rtl: bool) -> Vec<Block> {
    let is_h = |b: &Block| !b.lines.is_empty() && b.lines[0].dir.0 > 0.99;
    let items: Vec<Item> = blocks
        .iter()
        .enumerate()
        .filter(|(_, b)| is_h(b))
        .map(|(i, b)| (i, b.bbox))
        .collect();
    let mut other: Vec<usize> = (0..blocks.len()).filter(|&i| !is_h(&blocks[i])).collect();
    other.sort_by(|&a, &b| {
        cmp2(
            (blocks[a].bbox[0], blocks[a].bbox[1]),
            (blocks[b].bbox[0], blocks[b].bbox[1]),
        )
    });
    let mut order: Vec<usize> = if items.is_empty() {
        Vec::new()
    } else {
        xycut(&items, min_gap_x, min_gap_y, rtl, 0)
            .into_iter()
            .map(|(i, _)| i)
            .collect()
    };
    order.extend(other);
    let mut slots: Vec<Option<Block>> = blocks.into_iter().map(Some).collect();
    order
        .into_iter()
        .map(|i| slots[i].take().unwrap())
        .collect()
}
