"""Only borrow the current PDFium page and functions verified by ABI, and do not load or cache native handles."""

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
    """Reports the actual completed object tree read. It cannot be judged that the native path has been executed based on the existence of the extension alone."""
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


def read_clipped_objects(page, kind, max_depth):
    """The standard runtime completes the tree traversal within Rust, the special ctypes stand-in maintains the Python reference path."""
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


def read_text_visibility(page, page_bbox, rotation, max_depth):
    """Standard ABI Next traverses the TEXT object and returns visibility and visual clipping."""
    global _TEXT_VISIBILITY_CALLS, _TEXT_VISIBILITY_UNAVAILABLE_REASON
    native = get_native()
    reader = getattr(native, "read_pdfium_text_visibility", None)
    if native is None or reader is None or type(page) is not pdfium.PdfPage or not page.raw:
        _TEXT_VISIBILITY_UNAVAILABLE_REASON = "python backend or unsupported native extension"
        return None
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _TEXT_VISIBILITY_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
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
            return None
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
    """Only Rust axis extraction is performed for simple stroke pages compatible with ABI, and the reference path is returned for complex pages."""
    global _DRAWING_CALLS, _DRAWING_UNAVAILABLE_REASON
    native = get_native()
    if native is None or type(page) is not pdfium.PdfPage or not page.raw:
        _DRAWING_UNAVAILABLE_REASON = "python backend or nonstandard/closed page"
        return None
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _DRAWING_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
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
            return None
        addresses.append(ct.cast(function, ct.c_void_p).value)
    with pdfium_guard():
        batches = native.read_pdfium_drawing_lines(addresses, ct.cast(page.raw, ct.c_void_p).value, page_bbox, rotation)
    if batches is None:
        _DRAWING_UNAVAILABLE_REASON = "complex path, transform, or clip requires Python reference"
        return None
    _DRAWING_CALLS += 1
    _DRAWING_UNAVAILABLE_REASON = None
    return chain.from_iterable(batches)


def read_path_subpaths(raw_object):
    """Standard ABI uses native path decoding, aliases or non-upgraded extensions retain explicit reference paths."""
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


def read_path_evidence(page, page_bbox, rotation, max_depth, *, want_lines, want_path_infos):
    """A native pass simultaneously generates Path plot lines and summaries for standard reference path differentiation."""
    global _PATH_EVIDENCE_CALLS, _PATH_EVIDENCE_UNAVAILABLE_REASON
    native = get_native()
    reader = getattr(native, "read_pdfium_path_evidence", None)
    if native is None or reader is None or type(page) is not pdfium.PdfPage or not page.raw:
        _PATH_EVIDENCE_UNAVAILABLE_REASON = "python backend or unsupported native extension"
        return None
    if ct.sizeof(raw.FS_MATRIX) != 24 or any(
        getattr(raw.FS_MATRIX, name).offset != offset * 4 for offset, name in enumerate("abcdef")
    ):
        _PATH_EVIDENCE_UNAVAILABLE_REASON = "FS_MATRIX ABI mismatch"
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
            return None
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
