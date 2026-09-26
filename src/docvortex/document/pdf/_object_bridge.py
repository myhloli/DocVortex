"""只借用当前 PDFium 页面和经过 ABI 核验的函数，不加载或缓存原生句柄。"""

import ctypes as ct
from itertools import chain

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..._compute_backend import get_native
from .pdfium import pdfium_guard

_CALLS = 0
_UNAVAILABLE_REASON = "not probed"


def bridge_info():
    """报告真实完成的对象树读取，不能仅凭扩展存在判定原生路径已执行。"""
    return {"pdfium_object_bridge_calls": _CALLS, "pdfium_object_bridge_unavailable_reason": _UNAVAILABLE_REASON}


def read_clipped_objects(page, kind, max_depth):
    """标准运行时在 Rust 内完成树遍历，特殊 ctypes 替身保持 Python 参考路径。"""
    global _CALLS, _UNAVAILABLE_REASON
    native = get_native()
    if native is None or type(page) is not pdfium.PdfPage or not page.raw:
        _UNAVAILABLE_REASON = "python backend or nonstandard/closed page"
        return None
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
        return None
    obj, clip, segment = raw.FPDF_PAGEOBJECT, raw.FPDF_CLIPPATH, raw.FPDF_PATHSEGMENT
    specs = (
        ("FPDFPage_CountObjects", ct.c_int, (raw.FPDF_PAGE,)),
        ("FPDFPage_GetObject", obj, (raw.FPDF_PAGE, ct.c_int)),
        ("FPDFFormObj_CountObjects", ct.c_int, (obj,)),
        ("FPDFFormObj_GetObject", obj, (obj, ct.c_ulong)),
        ("FPDFPageObj_GetMatrix", ct.c_int, (obj, ct.POINTER(raw.FS_MATRIX))),
        ("FPDFPageObj_GetType", ct.c_int, (obj,)),
        ("FPDFPageObj_GetClipPath", clip, (obj,)),
        ("FPDFClipPath_CountPaths", ct.c_int, (clip,)),
        ("FPDFClipPath_CountPathSegments", ct.c_int, (clip, ct.c_int)),
        ("FPDFClipPath_GetPathSegment", segment, (clip, ct.c_int, ct.c_int)),
        ("FPDFPathSegment_GetPoint", ct.c_int, (segment, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float))),
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
            _UNAVAILABLE_REASON = f"unsupported symbol or ABI: {name}"
            return None
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        batches = native.read_pdfium_objects(addresses, ct.cast(page.raw, ct.c_void_p).value, kind, max_depth)
    _CALLS += 1
    _UNAVAILABLE_REASON = None
    return chain.from_iterable(batches)


def read_path_subpaths(raw_object):
    """标准 ABI 使用原生路径解码，替身或未升级扩展保留显式参考路径。"""
    native = get_native()
    if native is None or type(raw_object) is not raw.FPDF_PAGEOBJECT or not raw_object:
        return None
    obj, segment = raw.FPDF_PAGEOBJECT, raw.FPDF_PATHSEGMENT
    specs = (
        ("FPDFPath_CountSegments", ct.c_int, (obj,)),
        ("FPDFPath_GetPathSegment", segment, (obj, ct.c_int)),
        ("FPDFPathSegment_GetPoint", ct.c_int, (segment, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float))),
        ("FPDFPathSegment_GetType", ct.c_int, (segment,)),
        ("FPDFPathSegment_GetClose", ct.c_int, (segment,)),
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
            return None
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        return native.read_pdfium_subpaths(addresses, ct.cast(raw_object, ct.c_void_p).value)
