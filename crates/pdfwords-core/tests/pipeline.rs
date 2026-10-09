// SPDX-License-Identifier: Apache-2.0
//! Unit-level checks of grouping helpers (no PDFium needed) and an end-to-end test that runs when
//! `PDFWORDS_PDFIUM_LIB` points at a PDFium shared library (e.g. pypdfium2's libpdfium).
use pdfwords_core::layout::{Block, Char, Line};
use pdfwords_core::output::{self, Xf};
use pdfwords_core::*;

fn ch(c: char, x0: f64, y0: f64, x1: f64, y1: f64) -> Char {
    Char {
        c,
        ox: x0,
        oy: y1,
        bbox: [x0, y0, x1, y1],
        size: 10.0,
        font: 0,
        color: 0,
        synthetic: false,
        bidi: 0,
        idx: 0,
    }
}

fn line(text: &str, x: f64, y: f64) -> Line {
    let chars: Vec<Char> = text
        .chars()
        .enumerate()
        .map(|(i, c)| ch(c, x + 5.0 * i as f64, y, x + 5.0 * (i + 1) as f64, y + 10.0))
        .collect();
    let bbox = [x, y, x + 5.0 * text.chars().count() as f64, y + 10.0];
    Line {
        chars,
        dir: (1.0, 0.0),
        wmode: 0,
        bbox,
        visual_rtl: false,
    }
}

fn block(lines: Vec<Line>, number: usize) -> Block {
    let mut bb = lines[0].bbox;
    for l in &lines {
        bb = [
            bb[0].min(l.bbox[0]),
            bb[1].min(l.bbox[1]),
            bb[2].max(l.bbox[2]),
            bb[3].max(l.bbox[3]),
        ];
    }
    Block {
        lines,
        bbox: bb,
        number,
    }
}

#[test]
fn words_and_dehyphenation() {
    let b = block(
        vec![line("good exam-", 0.0, 0.0), line("ple here", 0.0, 12.0)],
        0,
    );
    let xf = Xf {
        rotation: 0,
        w: 600.0,
        h: 800.0,
        on: false,
    };
    let w = output::words(std::slice::from_ref(&b), xf, false, "");
    let t: Vec<&str> = w.iter().map(|w| w.text.as_str()).collect();
    assert_eq!(t, ["good", "exam-", "ple", "here"]);
    assert_eq!((w[2].block, w[2].line, w[2].wno), (0, 1, 0));
    assert_eq!(w[0].bbox, [0.0, 0.0, 20.0, 10.0]);
    let d = output::words(std::slice::from_ref(&b), xf, true, "");
    let t: Vec<&str> = d.iter().map(|w| w.text.as_str()).collect();
    assert_eq!(t, ["good", "example", "here"]);
    assert_eq!(output::block_text(&b, true), "good example here\n");
}

#[test]
fn rotation_transform() {
    let xf = Xf {
        rotation: 90,
        w: 600.0,
        h: 800.0,
        on: true,
    };
    assert_eq!(xf.pt(10.0, 20.0), (780.0, 10.0));
    assert_eq!(xf.rc([10.0, 20.0, 30.0, 40.0]), [760.0, 10.0, 780.0, 30.0]);
}

#[test]
fn xycut_reads_columns_first() {
    // two columns, written row-wise in the content stream
    let blocks = vec![
        block(vec![line("left one", 50.0, 100.0)], 0),
        block(vec![line("right one", 320.0, 100.0)], 1),
        block(vec![line("left two", 50.0, 200.0)], 2),
        block(vec![line("right two", 320.0, 200.0)], 3),
    ];
    let p = LayoutParams::default();
    let sorted = order::sort_xycut(blocks, p.xycut_gap_x, p.xycut_gap_y, false);
    let first: Vec<String> = sorted
        .iter()
        .map(|b| output::line_text(&b.lines[0]))
        .collect();
    assert_eq!(first, ["left one", "left two", "right one", "right two"]);
}

#[test]
fn default_params_are_sane() {
    let p = LayoutParams::default();
    assert!(p.word_gap < p.column_gap);
    assert!(p.baseline_tol < p.block_gap);
}

#[test]
fn end_to_end_with_pdfium() {
    let Ok(lib) = std::env::var("PDFWORDS_PDFIUM_LIB") else {
        eprintln!("PDFWORDS_PDFIUM_LIB not set: skipping PDFium end-to-end test");
        return;
    };
    let p = Pdfium::load(&lib).expect("load PDFium");
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/simple_text.pdf"
    );
    unsafe {
        let doc = open_document(&p, path, None).expect("open");
        assert_eq!((p.FPDF_GetPageCount)(doc), 2);
        let pg = extract_page(&p, doc, 0, false).expect("page");
        let blocks = layout_page(
            &pg,
            Sort::XyCut,
            Some([0.0, 0.0, pg.width, pg.height]),
            &LayoutParams::default(),
        );
        let xf = Xf {
            rotation: pg.rotation,
            w: pg.width,
            h: pg.height,
            on: false,
        };
        let words = output::words(&blocks, xf, false, "");
        let text: Vec<&str> = words.iter().take(3).map(|w| w.text.as_str()).collect();
        assert_eq!(text, ["Simple", "test", "document"]);
        assert!(words.iter().any(|w| w.text == "Efficient"));
        for w in &words {
            assert!(w.bbox[0] < w.bbox[2] && w.bbox[1] < w.bbox[3]);
        }
        (p.FPDF_CloseDocument)(doc);
    }
}
