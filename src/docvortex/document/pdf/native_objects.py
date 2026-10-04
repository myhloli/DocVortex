"""PDF Path, Image and Form object extraction, maintaining the native extraction algorithm and resource semantics."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Iterator

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ...schema import BBox
from .native_contracts import (
    DRAWING_FORM_MAX_DEPTH,
    DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE,
    DRAWING_LINE_AXIS_RATIO_TOLERANCE,
    DRAWING_LINE_MERGE_TOLERANCE,
    DRAWING_LINE_MIN_LENGTH,
    DRAWING_THIN_RECT_MAX_THICKNESS,
    DRAWING_THIN_RECT_MIN_ASPECT_RATIO,
    PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES,
    PDFDrawingLine,
    PDFImageInfo,
    PDFPathInfo,
    _PathSubpath,
)
from .native_coordinates import (
    _apply_pdf_matrix,
    _drawing_page_size,
    _get_raw_object_matrix,
    _multiply_pdf_matrices,
    _transform_drawing_point,
)

logger = logging.getLogger("docvortex.document.pdf._document")


@dataclass(frozen=True)
class _ClippedObject:
    """Save the object coordinate system and cumulative clipping to avoid multiplying the clip in the parent coordinates by the object matrix again."""

    raw: Any
    matrix: tuple[float, float, float, float, float, float]
    parent_matrix: tuple[float, float, float, float, float, float]
    depth: int
    clip: BBox | None


def _intersect_object_bbox(first: BBox, second: BBox | None) -> BBox:
    """Find the boundary intersection. An empty intersection remains an empty rectangle and cannot be re-considered as no clipping."""
    if second is None:
        return first
    return max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3])


def _transform_object_bbox(bbox: BBox, transform: Any) -> BBox:
    """After transforming the four corners, a conservative outer frame is adopted, which is compatible with rotation and beveling."""
    points = [transform((x, y)) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _object_clip_bbox(raw: Any, parent_matrix: tuple, inherited: BBox | None) -> BBox | None:
    """The clipping paths in the parent coordinate system are read; the paths intersect and the curve is constrained by a conservative outer bounding box with control points."""
    result = inherited
    try:
        clip = pdfium_c.FPDFPageObj_GetClipPath(raw)
        count = pdfium_c.FPDFClipPath_CountPaths(clip) if clip else 0
        for path_index in range(max(0, count)):
            points = []
            for index in range(pdfium_c.FPDFClipPath_CountPathSegments(clip, path_index)):
                segment = pdfium_c.FPDFClipPath_GetPathSegment(clip, path_index, index)
                x, y = ctypes.c_float(), ctypes.c_float()
                if pdfium_c.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y)):
                    points.append(_apply_pdf_matrix((x.value, y.value), parent_matrix))
            if points and all(math.isfinite(v) for p in points for v in p):
                bounds = (
                    min(p[0] for p in points),
                    min(p[1] for p in points),
                    max(p[0] for p in points),
                    max(p[1] for p in points),
                )
                result = _intersect_object_bbox(bounds, result)
    except Exception:
        # Missing or corrupted partial clip Confirmed parent crop cannot be erased.
        pass
    return result


def _walk_clipped_objects(
    container: Any,
    *,
    is_form: bool = False,
    parent_matrix: tuple = (1, 0, 0, 1, 0, 0),
    depth: int = 0,
    inherited_clip: BBox | None = None,
    max_depth: int = DRAWING_FORM_MAX_DEPTH,
) -> Iterator[_ClippedObject]:
    """Traverse visible leaves and propagate Form clipping; object matrix only transforms content, clip uses parent matrix."""
    if depth >= max_depth:
        return
    count_objects = pdfium_c.FPDFFormObj_CountObjects if is_form else pdfium_c.FPDFPage_CountObjects
    get = pdfium_c.FPDFFormObj_GetObject if is_form else pdfium_c.FPDFPage_GetObject
    try:
        size = int(count_objects(container))
    except Exception:
        return
    for index in range(max(0, size)):
        try:
            raw = get(container, index)
            matrix = _get_raw_object_matrix(raw) if raw else None
            if matrix is None:
                continue
            combined = _multiply_pdf_matrices(matrix, parent_matrix)
            clip = _object_clip_bbox(raw, parent_matrix, inherited_clip)
            if int(pdfium_c.FPDFPageObj_GetType(raw)) == pdfium_c.FPDF_PAGEOBJ_FORM:
                yield from _walk_clipped_objects(
                    raw,
                    is_form=True,
                    parent_matrix=combined,
                    depth=depth + 1,
                    inherited_clip=clip,
                    max_depth=max_depth,
                )
            else:
                yield _ClippedObject(raw, combined, parent_matrix, depth, clip)
        except Exception:
            continue


def _clip_object_visual_bbox(bbox: BBox, clip: BBox | None, page_bbox: BBox, rotation: int) -> BBox | None:
    """Apply cumulative cropping in unified page visual coordinates."""
    if clip is not None and (clip[2] <= clip[0] or clip[3] <= clip[1]):
        return None
    if clip is not None:
        bbox = _intersect_object_bbox(
            bbox, _transform_object_bbox(clip, lambda point: _transform_drawing_point(point, page_bbox, rotation))
        )
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def _clipped_form_extent(raw: Any, page_bbox: BBox, rotation: int) -> BBox | None:
    """Only in Form memory the boundaries are reconstructed with visible leaves when clipping; existing output without clip remains unchanged."""
    matrix = _get_raw_object_matrix(raw)
    if matrix is None:
        return None
    members = list(
        _walk_clipped_objects(
            raw, is_form=True, parent_matrix=matrix, depth=1, inherited_clip=_object_clip_bbox(raw, (1, 0, 0, 1, 0, 0), None)
        )
    )
    if not any(member.clip is not None for member in members):
        return None
    boxes = []
    for member in members:
        values = [ctypes.c_float() for _ in range(4)]
        if not pdfium_c.FPDFPageObj_GetBounds(member.raw, *(ctypes.byref(v) for v in values)):
            continue
        bounds = tuple(v.value for v in values)
        bounds = _transform_object_bbox(bounds, lambda point: _apply_pdf_matrix(point, member.parent_matrix))
        bounds = _intersect_object_bbox(bounds, member.clip)
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            boxes.append(_transform_object_bbox(bounds, lambda point: _transform_drawing_point(point, page_bbox, rotation)))
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _clipped_objects_of_type(
    page: Any,
    object_type: int,
    max_depth: int = DRAWING_FORM_MAX_DEPTH,
) -> Iterator[_ClippedObject]:
    """Filter the accumulated cropped object types, and the path source_index maintains the existing traversal order."""
    from ._object_bridge import read_clipped_objects

    records = read_clipped_objects(page, object_type, max_depth)
    if records is not None:
        for address, matrix, parent, depth, clip in records:
            yield _ClippedObject(ctypes.cast(address, pdfium_c.FPDF_PAGEOBJECT), matrix, parent, depth, clip)
        return
    for member in _walk_clipped_objects(page, max_depth=max_depth):
        if int(pdfium_c.FPDFPageObj_GetType(member.raw)) == object_type:
            yield member


def _text_object_visibility(
    page: Any, page_bbox: BBox, rotation: int, paint_page_reader=None
) -> dict[int, tuple[bool, BBox | None]]:
    """One traversal establishes the drawing state and effective cropping of the text object; the address is only used within this page extraction."""
    from ._object_bridge import read_text_visibility

    records = read_text_visibility(page, page_bbox, rotation, DRAWING_FORM_MAX_DEPTH)
    if records is not None:
        output = {address: (visible, clip) for address, visible, clip in records}
        if paint_page_reader is not None:
            from .text_occlusion import exclude_fully_overpainted_text

            output = exclude_fully_overpainted_text(page, page_bbox, rotation, output, paint_page_reader)
        return output
    output = {}
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_TEXT):
        address = ctypes.cast(member.raw, ctypes.c_void_p).value
        if not address:
            continue
        mode = int(pdfium_c.FPDFTextObj_GetTextRenderMode(member.raw))
        visible = mode not in {3, 7}
        if mode in {0, 1, 2, 4, 5, 6}:
            visible = (
                mode in {0, 2, 4, 6}
                and _get_raw_object_alpha(member.raw, pdfium_c.FPDFPageObj_GetFillColor) > 0
                or mode in {1, 2, 5, 6}
                and _get_raw_object_alpha(member.raw, pdfium_c.FPDFPageObj_GetStrokeColor) > 0
            )
        clip = member.clip
        if clip is not None:
            if clip[2] <= clip[0] or clip[3] <= clip[1]:
                visible = False
            else:
                clip = _transform_object_bbox(clip, lambda point: _transform_drawing_point(point, page_bbox, rotation))
        output[address] = (visible, clip)
    if paint_page_reader is not None:
        from .text_occlusion import exclude_fully_overpainted_text

        output = exclude_fully_overpainted_text(page, page_bbox, rotation, output, paint_page_reader)
    return output


def _walk_raw_page_objects_with_depth(
    container: Any,
    *,
    is_form: bool,
    parent_matrix: tuple[float, float, float, float, float, float],
    depth: int,
    target_type: int,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float], int]]:
    """Recursively traverse the target object and carry the accumulation matrix and its Form depth."""
    if depth >= DRAWING_FORM_MAX_DEPTH:
        return

    count_objects = pdfium_c.FPDFFormObj_CountObjects if is_form else pdfium_c.FPDFPage_CountObjects
    get_object = pdfium_c.FPDFFormObj_GetObject if is_form else pdfium_c.FPDFPage_GetObject
    try:
        object_count = int(count_objects(container))
    except Exception:
        return
    if object_count < 0:
        return

    for object_index in range(object_count):
        try:
            raw_obj = get_object(container, object_index)
            if not raw_obj:
                continue
            object_type = int(pdfium_c.FPDFPageObj_GetType(raw_obj))
            object_matrix = _get_raw_object_matrix(raw_obj)
            if object_matrix is None:
                continue
            combined_matrix = _multiply_pdf_matrices(object_matrix, parent_matrix)
        except Exception:
            # A single damaged page object cannot interrupt the traversal of other target objects on the same page.
            continue

        if object_type == target_type:
            yield raw_obj, combined_matrix, depth
        if object_type == pdfium_c.FPDF_PAGEOBJ_FORM:
            yield from _walk_raw_page_objects_with_depth(
                raw_obj,
                is_form=True,
                parent_matrix=combined_matrix,
                depth=depth + 1,
                target_type=target_type,
            )


def _walk_raw_page_objects(
    container: Any,
    *,
    is_form: bool,
    parent_matrix: tuple[float, float, float, float, float, float],
    depth: int,
    target_type: int,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float]]]:
    """Compatible with existing calls, only the target object and the matrix accumulated to the page coordinates are returned."""

    for raw_obj, matrix, _form_depth in _walk_raw_page_objects_with_depth(
        container,
        is_form=is_form,
        parent_matrix=parent_matrix,
        depth=depth,
        target_type=target_type,
    ):
        yield raw_obj, matrix


def _walk_raw_path_objects(
    container: Any,
    *,
    is_form: bool,
    parent_matrix: tuple[float, float, float, float, float, float],
    depth: int,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float]]]:
    """Recursively traverse the page or Path in Form and carry a matrix accumulated up to the page coordinates."""
    yield from _walk_raw_page_objects(
        container,
        is_form=is_form,
        parent_matrix=parent_matrix,
        depth=depth,
        target_type=pdfium_c.FPDF_PAGEOBJ_PATH,
    )


def _iter_raw_path_objects(
    page: pdfium.PdfPage,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float]]]:
    """Traverse all Paths starting from the page root object, including Path within nested Form."""
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    yield from _walk_raw_path_objects(
        page,
        is_form=False,
        parent_matrix=identity,
        depth=0,
    )


def _iter_raw_path_objects_with_depth(
    page: pdfium.PdfPage,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float], int]]:
    """Traverse all Path and retain its Form depth for upper filter icons."""

    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    yield from _walk_raw_page_objects_with_depth(
        page,
        is_form=False,
        parent_matrix=identity,
        depth=0,
        target_type=pdfium_c.FPDF_PAGEOBJ_PATH,
    )


def _iter_raw_image_objects(
    page: pdfium.PdfPage,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float]]]:
    """Traverse all bitmap images starting from the page root object, including images in nested Form."""
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    yield from _walk_raw_page_objects(
        page,
        is_form=False,
        parent_matrix=identity,
        depth=0,
        target_type=pdfium_c.FPDF_PAGEOBJ_IMAGE,
    )


def _iter_raw_root_form_objects(page: pdfium.PdfPage) -> Iterator[Any]:
    """Only traverse the Form at the root level of the page to avoid repeatedly outputting the nested sub-Form of the same vector image."""

    try:
        object_count = int(pdfium_c.FPDFPage_CountObjects(page))
    except Exception:
        return
    if object_count < 0:
        return

    for object_index in range(object_count):
        try:
            raw_obj = pdfium_c.FPDFPage_GetObject(page, object_index)
            if raw_obj and int(pdfium_c.FPDFPageObj_GetType(raw_obj)) == pdfium_c.FPDF_PAGEOBJ_FORM:
                yield raw_obj
        except Exception:
            # A single corrupt object cannot block the extraction of other top-level Forms on the same page.
            continue


def _get_raw_object_rgba(
    raw_obj: Any,
    color_getter: Any,
) -> tuple[int, int, int, int] | None:
    """Read object RGBA; returns None for old PDFium, unsupported object, or read failure."""

    red = ctypes.c_uint()
    green = ctypes.c_uint()
    blue = ctypes.c_uint()
    alpha = ctypes.c_uint()
    try:
        ok = color_getter(
            raw_obj,
            ctypes.byref(red),
            ctypes.byref(green),
            ctypes.byref(blue),
            ctypes.byref(alpha),
        )
    except Exception:
        return None
    if not ok:
        return None
    return (
        int(red.value),
        int(green.value),
        int(blue.value),
        int(alpha.value),
    )


def _get_raw_object_alpha(raw_obj: Any, color_getter: Any) -> int:
    """Read object color alpha; old PDFium or read as opaque when failed."""

    rgba = _get_raw_object_rgba(raw_obj, color_getter)
    return rgba[3] if rgba is not None else 255


def _get_path_visibility(raw_obj: Any) -> tuple[bool, bool]:
    """Determine whether the fill and stroke of Path are actually visible respectively."""
    fill_mode = ctypes.c_int()
    stroke = ctypes.c_int()
    try:
        ok = pdfium_c.FPDFPath_GetDrawMode(raw_obj, ctypes.byref(fill_mode), ctypes.byref(stroke))
    except Exception:
        return False, False
    if not ok:
        return False, False

    fill_visible = (
        fill_mode.value != pdfium_c.FPDF_FILLMODE_NONE and _get_raw_object_alpha(raw_obj, pdfium_c.FPDFPageObj_GetFillColor) > 0
    )
    stroke_visible = bool(stroke.value) and _get_raw_object_alpha(raw_obj, pdfium_c.FPDFPageObj_GetStrokeColor) > 0
    return fill_visible, stroke_visible


def _get_raw_stroke_width(raw_obj: Any) -> float:
    """Reads the stroke width of Path in the object's local coordinates."""
    stroke_width = ctypes.c_float()
    try:
        ok = pdfium_c.FPDFPageObj_GetStrokeWidth(raw_obj, ctypes.byref(stroke_width))
    except Exception:
        return 0.0
    if not ok:
        return 0.0
    width = abs(float(stroke_width.value))
    return width if math.isfinite(width) else 0.0


def _get_segment_stroke_width(
    raw_width: float,
    start: tuple[float, float],
    end: tuple[float, float],
    matrix: tuple[float, float, float, float, float, float],
) -> float:
    """The actual stroke width under the non-proportional matrix is converted according to the local normal vector of the line segment."""
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    segment_length = math.hypot(delta_x, delta_y)
    a, b, c, d, _, _ = matrix
    if segment_length <= 0:
        scale = math.sqrt(abs(a * d - b * c))
    else:
        normal_x = -delta_y / segment_length
        normal_y = delta_x / segment_length
        transformed_x = a * normal_x + c * normal_y
        transformed_y = b * normal_x + d * normal_y
        scale = math.hypot(transformed_x, transformed_y)
    width = raw_width * scale
    return width if math.isfinite(width) else 0.0


def _read_raw_path_subpaths(raw_obj: Any) -> list[_PathSubpath]:
    """Read the Path segment and split it into sub-paths, and only record LINETO and closed edges as straight line segments."""
    from ._object_bridge import read_path_subpaths

    records = read_path_subpaths(raw_obj)
    if records is not None:
        return [
            _PathSubpath(points, [(points[first], points[last]) for first, last in lines], closed)
            for points, lines, closed in records
        ]
    return _read_raw_path_subpaths_python(raw_obj)


