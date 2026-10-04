"""Same library ABI boundary for classified reads; only raw statistics snapshots off PDFium are returned."""

from __future__ import annotations

import ctypes as ct
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..._compute_backend import get_native
from .pdfium import pdfium_guard


_REASON = "not probed"


def classification_bridge_info():
    """Indicates whether the original statistics use the standard ABI and do not treat the installed extension as actually taking the native path."""
    return {"native_classification_unavailable_reason": _REASON}


class NativeClassificationError(RuntimeError):
    """Failure of native statistics must be reported upward and will not be changed to OCR by the classifier."""


def read_classification_snapshot(textpage, count, cjk_ranges, allowed_controls, private_range, normalize_font):
    """The original text page owner and function strong reference are retained. Non-standard ABI or special configuration explicitly selects the Python reference path."""
    global _REASON
    native = get_native()
    if native is None:
        _REASON = "Python backend"
        return None
    _REASON = "nonstandard text page, count, or classification rules"
    if type(textpage) is not pdfium.PdfTextPage or not textpage.raw or type(count) is not int or not 0 <= count < 2**31:
        return None
    if type(cjk_ranges) not in (tuple, list) or type(allowed_controls) not in (set, frozenset, tuple, list):
        return None
    if any(type(pair) not in (tuple, list) or len(pair) != 2 for pair in cjk_ranges):
        return None
    if any(
        type(value) is not int or not 0 <= value < 2**32
        for value in [*private_range, *allowed_controls, *(value for pair in cjk_ranges for value in pair)]
    ):
        return None
    args = (raw.FPDF_TEXTPAGE, ct.c_int)
    specifications = (
        ("FPDFText_GetUnicode", ct.c_uint, args),
        ("FPDFText_IsGenerated", ct.c_int, args),
        ("FPDFText_HasUnicodeMapError", ct.c_int, args),
        ("FPDFText_GetFontInfo", ct.c_ulong, (*args, ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_int))),
    )
    functions, addresses = [], []
    for name, result, arguments in specifications:
        function = getattr(raw, name, None)
        if (
            not isinstance(function, ct._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            _REASON = f"unsupported symbol or ABI: {name}"
            return None
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        try:
            snapshot = native.read_pdfium_classification(
                addresses,
                ct.cast(textpage.raw, ct.c_void_p).value,
                count,
                cjk_ranges,
                list(allowed_controls),
                private_range,
                normalize_font,
            )
        except native.PdfiumReadError as exc:
            raise pdfium.PdfiumError(str(exc)) from exc

    _REASON = None
    return snapshot
