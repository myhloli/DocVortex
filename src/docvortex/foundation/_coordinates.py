"""Coordinate primitives that do not rely on the PDF runtime or image codec."""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
from loguru import logger

from ..schema import BBox


def bbox_to_quad(bbox: list[float] | tuple[float, ...]) -> np.ndarray:
    """Converts an axis-aligned rectangle to four-point coordinates in bounds order."""
    x0, y0, x1, y1 = [float(v) for v in bbox]
    return np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)


def bbox_center(bbox: BBox) -> tuple[float, float]:
    """Calculate bbox center point, used to determine which table the picture or formula should belong to."""
    return (float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0


def normalize_quarter_turn_angle(angle: Any) -> int:
    """The standard visual block angle is 0/90/180/270, and unrecognizable angles are treated as 0."""
    try:
        normalized_angle = int(float(angle or 0)) % 360
    except (TypeError, ValueError):
        logger.warning(f"Unsupported visual block angle: {angle}, using 0")
        return 0
    if normalized_angle not in {0, 90, 180, 270}:
        logger.warning(f"Unsupported visual block angle: {angle}, using 0")
        return 0
    return normalized_angle


def rotate_bbox(
    bbox: BBox,
    image_width: float,
    image_height: float,
    angle: int,
) -> BBox:
    """Synchronously convert bbox in the original table cropping to the rotated cropping coordinate system."""
    x0, y0, x1, y1 = [float(value) for value in bbox]
    if angle == 270:
        # Rotated 90 degrees clockwise, the new x axis comes from the opposite direction of the original y axis.
        return (image_height - y1, x0, image_height - y0, x1)
    if angle == 90:
        # After rotating 90 degrees counterclockwise, the new y axis comes from the opposite direction of the original x axis.
        return (y0, image_width - x1, y1, image_width - x0)
    if angle == 180:
        return (image_width - x1, image_height - y1, image_width - x0, image_height - y0)
    return (x0, y0, x1, y1)


def convert_bbox(
    bbox: BBox | None,
    *,
    source_space: Literal["unit", "pixel", "point"],
    target_space: Literal["unit", "pixel", "point"],
    page_size: tuple[float, float],
    render_scale: float = 1.0,
    clip: bool = False,
) -> BBox | None:
    """Explicitly transform the coordinate space; page_size uses PDF to point, and render_scale represents the number of pixels per point."""
    spaces = {"unit", "pixel", "point"}
    if source_space not in spaces or target_space not in spaces:
        raise ValueError("Unknown coordinate space")
    if bbox is None or len(bbox) != 4 or min(page_size) <= 0 or render_scale <= 0:
        return None
    try:
        x0, y0, x1, y1 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    width, height = page_size
    sizes = {"unit": (1.0, 1.0), "point": (width, height), "pixel": (width * render_scale, height * render_scale)}
    source_width, source_height = sizes[source_space]
    target_width, target_height = sizes[target_space]
    if source_space == "pixel" and target_space == "point":
        x0, x1, y0, y1 = x0 / render_scale, x1 / render_scale, y0 / render_scale, y1 / render_scale
    elif target_space == "unit":
        x0, x1, y0, y1 = x0 / source_width, x1 / source_width, y0 / source_height, y1 / source_height
    else:
        scale_x, scale_y = target_width / source_width, target_height / source_height
        x0, x1 = x0 * scale_x, x1 * scale_x
        y0, y1 = y0 * scale_y, y1 * scale_y
    if clip:
        x0, x1 = max(0.0, min(target_width, x0)), max(0.0, min(target_width, x1))
        y0, y1 = max(0.0, min(target_height, y0)), max(0.0, min(target_height, y1))
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def normalize_bbox(raw_bbox: Any) -> BBox | None:
    """Verify the four-point box of model block and return floating point coordinates that can be used to determine area inclusion."""
    try:
        if raw_bbox is None or len(raw_bbox) != 4:
            return None
        bbox = tuple(float(value) for value in raw_bbox)
    except (TypeError, ValueError):
        return None

    if not all(math.isfinite(value) for value in bbox):
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox
