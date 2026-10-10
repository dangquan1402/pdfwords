# SPDX-License-Identifier: Apache-2.0
"""Structure-preserving PDF size optimiser: ``pdfwords.optimize()``, ``Document.optimize()`` and
``pdfwords shrink``. Needs the optional extra: ``pip install "pdfwords[shrink]"``.

Text, vector graphics, fonts, links, annotations, form fields, outlines and tags are kept; only
images are re-encoded (resampled to a target DPI, JPEG, gray, CCITT G4 / JBIG2 for 1-bit, MRC for
scans), duplicate streams are merged and the file is re-written with object streams. The result is
never larger than the input. See docs/SHRINK.md.
"""
from __future__ import annotations

try:
    import pikepdf  # noqa: F401
    from PIL import Image  # noqa: F401
    import numpy  # noqa: F401
except ImportError as e:  # pragma: no cover
    raise ImportError("pdfwords.shrink needs the 'shrink' extra: pip install \"pdfwords[shrink]\" "
                      f"(missing: {e.name})") from e

from ._codecs import available as available_codecs
from ._core import PRESETS, OptimizeReport, Preset, optimize

__all__ = ["optimize", "OptimizeReport", "Preset", "PRESETS", "available_codecs"]
