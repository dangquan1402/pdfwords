# SPDX-License-Identifier: Apache-2.0
"""Byte-faithful PDF content-stream tokenizer and serializer.

Written for redaction: string operands keep their exact bytes (no text decoding), every
operator remembers the byte span it came from so untouched operators are copied verbatim,
and inline images (BI ... ID <binary> EI) are carried through as opaque data.
"""
from __future__ import annotations

import re

WS = b"\x00\t\n\x0c\r "
DELIM = b"()<>[]{}/%"
_ws_re = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)+")
_regular_re = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_num_re = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)$")
_hex_re = re.compile(rb"<([0-9A-Fa-f\x00\t\n\x0c\r ]*)>")
_name_esc_re = re.compile(rb"#([0-9A-Fa-f]{2})")


class Name(str):
    """PDF name operand, stored without the leading slash."""
    __slots__ = ()

    def __repr__(self):
        return "/" + str(self)


class PdfStr(bytes):
    """PDF string operand: exact bytes, plus whether it was written in hex form."""
    hex = False

    def __new__(cls, data, hex=False):  # noqa: A002
        o = super().__new__(cls, data)
        o.hex = hex
        return o


class Op:
    __slots__ = ("op", "args", "start", "end", "changed", "pre")

    def __init__(self, op, args, start, end):
        self.op, self.args, self.start, self.end = op, args, start, end
        self.changed = False
        self.pre = None  # synthetic ops emitted before this one (when changed)

    def __repr__(self):
        return f"Op({self.op!r}, {self.args!r})"


class _Kw(str):
    """Bare keyword inside arrays/dicts (true/false/null or junk)."""


_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
        ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


def _literal(data, i):
    """Parse a literal string starting after '('. Returns (bytes, end_index)."""
    out = bytearray()
    depth = 1
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            c = data[i]
            if c in _ESC:
                out += _ESC[c]
                i += 1
            elif 0x30 <= c <= 0x37:
                j = i
                while j < n and j < i + 3 and 0x30 <= data[j] <= 0x37:
                    j += 1
                out.append(int(data[i:j], 8) & 0xFF)
                i = j
            elif c == 0x0D:  # line continuation
                i += 1
                if i < n and data[i] == 0x0A:
                    i += 1
            elif c == 0x0A:
                i += 1
            else:
                out.append(c)
                i += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out.append(c)
        i += 1
    return bytes(out), n


def _number(tok):
    if _num_re.match(tok):
        if b"." in tok:
            return float(tok)
        return int(tok)
    return None


class _Parser:
    def __init__(self, data):
        self.d = data
        self.n = len(data)

    def skip(self, i):
        m = _ws_re.match(self.d, i)
        return m.end() if m else i

    def obj(self, i):
        """Parse one object at i (whitespace already skipped). Returns (value, end, is_operator)."""
        d = self.d
        c = d[i]
        if c == 0x2F:  # /
            m = _regular_re.match(d, i + 1)
            raw = m.group(0) if m else b""
            j = i + 1 + len(raw)
            if b"#" in raw:
                raw = _name_esc_re.sub(lambda mm: bytes([int(mm.group(1), 16)]), raw)
            return Name(raw.decode("latin-1")), j, False
        if c == 0x28:
            s, j = _literal(d, i + 1)
            return PdfStr(s), j, False
        if c == 0x3C:
            if d[i + 1:i + 2] == b"<":
                return self.dict_(i + 2)
            m = _hex_re.match(d, i)
            if not m:
                j = d.find(b">", i)
                j = self.n if j < 0 else j + 1
                return PdfStr(b""), j, False
            h = re.sub(rb"[\x00\t\n\x0c\r ]", b"", m.group(1))
            if len(h) % 2:
                h += b"0"
            return PdfStr(bytes.fromhex(h.decode()), hex=True), m.end(), False
        if c == 0x5B:  # [
            arr = []
            j = i + 1
            while True:
                j = self.skip(j)
                if j >= self.n:
                    return arr, j, False
                if d[j] == 0x5D:
                    return arr, j + 1, False
                v, j, isop = self.obj(j)
                arr.append(v)
        if c in b"]>)}{":  # stray delimiter: treat as a one-byte operator (garbage tolerant)
            return d[i:i + 1], i + 1, True
        m = _regular_re.match(d, i)
        tok = m.group(0)
        num = _number(tok)
        if num is not None:
            return num, m.end(), False
        if tok == b"true":
            return True, m.end(), False
        if tok == b"false":
            return False, m.end(), False
        if tok == b"null":
            return None, m.end(), False
        return tok, m.end(), True

    def dict_(self, i):
        d = self.d
        out = {}
        key = None
        j = i
        while True:
            j = self.skip(j)
            if j >= self.n:
                return out, j, False
            if d[j:j + 2] == b">>":
                return out, j + 2, False
            v, j, isop = self.obj(j)
            if key is None:
                key = v if isinstance(v, Name) else Name(str(v))
            else:
                out[key] = _Kw(v.decode("latin-1")) if isop else v
                key = None


