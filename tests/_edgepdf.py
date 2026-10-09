# SPDX-License-Identifier: Apache-2.0
"""Build a small PDF exercising content-stream constructs that redaction must handle."""
import io

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                           NumberObject)


def _stream(data, **kw):
    s = DecodedStreamObject()
    s.set_data(data)
    for k, v in kw.items():
        s[NameObject("/" + k)] = v
    return s


def edge_pdf() -> bytes:
    w = PdfWriter()
    font = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),  # no /Widths: standard-14 metrics
    }))
    res_font = DictionaryObject({NameObject("/F1"): font})
    form = w._add_object(_stream(
        b"BT /F1 12 Tf 0 0 Td (FormSecret FormKeep) Tj ET",
        Type=NameObject("/XObject"), Subtype=NameObject("/Form"),
        BBox=ArrayObject([FloatObject(v) for v in (0, -5, 300, 20)]),
        Resources=DictionaryObject({NameObject("/Font"): res_font})))
    img = w._add_object(_stream(
        b"\xff\x00\x00" * 50, Type=NameObject("/XObject"), Subtype=NameObject("/Image"),
        Width=NumberObject(10), Height=NumberObject(5), ColorSpace=NameObject("/DeviceRGB"),
        BitsPerComponent=NumberObject(8)))
    res = DictionaryObject({
        NameObject("/Font"): res_font,
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): form, NameObject("/Im1"): img}),
    })
    s1 = (b"q 0.95 g 0 0 612 792 re f Q\n"
          b"BT /F1 12 Tf 72 700 Td [(Hel) -200 (lo World)] TJ\n")
    s2 = (b"0 -20 Td (Second) Tj 14 TL (Third line) ' 2 1 (Fourth line) \" ET\n"
          b"/Span <</ActualText (ActualSecret)>> BDC BT /F1 12 Tf 72 600 Td (Hidden) Tj ET EMC\n"
          b"q 1 0 0 1 72 500 cm /Fm1 Do Q\n"
          b"q 100 0 0 50 300 400 cm /Im1 Do Q\n"
          b"q 0 0 1 rg 72 300 50 20 re f Q\n"
          b"q 30 0 0 30 400 300 cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI Q\n")
    for contents in ([s1, s2], [b"q 1 0 0 1 72 500 cm /Fm1 Do Q\n"]):
        page = w.add_blank_page(612, 792)
        page[NameObject("/Resources")] = res
        page[NameObject("/Contents")] = ArrayObject([w._add_object(_stream(c)) for c in contents])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
