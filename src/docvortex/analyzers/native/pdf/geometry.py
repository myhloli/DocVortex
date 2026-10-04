"""Flash Native PDF extraction using pure geometry tools."""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence


from ....schema import BBox
from ....document.pdf.text._contracts import Bbox as CharacterBbox

from .models import _AxisLine, _LocalAxisLine


def _horizontal_bbox_gap(first_bbox: BBox, second_bbox: BBox) -> float:
    """Returns the undirectional headroom of two local bboxs on the x axis, zero when overlapping."""

    return max(first_bbox[0] - second_bbox[2], second_bbox[0] - first_bbox[2], 0.0)


def _transform_axis_lines(
    lines: list[_AxisLine],
    page_size: tuple[float, float],
    angle: int,
) -> list[_LocalAxisLine]:
    """Convert the horizontal and vertical lines of the original page to the local coordinates of the current text direction."""

    output: list[_LocalAxisLine] = []
    for line in lines:
        local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, angle)
        orientation: Literal["horizontal", "vertical"] = (
            "horizontal" if local_bbox[2] - local_bbox[0] >= local_bbox[3] - local_bbox[1] else "vertical"
        )
        output.append(
            _LocalAxisLine(
                bbox=local_bbox,
                original_bbox=line.bbox,
                orientation=orientation,
                width=line.width,
            )
        )
    return output


def _bbox_overlap_in_first(first: BBox, second: BBox) -> float:
    """Calculate the ratio of the intersection area to the area of the first bbox."""

    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    first_area = _bbox_area(first)
    return width * height / first_area if first_area > 0 else 0.0


def _bbox_area(bbox: BBox) -> float:
    """Returns the non-negative area of legal bbox."""

    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _normalize_bbox_to_unit(
    bbox: BBox,
    page_size: tuple[float, float],
) -> list[float]:
    """Normalize the absolute bbox to the 0-1 unit interval, and ensure that the rounded width and height each occupy at least one tick."""

    page_width, page_height = page_size
    ticks = [
        max(0, min(1000, int(round(bbox[0] / page_width * 1000)))),
        max(0, min(1000, int(round(bbox[1] / page_height * 1000)))),
        max(0, min(1000, int(round(bbox[2] / page_width * 1000)))),
        max(0, min(1000, int(round(bbox[3] / page_height * 1000)))),
    ]
    if ticks[2] <= ticks[0]:
        if ticks[0] < 1000:
            ticks[2] = ticks[0] + 1
        else:
            ticks[0] = max(0, ticks[2] - 1)
    if ticks[3] <= ticks[1]:
        if ticks[1] < 1000:
            ticks[3] = ticks[1] + 1
        else:
            ticks[1] = max(0, ticks[3] - 1)
    return [tick / 1000 for tick in ticks]


def _rotate_bbox_to_upright(
    bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
) -> BBox:
    """Moves page bbox to the positive local coordinates of the current text direction."""

    page_width, page_height = page_size
    x0, y0, x1, y1 = bbox
    if angle == 270:
        return (page_height - y1, x0, page_height - y0, x1)
    if angle == 90:
        return (y0, page_width - x1, y1, page_width - x0)
    if angle == 180:
        return (page_width - x1, page_height - y1, page_width - x0, page_height - y0)
    return bbox


def _rotate_bbox_from_upright(
    bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
) -> BBox:
    """Inversely transform forward local bbox back to PDF page coordinates."""

    page_width, page_height = page_size
    x0, y0, x1, y1 = bbox
    if angle == 270:
        return (y0, page_height - x1, y1, page_height - x0)
    if angle == 90:
        return (page_width - y1, x0, page_width - y0, x1)
    if angle == 180:
        return (page_width - x1, page_height - y1, page_width - x0, page_height - y0)
    return bbox


def _rotate_origin_to_upright(
    origin: tuple[float, float],
    page_size: tuple[float, float],
    angle: int,
) -> tuple[float, float]:
    """Move page character origin to the positive local coordinate of the current text direction."""

    page_width, page_height = page_size
    x, y = origin
    if angle == 270:
        return (page_height - y, x)
    if angle == 90:
        return (y, page_width - x)
    if angle == 180:
        return (page_width - x, page_height - y)
    return origin


