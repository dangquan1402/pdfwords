#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Download third-party test PDFs that are not redistributed in this repository.

They are fetched from their original locations into tests/data/ (git-ignored) and verified
by SHA-256. Tests that need them are skipped when they are missing (e.g. offline CI).

    python tools/fetch_test_pdfs.py          # fetch all
    python tools/fetch_test_pdfs.py --list
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "tests", "data")
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")

# name -> (url, sha256, note)
REMOTE = {
    "arxiv_attention.pdf": ("https://arxiv.org/pdf/1706.03762v7",
                            "bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697",
                            "Vaswani et al., Attention Is All You Need (arXiv:1706.03762v7), 15 pages"),
    "arxiv_resnet.pdf": ("https://arxiv.org/pdf/1512.03385v1",
                         "1e0651b6810ecba34a3dbc5b5b0209226f889004607c1f203540a48d64e5a93a",
                         "He et al., Deep Residual Learning (arXiv:1512.03385v1), 12 pages, 2 columns"),
    "table_camelot.pdf": ("https://raw.githubusercontent.com/camelot-dev/camelot/master/docs/_static/pdf/foo.pdf",
                          "7d6c24e45b38375e322419281cee1335ddb97e00fc86abe9f22afcd8b42e6d82",
                          "camelot documentation sample table, 1 page"),
}
# committed fixtures (see tests/fixtures/README.md for provenance / licences)
LOCAL = ["irs_w9.pdf", "simple_text.pdf", "rot90.pdf"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name, timeout=30.0):
    """Return the local path of a remote test PDF, downloading it if needed; None on failure."""
    url, digest, _ = REMOTE[name]
    path = os.path.join(DATA_DIR, name)
    if os.path.isfile(path) and sha256(path) == digest:
        return path
    if os.environ.get("PDFWORDS_NO_DOWNLOAD"):
        return None
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pdfwords-tests (+https://github.com/dangquan1402/pdfwords)"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            f.write(r.read())
        if sha256(tmp) != digest:
            os.remove(tmp)
            return None
        os.replace(tmp, path)
        return path
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        return None


def path_of(name, download=True):
    """Local fixture path, or remote test PDF path (downloaded on demand), or None."""
    if name in LOCAL:
        return os.path.join(FIXTURE_DIR, name)
    p = os.path.join(DATA_DIR, name)
    if os.path.isfile(p):
        return p
    return fetch(name) if download else None


def main():
    if "--list" in sys.argv:
        for k, (u, _, note) in REMOTE.items():
            print(f"{k:22} {note}\n{'':22} {u}")
        return 0
    ok = True
    for name in REMOTE:
        p = fetch(name)
        print(("ok      " if p else "FAILED  ") + name)
        ok &= bool(p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
