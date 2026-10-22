"""只借用当前 PDFium 页面和经过 ABI 核验的函数，不加载或缓存原生句柄。"""

import ctypes as ct
from itertools import chain

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..._compute_backend import get_native
from .pdfium import pdfium_guard

_CALLS = 0
_UNAVAILABLE_REASON = "not probed"
_DRAWING_CALLS = 0
_DRAWING_UNAVAILABLE_REASON = "not probed"
_PATH_EVIDENCE_CALLS = 0
_PATH_EVIDENCE_UNAVAILABLE_REASON = "not probed"
_TEXT_VISIBILITY_CALLS = 0
_TEXT_VISIBILITY_UNAVAILABLE_REASON = "not probed"


def bridge_info():
    """报告真实完成的对象树读取，不能仅凭扩展存在判定原生路径已执行。"""
    return {
        "pdfium_object_bridge_calls": _CALLS,
        "pdfium_object_bridge_unavailable_reason": _UNAVAILABLE_REASON,
        "pdfium_drawing_line_bridge_calls": _DRAWING_CALLS,
        "pdfium_drawing_line_bridge_unavailable_reason": _DRAWING_UNAVAILABLE_REASON,
        "pdfium_path_evidence_bridge_calls": _PATH_EVIDENCE_CALLS,
        "pdfium_path_evidence_bridge_unavailable_reason": _PATH_EVIDENCE_UNAVAILABLE_REASON,
        "pdfium_text_visibility_bridge_calls": _TEXT_VISIBILITY_CALLS,
        "pdfium_text_visibility_bridge_unavailable_reason": _TEXT_VISIBILITY_UNAVAILABLE_REASON,
    }


def read_clipped_objects(page, kind, max_depth, *, root=None):
    """在 Rust 内完成树遍历，ABI 或句柄错误在解引用前显式失败。"""
    global _CALLS, _UNAVAILABLE_REASON
    native = get_native()
    form_root = root is not None and root[0]
    handle = page if form_root else getattr(page, "raw", None)
    if not handle or (type(page) is not raw.FPDF_PAGEOBJECT if form_root else type(page) is not pdfium.PdfPage):
        _UNAVAILABLE_REASON = "nonstandard or closed page"
        raise RuntimeError(_UNAVAILABLE_REASON)
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
        raise RuntimeError(_UNAVAILABLE_REASON)
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
            raise RuntimeError(_UNAVAILABLE_REASON)
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        batches = native.read_pdfium_objects(addresses, ct.cast(handle, ct.c_void_p).value, kind, max_depth, root)
    _CALLS += 1
    _UNAVAILABLE_REASON = None
    return chain.from_iterable(batches)


