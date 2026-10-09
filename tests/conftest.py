# SPDX-License-Identifier: Apache-2.0
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import fetch_test_pdfs  # noqa: E402
import pdfwords  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
BACKENDS = pdfwords.available_backends()


def pdf_path(name):
    """Path of a test PDF; remote ones are downloaded on demand (skip when offline)."""
    p = fetch_test_pdfs.path_of(name)
    if not p:
        pytest.skip(f"{name} not available (offline?) - run tools/fetch_test_pdfs.py")
    return p


@pytest.fixture(params=BACKENDS)
def backend(request, monkeypatch):
    """Run a test against each available backend (rust, python)."""
    monkeypatch.setenv("PDFWORDS_BACKEND", request.param)
    return request.param


def pytest_report_header(config):
    return f"pdfwords backends: {BACKENDS} (PDFWORDS_EXPECT_BACKENDS={os.environ.get('PDFWORDS_EXPECT_BACKENDS')})"
