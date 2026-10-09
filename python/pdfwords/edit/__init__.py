# SPDX-License-Identifier: Apache-2.0
"""Editing support for pdfwords (optional extra: ``pip install "pdfwords[edit]"``).

Content access, true redaction and text insertion are implemented on pypdf (BSD-3-Clause)
plus fontTools (MIT) for font embedding and Pillow (MIT-CMU) for image pixel redaction.
The Document/Page methods in :mod:`pdfwords` call into this package lazily.
"""
from ._redact import (IMAGE_NONE, IMAGE_PIXELS, IMAGE_REMOVE, LINE_ART_NONE, LINE_ART_REMOVE_IF_COVERED,  # noqa: F401
                      LINE_ART_REMOVE_IF_TOUCHED, TEXT_NONE, TEXT_REMOVE)


class RedactionError(RuntimeError):
    """Text that should have been redacted is still extractable after apply_redactions()."""


class RedactionWarning(UserWarning):
    """Redaction succeeded but moved/removed characters outside the redaction areas."""


def _editor_cls():
    try:
        import pypdf  # noqa: F401
    except ImportError as e:
        raise ImportError('editing needs pypdf: pip install "pdfwords[edit]"') from e
    from ._doc import Editor
    return Editor