def read_text_visibility(page, page_bbox, rotation, max_depth, *, with_roots=False):
    """标准 ABI 下一次遍历 TEXT 对象并返回可见性与视觉裁剪。"""
    global _TEXT_VISIBILITY_CALLS, _TEXT_VISIBILITY_UNAVAILABLE_REASON
    native = get_native()
    reader = getattr(native, "read_pdfium_text_visibility_with_roots" if with_roots else "read_pdfium_text_visibility", None)
    if type(page) is not pdfium.PdfPage or not page.raw:
        _TEXT_VISIBILITY_UNAVAILABLE_REASON = "nonstandard or closed page"
        raise RuntimeError(_TEXT_VISIBILITY_UNAVAILABLE_REASON)
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _TEXT_VISIBILITY_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
        raise RuntimeError(_TEXT_VISIBILITY_UNAVAILABLE_REASON)
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
        ("FPDFTextObj_GetTextRenderMode", ct.c_int, (obj,)),
        (
            "FPDFPageObj_GetFillColor",
            ct.c_int,
            (obj, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)),
        ),
        (
            "FPDFPageObj_GetStrokeColor",
            ct.c_int,
            (obj, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)),
        ),
    )
    if with_roots:
        specs += (
            (
                "FPDFPageObj_GetBounds",
                ct.c_int,
                (obj, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.POINTER(ct.c_float)),
            ),
        )
        from .text_occlusion import _allow_native_cover_prefilter

        if _allow_native_cover_prefilter():
            specs += (
                ("FPDFPath_CountSegments", ct.c_int, (obj,)),
                ("FPDFPath_GetDrawMode", ct.c_int, (obj, ct.POINTER(ct.c_int), ct.POINTER(ct.c_int))),
            )
    addresses = []
    for name, result, arguments in specs:
        function = getattr(raw, name, None)
        if (
            not isinstance(function, ct._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            _TEXT_VISIBILITY_UNAVAILABLE_REASON = f"unsupported symbol or ABI: {name}"
            raise RuntimeError(_TEXT_VISIBILITY_UNAVAILABLE_REASON)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    try:
        with pdfium_guard():
            records = reader(
                addresses,
                ct.cast(page.raw, ct.c_void_p).value,
                tuple(page_bbox),
                rotation,
                max_depth,
            )
    except native.PdfiumReadError as exc:
        raise pdfium.PdfiumError(str(exc)) from exc
    _TEXT_VISIBILITY_CALLS += 1
    _TEXT_VISIBILITY_UNAVAILABLE_REASON = None
    return records


def read_drawing_lines(page, page_bbox, rotation):
    """简单描边页使用原生轴线快路径，复杂页交给完整原生路径证据读取。"""
    global _DRAWING_CALLS, _DRAWING_UNAVAILABLE_REASON
    native = get_native()
    if type(page) is not pdfium.PdfPage or not page.raw:
        _DRAWING_UNAVAILABLE_REASON = "nonstandard or closed page"
        raise RuntimeError(_DRAWING_UNAVAILABLE_REASON)
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _DRAWING_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
        raise RuntimeError(_DRAWING_UNAVAILABLE_REASON)
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
        ("FPDFPath_CountSegments", ct.c_int, (obj,)),
        ("FPDFPath_GetPathSegment", segment, (obj, ct.c_int)),
        ("FPDFPathSegment_GetPoint", ct.c_int, (segment, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float))),
        ("FPDFPathSegment_GetType", ct.c_int, (segment,)),
        ("FPDFPathSegment_GetClose", ct.c_int, (segment,)),
        ("FPDFPath_GetDrawMode", ct.c_int, (obj, ct.POINTER(ct.c_int), ct.POINTER(ct.c_int))),
        (
            "FPDFPageObj_GetStrokeColor",
            ct.c_int,
            (obj, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)),
        ),
        ("FPDFPageObj_GetStrokeWidth", ct.c_int, (obj, ct.POINTER(ct.c_float))),
    )
    addresses = []
    for name, result, arguments in specs:
        function = getattr(raw, name, None)
        if (
            not isinstance(function, ct._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            _DRAWING_UNAVAILABLE_REASON = f"unsupported symbol or ABI: {name}"
            raise RuntimeError(_DRAWING_UNAVAILABLE_REASON)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        batches = native.read_pdfium_drawing_lines(addresses, ct.cast(page.raw, ct.c_void_p).value, page_bbox, rotation)
    if batches is None:
        _DRAWING_UNAVAILABLE_REASON = "complex path, transform, or clip uses full native path evidence"
        return None
    _DRAWING_CALLS += 1
    _DRAWING_UNAVAILABLE_REASON = None
    return chain.from_iterable(batches)


def read_path_subpaths(raw_object):
    """核验 ABI 后使用唯一原生路径解码器。"""
    native = get_native()
    if type(raw_object) is not raw.FPDF_PAGEOBJECT or not raw_object:
        raise RuntimeError("Invalid PDFium path handle or ABI")
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
            raise RuntimeError("Invalid PDFium path handle or ABI")
        functions.append(function)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        return native.read_pdfium_subpaths(addresses, ct.cast(raw_object, ct.c_void_p).value)


def read_path_evidence(page, page_bbox, rotation, max_depth, *, want_lines, want_path_infos):
    """一次原生遍历同时生成 Path 绘图线与摘要。"""
    global _PATH_EVIDENCE_CALLS, _PATH_EVIDENCE_UNAVAILABLE_REASON
    native = get_native()
    reader = getattr(native, "read_pdfium_path_evidence", None)
    if type(page) is not pdfium.PdfPage or not page.raw:
        _PATH_EVIDENCE_UNAVAILABLE_REASON = "nonstandard or closed page"
        raise RuntimeError(_PATH_EVIDENCE_UNAVAILABLE_REASON)
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _PATH_EVIDENCE_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
        raise RuntimeError(_PATH_EVIDENCE_UNAVAILABLE_REASON)
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
        ("FPDFPath_CountSegments", ct.c_int, (obj,)),
        ("FPDFPath_GetPathSegment", segment, (obj, ct.c_int)),
        ("FPDFPathSegment_GetPoint", ct.c_int, (segment, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float))),
        ("FPDFPathSegment_GetType", ct.c_int, (segment,)),
        ("FPDFPathSegment_GetClose", ct.c_int, (segment,)),
        ("FPDFPath_GetDrawMode", ct.c_int, (obj, ct.POINTER(ct.c_int), ct.POINTER(ct.c_int))),
        (
            "FPDFPageObj_GetStrokeColor",
            ct.c_int,
            (obj, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)),
        ),
        ("FPDFPageObj_GetStrokeWidth", ct.c_int, (obj, ct.POINTER(ct.c_float))),
        (
            "FPDFPageObj_GetFillColor",
            ct.c_int,
            (obj, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint)),
        ),
    )
    addresses = []
    for name, result, arguments in specs:
        function = getattr(raw, name, None)
        if (
            not isinstance(function, ct._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            _PATH_EVIDENCE_UNAVAILABLE_REASON = f"unsupported symbol or ABI: {name}"
            raise RuntimeError(_PATH_EVIDENCE_UNAVAILABLE_REASON)
        addresses.append(ct.cast(function, ct.c_void_p).value)
    try:
        with pdfium_guard():
            records = reader(
                addresses,
                ct.cast(page.raw, ct.c_void_p).value,
                tuple(page_bbox),
                rotation,
                max_depth,
                want_lines,
                want_path_infos,
            )
    except native.PdfiumReadError as exc:
        raise pdfium.PdfiumError(str(exc)) from exc
    _PATH_EVIDENCE_CALLS += 1
    _PATH_EVIDENCE_UNAVAILABLE_REASON = None
    line_batches, info_batches = records
    return chain.from_iterable(line_batches), chain.from_iterable(info_batches)
