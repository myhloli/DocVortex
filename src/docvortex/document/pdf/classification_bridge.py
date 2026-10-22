"""分类读取的同库 ABI 边界；只返回脱离 PDFium 的原始统计快照。"""

from __future__ import annotations

import ctypes as ct
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..._compute_backend import get_native
from .pdfium import pdfium_guard


_REASON = "not probed"
_IMAGE_REASON = "not probed"


def classification_bridge_info():
    """说明原始统计是否使用标准 ABI，不把扩展已安装当作实际走了原生路径。"""
    return {"native_classification_unavailable_reason": _REASON, "native_image_text_unavailable_reason": _IMAGE_REASON}


class NativeClassificationError(RuntimeError):
    """原生统计失败必须向上报告，不由分类器改判为 OCR。"""


def read_classification_snapshot(textpage, count, cjk_ranges, allowed_controls, private_range, normalize_font):
    """保留原文本页所有者与函数强引用，在原生调用前验证 ABI 和分类配置。"""
    global _REASON
    native = get_native()
    _REASON = "nonstandard text page, count, or classification rules"
    if type(textpage) is not pdfium.PdfTextPage or not textpage.raw or type(count) is not int or not 0 <= count < 2**31:
        raise NativeClassificationError(_REASON)
    if type(cjk_ranges) not in (tuple, list) or type(allowed_controls) not in (set, frozenset, tuple, list):
        raise NativeClassificationError(_REASON)
    if any(type(pair) not in (tuple, list) or len(pair) != 2 for pair in cjk_ranges):
        raise NativeClassificationError(_REASON)
    if any(
        type(value) is not int or not 0 <= value < 2**32
        for value in [*private_range, *allowed_controls, *(value for pair in cjk_ranges for value in pair)]
    ):
        raise NativeClassificationError(_REASON)
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
            raise NativeClassificationError(_REASON)
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


def read_image_text_snapshot(textpage, frame, rotation, images, visibility, overlap):
    """核验同库字符 ABI 后批量读取几何，输入契约或 ABI 错误显式失败。"""
    global _IMAGE_REASON
    native = get_native()
    if type(textpage) is not pdfium.PdfTextPage or not textpage.raw:
        _IMAGE_REASON = "nonstandard or closed text page"
        raise NativeClassificationError(_IMAGE_REASON)
    count = textpage.count_chars()
    if type(count) is not int or not 0 <= count < 2**31:
        _IMAGE_REASON = "unsupported character count"
        raise NativeClassificationError(_IMAGE_REASON)
    args = (raw.FPDF_TEXTPAGE, ct.c_int)
    double_pointer = ct.POINTER(ct.c_double)
    specifications = (
        ("FPDFText_GetUnicode", ct.c_uint, args),
        ("FPDFText_GetTextObject", raw.FPDF_PAGEOBJECT, args),
        ("FPDFText_GetCharBox", ct.c_int, (*args, double_pointer, double_pointer, double_pointer, double_pointer)),
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
            _IMAGE_REASON = f"unsupported symbol or ABI: {name}"
            raise NativeClassificationError(_IMAGE_REASON)
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        snapshot = native.read_pdfium_image_text(
            addresses,
            ct.cast(textpage.raw, ct.c_void_p).value,
            count,
            tuple(frame),
            rotation,
            images,
            [(address, value[0], value[1]) for address, value in visibility.items()],
            overlap,
        )
    if snapshot is None:
        _IMAGE_REASON = "invalid image classification geometry"
        raise NativeClassificationError(_IMAGE_REASON)
    _IMAGE_REASON = None
    return snapshot