def _read_raw_path_subpaths_python(raw_obj: Any) -> list[_PathSubpath]:
    """Retained segment-by-section reference implementation for non-standard ABI with migrated differentials."""
    try:
        segment_count = int(pdfium_c.FPDFPath_CountSegments(raw_obj))
    except Exception:
        return []
    if segment_count <= 0:
        return []

    subpaths: list[_PathSubpath] = []
    current_subpath: _PathSubpath | None = None
    current_point: tuple[float, float] | None = None
    subpath_start: tuple[float, float] | None = None

    for segment_index in range(segment_count):
        try:
            segment = pdfium_c.FPDFPath_GetPathSegment(raw_obj, segment_index)
            if not segment:
                continue
            x = ctypes.c_float()
            y = ctypes.c_float()
            if not pdfium_c.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y)):
                continue
            point = (float(x.value), float(y.value))
            segment_type = int(pdfium_c.FPDFPathSegment_GetType(segment))
            segment_closes = bool(pdfium_c.FPDFPathSegment_GetClose(segment))
        except Exception:
            continue

        if segment_type == pdfium_c.FPDF_SEGMENT_MOVETO:
            if current_subpath is not None and current_subpath.points:
                subpaths.append(current_subpath)
            current_subpath = _PathSubpath(points=[point], straight_segments=[])
            current_point = point
            subpath_start = point
        else:
            if current_subpath is None:
                current_subpath = _PathSubpath(points=[point], straight_segments=[])
                current_point = point
                subpath_start = point
            else:
                current_subpath.points.append(point)
                if segment_type == pdfium_c.FPDF_SEGMENT_LINETO and current_point is not None:
                    current_subpath.straight_segments.append((current_point, point))
                current_point = point

        if segment_closes and current_subpath is not None and current_point is not None and subpath_start is not None:
            if current_point != subpath_start:
                current_subpath.straight_segments.append((current_point, subpath_start))
            current_subpath.closed = True
            current_point = subpath_start

    if current_subpath is not None and current_subpath.points:
        subpaths.append(current_subpath)
    return subpaths


