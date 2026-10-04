"""Native PDF Table structure recovery uses local coordinates and clustering primitives."""

from __future__ import annotations

from collections.abc import Iterable

from .....schema import BBox
from ..table_geometry import normalize_bbox, rotate_local_bbox


def normalize_angle(value: object) -> int:
    """Limit the input angle to the four standard directions supported by the form process."""

    try:
        angle = int(float(value or 0)) % 360
    except (TypeError, ValueError):
        return 0
    return angle if angle in {0, 90, 180, 270} else 0


def bbox_area(bbox: BBox) -> float:
    """Returns the nonnegative area of bbox."""

    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(
        0.0,
        float(bbox[3]) - float(bbox[1]),
    )


def bbox_union(bboxes: Iterable[BBox]) -> BBox:
    """Returns a set of minimum bounding boxes for a valid bbox."""

    items = list(bboxes)
    if not items:
        raise ValueError("bbox union requires at least one bbox")
    return (
        min(item[0] for item in items),
        min(item[1] for item in items),
        max(item[2] for item in items),
        max(item[3] for item in items),
    )


def bbox_intersection(first: BBox, second: BBox) -> BBox | None:
    """Returns the valid intersection of two bbox, or returns empty if there is no intersection."""

    intersection = (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )
    return intersection if intersection[2] > intersection[0] and intersection[3] > intersection[1] else None


def bbox_overlap_ratio(inner: BBox, outer: BBox) -> float:
    """Returns the proportion of the area of inner covered by outer."""

    area = bbox_area(inner)
    intersection = bbox_intersection(inner, outer)
    if area <= 0 or intersection is None:
        return 0.0
    return min(1.0, bbox_area(intersection) / area)


def bbox_center(bbox: BBox) -> tuple[float, float]:
    """Returns the center coordinates of bbox."""

    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def page_bbox_to_table_local(
    bbox: BBox,
    table_bbox: BBox,
    angle: int,
) -> BBox | None:
    """Crop page bbox and convert to forward table local coordinates."""

    clipped = bbox_intersection(bbox, table_bbox)
    if clipped is None:
        return None
    width = table_bbox[2] - table_bbox[0]
    height = table_bbox[3] - table_bbox[1]
    relative = (
        clipped[0] - table_bbox[0],
        clipped[1] - table_bbox[1],
        clipped[2] - table_bbox[0],
        clipped[3] - table_bbox[1],
    )
    return rotate_local_bbox(relative, width, height, angle)


def table_local_size(table_bbox: BBox, angle: int) -> tuple[float, float]:
    """Returns the local width and height of the table after rotation."""

    width = table_bbox[2] - table_bbox[0]
    height = table_bbox[3] - table_bbox[1]
    return (height, width) if angle in {90, 270} else (width, height)


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Limit floating point values to a closed interval."""

    return max(minimum, min(maximum, value))


def cluster_positions(values: Iterable[float], tolerance: float) -> list[float]:
    """Clusters one-dimensional coordinates by neighbor distance and returns the mean of each cluster."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return []
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        current_mean = sum(clusters[-1]) / len(clusters[-1])
        if abs(value - current_mean) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def covered_interval_ratio(
    intervals: Iterable[tuple[float, float]],
    start: float,
    end: float,
) -> float:
    """Returns the union coverage ratio of several intervals on the target interval."""

    if end <= start:
        return 0.0
    clipped = sorted(
        (max(start, min(first, second)), min(end, max(first, second)))
        for first, second in intervals
        if min(end, max(first, second)) > max(start, min(first, second))
    )
    if not clipped:
        return 0.0
    covered = 0.0
    current_start, current_end = clipped[0]
    for item_start, item_end in clipped[1:]:
        if item_start <= current_end:
            current_end = max(current_end, item_end)
            continue
        covered += current_end - current_start
        current_start, current_end = item_start, item_end
    covered += current_end - current_start
    return min(1.0, covered / (end - start))


__all__ = [
    "bbox_area",
    "bbox_center",
    "bbox_intersection",
    "bbox_overlap_ratio",
    "bbox_union",
    "clamp",
    "cluster_positions",
    "covered_interval_ratio",
    "normalize_angle",
    "normalize_bbox",
    "page_bbox_to_table_local",
    "table_local_size",
]
