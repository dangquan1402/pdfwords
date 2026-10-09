# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from pdfwords import _libpath


def test_finds_loaded_library():
    p = _libpath.find_pdfium_library()
    assert os.path.isfile(p) and "pdfium" in os.path.basename(p).lower()


def test_loaded_matches_packaged():
    loaded, packaged = _libpath._loaded_library(), _libpath._packaged_library()
    if loaded and packaged:
        assert os.path.samefile(loaded, packaged)


def test_env_override(monkeypatch, tmp_path):
    real = _libpath.find_pdfium_library()
    monkeypatch.setenv(_libpath.ENV_VAR, real)
    assert _libpath.find_pdfium_library() == real
    monkeypatch.setenv(_libpath.ENV_VAR, str(tmp_path / "missing.so"))
    with pytest.raises(_libpath.PdfiumLibraryNotFound):
        _libpath.find_pdfium_library()
