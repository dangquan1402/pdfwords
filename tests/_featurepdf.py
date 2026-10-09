# SPDX-License-Identifier: Apache-2.0
"""A small hand-written PDF exercising links, annotations, form fields, the outline,
hyphenation and superscripts (no third-party writer needed)."""


def _pdf(objs):
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    return bytes(out)


def _stream(data, extra=b""):
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def feature_pdf():
    p1 = (b"BT /F1 12 Tf 72 720 Td (Click here for the docs.) Tj ET\n"
          b"BT /F1 12 Tf 72 700 Td (Visit https://example.com/page today.) Tj ET\n"
          b"BT /F1 12 Tf 72 680 Td (Go to the second page now.) Tj ET\n"
          b"BT /F1 12 Tf 72 660 Td (Highlighted words here.) Tj ET\n"
          b"BT /F1 12 Tf 72 640 Td (E = mc) Tj /F1 7 Tf 37.68 4 Td (2) Tj /F1 12 Tf 8 -4 Td (holds.) Tj ET\n"
          b"BT /F1 12 Tf 72 600 Td (Name:) Tj ET\n")
    p2 = (b"BT /F1 12 Tf 72 720 Td (This line ends with a hyphen-) Tj 0 -14 Td (ation and continues.) Tj ET\n"
          b"BT /F1 12 Tf 72 680 Td (A well-known fact.) Tj ET\n")
    ap = b"/Tx BMC BT /F1 10 Tf 2 5 Td (John Doe) Tj ET EMC"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Outlines 13 0 R /AcroForm << /Fields [10 0 R 12 0 R] "
        b"/DR << /Font << /F1 5 0 R >> >> /DA (/F1 10 Tf 0 g) >> >>",                          # 1
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",                                     # 2
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Contents 6 0 R /Annots [8 0 R 9 0 R 7 0 R 10 0 R 12 0 R] >>",                      # 3
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Contents 15 0 R >>",                                                                # 4
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",   # 5
        _stream(p1),                                                                            # 6
        # 7: highlight over "Highlighted"
        b"<< /Type /Annot /Subtype /Highlight /Rect [72 656 140 672] /QuadPoints [72 672 140 672 72 656 140 656] "
        b"/Contents (Check this) /T (Reviewer) /NM (hl-1) /C [1 1 0] /F 4 >>",
        # 8: URI link over "Click here"
        b"<< /Type /Annot /Subtype /Link /Rect [72 716 128 732] /Border [0 0 0] "
        b"/A << /S /URI /URI (https://pdfwords.dev/docs) >> >>",
        # 9: internal link over "second page" -> page 2 at y=700
        b"<< /Type /Annot /Subtype /Link /Rect [124 676 194 692] /Border [0 0 0] /Dest [4 0 R /XYZ 72 700 0] >>",
        # 10: text field with value + appearance
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /TU (Your name) /V (John Doe) /Rect [110 595 260 612] "
        b"/P 3 0 R /F 4 /DA (/F1 10 Tf 0 g) /AP << /N 11 0 R >> >>",
        _stream(ap, b"/Type /XObject /Subtype /Form /BBox [0 0 150 17] /Resources << /Font << /F1 5 0 R >> >>"),  # 11
        # 12: checked check box
        b"<< /Type /Annot /Subtype /Widget /FT /Btn /T (agree) /V /Yes /AS /Yes /Rect [72 570 84 582] /P 3 0 R /F 4 "
        b"/AP << /N << /Yes 16 0 R /Off 16 0 R >> >> >>",
        b"<< /Type /Outlines /First 14 0 R /Last 17 0 R /Count 2 >>",                           # 13
        b"<< /Title (Introduction) /Parent 13 0 R /Next 17 0 R /Dest [3 0 R /XYZ 0 792 0] >>",  # 14
        _stream(p2),                                                                            # 15
        _stream(b"0 0 12 12 re S", b"/Type /XObject /Subtype /Form /BBox [0 0 12 12]"),         # 16
        b"<< /Title (Hyphenation) /Parent 13 0 R /Prev 14 0 R /Dest [4 0 R /XYZ 0 792 0] >>",   # 17
    ]
    return _pdf(objs)


