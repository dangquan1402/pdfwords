//! Pure-Rust benchmark (no Python): cargo run --release --example bench -- <libpdfium> <pdf>...
use pdfwords_core::*;
use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let p = Pdfium::load(&args[1]).expect("load pdfium");
    for path in &args[2..] {
        let mut best = (f64::MAX, Timings::default(), 0u64, 0u64, 0usize, 0i32);
        for _ in 0..5 {
            unsafe {
                let t = Instant::now();
                let doc = open_document(&p, path, None).unwrap();
                let n = (p.FPDF_GetPageCount)(doc);
                let (mut tm, mut lay, mut wds, mut nw) = (Timings::default(), 0u64, 0u64, 0usize);
                for i in 0..n {
                    let pg = extract_page(&p, doc, i, false).unwrap();
                    tm.load_page_ns += pg.timings.load_page_ns;
                    tm.load_text_ns += pg.timings.load_text_ns;
                    tm.chars_ns += pg.timings.chars_ns;
                    tm.close_ns += pg.timings.close_ns;
                    let t1 = Instant::now();
                    let blocks = layout_page(
                        &pg,
                        Sort::None,
                        Some([0.0, 0.0, pg.width, pg.height]),
                        &LayoutParams::default(),
                    );
                    let t2 = Instant::now();
                    let w = output::words(
                        &blocks,
                        output::Xf {
                            rotation: pg.rotation,
                            w: pg.width,
                            h: pg.height,
                            on: false,
                        },
                        false,
                        "",
                    );
                    let t3 = Instant::now();
                    lay += (t2 - t1).as_nanos() as u64;
                    wds += (t3 - t2).as_nanos() as u64;
                    nw += w.len();
                }
                (p.FPDF_CloseDocument)(doc);
                let total = t.elapsed().as_secs_f64();
                if total < best.0 {
                    best = (total, tm, lay, wds, nw, n);
                }
            }
        }
        let (total, tm, lay, wds, nw, n) = best;
        println!(
            "{path}: pages={n} words={nw} total={:.1}ms ({:.2}ms/page) load_page={:.1} load_text={:.1} chars={:.1} close={:.1} layout={:.1} words={:.1} (ms)",
            total * 1e3, total * 1e3 / n as f64,
            tm.load_page_ns as f64 / 1e6, tm.load_text_ns as f64 / 1e6, tm.chars_ns as f64 / 1e6,
            tm.close_ns as f64 / 1e6, lay as f64 / 1e6, wds as f64 / 1e6
        );
    }
}