def _transform_path_subpath(
    subpath: _PathSubpath,
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
) -> _PathSubpath:
    """Converts a subpath from object local coordinates to page top-left coordinates."""

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        """Apply the object/Form matrix first, then apply the page coordinates and rotation transformation."""
        return _transform_drawing_point(_apply_pdf_matrix(point, matrix), page_bbox, page_rotation)

    return _PathSubpath(
        points=[transform(point) for point in subpath.points],
        straight_segments=[(transform(start), transform(end)) for start, end in subpath.straight_segments],
        closed=subpath.closed,
    )


def _path_object_evidence(
    raw_obj: Any,
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
    form_depth: int,
    source_index: int,
    *,
    subpaths: list[_PathSubpath],
    want_lines: bool,
    want_path_infos: bool,
) -> tuple[list[PDFDrawingLine], PDFPathInfo | None]:
    """Reads the Path state once and converts subpaths for reuse by both plot lines and path summaries."""

    fill_visible, stroke_visible = _get_path_visibility(raw_obj)
    raw_stroke_width = _get_raw_stroke_width(raw_obj) if stroke_visible else 0.0
    prepared = [
        (raw_subpath, _transform_path_subpath(raw_subpath, matrix, page_bbox, page_rotation)) for raw_subpath in subpaths
    ]
    drawing_lines: list[PDFDrawingLine] = []
    if want_lines and (fill_visible or stroke_visible):
        page_size = _drawing_page_size(page_bbox, page_rotation)
        for raw_subpath, subpath in prepared:
            if fill_visible:
                filled_line = _get_thin_filled_subpath_line(subpath, page_size)
                if filled_line is not None:
                    drawing_lines.append(filled_line)
                    continue
            if not stroke_visible:
                continue
            for (raw_start, raw_end), (segment_start, segment_end) in zip(
                raw_subpath.straight_segments,
                subpath.straight_segments,
            ):
                stroke_width = _get_segment_stroke_width(raw_stroke_width, raw_start, raw_end, matrix)
                drawing_line = _make_axis_drawing_line(segment_start, segment_end, stroke_width, page_size)
                if drawing_line is not None:
                    drawing_lines.append(drawing_line)

    path_info: PDFPathInfo | None = None
    if want_path_infos:
        try:
            segment_count = int(pdfium_c.FPDFPath_CountSegments(raw_obj))
        except Exception:
            segment_count = 0
        points = [point for _, subpath in prepared for point in subpath.points]
        if segment_count > 0 and points:
            coordinates = [coordinate for point in points for coordinate in point]
            if all(math.isfinite(value) for value in coordinates):
                page_width, page_height = _drawing_page_size(page_bbox, page_rotation)
                stroke_margin = 0.0
                if stroke_visible:
                    a, b, c, d, _, _ = matrix
                    stroke_scale = max(math.hypot(a, b), math.hypot(c, d))
                    stroke_margin = 0.5 * raw_stroke_width * stroke_scale
                bbox = (
                    max(0.0, min(point[0] for point in points) - stroke_margin),
                    max(0.0, min(point[1] for point in points) - stroke_margin),
                    min(page_width, max(point[0] for point in points) + stroke_margin),
                    min(page_height, max(point[1] for point in points) + stroke_margin),
                )
                if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    path_info = PDFPathInfo(
                        bbox=bbox,
                        segment_count=segment_count,
                        fill_visible=fill_visible,
                        stroke_visible=stroke_visible,
                        form_depth=form_depth,
                        source_index=source_index,
                        fill_rgba=(_get_raw_object_rgba(raw_obj, pdfium_c.FPDFPageObj_GetFillColor) if fill_visible else None),
                        rectangle_bboxes=tuple(
                            rectangle
                            for _, subpath in prepared
                            if fill_visible and (rectangle := _filled_rectangle_bbox(subpath)) is not None
                        ),
                    )
    return drawing_lines, path_info


def _filled_rectangle_bbox(subpath: _PathSubpath) -> BBox | None:
    """Only filled sub-paths with four corners and straight sides aligned on the average axis are recorded as rectangles. Curves and glyph outlines cannot use the outer frame to pretend to be a cylinder."""
    points = subpath.points
    if len(points) not in (4, 5) or len(subpath.straight_segments) not in (3, 4):
        return None
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    if x1 <= x0 or y1 <= y0:
        return None
    corners = {(round(x, 3), round(y, 3)) for x, y in points}
    if corners != {(round(x, 3), round(y, 3)) for x in (x0, x1) for y in (y0, y1)}:
        return None
    if any(abs(a[0] - b[0]) > 0.001 and abs(a[1] - b[1]) > 0.001 for a, b in subpath.straight_segments):
        return None
    return (x0, y0, x1, y1)


def _path_info_from_object(
    raw_obj: Any,
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
    form_depth: int,
    source_index: int,
    *,
    subpaths: list[_PathSubpath] | None = None,
) -> PDFPathInfo | None:
    """Preserve independent path summary entries; the default is to read and convert the original Path before reusing the joint evidence."""

    resolved = _read_raw_path_subpaths(raw_obj) if subpaths is None else subpaths
    return _path_object_evidence(
        raw_obj,
        matrix,
        page_bbox,
        page_rotation,
        form_depth,
        source_index,
        subpaths=resolved,
        want_lines=False,
        want_path_infos=True,
    )[1]