def parse(data: bytes):
    """Tokenize a content stream into a list of Op (operator bytes, operand list, byte span)."""
    p = _Parser(data)
    ops = []
    args = []
    start = None
    i = p.skip(0)
    n = len(data)
    while i < n:
        if start is None:
            start = i
        v, j, isop = p.obj(i)
        if isop:
            if v == b"BI":
                op, j = _inline_image(p, j, start)
                ops.append(op)
            else:
                ops.append(Op(v, args, start, j))
            args = []
            start = None
        else:
            args.append(v)
        i = p.skip(j)
    return ops


def _inline_image(p, j, start):
    d = p.d
    params = {}
    key = None
    while True:
        j = p.skip(j)
        if j >= p.n:
            return Op(b"BI", [params, b""], start, j), j
        v, j2, isop = p.obj(j)
        if isop and v == b"ID":
            j = j2
            break
        if key is None:
            key = v if isinstance(v, Name) else Name(str(v))
        else:
            params[key] = v.decode("latin-1") if isop else v
            key = None
        j = j2
    if j < p.n and d[j] in WS:
        j += 1  # single whitespace after ID
    # data ends at "EI" preceded by whitespace and followed by whitespace/EOF
    length = params.get("L", params.get("Length"))
    k = j + length if isinstance(length, int) and 0 <= length and j + length <= p.n else j
    while True:
        m = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r ]|$)").search(d, max(k - 1, j))
        if not m:
            return Op(b"BI", [params, d[j:]], start, p.n), p.n
        data = d[j:m.start()]
        end = m.end()
        # sanity: what follows should look like an operator stream, not more binary data
        tail = d[end:end + 32]
        if not tail.strip() or re.match(rb"[\x00\t\n\x0c\r ]*(?:[A-Za-z'\"*]{1,3}|[\d.+-/\[(<%])", tail):
            return Op(b"BI", [params, data], start, end), end
        k = m.end()


# ---------------------------------------------------------------------- serialize
def _fmt_num(x):
    if isinstance(x, bool):
        return b"true" if x else b"false"
    if isinstance(x, int):
        return str(x).encode()
    if x != x or x in (float("inf"), float("-inf")):
        return b"0"
    s = repr(float(x))
    if "e" in s or "E" in s:
        s = f"{x:.12f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return (s if s not in ("", "-0", "-") else "0").encode()


def _fmt_name(n):
    out = bytearray(b"/")
    for b in str(n).encode("latin-1", "replace"):
        if b < 0x21 or b > 0x7E or b in DELIM or b == 0x23:
            out += b"#%02X" % b
        else:
            out.append(b)
    return bytes(out)


def _fmt_str(s):
    if getattr(s, "hex", False):
        return b"<" + bytes(s).hex().upper().encode() + b">"
    out = bytearray(b"(")
    for b in bytes(s):
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b == 0x0D:
            out += b"\\r"
        elif b == 0x0A:
            out += b"\\n"
        else:
            out.append(b)
    out += b")"
    return bytes(out)


def fmt(v):
    if v is None:
        return b"null"
    if isinstance(v, (bool, int, float)):
        return _fmt_num(v)
    if isinstance(v, Name):
        return _fmt_name(v)
    if isinstance(v, _Kw):
        return v.encode("latin-1")
    if isinstance(v, (PdfStr, bytes)):
        return _fmt_str(v)
    if isinstance(v, str):
        return _fmt_name(v)
    if isinstance(v, list):
        return b"[" + b" ".join(fmt(x) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b" ".join(fmt(Name(k)) + b" " + fmt(x) for k, x in v.items()) + b">>"
    raise TypeError(type(v))


def fmt_op(op: Op) -> bytes:
    if op.op == b"BI":
        params, data = op.args
        body = b" ".join(fmt(Name(k)) + b" " + (v.encode("latin-1") if isinstance(v, str) and not isinstance(v, Name) else fmt(v))
                         for k, v in params.items())
        return b"BI " + body + b" ID " + data + b"\nEI"
    return b" ".join([fmt(a) for a in op.args] + [op.op])


def serialize(data: bytes, ops) -> bytes:
    """Rebuild a stream: unchanged ops are copied from `data` byte-for-byte, changed ops are
    re-serialized, ops with op.op is None are dropped."""
    out = []
    for o in ops:
        if o.op is None:
            continue
        if o.changed or o.start is None:
            for p in o.pre or ():
                out.append(fmt_op(p))
            out.append(fmt_op(o))
        else:
            out.append(data[o.start:o.end])
    return b"\n".join(out) + b"\n"
