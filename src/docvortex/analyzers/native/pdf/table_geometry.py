"""Provides table recovery and local geometry shared with text projection without changing the verification strategy of each business layer."""

from __future__ import annotations

from ....document.pdf.text._contracts import Bbox
from ....schema import BBox


def normalize_bbox(value: object) -> BBox | None:
    """Normalizes any quadruple to a valid floating point bbox, exception or degeneracy box returns empty."""

    # Own Bbox's subscripting protocol only forwards the underlying array, direct reading avoids five Python calls per box.
    # Only exact types are matched. Third-party iterable objects and subclasses that override subscript behavior still follow the original protocol.
    if type(value) is Bbox:
        value = value.bbox
    try:
        x0, y0, x1, y1 = [float(item) for item in value]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Uses the same comparison direction as a stable sort of two elements, preserving equal values and the order of NaN.
    left, right = (x1, x0) if x1 < x0 else (x0, x1)
    top, bottom = (y1, y0) if y1 < y0 else (y0, y1)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def rotate_local_bbox(
    bbox: BBox,
    width: float,
    height: float,
    angle: int,
) -> BBox:
    """Convert bbox in the table cropping frame to forward table local coordinates."""

    x0, y0, x1, y1 = bbox
    if angle == 270:
        return height - y1, x0, height - y0, x1
    if angle == 90:
        return y0, width - x1, y1, width - x0
    if angle == 180:
        return width - x1, height - y1, width - x0, height - y0
    return bbox


__all__ = ["normalize_bbox", "rotate_local_bbox"]
