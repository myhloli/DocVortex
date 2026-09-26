"""核验标准 ABI 后直接创建 Rust 自有文本快照，不先提取 Python 字符字典。"""

from __future__ import annotations

import ctypes as ct
import math

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..._compute_backend import get_native
from .pdfium import pdfium_guard
from .text.extract import _native_visibility_supported

_CALLS = 0
_EMPTY = 0
_REASON = "not probed"


def snapshot_bridge_info():
    """报告真实原生快照创建和明确参考选择，空页不伪报字符 FFI 已执行。"""
    return {
        "native_text_snapshot_calls": _CALLS,
        "native_text_snapshot_empty_pages": _EMPTY,
        "native_text_snapshot_unavailable_reason": _REASON,
    }


def read_text_snapshot(textpage, frame, rotation, extended, visibility=None):
    """在原锁和 textpage 生命周期内读取、规范化与去重，标准运算错误直接传播。"""
    global _CALLS, _EMPTY, _REASON
    native = get_native()
    if native is None or not hasattr(native, "read_pdfium_text_snapshot"):
        _REASON = "Python backend or native text snapshot unavailable"
        return None
    if (
        type(textpage) is not pdfium.PdfTextPage
        or not textpage.raw
        or type(extended) is not bool
        or type(rotation) is not int
        or rotation not in (0, 90, 180, 270)
        or type(frame) is not list
        or len(frame) != 4
        or any(type(value) is not float or not math.isfinite(value) for value in frame)
        or not _native_visibility_supported(visibility)
        or visibility is not None
        and 0 in visibility
    ):
        _REASON = "nonstandard text page, numerical metadata, or visibility"
        return None
    if ct.sizeof(raw.FS_RECTF) != 16 or any(
        getattr(raw.FS_RECTF, name).offset != offset for name, offset in (("left", 0), ("top", 4), ("right", 8), ("bottom", 12))
    ):
        _REASON = "FS_RECTF ABI mismatch"
        return None
    args = (raw.FPDF_TEXTPAGE, ct.c_int)
    double_pointer, uint_pointer = ct.POINTER(ct.c_double), ct.POINTER(ct.c_uint)
    specs = (
        ("FPDFText_GetUnicode", ct.c_uint, args),
        ("FPDFText_GetCharAngle", ct.c_float, args),
        ("FPDFText_GetLooseCharBox", ct.c_int, (*args, ct.POINTER(raw.FS_RECTF))),
        ("FPDFText_GetCharBox", ct.c_int, (*args, double_pointer, double_pointer, double_pointer, double_pointer)),
        ("FPDFText_GetFontInfo", ct.c_ulong, (*args, ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_int))),
        ("FPDFText_GetFontSize", ct.c_double, args),
        ("FPDFText_GetFontWeight", ct.c_int, args),
        ("FPDFText_GetTextObject", raw.FPDF_PAGEOBJECT, args),
        ("FPDFTextObj_GetTextRenderMode", ct.c_int, (raw.FPDF_PAGEOBJECT,)),
        ("FPDFText_GetCharOrigin", ct.c_int, (*args, double_pointer, double_pointer)),
        ("FPDFText_GetFillColor", ct.c_int, (*args, uint_pointer, uint_pointer, uint_pointer, uint_pointer)),
        ("FPDFText_GetStrokeColor", ct.c_int, (*args, uint_pointer, uint_pointer, uint_pointer, uint_pointer)),
    )
    functions, addresses = [], []
    for name, result, arguments in specs:
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
    # 函数强引用和 textpage 在整个读取阶段存活，产物不保存任何 PDFium 地址。
    packed_visibility = (
        {0 if key is None else key: value for key, value in visibility.items()} if visibility is not None else None
    )
    with pdfium_guard():
        count = textpage.count_chars()
        if count < 0:
            _REASON = "negative character count"
            return None
        try:
            snapshot = native.read_pdfium_text_snapshot(
                addresses[:10],
                addresses[10:],
                ct.cast(textpage.raw, ct.c_void_p).value,
                count,
                extended,
                frame,
                rotation,
                packed_visibility,
            )
        except native.PdfiumReadError as exc:
            raise pdfium.PdfiumError(str(exc)) from exc
    if snapshot is None:
        _REASON = "raw PDF numerical metadata outside native canonical admission"
        return None
    if count:
        _CALLS += 1
    else:
        _EMPTY += 1
    _REASON = None
    return snapshot
