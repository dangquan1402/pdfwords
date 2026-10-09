# Table detection (0.4)

```python
tabs = page.find_tables()                  # strategy="auto" | "lines" | "text"
for t in tabs:
    t.bbox, t.row_count, t.col_count, t.strategy
    t.extract()        # [[cell text | None (covered by a spanning cell)], ...]
    t.header           # first row
    t.cells            # [{"row", "col", "rowspan", "colspan", "bbox", "text"}, ...]
    t.to_markdown(); t.to_text(); t.to_csv("t.csv"); t.to_pandas(); t.to_dict()
```

```bash
pdfwords tables file.pdf --pages 3 -f md          # json (default) | markdown | csv (-o dir/)
```

`to_markdown()` / `page.get_text("markdown")` / `pdfwords.to_markdown()` use tagged
`Table` elements when the PDF is tagged, and detected tables otherwise.

The algorithm was written from scratch for pdfwords and does not port any other library's code.

1. **Rules.** Stroked lines, thin filled rectangles and rectangle edges from
   `get_drawings()` are snapped (1.5 pt) and joined into maximal horizontal and vertical
   segments.
2. **Regions.** These are connected components of crossing rules (grids), plus stacks of two
   or more horizontal rules with the same extent ("booktabs" / rule-delimited tables). With
   `strategy="text"`, runs of aligned text lines are candidates too.
3. **Rows.** Rows come from the horizontal rules when the rules separate (almost) every text
   row. Otherwise each text line is a row.
4. **Columns.** Columns come from vertical rules plus whitespace corridors, which are x-ranges
   that no word of the body rows covers. Header rows above the first inner rule may span
   columns, and empty columns are dropped. Full grids use their vertical rules only.
5. **Cells.** On ruled grids, units with no rule between them merge into spanning cells, so
   headers like "Percent Fuel Savings" span four columns. Words are assigned to cells by
   their centre.
6. **Validation.** A table needs at least `min_rows` x `min_cols`, at most 20 columns, enough
   filled cells and short cell texts. This rejects prose boxes, chart grids and attention
   visualisations.

## Results on the test documents

| Document | Pages | Tables found | Time (ms/page) |
|---|---|---|---|
| Attention Is All You Need | 15 | 4: all of Tables 1–4, including the 13-column Table 3 with its base-row values | 14.9 |
| ResNet | 12 | 15 (the main tables and the appendix tables) | 11.0 |
| camelot sample | 1 | 1: 7x7 with spanning header | 9.8 |
| IRS W-9 | 6 | 7: form boxes and line-item lists, see the limitations | 6.1 |

Regression tests (`tests/test_tables.py`) pin these cases:

* camelot rows and cells;
* Attention p8 has 13 columns;
* Attention p13 (attention figures) has no tables;
* the ResNet p5 shapes are (11,3), (11,3) and (7,2).

## Limitations

* Rule-delimited (unruled) tables cannot show spanning cells, so a value spanning two
  columns is assigned to the column under its centre.
* In unruled tables, multi-line cells become separate rows.
* Form layouts (W-9 boxes) are detected as tables. Use `min_rows`/`min_cols`/`clip` or the
  tagged structure to filter them.
* Tables drawn as images need OCR plus a layout model. `page.table_cells(boxes)` takes cell
  boxes from such a model.
