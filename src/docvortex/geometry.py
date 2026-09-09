"""稳定的几何 SDK；不加载 PDFium 或图像编解码实现。"""

from .foundation._coordinates import (
    bbox_center,
    bbox_to_quad,
    convert_bbox,
    normalize_bbox,
    normalize_quarter_turn_angle,
    rotate_bbox,
)
from .foundation._geometry import (
    bbox_center_distance,
    bbox_distance,
    bbox_relative_pos,
    calculate_overlap_area_2_minbox_area_ratio,
    calculate_overlap_area_in_bbox1_area_ratio,
    normalize_to_int_bbox,
)

__all__ = [
    "normalize_bbox",
    "normalize_to_int_bbox",
    "bbox_relative_pos",
    "bbox_distance",
    "bbox_center_distance",
    "calculate_overlap_area_2_minbox_area_ratio",
    "calculate_overlap_area_in_bbox1_area_ratio",
    "bbox_to_quad",
    "bbox_center",
    "normalize_quarter_turn_angle",
    "rotate_bbox",
    "convert_bbox",
]
