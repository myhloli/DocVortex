"""提供标题分类共享的几何与文本规则。"""

from __future__ import annotations

import re

from .....schema import BBox
from ..geometry import _bbox_axis_overlap_ratio, _bbox_center_x, _bbox_center_y
from ..line_layout import _line_effective_height
from ..models import _LineItem

_NUMBERED_SECTION_TITLE_RE = re.compile(
    r"^(?P<number>(?:[A-Z]\s*\.\s*)?\d+(?:\s*\.\s*\d+)*)\s+(?P<label>\S.*)$",
)


_SECTION_NUMBER_ONLY_RE = re.compile(
    r"^\d+(?:\s*\.\s*\d+)*\.?$",
)


_SECTION_TITLE_TERMINAL_RE = re.compile(
    r"[.!?。！？:：;；,，]$",
)


_UNNUMBERED_SECTION_HEADING_RE = re.compile(
    r"^(?:introduction|references?|bibliography|acknowledg(?:e)?ments?|引言|绪论|参考文献|参考资料)$",
    re.IGNORECASE,
)


def _build_physical_title_gap_map(line_geometry: list[tuple[_LineItem, BBox]]) -> dict[int, tuple[float | None, float | None]]:
    """将稳定行身份与当前几何一次交给原生内核计算上下物理净空。"""
    from ....._compute_backend import get_native

    row_ids = {}
    records = [
        (
            id(line),
            None if line.visual_row_id is None else row_ids.setdefault(line.visual_row_id, len(row_ids)),
            box,
            _line_effective_height(line, box),
            line.restored_inline_cluster,
        )
        for line, box in line_geometry
    ]
    values = get_native().title_gaps(records)
    if values is None:
        raise ValueError("native title gap calculation rejected normalized geometry")
    return {line.source_index: value for (line, _box), value in zip(line_geometry, values, strict=True)}


def _line_inside_visual_container(
    line_bbox: BBox,
    container_bboxes: list[BBox],
) -> bool:
    """检查文本行中心是否落入视觉容器，容器内标签不得借标题原型晋升。"""

    center_x = _bbox_center_x(line_bbox)
    center_y = _bbox_center_y(line_bbox)
    return any(bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3] for bbox in container_bboxes)


def _line_near_visual_container(
    line_bbox: BBox,
    container_bboxes: list[BBox],
    body_height: float,
) -> bool:
    """检查短行是否紧邻图、表等容器，以纯几何方式抑制 caption 误判。"""

    for container_bbox in container_bboxes:
        if _bbox_axis_overlap_ratio(line_bbox, container_bbox, axis="x") < 0.35:
            continue
        container_width = max(0.1, container_bbox[2] - container_bbox[0])
        if line_bbox[2] - line_bbox[0] > 0.8 * container_width:
            continue
        vertical_gap = max(line_bbox[1] - container_bbox[3], container_bbox[1] - line_bbox[3], 0.0)
        if vertical_gap <= 2.0 * body_height:
            return True
    return False


__all__ = [
    "_NUMBERED_SECTION_TITLE_RE",
    "_SECTION_NUMBER_ONLY_RE",
    "_SECTION_TITLE_TERMINAL_RE",
    "_UNNUMBERED_SECTION_HEADING_RE",
    "_build_physical_title_gap_map",
    "_line_inside_visual_container",
    "_line_near_visual_container",
]
