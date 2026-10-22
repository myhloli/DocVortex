"""PDF Path、Image 与 Form 对象提取，保持原生提取算法与资源语义。"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import math
from dataclasses import dataclass
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
    """保存对象坐标系和累积裁剪，避免把父坐标中的 clip 再乘一次对象矩阵。"""

    raw: Any
    matrix: tuple[float, float, float, float, float, float]
    parent_matrix: tuple[float, float, float, float, float, float]
    depth: int
    clip: BBox | None


def _intersect_object_bbox(first: BBox, second: BBox | None) -> BBox:
    """求边界交集，空交集保持为空矩形，不能重新当成无裁剪。"""
    if second is None:
        return first
    return max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3])


def _transform_object_bbox(bbox: BBox, transform: Any) -> BBox:
    """变换四角后取保守外框，兼容旋转和斜切。"""
    points = [transform((x, y)) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _object_clip_bbox(raw: Any, parent_matrix: tuple, inherited: BBox | None) -> BBox | None:
    """读取父坐标系中的裁剪路径；各路径相交，曲线以含控制点的保守外框约束。"""
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
        # 缺失或损坏的局部 clip 不能抹掉已经确认的父级裁剪。
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
    """由 Rust 遍历页面或 Form 子树，Python 仅包装借用叶子句柄。"""
    from ._object_bridge import read_clipped_objects

    root = (is_form, tuple(float(value) for value in parent_matrix), depth, inherited_clip)
    for address, matrix, parent, level, clip in read_clipped_objects(container, -1, max_depth, root=root):
        yield _ClippedObject(ctypes.cast(address, pdfium_c.FPDF_PAGEOBJECT), matrix, parent, level, clip)


def _clip_object_visual_bbox(bbox: BBox, clip: BBox | None, page_bbox: BBox, rotation: int) -> BBox | None:
    """在统一页面视觉坐标中应用累积裁剪。"""
    if clip is not None and (clip[2] <= clip[0] or clip[3] <= clip[1]):
        return None
    if clip is not None:
        bbox = _intersect_object_bbox(
            bbox, _transform_object_bbox(clip, lambda point: _transform_drawing_point(point, page_bbox, rotation))
        )
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def _clipped_form_extent(raw: Any, page_bbox: BBox, rotation: int) -> BBox | None:
    """仅在 Form 内存在裁剪时用可见叶子重建边界；无 clip 的既有输出保持原值。"""
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
    """物化 Rust 遍历得到的对象，保留同库句柄与稳定来源顺序。"""
    from ._object_bridge import read_clipped_objects

    for address, matrix, parent, depth, clip in read_clipped_objects(page, object_type, max_depth):
        yield _ClippedObject(ctypes.cast(address, pdfium_c.FPDF_PAGEOBJECT), matrix, parent, depth, clip)


def _text_object_visibility(
    page: Any, page_bbox: BBox, rotation: int, paint_page_reader=None
) -> dict[int, tuple[bool, BBox | None]]:
    """一次遍历建立文字对象的绘制状态与有效裁剪；地址只在本次页面提取内使用。"""
    from ._object_bridge import read_text_visibility

    records = read_text_visibility(page, page_bbox, rotation, DRAWING_FORM_MAX_DEPTH, with_roots=paint_page_reader is not None)
    roots = None
    if paint_page_reader is not None:
        records, roots, paint = records
    output = {address: (visible, clip) for address, visible, clip in records}
    if paint_page_reader is not None:
        from .text_occlusion import exclude_fully_overpainted_text

        if len(output) != len(records):
            # 非标准重复对象地址需要使用最终映射重新读框，保持原有合并可见性的语义。
            roots, paint = None, None
        output = exclude_fully_overpainted_text(page, page_bbox, rotation, output, paint_page_reader, roots=roots, paint=paint)
    return output


def _walk_raw_page_objects_with_depth(
    container: Any,
    *,
    is_form: bool,
    parent_matrix: tuple[float, float, float, float, float, float],
    depth: int,
    target_type: int,
) -> Iterator[tuple[Any, tuple[float, float, float, float, float, float], int]]:
    """递归遍历目标对象，并携带累积矩阵及所在 Form 深度。"""
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
            # 单个损坏页面对象不能中断同页其他目标对象遍历。
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
    """兼容既有调用，仅返回目标对象及累积到页面坐标的矩阵。"""

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
    """递归遍历页面或 Form 中的 Path，并携带累积到页面坐标的矩阵。"""
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
    """从页面根对象开始遍历全部 Path，包括嵌套 Form 中的 Path。"""
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
    """遍历全部 Path，并保留其所在 Form 深度供上层过滤图标。"""

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
    """从页面根对象开始遍历全部点阵图，包括嵌套 Form 中的图片。"""
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    yield from _walk_raw_page_objects(
        page,
        is_form=False,
        parent_matrix=identity,
        depth=0,
        target_type=pdfium_c.FPDF_PAGEOBJ_IMAGE,
    )


def _iter_raw_root_form_objects(page: pdfium.PdfPage) -> Iterator[Any]:
    """只遍历页面根层的 Form，避免把同一矢量图的嵌套子 Form 重复输出。"""

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
            # 单个损坏对象不能阻断同页其他顶层 Form 的提取。
            continue


def _get_raw_object_rgba(
    raw_obj: Any,
    color_getter: Any,
) -> tuple[int, int, int, int] | None:
    """读取对象 RGBA；旧 PDFium、不支持的对象或读取失败时返回 None。"""

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
    """读取对象颜色 alpha；旧 PDFium 或读取失败时按不透明处理。"""

    rgba = _get_raw_object_rgba(raw_obj, color_getter)
    return rgba[3] if rgba is not None else 255


def _read_raw_path_subpaths(raw_obj: Any) -> list[_PathSubpath]:
    """由原生解码器读取 Path 子路径，仅在 Python 中包装结果。"""
    from ._object_bridge import read_path_subpaths

    return [
        _PathSubpath(points, [(points[first], points[last]) for first, last in lines], closed)
        for points, lines, closed in read_path_subpaths(raw_obj)
    ]


def _transform_path_subpath(
    subpath: _PathSubpath,
    matrix: tuple[float, float, float, float, float, float],
    page_bbox: BBox,
    page_rotation: int,
) -> _PathSubpath:
    """将一个子路径从对象局部坐标转换为页面左上坐标。"""

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        """先应用对象/Form 矩阵，再应用页面坐标与旋转转换。"""
        return _transform_drawing_point(_apply_pdf_matrix(point, matrix), page_bbox, page_rotation)

    return _PathSubpath(
        points=[transform(point) for point in subpath.points],
        straight_segments=[(transform(start), transform(end)) for start, end in subpath.straight_segments],
        closed=subpath.closed,
    )


_FILLED_RECTANGLE_ROUND = round


def _filled_rectangle_bbox(subpath: _PathSubpath) -> BBox | None:
    """仅把四角与直边均轴对齐的填充子路径记录为矩形，曲线和字形轮廓不能借外框冒充柱体。"""
    points = subpath.points
    if len(points) not in (4, 5) or len(subpath.straight_segments) not in (3, 4):
        return None
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    if x1 <= x0 or y1 <= y0:
        return None
    # 已含完整四角时，相同 round 映射后的集合必然相同；近似矩形仍完整执行原容差判定。
    exact_corners = (
        round is _FILLED_RECTANGLE_ROUND
        and type(points) in (list, tuple)
        and all(
            type(point) is tuple and len(point) == 2 and type(point[0]) is float and type(point[1]) is float for point in points
        )
        and set(points) == {(x0, y0), (x0, y1), (x1, y0), (x1, y1)}
    )
    if not exact_corners:
        corners = {(round(x, 3), round(y, 3)) for x, y in points}
        if corners != {(round(x, 3), round(y, 3)) for x in (x0, x1) for y in (y0, y1)}:
            return None
    if any(abs(a[0] - b[0]) > 0.001 and abs(a[1] - b[1]) > 0.001 for a, b in subpath.straight_segments):
        return None
    return (x0, y0, x1, y1)


def _make_axis_drawing_line(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    page_size: tuple[float, float],
) -> PDFDrawingLine | None:
    """将近水平或近竖直线段吸附到坐标轴、裁剪到页面并生成公开结果。"""
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


def _line_axis_coordinate(line: PDFDrawingLine) -> float:
    """返回绘图线在垂直于自身方向的轴坐标。"""
    return line.start[1] if line.orientation == "horizontal" else line.start[0]


def _line_main_interval(line: PDFDrawingLine) -> tuple[float, float]:
    """返回绘图线沿自身方向的起止区间。"""
    if line.orientation == "horizontal":
        return line.start[0], line.end[0]
    return line.start[1], line.end[1]


def _combine_collinear_line_group(
    lines: list[PDFDrawingLine],
    page_size: tuple[float, float],
) -> PDFDrawingLine | None:
    """把坐标接近且区间相连的一组线合并为一条稳定中心线。"""
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
    """按轴坐标聚类，再合并间距不超过约 2pt 的同方向线段。"""
    if not lines:
        return []
    coordinate_clusters: list[list[PDFDrawingLine]] = []
    # 输入按轴坐标升序排序，聚类最小值恒为其首元素，记住起点即可免整簇重扫。
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
    """合并水平和竖直共线段，并按页面视觉位置稳定排序。"""
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
    """把点阵图单位矩形经对象/Form 矩阵转换并裁剪为页面左上坐标 bbox。"""
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
    """读取顶层 Form 的页面坐标边界，并转换、裁剪到视觉页面范围。"""

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
    """在调用方持有 PDFium 锁时提取全部有效点阵图 bbox，并隔离单对象异常。"""
    image_bboxes: list[BBox] = []
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_IMAGE):
        try:
            image_bbox = _image_bbox_from_matrix(member.matrix, page_bbox, page_rotation)
            if image_bbox is not None:
                image_bbox = _clip_object_visual_bbox(image_bbox, member.clip, page_bbox, page_rotation)
        except Exception:
            # 单个损坏 Image 对象不能中断同页其他点阵图提取。
            continue
        if image_bbox is not None:
            image_bboxes.append(image_bbox)
    return sorted(image_bboxes, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))


def _get_raw_image_fingerprint(raw_obj: Any, page: pdfium.PdfPage) -> str | None:
    """读取受限大小的图片原始流，并结合像素宽高生成 SHA-256 指纹。"""

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
    """提取全部有效点阵图几何；单图指纹失败时保留 bbox 并按放行处理。"""

    image_infos: list[PDFImageInfo] = []
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_IMAGE):
        raw_obj, matrix = member.raw, member.matrix
        try:
            image_bbox = _image_bbox_from_matrix(matrix, page_bbox, page_rotation)
            if image_bbox is not None:
                image_bbox = _clip_object_visual_bbox(image_bbox, member.clip, page_bbox, page_rotation)
        except Exception:
            # 单个损坏 Image 对象不能中断同页其他点阵图提取。
            continue
        if image_bbox is None:
            continue
        fingerprint = _get_raw_image_fingerprint(raw_obj, page)
        smooth, blank_top = _image_visual_evidence(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation)
        image_infos.append(
            PDFImageInfo(
                bbox=image_bbox,
                fingerprint=fingerprint,
                smooth_background=smooth,
                blank_top_bbox=blank_top,
            )
        )
    return sorted(
        image_infos,
        key=lambda info: (info.bbox[1], info.bbox[0], info.bbox[3], info.bbox[2]),
    )


def _blank_image_top_fraction(image) -> float:
    """按原生阈值识别全宽近白顶边，Pillow 只负责透明像素白底合成。"""
    from PIL import Image
    from ..._compute_backend import get_native

    if image.width * image.height > 4_000_000:
        return 0.0
    native = get_native()
    if "transparency" not in image.info:
        if image.mode == "RGB":
            return native.blank_top_rgb(image.tobytes(), image.width, image.height)
        if image.mode == "L":
            return native.blank_top_gray(image.tobytes(), image.width, image.height)
    with (
        image.convert("RGBA") as rgba,
        Image.new("RGBA", rgba.size, "white") as background,
        Image.alpha_composite(background, rgba) as flat,
        flat.convert("RGB") as rgb,
    ):
        return native.blank_top_rgb(rgb.tobytes(), rgb.width, rgb.height)


_BITMAP_BLANK_REFERENCES = (_blank_image_top_fraction, pdfium.PdfBitmap.to_pil, pdfium.PdfBitmap.__getattribute__)


def _bitmap_blank_top_fraction(bitmap):
    """锁内复制普通无透明位图的独立缓冲，保留 Pillow 通道与白边阈值，透明格式完整回退。"""
    from ..._compute_backend import get_native

    native = get_native()
    if (
        type(bitmap) is pdfium.PdfBitmap
        and (_blank_image_top_fraction, pdfium.PdfBitmap.to_pil, pdfium.PdfBitmap.__getattribute__) == _BITMAP_BLANK_REFERENCES
        and bitmap.mode in ("RGB", "BGR", "RGBX", "BGRX", "L")
    ):
        value = native.blank_top_bitmap(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, bitmap.mode)
        if value is not None:
            return value
    with bitmap.to_pil() as original:
        return _blank_image_top_fraction(original)


def _native_image_blank_top_bbox(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation):
    """正向轴对齐且未裁切顶部的原图才能提供白边证据，损坏或旋转对象均保守保留。"""
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
        fraction = _bitmap_blank_top_fraction(bitmap)
        top = full[1] + fraction * (full[3] - full[1])
        return (image_bbox[0], top, image_bbox[2], image_bbox[3]) if fraction and top < image_bbox[3] else None
    except Exception:
        return None
    finally:
        if bitmap is not None:
            bitmap.close()


def _is_smooth_page_background(raw_obj: Any, page: pdfium.PdfPage, image_bbox: BBox, page_bbox: BBox) -> bool:
    """仅为近整页图提取低分辨率色差证据，区分渐变背景与包含文字或图形的内容图。"""
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
        with bitmap.to_pil() as original:
            return _smooth_image_background_pixels(original)
    except Exception:
        # 证据读取失败保留图像，不能因损坏对象或不支持的编码删除内容。
        return False
    finally:
        if bitmap is not None:
            bitmap.close()


def _smooth_image_background_pixels(original):
    """独立 RGB 缓冲沿用 Pillow 默认缩放、色差阈值和计数顺序，不修改输入像素。"""
    with original.convert("RGB") as rgb, rgb.resize((64, 64)) as small:
        pixels = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    differences = [
        max(abs(a - b) for a, b in zip(pixels[y * 64 + x], pixels[(y + dy) * 64 + x + dx]))
        for y in range(63)
        for x in range(63)
        for dx, dy in ((1, 0), (0, 1))
    ]
    return sum(value > 12 for value in differences) / len(differences) <= 0.015


_IMAGE_VISUAL_REFERENCE_FUNCTIONS = (_is_smooth_page_background, _native_image_blank_top_bbox)


def _image_visual_evidence(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation):
    """同时符合两种像素检查条件时只解码一次，所有转换缓冲独立并在页面锁内释放。"""
    shared = _shared_image_visual_evidence(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation)
    if shared is not None:
        return shared
    return (
        _is_smooth_page_background(raw_obj, page, image_bbox, page_bbox),
        _native_image_blank_top_bbox(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation),
    )


def _shared_image_visual_evidence(raw_obj, page, matrix, image_bbox, page_bbox, page_rotation):
    """只复用成功解码的只读原图；不支持、损坏或替换参考函数时各自执行旧路径。"""
    if (_is_smooth_page_background, _native_image_blank_top_bbox) != _IMAGE_VISUAL_REFERENCE_FUNCTIONS:
        return None
    area = max(0.0, page_bbox[2] - page_bbox[0]) * max(0.0, page_bbox[3] - page_bbox[1])
    if (
        area <= 0
        or (image_bbox[2] - image_bbox[0]) * (image_bbox[3] - image_bbox[1]) < 0.95 * area
        or page_rotation != 0
        or matrix[0] <= 0
        or matrix[3] <= 0
        or abs(matrix[1]) + abs(matrix[2]) > 1e-6
    ):
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
            try:
                smooth = _smooth_image_background_pixels(original)
            except Exception:
                smooth = False
            try:
                fraction = _blank_image_top_fraction(original)
                top = full[1] + fraction * (full[3] - full[1])
                blank_top = (image_bbox[0], top, image_bbox[2], image_bbox[3]) if fraction and top < image_bbox[3] else None
            except Exception:
                blank_top = None
        return smooth, blank_top
    except Exception:
        return None
    finally:
        if bitmap is not None:
            bitmap.close()


def _extract_page_form_bboxes(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
) -> list[BBox]:
    """在调用方持有 PDFium 锁时提取顶层 Form bbox，并隔离单对象异常。"""

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
            # PDFium 遇到个别损坏 Form 时，保留同页其他有效结果。
            continue
        if form_bbox is not None:
            form_bboxes.append(form_bbox)
    return sorted(form_bboxes, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))


def _extract_page_path_infos(page: pdfium.PdfPage, page_bbox: BBox, page_rotation: int) -> list[PDFPathInfo]:
    """复用含裁剪的统一提取，确保直接查询和快照一致。"""
    return _extract_page_paths_and_lines(page, page_bbox, page_rotation, want_lines=False)[1]


def _extract_page_paths_and_lines(
    page: pdfium.PdfPage,
    page_bbox: BBox,
    page_rotation: int,
    *,
    want_lines: bool = True,
    want_path_infos: bool = True,
) -> tuple[list[PDFDrawingLine], list[PDFPathInfo]]:
    """一次原生遍历与解码 Path，Python 仅包装路径摘要并合并共线输出。"""
    from ._object_bridge import read_path_evidence

    native_lines, native_infos = read_path_evidence(
        page,
        page_bbox,
        page_rotation,
        DRAWING_FORM_MAX_DEPTH,
        want_lines=want_lines,
        want_path_infos=want_path_infos,
    )
    drawing_lines = [
        PDFDrawingLine(start, end, bbox, width, "horizontal" if orientation == 0 else "vertical")
        for start, end, bbox, width, orientation in native_lines
    ]
    path_infos = [PDFPathInfo(*record[:7], tuple(record[7])) for record in native_infos]
    return (
        _merge_collinear_drawing_lines(drawing_lines, _drawing_page_size(page_bbox, page_rotation))
        if want_lines
        else drawing_lines,
        path_infos,
    )


def _extract_page_drawing_lines(page: pdfium.PdfPage, page_bbox: BBox, page_rotation: int) -> list[PDFDrawingLine]:
    """复用统一裁剪，避免独立线查询重新暴露被 Form 隐藏的路径。"""
    from ._object_bridge import read_drawing_lines

    records = read_drawing_lines(page, page_bbox, page_rotation)
    if records is not None:
        # 原生层只构建未合并轴线；共线合并继续沿用 Python 的稳定输出规则。
        lines = [
            PDFDrawingLine(start, end, bbox, width, "horizontal" if orientation == 0 else "vertical")
            for start, end, bbox, width, orientation in records
        ]
        return _merge_collinear_drawing_lines(lines, _drawing_page_size(page_bbox, page_rotation))
    return _extract_page_paths_and_lines(page, page_bbox, page_rotation, want_path_infos=False)[0]