def _get_thin_filled_subpath_line(
    subpath: _PathSubpath,
    page_size: tuple[float, float],
) -> PDFDrawingLine | None:
    """Collapse the closed slender filled sub-path into a center line to avoid repeated output of the four sides of the rectangle."""
    if len(subpath.points) < 4:
        return None
    x_values = [point[0] for point in subpath.points]
    y_values = [point[1] for point in subpath.points]
    x0, x1 = min(x_values), max(x_values)
    y0, y1 = min(y_values), max(y_values)
    width = x1 - x0
    height = y1 - y0
    if not subpath.closed:
        # PDF The fill operation implicitly closes the subpath; only four axis-aligned vertices and three straight edges are accepted.
        # Do not treat the bounding box of an open polyline, triangle, or Bézier control point as a rectangular horizontal line.
        if len(subpath.points) != 4 or len(subpath.straight_segments) != 3:
            return None
        corners = {(round(x, 3), round(y, 3)) for x, y in subpath.points}
        if corners != {(round(x, 3), round(y, 3)) for x in (x0, x1) for y in (y0, y1)}:
            return None
        if any(abs(a[0] - b[0]) > 0.001 and abs(a[1] - b[1]) > 0.001 for a, b in subpath.straight_segments):
            return None
    long_side = max(width, height)
    short_side = min(width, height)
    if (
        long_side < DRAWING_LINE_MIN_LENGTH
        or short_side > DRAWING_THIN_RECT_MAX_THICKNESS
        or long_side < DRAWING_THIN_RECT_MIN_ASPECT_RATIO * max(short_side, 0.01)
    ):
        return None
    if width >= height:
        return _make_axis_drawing_line((x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2), short_side, page_size)
    return _make_axis_drawing_line(((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), short_side, page_size)


def _make_axis_drawing_line(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    page_size: tuple[float, float],
) -> PDFDrawingLine | None:
    """Snap near-horizontal or near-vertical segments to the coordinate axis, crop to the page, and generate public results."""
    page_width, page_height = page_size
    x0, y0 = start
    x1, y1 = end
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1, width)):
        return None
    delta_x = abs(x1 - x0)
    delta_y = abs(y1 - y0)

    if delta_x >= delta_y and delta_y <= max(
        DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE,
        delta_x * DRAWING_LINE_AXIS_RATIO_TOLERANCE,
    ):
        coordinate = (y0 + y1) / 2
        if coordinate < 0 or coordinate > page_height:
            return None
        main_start = max(0.0, min(x0, x1))
        main_end = min(page_width, max(x0, x1))
        if main_end - main_start < DRAWING_LINE_MIN_LENGTH:
            return None
        line_width = max(0.0, width, delta_y)
        half_width = line_width / 2
        bbox = (
            main_start,
            max(0.0, coordinate - half_width),
            main_end,
            min(page_height, coordinate + half_width),
        )
        return PDFDrawingLine(
            start=(main_start, coordinate),
            end=(main_end, coordinate),
            bbox=bbox,
            width=line_width,
            orientation="horizontal",
        )

    if delta_y > delta_x and delta_x <= max(
        DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE,
        delta_y * DRAWING_LINE_AXIS_RATIO_TOLERANCE,
    ):
        coordinate = (x0 + x1) / 2
        if coordinate < 0 or coordinate > page_width:
            return None
        main_start = max(0.0, min(y0, y1))
        main_end = min(page_height, max(y0, y1))
        if main_end - main_start < DRAWING_LINE_MIN_LENGTH:
            return None
        line_width = max(0.0, width, delta_x)
        half_width = line_width / 2
        bbox = (
            max(0.0, coordinate - half_width),
            main_start,
            min(page_width, coordinate + half_width),
            main_end,
        )
        return PDFDrawingLine(
            start=(coordinate, main_start),
            end=(coordinate, main_end),
            bbox=bbox,
            width=line_width,
            orientation="vertical",
        )
    return None


def _extract_path_drawing_lines(
    raw_obj: Any,
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
    *,
    subpaths: list[_PathSubpath] | None = None,
) -> list[PDFDrawingLine]:
    """Preserve independent plot line entries; default is to read and convert the original Path before reusing the joint evidence."""

    resolved = _read_raw_path_subpaths(raw_obj) if subpaths is None else subpaths
    return _path_object_evidence(
        raw_obj,
        matrix,
        page_bbox,
        page_rotation,
        0,
        0,
        subpaths=resolved,
        want_lines=True,
        want_path_infos=False,
    )[0]


_STANDARD_PATH_INFO_EXTRACTOR = _path_info_from_object
_STANDARD_PATH_LINE_EXTRACTOR = _extract_path_drawing_lines


def _line_axis_coordinate(line: PDFDrawingLine) -> float:
    """Returns the axis coordinate of the plot line perpendicular to itself."""
    return line.start[1] if line.orientation == "horizontal" else line.start[0]


def _line_main_interval(line: PDFDrawingLine) -> tuple[float, float]:
    """Returns the starting and ending intervals of the drawing line along its own direction."""
    if line.orientation == "horizontal":
        return line.start[0], line.end[0]
    return line.start[1], line.end[1]


def _combine_collinear_line_group(
    lines: list[PDFDrawingLine],
    page_size: tuple[float, float],
) -> PDFDrawingLine | None:
    """Merge a group of lines with close coordinates and connected intervals into a stable center line."""
    orientation = lines[0].orientation
    intervals = [_line_main_interval(line) for line in lines]
    lengths = [max(end - start, 0.01) for start, end in intervals]
    coordinates = [_line_axis_coordinate(line) for line in lines]
    total_length = sum(lengths)
    coordinate = sum(value * length for value, length in zip(coordinates, lengths)) / total_length
    coordinate_span = max(coordinates) - min(coordinates)
    width = max(max(line.width for line in lines), coordinate_span + max(line.width for line in lines))
    main_start = min(start for start, _ in intervals)
    main_end = max(end for _, end in intervals)
    if orientation == "horizontal":
        return _make_axis_drawing_line((main_start, coordinate), (main_end, coordinate), width, page_size)
    return _make_axis_drawing_line((coordinate, main_start), (coordinate, main_end), width, page_size)


