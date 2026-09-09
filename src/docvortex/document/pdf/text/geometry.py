"""几何载荷与独立矩形类型之间的纯值转换。"""

from __future__ import annotations

from ._contracts import Bbox


def char_bbox_values(bbox: object) -> tuple[float, float, float, float] | None:
    """读取固定四元坐标，拒绝结构不完整的几何对象。"""
    if isinstance(bbox, Bbox):
        bbox = bbox.bbox
    if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
        return tuple(float(value) for value in bbox)
    return None


__all__ = ["char_bbox_values"]