def text_pdf(content, pages=1):
    """Pages with the given content stream and Helvetica as /F1."""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages]
    font_no = 3 + 2 * pages
    for i in range(pages):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> "
                    b"/Contents %d 0 R >>" % (font_no, 4 + 2 * i))
        objs.append(_stream(content.replace(b"{n}", str(i).encode())))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    return _pdf(objs)


def tagged_pdf():
    """A tagged page whose structure order differs from the geometric order:
    H1 "Annual Report" (top), then P "Alpha comes first logically." drawn LOWER (y=600) than
    P "Beta is drawn higher on the page." (y=650); a list (L/LI x2), a 2x2 table (Table/TR/TD)
    with ruling lines, an image (Figure), a page-number footer marked /Artifact."""
    content = (
        b"/H1 <</MCID 0>> BDC BT /F2 20 Tf 72 720 Td (Annual Report) Tj ET EMC\n"
        b"/P <</MCID 2>> BDC BT /F1 11 Tf 72 650 Td (Beta is drawn higher on the page.) Tj ET EMC\n"
        b"/P <</MCID 1>> BDC BT /F1 11 Tf 72 600 Td (Alpha comes first logically.) Tj ET EMC\n"
        b"/LI <</MCID 3>> BDC BT /F1 11 Tf 72 560 Td (- First item) Tj ET EMC\n"
        b"/LI <</MCID 4>> BDC BT /F1 11 Tf 72 545 Td (- Second item) Tj ET EMC\n"
        b"/Artifact BMC 0.5 w 72 480 m 312 480 l S 72 455 m 312 455 l S 72 430 m 312 430 l S "
        b"72 480 m 72 430 l S 192 480 m 192 430 l S 312 480 m 312 430 l S EMC\n"
        b"/TD <</MCID 5>> BDC BT /F2 11 Tf 80 463 Td (Year) Tj ET EMC\n"
        b"/TD <</MCID 6>> BDC BT /F2 11 Tf 200 463 Td (Revenue) Tj ET EMC\n"
        b"/TD <</MCID 7>> BDC BT /F1 11 Tf 80 438 Td (2025) Tj ET EMC\n"
        b"/TD <</MCID 8>> BDC BT /F1 11 Tf 200 438 Td (42) Tj ET EMC\n"
        b"/Figure <</MCID 9>> BDC q 100 0 0 50 400 600 cm /Im1 Do Q EMC\n"
        b"/Artifact BMC BT /F1 9 Tf 300 40 Td (1) Tj ET EMC\n")
    objs = {}
    objs[1] = (b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 6 0 R /MarkInfo << /Marked true >> "
               b"/Lang (en-US) >>")
    objs[2] = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    objs[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /StructParents 0 "
               b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> /XObject << /Im1 30 0 R >> >> /Contents 31 0 R >>")
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    objs[5] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    objs[6] = b"<< /Type /StructTreeRoot /K [7 0 R] /ParentTree 32 0 R >>"
    objs[7] = b"<< /Type /StructElem /S /Document /P 6 0 R /K [8 0 R 9 0 R 10 0 R 11 0 R 14 0 R 21 0 R] >>"
    elem = b"<< /Type /StructElem /S /%s /P %d 0 R /Pg 3 0 R /K %s >>"
    objs[8] = elem % (b"H1", 7, b"0")
    objs[9] = elem % (b"P", 7, b"1")       # Alpha (lower on the page) first
    objs[10] = elem % (b"P", 7, b"2")      # Beta second
    objs[11] = elem % (b"L", 7, b"[12 0 R 13 0 R]")
    objs[12] = elem % (b"LI", 11, b"3")
    objs[13] = elem % (b"LI", 11, b"4")
    objs[14] = elem % (b"Table", 7, b"[15 0 R 16 0 R]")
    objs[15] = elem % (b"TR", 14, b"[17 0 R 18 0 R]")
    objs[16] = elem % (b"TR", 14, b"[19 0 R 20 0 R]")
    objs[17] = elem % (b"TH", 15, b"5")
    objs[18] = elem % (b"TH", 15, b"6")
    objs[19] = elem % (b"TD", 16, b"7")
    objs[20] = elem % (b"TD", 16, b"8")
    objs[21] = (b"<< /Type /StructElem /S /Figure /P 7 0 R /Pg 3 0 R /K 9 /Alt (A red square) >>")
    for i in range(22, 30):
        objs[i] = b"null"
    px = b"\xff\x00\x00" * 4
    objs[30] = _stream(px, b"/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
                           b"/BitsPerComponent 8")
    objs[31] = _stream(content)
    objs[32] = (b"<< /Nums [0 [8 0 R 9 0 R 10 0 R 12 0 R 13 0 R 17 0 R 18 0 R 19 0 R 20 0 R 21 0 R]] >>")
    return _pdf([objs[i] for i in range(1, 33)])


def tables_pdf():
    """Page 1: a ruled 4x3 grid whose header cell "Savings" spans two columns, and a
    rule-delimited ("booktabs") 3-column table; page 2: prose between two rules (no table)."""
    def t(x, y, s, font=b"/F1 10 Tf"):
        return b"BT " + font + b" %g %g Td (%s) Tj ET\n" % (x, y, s)
    g = b"0.5 w "
    # grid: x = 72, 172, 272, 372 ; y = 700, 680, 660, 640, 620
    for y in (700, 680, 660, 640, 620):
        g += b"72 %d m 372 %d l S " % (y, y)
    g += b"72 620 m 72 700 l S 172 620 m 172 700 l S 372 620 m 372 700 l S 272 620 m 272 680 l S\n"
    p1 = g
    p1 += t(80, 686, b"Name") + t(240, 686, b"Savings")
    p1 += t(180, 666, b"Speed") + t(280, 666, b"Idle")
    for i, (a, b_, c) in enumerate([(b"alpha", b"5.9%", b"17.4%"), (b"beta", b"2.4%", b"2.7%")]):
        y = 646 - 20 * i
        p1 += t(80, y, a) + t(180, y, b_) + t(280, y, c)
    # booktabs: rules at 500, 482, 420 (pt), columns at 80 / 200 / 300
    p1 += b"1 w 72 500 m 372 500 l S 0.5 w 72 482 m 372 482 l S 1 w 72 420 m 372 420 l S\n"
    p1 += t(80, 488, b"Model") + t(200, 488, b"BLEU") + t(300, 488, b"Cost")
    for i, (a, b_, c) in enumerate([(b"ByteNet", b"23.75", b"1.0e20"), (b"GNMT + RL", b"24.6", b"2.3e19"),
                                    (b"Transformer", b"28.4", b"2.3e19")]):
        y = 468 - 16 * i
        p1 += t(80, y, a) + t(200, y, b_) + t(300, y, c)
    p2 = b"0.5 w 72 700 m 540 700 l S 72 600 m 540 600 l S\n"
    for i, line in enumerate([b"This is ordinary prose between two horizontal rules, set in one",
                              b"column with normal word spacing, which must not be detected as",
                              b"a table even though the lines start at the same position and",
                              b"the rules above and below have the same extent."]):
        p2 += t(72, 680 - 14 * i, line)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> "
            b"/Contents 4 0 R >>",
            _stream(p1),
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> "
            b"/Contents 6 0 R >>",
            _stream(p2),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    return _pdf(objs)


def rtl_pdf():
    """Hebrew/Latin/digit lines drawn in VISUAL order (left to right), as many producers do,
    with a Type3 font whose ToUnicode maps codes to the characters. Logical texts:
    1 "שלום עולם" (RTL), 2 "פרק 2 ראשון" (RTL + number), 3 "abc שלום def" (LTR + RTL word),
    4 "(שלום)" (RTL + mirrored brackets)."""
    alphabet = "שלוםערקפאןנabcdef0123456789 ()"
    code = {ch: 0x21 + i for i, ch in enumerate(alphabet)}
    visual = ["םלוע םולש", "ןושאר 2 קרפ", "abc םולש def", "(םולש)"]
    content = b""
    for i, line in enumerate(visual):
        s = bytes(code[c] for c in line)
        esc = s.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        content += b"BT /T3 12 Tf 72 %d Td (" % (700 - 30 * i) + esc + b") Tj ET\n"
    first, last = 0x21, 0x21 + len(alphabet) - 1
    procs = b" ".join(b"/g%d %d 0 R" % (i, 7 + i) for i in range(len(alphabet)))
    diffs = b" ".join(b"/g%d" % i for i in range(len(alphabet)))
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T3 def "
            b"1 begincodespacerange <00> <FF> endcodespacerange %d beginbfchar\n" % len(alphabet))
    for ch, c in code.items():
        cmap += b"<%02X> <%04X>\n" % (c, ord(ch))
    cmap += b"endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
    widths = b" ".join(b"600" if ch != " " else b"300" for ch in alphabet)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /T3 5 0 R >> >> "
        b"/Contents 4 0 R >>",
        _stream(content),
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 600 700] /FontMatrix [0.001 0 0 0.001 0 0] "
        b"/CharProcs << " + procs + b" >> /Encoding << /Type /Encoding /Differences [%d " % first + diffs
        + b"] >> /FirstChar %d /LastChar %d /Widths [" % (first, last) + widths + b"] /ToUnicode 6 0 R "
        b"/Resources << >> >>",
        _stream(cmap),
    ]
    for ch in alphabet:
        w = 300 if ch == " " else 600
        glyph = b"%d 0 0 0 %d 700 d1" % (w, w) + (b"" if ch == " " else b" 50 0 500 700 re f")
        objs.append(_stream(glyph))
    return _pdf(objs)