def _merge_orientation_lines(
    lines: list[PDFDrawingLine],
    page_size: tuple[float, float],
) -> list[PDFDrawingLine]:
    """Cluster according to the axis coordinates, and then merge the line segments in the same direction with a distance of no more than about 2pt."""
    if not lines:
        return []
    coordinate_clusters: list[list[PDFDrawingLine]] = []
    # The input is sorted in ascending order of axis coordinates, and the minimum value of the cluster is always the first element. Remembering the starting point can avoid rescanning the entire cluster.
    cluster_start = 0.0
    for line in sorted(lines, key=lambda item: (_line_axis_coordinate(item), _line_main_interval(item)[0])):
        coordinate = _line_axis_coordinate(line)
        if not coordinate_clusters or coordinate - cluster_start > DRAWING_LINE_MERGE_TOLERANCE:
            coordinate_clusters.append([line])
            cluster_start = coordinate
        else:
            coordinate_clusters[-1].append(line)

    merged: list[PDFDrawingLine] = []
    for cluster in coordinate_clusters:
        interval_group: list[PDFDrawingLine] = []
        interval_end = -math.inf
        for line in sorted(cluster, key=lambda item: _line_main_interval(item)[0]):
            line_start, line_end = _line_main_interval(line)
            if interval_group and line_start > interval_end + DRAWING_LINE_MERGE_TOLERANCE:
                combined = _combine_collinear_line_group(interval_group, page_size)
                if combined is not None:
                    merged.append(combined)
                interval_group = []
                interval_end = -math.inf
            interval_group.append(line)
            interval_end = max(interval_end, line_end)
        if interval_group:
            combined = _combine_collinear_line_group(interval_group, page_size)
            if combined is not None:
                merged.append(combined)
    return merged


def _merge_collinear_drawing_lines(
    lines: list[PDFDrawingLine],
    page_size: tuple[float, float],
) -> list[PDFDrawingLine]:
    """Merge horizontal and vertical collinear segments and stably sort by visual position on the page."""
    horizontal = _merge_orientation_lines(
        [line for line in lines if line.orientation == "horizontal"],
        page_size,
    )
    vertical = _merge_orientation_lines(
        [line for line in lines if line.orientation == "vertical"],
        page_size,
    )
    return sorted(
        [*horizontal, *vertical],
        key=lambda line: (line.bbox[1], line.bbox[0], line.orientation),
    )


def _image_bbox_from_matrix(
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
) -> BBox | None:
    """Convert the bitmap unit rectangle through the object/Form matrix and crop it to the upper left coordinate of the page, bbox."""
    page_points = [
        _transform_drawing_point(
            _apply_pdf_matrix(point, matrix),
            page_bbox,
            page_rotation,
        )
        for point in ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0))
    ]
    coordinates = [coordinate for point in page_points for coordinate in point]
    if not all(math.isfinite(value) for value in coordinates):
        return None

    page_width, page_height = _drawing_page_size(page_bbox, page_rotation)
    left = max(0.0, min(point[0] for point in page_points))
    top = max(0.0, min(point[1] for point in page_points))
    right = min(page_width, max(point[0] for point in page_points))
    bottom = min(page_height, max(point[1] for point in page_points))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _form_bbox_from_object(
    raw_obj: Any,
    page_bbox: BBox,
    page_rotation: int,
) -> BBox | None:
    """Read the page coordinate boundary of the top-level Form, and convert and crop it to the visual page range."""

    left = ctypes.c_float()
    bottom = ctypes.c_float()
    right = ctypes.c_float()
    top = ctypes.c_float()
    try:
        ok = pdfium_c.FPDFPageObj_GetBounds(
            raw_obj,
            ctypes.byref(left),
            ctypes.byref(bottom),
            ctypes.byref(right),
            ctypes.byref(top),
        )
    except Exception:
        return None
    if not ok:
        return None

    raw_bbox = (float(left.value), float(bottom.value), float(right.value), float(top.value))
    if not all(math.isfinite(value) for value in raw_bbox):
        return None
    page_points = [
        _transform_drawing_point(point, page_bbox, page_rotation)
        for point in (
            (raw_bbox[0], raw_bbox[1]),
            (raw_bbox[0], raw_bbox[3]),
            (raw_bbox[2], raw_bbox[1]),
            (raw_bbox[2], raw_bbox[3]),
        )
    ]
    page_width, page_height = _drawing_page_size(page_bbox, page_rotation)
    visual_left = max(0.0, min(point[0] for point in page_points))
    visual_top = max(0.0, min(point[1] for point in page_points))
    visual_right = min(page_width, max(point[0] for point in page_points))
    visual_bottom = min(page_height, max(point[1] for point in page_points))
    if visual_right <= visual_left or visual_bottom <= visual_top:
        return None
    return visual_left, visual_top, visual_right, visual_bottom


def _extract_page_image_bboxes(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
) -> list[BBox]:
    """Extract all valid bitmaps bbox while the caller holds the PDFium lock, and isolate single-object exceptions."""
    image_bboxes: list[BBox] = []
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_IMAGE):
        try:
            image_bbox = _image_bbox_from_matrix(member.matrix, page_bbox, page_rotation)
            if image_bbox is not None:
                image_bbox = _clip_object_visual_bbox(image_bbox, member.clip, page_bbox, page_rotation)
        except Exception:
            # A single corrupted Image object cannot interrupt other bitmap extractions on the same page.
            continue
        if image_bbox is not None:
            image_bboxes.append(image_bbox)
    return sorted(image_bboxes, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))


def _get_raw_image_fingerprint(raw_obj: Any, page: pdfium.PdfPage) -> str | None:
    """Read the original stream of images with limited size and combine the pixel width and height to generate SHA-256 fingerprint."""

    metadata = pdfium_c.FPDF_IMAGEOBJ_METADATA()
    try:
        if not pdfium_c.FPDFImageObj_GetImageMetadata(
            raw_obj,
            page.raw,
            ctypes.byref(metadata),
        ):
            return None
        width = int(metadata.width)
        height = int(metadata.height)
        raw_size = int(pdfium_c.FPDFImageObj_GetImageDataRaw(raw_obj, None, 0))
    except Exception:
        return None
    if width <= 0 or height <= 0 or raw_size <= 0 or raw_size > PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES:
        return None

    try:
        buffer = ctypes.create_string_buffer(raw_size)
    except (MemoryError, OverflowError):
        return None
    try:
        bytes_read = int(pdfium_c.FPDFImageObj_GetImageDataRaw(raw_obj, buffer, raw_size))
    except Exception:
        return None
    if bytes_read <= 0 or bytes_read > raw_size:
        return None

    digest = hashlib.sha256()
    digest.update(f"{width}:{height}:".encode("ascii"))
    digest.update(buffer.raw[:bytes_read])
    return digest.hexdigest()


