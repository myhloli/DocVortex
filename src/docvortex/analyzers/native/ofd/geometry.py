"""OFD Millimeter coordinates, affine matrices and bbox tools."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from ....schema import BBox
from .constants import MM_TO_POINTS

Point = tuple[float, float]
Quad = tuple[Point, Point, Point, Point]


@dataclass(frozen=True, slots=True)
class Affine:
    """Save OFD six-parameter affine matrix."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def apply(self, point: Point) -> Point:
        """Transform a local point into the target coordinate space."""
        x, y = point
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def compose(self, inner: Affine) -> Affine:
        """Return the combined result of executing inner first and then executing the current matrix."""
        return Affine(
            a=self.a * inner.a + self.c * inner.b,
            b=self.b * inner.a + self.d * inner.b,
            c=self.a * inner.c + self.c * inner.d,
            d=self.b * inner.c + self.d * inner.d,
            e=self.a * inner.e + self.c * inner.f + self.e,
            f=self.b * inner.e + self.d * inner.f + self.f,
        )

    @classmethod
    def translation(cls, x: float, y: float) -> Affine:
        """Construct a matrix containing only translations."""
        return cls(e=x, f=y)

    @classmethod
    def rotation(cls, angle: float) -> Affine:
        """Construct a matrix that rotates a specified angle around the local origin."""
        radians = math.radians(angle)
        cosine = math.cos(radians)
        sine = math.sin(radians)
        return cls(a=cosine, b=sine, c=-sine, d=cosine)


def parse_numbers(value: str | None, *, expected: int | None = None) -> tuple[float, ...] | None:
    """Parse white space separated finite values into tuples."""
    if not value:
        return None
    try:
        numbers = tuple(float(item) for item in value.replace(",", " ").split())
    except (TypeError, ValueError):
        return None
    if expected is not None and len(numbers) != expected:
        return None
    if not all(math.isfinite(item) for item in numbers):
        return None
    return numbers


def parse_st_box(value: str | None) -> BBox | None:
    """Convert x/y/width/height of OFD to x0/y0/x1/y1."""
    numbers = parse_numbers(value, expected=4)
    if numbers is None:
        return None
    x, y, width, height = numbers
    if width <= 0 or height <= 0:
        return None
    return (x, y, x + width, y + height)


def parse_affine(value: str | None) -> Affine:
    """Parse optional CTM, return the identity matrix when missing or illegal."""
    numbers = parse_numbers(value, expected=6)
    return Affine(*numbers) if numbers is not None else Affine()


def rect_quad(bbox: BBox) -> Quad:
    """Convert the axis-aligned rectangle to four clockwise points."""
    x0, y0, x1, y1 = bbox
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def quad_bbox(points: Iterable[Point]) -> BBox | None:
    """Compute an axis-aligned bounding box for a set of finite points."""
    materialized = list(points)
    if not materialized or not all(math.isfinite(value) for point in materialized for value in point):
        return None
    xs = [point[0] for point in materialized]
    ys = [point[1] for point in materialized]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def transform_quad(quad: Quad, transform: Affine) -> Quad:
    """Transform four points according to a given affine matrix."""
    return tuple(transform.apply(point) for point in quad)  # type: ignore[return-value]


def transform_bbox(bbox: BBox, transform: Affine) -> BBox | None:
    """Transform the four corners of the rectangle and return to the target space AABB."""
    return quad_bbox(transform_quad(rect_quad(bbox), transform))


def bbox_union(bboxes: Iterable[BBox]) -> BBox | None:
    """Merge all valid bbox."""
    materialized = list(bboxes)
    if not materialized:
        return None
    return (
        min(item[0] for item in materialized),
        min(item[1] for item in materialized),
        max(item[2] for item in materialized),
        max(item[3] for item in materialized),
    )


def bbox_intersection(first: BBox, second: BBox) -> BBox | None:
    """Return the non-degenerate intersection of two bboxs."""
    bbox = (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def bbox_center(bbox: BBox) -> Point:
    """Return bbox center point."""
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_area(bbox: BBox) -> float:
    """Returns the non-negative bbox area."""
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def bbox_overlap_ratio(first: BBox, second: BBox) -> float:
    """Return the area ratio of the relatively small intersection bbox."""
    intersection = bbox_intersection(first, second)
    smaller = min(bbox_area(first), bbox_area(second))
    return bbox_area(intersection) / smaller if intersection is not None and smaller > 0 else 0.0


def transform_angle(transform: Affine) -> float:
    """Return the page angle after the local X axis passes through the matrix."""
    return math.degrees(math.atan2(transform.b, transform.a)) % 360.0


def canonical_angle(angle: float, *, tolerance: float = 5.0) -> int:
    """The angles close to right angles converge to 0/90/180/270."""
    normalized = angle % 360.0
    nearest = min((0, 90, 180, 270, 360), key=lambda item: abs(normalized - item))
    if abs(normalized - nearest) <= tolerance:
        return nearest % 360
    return int(round(normalized)) % 360


def bbox_to_points(bbox: BBox) -> list[float]:
    """Convert millimeters bbox to point coordinates for use by shared XYCut."""
    return [value * MM_TO_POINTS for value in bbox]


def normalize_bbox(bbox: BBox, physical_box: BBox) -> list[float] | None:
    """Crop and normalize page bbox to PhysicalBox."""
    clipped = bbox_intersection(bbox, physical_box)
    if clipped is None:
        return None
    width = physical_box[2] - physical_box[0]
    height = physical_box[3] - physical_box[1]
    if width <= 0 or height <= 0:
        return None
    values = [
        round((clipped[0] - physical_box[0]) / width, 3),
        round((clipped[1] - physical_box[1]) / height, 3),
        round((clipped[2] - physical_box[0]) / width, 3),
        round((clipped[3] - physical_box[1]) / height, 3),
    ]
    values = [max(0.0, min(1.0, value)) for value in values]
    return values if values[2] > values[0] and values[3] > values[1] else None


__all__ = [
    "Affine",
    "Point",
    "Quad",
    "bbox_area",
    "bbox_center",
    "bbox_intersection",
    "bbox_overlap_ratio",
    "bbox_to_points",
    "bbox_union",
    "canonical_angle",
    "normalize_bbox",
    "parse_affine",
    "parse_numbers",
    "parse_st_box",
    "quad_bbox",
    "rect_quad",
    "transform_angle",
    "transform_bbox",
    "transform_quad",
]