def _coerce_bbox(value: Any) -> BBox | None:
    """Normalize any quaternion bbox to non-degenerate floating point coordinates."""

    # The own Bbox is a fixed slot container in the hot path; first read the internal according to the original verification rules
    # Four floats, avoid generic iterative transformations and repeated subscript calls, and abnormal shapes still follow the old path.
    if type(value) is CharacterBbox:
        raw = value.bbox
        if (
            type(raw) is list
            and len(raw) == 4
            and all(type(item) is float and math.isfinite(item) for item in raw)
            and raw[0] < raw[2]
            and raw[1] < raw[3]
        ):
            return (raw[0], raw[1], raw[2], raw[3])
    try:
        x0, y0, x1, y1 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if not all(math.isfinite(item) for item in (left, top, right, bottom)):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _clip_bbox(
    bbox: BBox | None,
    page_size: tuple[float, float],
) -> BBox | None:
    """Crops bbox to the page range and the degradation box returns None."""

    if bbox is None:
        return None
    page_width, page_height = page_size
    return _coerce_bbox(
        (
            max(0.0, min(page_width, bbox[0])),
            max(0.0, min(page_height, bbox[1])),
            max(0.0, min(page_width, bbox[2])),
            max(0.0, min(page_height, bbox[3])),
        )
    )


def _clip_validated_bbox(bbox: BBox | None, page_size: tuple[float, float]) -> BBox | None:
    """Reuse the verified limited forward frame at this stage; do not convert, sort or verify the same set of coordinates again."""
    if bbox is None:
        return None
    page_width, page_height = page_size
    left = float(max(0.0, min(page_width, bbox[0])))
    top = float(max(0.0, min(page_height, bbox[1])))
    right = float(max(0.0, min(page_width, bbox[2])))
    bottom = float(max(0.0, min(page_height, bbox[3])))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bbox_union(first: BBox, second: BBox) -> BBox:
    """Returns the external union box of two bbox."""

    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _bbox_union_many(bboxes: Sequence[BBox]) -> BBox:
    """Returns the bounding union box of the non-empty bbox sequence."""

    if not bboxes:
        raise ValueError("bbox sequence must not be empty")
    result = bboxes[0]
    for bbox in bboxes[1:]:
        result = _bbox_union(result, bbox)
    return result


def _expand_bbox(bbox: BBox, margin: float) -> BBox:
    """Expand bbox to all sides, only for geometric tolerance determination."""

    return (
        bbox[0] - margin,
        bbox[1] - margin,
        bbox[2] + margin,
        bbox[3] + margin,
    )


def _bbox_center_x(bbox: BBox) -> float:
    """Returns the horizontal center of bbox."""

    return (bbox[0] + bbox[2]) / 2.0


def _bbox_center_y(bbox: BBox) -> float:
    """Returns the vertical center of bbox."""

    return (bbox[1] + bbox[3]) / 2.0


def _bbox_intersects(first: BBox, second: BBox) -> bool:
    """Check for positive area overlap of two bboxs."""

    return min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1])


def _bbox_distance(first: BBox, second: BBox) -> float:
    """Returns the Euclidean clearance distance of two bboxs, which is zero if they overlap or touch."""

    horizontal_gap = max(first[0] - second[2], second[0] - first[2], 0.0)
    vertical_gap = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(horizontal_gap, vertical_gap)


def _bbox_overlap_in_smaller(first: BBox, second: BBox) -> float:
    """Calculate the intersection area as a proportion of the smaller bbox area."""

    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = width * height
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    smaller_area = min(first_area, second_area)
    return intersection / smaller_area if smaller_area > 0 else 0.0


def _bbox_axis_overlap_ratio(
    first: BBox,
    second: BBox,
    *,
    axis: Literal["x", "y"],
) -> float:
    """Calculates the length of the overlap on the specified axis as a proportion of the length of the shorter axis."""

    if axis == "x":
        first_start, first_end = first[0], first[2]
        second_start, second_end = second[0], second[2]
    else:
        first_start, first_end = first[1], first[3]
        second_start, second_end = second[1], second[3]
    overlap = max(0.0, min(first_end, second_end) - max(first_start, second_start))
    shorter = min(first_end - first_start, second_end - second_start)
    return overlap / shorter if shorter > 0 else 0.0


def _point_in_bbox(point: tuple[float, float], bbox: BBox) -> bool:
    """Check if the point is inside or on the boundary of bbox."""

    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]