def _extract_page_image_infos(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
) -> list[PDFImageInfo]:
    """Extract all valid bitmap geometries; when a single image fingerprint fails, bbox is retained and processed as released."""

    image_infos: list[PDFImageInfo] = []
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_IMAGE):
        raw_obj, matrix = member.raw, member.matrix
        try:
            image_bbox = _image_bbox_from_matrix(matrix, page_bbox, page_rotation)
            if image_bbox is not None:
                image_bbox = _clip_object_visual_bbox(image_bbox, member.clip, page_bbox, page_rotation)
        except Exception:
            # A single corrupted Image object cannot interrupt other bitmap extractions on the same page.
            continue
        if image_bbox is None:
            continue
        image_infos.append(
            PDFImageInfo(
                bbox=image_bbox,
                fingerprint=_get_raw_image_fingerprint(raw_obj, page),
                smooth_background=_is_smooth_page_background(raw_obj, page, image_bbox, page_bbox),
                blank_top_bbox=_native_image_blank_top_bbox(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation),
            )
        )
    return sorted(
        image_infos,
        key=lambda info: (info.bbox[1], info.bbox[0], info.bbox[3], info.bbox[2]),
    )


def _blank_image_top_fraction(image) -> float:
    """Only the full-width near-white or transparent top edge is recognized, text in the image is not recognized, and any visible non-white content is not cropped."""
    from PIL import Image, ImageChops

    if image.width * image.height > 4_000_000:
        return 0.0
    with (
        image.convert("RGBA") as rgba,
        Image.new("RGBA", rgba.size, "white") as background,
        Image.alpha_composite(background, rgba) as flat,
    ):
        with (
            flat.convert("RGB") as rgb,
            Image.new("RGB", rgb.size, "white") as white,
            ImageChops.difference(rgb, white) as diff,
        ):
            channels = diff.split()
            try:
                with ImageChops.lighter(channels[0], channels[1]) as first, ImageChops.lighter(first, channels[2]) as strongest:
                    with strongest.point(lambda value: 255 if value > 5 else 0) as mask:
                        bounds = mask.getbbox()
            finally:
                for channel in channels:
                    channel.close()
    fraction = bounds[1] / image.height if bounds else 0.0
    return fraction if 0.03 <= fraction <= 0.12 else 0.0


def _native_image_blank_top_bbox(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation):
    """The original image with positive axis alignment and uncropped top can provide evidence of white edges, and damaged or rotated objects are conservatively preserved."""
    if page_rotation != 0 or matrix[0] <= 0 or matrix[3] <= 0 or abs(matrix[1]) + abs(matrix[2]) > 1e-6:
        return None
    full = _image_bbox_from_matrix(matrix, page_bbox, page_rotation)
    if full is None or abs(full[1] - image_bbox[1]) > 0.5:
        return None
    bitmap = None
    try:
        metadata = pdfium_c.FPDF_IMAGEOBJ_METADATA()
        if (
            not pdfium_c.FPDFImageObj_GetImageMetadata(raw_obj, page, ctypes.byref(metadata))
            or not 0 < metadata.width * metadata.height <= 4_000_000
        ):
            return None
        raw_bitmap = pdfium_c.FPDFImageObj_GetBitmap(raw_obj)
        if not raw_bitmap:
            return None
        bitmap = pdfium.PdfBitmap.from_raw(raw_bitmap)
        with bitmap.to_pil() as original:
            fraction = _blank_image_top_fraction(original)
        top = full[1] + fraction * (full[3] - full[1])
        return (image_bbox[0], top, image_bbox[2], image_bbox[3]) if fraction and top < image_bbox[3] else None
    except Exception:
        return None
    finally:
        if bitmap is not None:
            bitmap.close()


def _is_smooth_page_background(raw_obj: Any, page: pdfium.PdfPage, image_bbox: BBox, page_bbox: BBox) -> bool:
    """Extract low-resolution color difference evidence only for nearly full-page images to distinguish between gradient backgrounds and content images containing text or graphics."""
    page_area = max(0.0, page_bbox[2] - page_bbox[0]) * max(0.0, page_bbox[3] - page_bbox[1])
    if page_area <= 0 or (image_bbox[2] - image_bbox[0]) * (image_bbox[3] - image_bbox[1]) < 0.95 * page_area:
        return False
    bitmap = None
    try:
        metadata = pdfium_c.FPDF_IMAGEOBJ_METADATA()
        if (
            not pdfium_c.FPDFImageObj_GetImageMetadata(raw_obj, page, ctypes.byref(metadata))
            or metadata.width * metadata.height > 16_000_000
        ):
            return False
        raw_bitmap = pdfium_c.FPDFImageObj_GetBitmap(raw_obj)
        if not raw_bitmap:
            return False
        bitmap = pdfium.PdfBitmap.from_raw(raw_bitmap)
        with bitmap.to_pil() as original, original.convert("RGB") as rgb, rgb.resize((64, 64)) as small:
            pixels = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
        differences = [
            max(abs(a - b) for a, b in zip(pixels[y * 64 + x], pixels[(y + dy) * 64 + x + dx]))
            for y in range(63)
            for x in range(63)
            for dx, dy in ((1, 0), (0, 1))
        ]
        return sum(value > 12 for value in differences) / len(differences) <= 0.015
    except Exception:
        # Evidence read failure retains the image, content cannot be deleted due to corrupted objects or unsupported encoding.
        return False
    finally:
        if bitmap is not None:
            bitmap.close()


def _extract_page_form_bboxes(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
) -> list[BBox]:
    """Extract the top-level Form bbox while the caller holds the PDFium lock, and isolate the single-object exception."""

    form_bboxes: list[BBox] = []
    for raw_obj in _iter_raw_root_form_objects(page):
        try:
            form_bbox = _form_bbox_from_object(raw_obj, page_bbox, page_rotation)
            clipped = _clipped_form_extent(raw_obj, page_bbox, page_rotation)
            if form_bbox is not None and clipped is not None:
                form_bbox = _intersect_object_bbox(form_bbox, clipped)
                if form_bbox[2] <= form_bbox[0] or form_bbox[3] <= form_bbox[1]:
                    form_bbox = None
        except Exception:
            # PDFium When encountering individual damaged Form, keep other valid results on the same page.
            continue
        if form_bbox is not None:
            form_bboxes.append(form_bbox)
    return sorted(form_bboxes, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))


