"""用源文字与绘图轨道交叉确认被图件承接的原生表格。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from ....schema import BBox
from .geometry import _bbox_center_x, _bbox_center_y, _bbox_overlap_in_first, _bbox_union_many, _transform_axis_lines
from .table_rules import (
    _build_fragments,
    _closed_grid_vertical_track_positions,
    _cluster_fragment_rows,
    _count_occupied_closed_grid_columns,
    _count_stable_columns,
    _group_long_horizontal_rules,
)

if TYPE_CHECKING:
    from ....document.pdf.native_contracts import PDFPathInfo
    from .models import _AxisLine, _LineItem, _LocalAxisLine, _VisualRow

_NUMBER = re.compile(r"[\s$€£¥+\-−(]*\d[\d.,’'\s]*[%％]?[\s)$€£¥]*")


@dataclass(frozen=True, slots=True)
class VisualTableEvidence:
    """储存已经得到实物和文字双重验证的表区及可解释计数。"""

    bbox: BBox
    rows: int
    columns: int
    support: str


def collect_visual_tables(
    lines: list[_LineItem],
    drawing_lines: list[_AxisLine],
    path_infos: list[PDFPathInfo],
    page_size: tuple[float, float],
) -> tuple[VisualTableEvidence, ...]:
    """复用同页已提取的印刷行与路径，收集少量具有物理轨道支撑的表区。"""
    horizontal = sum(line.orientation == "horizontal" for line in drawing_lines)
    if horizontal < 3 or len(lines) < 9:
        return ()
    fragments = _build_fragments([line for line in lines if line.angle == 0 and line.text.strip()], page_size)
    heights = [box[3] - box[1] for fragment in fragments if (box := fragment.local_bbox)[3] > box[1]]
    if len(heights) < 9:
        return ()
    em = float(median(heights))
    rules = _transform_axis_lines(drawing_lines, page_size, 0)
    groups = _group_long_horizontal_rules(rules, em)
    shaded = [
        rect
        for path in path_infos
        if path.fill_visible and path.fill_rgba and 0 < max(path.fill_rgba[:3]) and min(path.fill_rgba[:3]) < 245
        for rect in path.rectangle_bboxes
        if 0.55 * em <= rect[3] - rect[1] <= 2.2 * em and rect[2] - rect[0] >= 2.0 * em
    ]
    if not shaded and not any(len(group) >= 3 for group in groups):
        return ()
    rows = _cluster_fragment_rows(fragments, em)
    return (*_ruled_tables(groups, rules, rows, em), *_shaded_numeric_tables(shaded, rows, em))


def _ruled_tables(
    groups: list[list[_LocalAxisLine]],
    rules: list[_LocalAxisLine],
    rows: list[_VisualRow],
    em: float,
) -> list[VisualTableEvidence]:
    """以闭合列轨验证多行，或以三条横边界验证紧凑的三列数值表。"""
    regions = []
    for group in groups:
        if len(group) < 3:
            continue
        extent = _bbox_union_many([rule.bbox for rule in group])
        tracks = _closed_grid_vertical_track_positions(group, rules, em)
        if len(group) >= 4 and len(tracks) >= 4 and max(abs(tracks[0] - extent[0]), abs(tracks[-1] - extent[2])) <= 0.4 * em:
            occupied = sum(
                _count_occupied_closed_grid_columns(
                    [row for row in rows if _bbox_center_y(top.bbox) < row.center_y < _bbox_center_y(bottom.bbox)],
                    tracks,
                )
                >= 3
                for top, bottom in zip(group, group[1:])
            )
            if occupied >= 3:
                regions.append(VisualTableEvidence(extent, occupied, len(tracks) - 1, "闭合横竖轨"))
                continue
        for first, middle, last in zip(group, group[1:], group[2:]):
            region = _bbox_union_many([first.bbox, middle.bbox, last.bbox])
            if region[3] - region[1] > 9 * em:
                continue
            middle_y = _bbox_center_y(middle.bbox)
            dense = [
                row
                for row in rows
                if len(row.fragments) >= 3
                and all(_bbox_overlap_in_first(fragment.local_bbox, region) >= 0.85 for fragment in row.fragments)
            ]
            if len(dense) < 3 or not min(row.center_y for row in dense) < middle_y < max(row.center_y for row in dense):
                continue
            numeric_rows = sum(
                any(letter.isalpha() for letter in row.fragments[0].text)
                and all(_NUMBER.fullmatch(fragment.text) for fragment in row.fragments[-2:])
                for row in dense
            )
            columns, _ = _count_stable_columns(dense, em)
            if numeric_rows >= 2 and columns >= 3:
                regions.append(VisualTableEvidence(region, len(dense), columns, "重复列轨与三条横线"))
    return regions


def _shaded_numeric_tables(
    fills: list[BBox],
    rows: list[_VisualRow],
    em: float,
) -> list[VisualTableEvidence]:
    """要求重复着色与两个分离的数值列共同支持多行表区。"""
    if len(fills) < 4:
        return []
    regions = []
    band: list[tuple[BBox, BBox, BBox, bool]] = []
    for row in rows:
        fragments = row.fragments
        if len(fragments) < 3:
            continue
        label = fragments[0]
        numeric = [fragment.local_bbox for fragment in fragments[1:] if _NUMBER.fullmatch(fragment.text)]
        if not any(letter.isalpha() for letter in label.text) or len(numeric) < 2:
            continue
        left, right = numeric[-2:]
        if _bbox_center_x(right) - _bbox_center_x(left) < 4 * em or left[0] - label.local_bbox[2] < 0.5 * em:
            continue
        shaded = all(
            any(box[0] <= _bbox_center_x(value) <= box[2] and box[1] <= row.center_y <= box[3] for box in fills)
            for value in (left, right)
        )
        current = (label.local_bbox, left, right, shaded)
        if band and (
            current[0][1] - band[-1][0][3] > 4.5 * em
            or abs(current[0][0] - band[0][0][0]) > 2 * em
            or any(abs(_bbox_center_x(a) - _bbox_center_x(b)) > 1.8 * em for a, b in zip(current[1:3], band[0][1:3]))
        ):
            if evidence := _shaded_band_evidence(band):
                regions.append(evidence)
            band.clear()
        band.append(current)
    if evidence := _shaded_band_evidence(band):
        regions.append(evidence)
    return regions


def _shaded_band_evidence(band: list[tuple[BBox, BBox, BBox, bool]]) -> VisualTableEvidence | None:
    """仅保留至少四个对齐行且两个以上金额行具底色支撑的条带。"""
    if len(band) < 4 or sum(shaded for _label, _left, _right, shaded in band) < 2:
        return None
    region = _bbox_union_many([box for row in band for box in row[:3]])
    return VisualTableEvidence(region, len(band), 3, "重复底纹与数值列轨")