def vertical_cjk_pdf(cols=("縦書きの文章", "二行目です。"), horiz=("横書きの行", "次")):
    """Japanese vertical writing: a Type0 font with /Identity-V (non-embedded Adobe-Japan1 CID font,
    default vertical metrics DW2 [880 -1000]) drawing two columns right to left, plus a horizontal
    (/Identity-H) two-line paragraph whose second line is a single glyph."""
    chars = sorted(set("".join(cols) + "".join(horiz)))
    cid = {c: i + 1 for i, c in enumerate(chars)}

    def hexs(t):
        return b"".join(b"%04X" % cid[c] for c in t)

    content = b""
    x = 400
    for col in cols:
        content += b"BT /V 20 Tf %d 700 Td <" % x + hexs(col) + b"> Tj ET\n"
        x -= 30
    content += b"BT /H 20 Tf 72 300 Td <" + hexs(horiz[0]) + b"> Tj 0 -26 Td <" + hexs(horiz[1]) + b"> Tj ET\n"
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /U def "
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange %d beginbfchar\n" % len(chars))
    for c, i in cid.items():
        cmap += b"<%04X> <%04X>\n" % (i, ord(c))
    cmap += b"endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /V 5 0 R /H 9 0 R >> >> "
        b"/Contents 4 0 R >>",
        _stream(content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiMin-W3 /Encoding /Identity-V /DescendantFonts [6 0 R] "
        b"/ToUnicode 7 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiMin-W3 /CIDSystemInfo << /Registry (Adobe) "
        b"/Ordering (Japan1) /Supplement 2 >> /FontDescriptor 8 0 R /DW 1000 /DW2 [880 -1000] >>",
        _stream(cmap),
        b"<< /Type /FontDescriptor /FontName /HeiseiMin-W3 /Flags 4 /FontBBox [0 -141 1000 859] /ItalicAngle 0 "
        b"/Ascent 859 /Descent -141 /CapHeight 700 /StemV 80 >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiMin-W3 /Encoding /Identity-H /DescendantFonts [6 0 R] "
        b"/ToUnicode 7 0 R >>",
    ]
    return _pdf(objs)