def _extract_page_path_infos(page: pdfium.PdfPage, page_bbox: BBox, page_rotation: int) -> list[PDFPathInfo]:
    """Reuse unified extraction with clipping to ensure consistency between direct query and snapshot."""
    return _extract_page_paths_and_lines(page, page_bbox, page_rotation, want_lines=False)[1]


def _extract_page_paths_and_lines(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
    *,
    want_lines: bool = True,
    want_path_infos: bool = True,
) -> tuple[list[PDFDrawingLine], list[PDFPathInfo]]:
    """Traverse and decode Path in one go, isolating the derived exceptions for drawing lines and path information respectively.

    When only one side of the result is needed, use want_* to turn off the other side to avoid discarding it after construction.
    PDFPathInfo/Plot Line Overhead; having both sides turned off has no meaning and is not a supported usage.
    """

    drawing_lines: list[PDFDrawingLine] = []
    path_infos: list[PDFPathInfo] = []
    standard_path = (
        _path_info_from_object is _STANDARD_PATH_INFO_EXTRACTOR and _extract_path_drawing_lines is _STANDARD_PATH_LINE_EXTRACTOR
    )
    if standard_path:
        from ._object_bridge import read_path_evidence

        native_records = read_path_evidence(
            page,
            page_bbox,
            page_rotation,
            DRAWING_FORM_MAX_DEPTH,
            want_lines=want_lines,
            want_path_infos=want_path_infos,
        )
        if native_records is not None:
            native_lines, native_infos = native_records
            drawing_lines = [
                PDFDrawingLine(start, end, bbox, width, "horizontal" if orientation == 0 else "vertical")
                for start, end, bbox, width, orientation in native_lines
            ]
            path_infos = [PDFPathInfo(*record[:7], tuple(record[7])) for record in native_infos]
            merged_lines = (
                _merge_collinear_drawing_lines(drawing_lines, _drawing_page_size(page_bbox, page_rotation))
                if want_lines
                else drawing_lines
            )
            return merged_lines, path_infos
    for source_index, member in enumerate(_clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_PATH)):
        raw_obj, matrix, form_depth = member.raw, member.matrix, member.depth
        try:
            subpaths = _read_raw_path_subpaths(raw_obj)
        except Exception:
            continue
        if standard_path:
            try:
                candidates, info = _path_object_evidence(
                    raw_obj,
                    matrix,
                    page_bbox,
                    page_rotation,
                    form_depth,
                    source_index,
                    subpaths=subpaths,
                    want_lines=want_lines,
                    want_path_infos=want_path_infos,
                )
            except Exception:
                continue
            if want_lines:
                for line in candidates:
                    clipped = _clip_object_visual_bbox(line.bbox, member.clip, page_bbox, page_rotation)
                    if clipped is not None:
                        if clipped == line.bbox:
                            drawing_lines.append(line)
                        else:
                            if line.orientation == "horizontal":
                                y = min(clipped[3], max(clipped[1], line.start[1]))
                                start, end = (clipped[0], y), (clipped[2], y)
                            else:
                                x = min(clipped[2], max(clipped[0], line.start[0]))
                                start, end = (x, clipped[1]), (x, clipped[3])
                            drawing_lines.append(replace(line, bbox=clipped, start=start, end=end))
            if want_path_infos and info is not None:
                clipped = _clip_object_visual_bbox(info.bbox, member.clip, page_bbox, page_rotation)
                if clipped is not None:
                    rectangles = tuple(
                        box
                        for rectangle in info.rectangle_bboxes
                        if (box := _clip_object_visual_bbox(rectangle, member.clip, page_bbox, page_rotation)) is not None
                    )
                    path_infos.append(replace(info, bbox=clipped, rectangle_bboxes=rectangles))
            continue
        if want_lines:
            try:
                for line in _extract_path_drawing_lines(raw_obj, matrix, page_bbox, page_rotation, subpaths=subpaths):
                    clipped = _clip_object_visual_bbox(line.bbox, member.clip, page_bbox, page_rotation)
                    if clipped is not None:
                        if clipped == line.bbox:
                            drawing_lines.append(line)
                        else:
                            if line.orientation == "horizontal":
                                y = min(clipped[3], max(clipped[1], line.start[1]))
                                start, end = (clipped[0], y), (clipped[2], y)
                            else:
                                x = min(clipped[2], max(clipped[0], line.start[0]))
                                start, end = (x, clipped[1]), (x, clipped[3])
                            drawing_lines.append(replace(line, bbox=clipped, start=start, end=end))
            except Exception:
                pass
        if want_path_infos:
            try:
                info = _path_info_from_object(
                    raw_obj,
                    matrix,
                    page_bbox,
                    page_rotation,
                    form_depth,
                    source_index,
                    subpaths=subpaths,
                )
            except Exception:
                continue
            if info is not None:
                clipped = _clip_object_visual_bbox(info.bbox, member.clip, page_bbox, page_rotation)
                if clipped is not None:
                    path_infos.append(info if clipped == info.bbox else replace(info, bbox=clipped))
    merged_lines = (
        _merge_collinear_drawing_lines(drawing_lines, _drawing_page_size(page_bbox, page_rotation))
        if want_lines
        else drawing_lines
    )
    return merged_lines, path_infos


def _extract_page_drawing_lines(page: pdfium.PdfPage, page_bbox: BBox, page_rotation: int) -> list[PDFDrawingLine]:
    """Reuse unified clipping to avoid independent line queries from re-exposing paths hidden by Form."""
    from ._object_bridge import read_drawing_lines

    records = read_drawing_lines(page, page_bbox, page_rotation)
    if records is not None:
        # The native layer only builds unmerged axes; collinear merging continues to use the stable output rules of Python.
        lines = [
            PDFDrawingLine(start, end, bbox, width, "horizontal" if orientation == 0 else "vertical")
            for start, end, bbox, width, orientation in records
        ]
        return _merge_collinear_drawing_lines(lines, _drawing_page_size(page_bbox, page_rotation))
    return _extract_page_paths_and_lines(page, page_bbox, page_rotation, want_path_infos=False)[0]
