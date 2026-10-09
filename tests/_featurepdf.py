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
