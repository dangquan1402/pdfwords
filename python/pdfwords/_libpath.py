# SPDX-License-Identifier: Apache-2.0
"""Locate the PDFium shared library that pypdfium2 has loaded.

The native backend must use the *same* PDFium instance as pypdfium2 (documents are opened by
pypdfium2 and their handles passed to Rust), so we ask the OS which file the already-loaded
symbol ``FPDF_InitLibrary`` lives in, rather than relying on pypdfium2 internals.

Resolution order:
1. ``$PDFWORDS_PDFIUM_LIB`` (explicit override; must be the library pypdfium2 uses)
2. the file containing pypdfium2_raw's loaded ``FPDF_InitLibrary`` (dladdr / GetModuleHandleExW)
3. the shared library shipped inside the ``pypdfium2_raw`` package directory
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import sys

ENV_VAR = "PDFWORDS_PDFIUM_LIB"
_NAMES = ("pdfium.dll", "libpdfium.dylib", "libpdfium.so")


class PdfiumLibraryNotFound(RuntimeError):
    pass


def _fn_address():
    import pypdfium2_raw
    return ctypes.cast(pypdfium2_raw.FPDF_InitLibrary, ctypes.c_void_p).value


def _module_of_address_windows(addr):
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetModuleHandleExW.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HMODULE)]
    k32.GetModuleHandleExW.restype = wintypes.BOOL
    k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetModuleFileNameW.restype = wintypes.DWORD
    hmod = wintypes.HMODULE()
    # GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT
    if not k32.GetModuleHandleExW(0x4 | 0x2, ctypes.c_void_p(addr), ctypes.byref(hmod)):
        return None
    buf = ctypes.create_unicode_buffer(32768)
    n = k32.GetModuleFileNameW(hmod, buf, len(buf))
    return buf.value if n else None


class _DlInfo(ctypes.Structure):
    _fields_ = [("dli_fname", ctypes.c_char_p), ("dli_fbase", ctypes.c_void_p),
                ("dli_sname", ctypes.c_char_p), ("dli_saddr", ctypes.c_void_p)]


def _module_of_address_posix(addr):
    candidates = [None]  # the running process (glibc >= 2.34, macOS: dladdr is in libc)
    dl = ctypes.util.find_library("dl")
    if dl:
        candidates.append(dl)
    for c in candidates:
        try:
            dladdr = ctypes.CDLL(c).dladdr
        except (OSError, AttributeError):
            continue
        dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
        dladdr.restype = ctypes.c_int
        info = _DlInfo()
        if dladdr(ctypes.c_void_p(addr), ctypes.byref(info)) and info.dli_fname:
            return os.fsdecode(info.dli_fname)
    return None


def _loaded_library():
    try:
        addr = _fn_address()
        f = _module_of_address_windows(addr) if sys.platform == "win32" else _module_of_address_posix(addr)
    except Exception:
        return None
    return f if f and os.path.isfile(f) else None


def _packaged_library():
    try:
        import pypdfium2_raw
    except ImportError:
        return None
    d = os.path.dirname(os.path.abspath(pypdfium2_raw.__file__))
    for n in _NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    hits = glob.glob(os.path.join(d, "*pdfium*.*"))
    hits = [h for h in hits if h.endswith((".so", ".dylib", ".dll"))]
    return hits[0] if hits else None


def find_pdfium_library() -> str:
    """Return the path of the PDFium shared library to bind the native backend to."""
    env = os.environ.get(ENV_VAR)
    if env:
        if not os.path.isfile(env):
            raise PdfiumLibraryNotFound(f"${ENV_VAR}={env!r} does not exist")
        return env
    for fn in (_loaded_library, _packaged_library):
        p = fn()
        if p:
            return p
    raise PdfiumLibraryNotFound(
        "could not locate the PDFium shared library used by pypdfium2. "
        f"Set ${ENV_VAR} to its path (e.g. .../site-packages/pypdfium2_raw/libpdfium.so), "
        "or use backend='python'.")
