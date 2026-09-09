"""PDF 规则线与文本行候选；保留原有认领顺序与判定规则。"""

from __future__ import annotations

import statistics
from typing import Any

from ....document.pdf._document import PDFPathInfo
from ....schema import BBox
from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_smaller,
    _bbox_union,
    _bbox_union_many,
    _point_in_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
    _transform_axis_lines,
)
from .models import _Fragment, _LineItem, _LocalAxisLine, _PageSource, _TableCandidate, _VisualRow
from .table_annotations import (
    _build_table_annotation,
    _collect_caption_rows,
    _collect_footnote_rows,
    _find_table_caption,
    _merge_table_candidate_annotations,
)
from .table_rows import _clip_visual_row_to_corridor


def _build_fragments(
    lines: list[_LineItem],
    page_size: tuple[float, float],
) -> list[_Fragment]:
    """将精修后的原生 run 转换成表格单元候选。"""

    fragments: list[_Fragment] = []
    for line in lines:
        local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, line.angle)
        fragments.append(
            _Fragment(
                text=line.text,
                bbox=line.bbox,
                local_bbox=local_bbox,
                line_index=line.source_index,
                # 复用原生粗行身份，避免同一字符行内不同 cell
                # 因轻微基线差异被误拆成多行。
                visual_row_id=line.visual_row_id,
            )
        )
    return fragments


def _cluster_fragment_rows(
    fragments: list[_Fragment],
    median_height: float,
) -> list[_VisualRow]:
    """优先复用原生视觉行身份，其余片段按中心线容差聚成表格行。"""

    tolerance = max(2.0, median_height * 0.5)
    native_groups: dict[int, list[_Fragment]] = {}
    geometric_fragments: list[_Fragment] = []
    for fragment in fragments:
        if fragment.visual_row_id is None:
            geometric_fragments.append(fragment)
        else:
            native_groups.setdefault(fragment.visual_row_id, []).append(fragment)

    # 先锁定同一原生粗行拆出的 run，再允许不同粗行按基线几何合并；
    # 旋转表格常把同一数据行的各 cell 分成多个 pdftext 粗行，不能只依赖 row id。
    seed_groups = [*native_groups.values(), *[[fragment] for fragment in geometric_fragments]]
    seed_groups.sort(
        key=lambda group: (
            statistics.fmean(_bbox_center_y(item.local_bbox) for item in group),
            min(item.local_bbox[0] for item in group),
        )
    )
    grouped: list[list[_Fragment]] = []
    for seed_group in seed_groups:
        center_y = statistics.fmean(_bbox_center_y(item.local_bbox) for item in seed_group)
        target_group: list[_Fragment] | None = None
        for group in grouped:
            group_center = statistics.fmean(_bbox_center_y(item.local_bbox) for item in group)
            if abs(center_y - group_center) <= tolerance:
                target_group = group
                break
        if target_group is None:
            grouped.append(list(seed_group))
        else:
            target_group.extend(seed_group)

    rows: list[_VisualRow] = []
    for group in grouped:
        group.sort(key=lambda item: item.local_bbox[0])
        bbox = _bbox_union_many([item.local_bbox for item in group])
        visual_row_ids = {item.visual_row_id for item in group if item.visual_row_id is not None}
        rows.append(
            _VisualRow(
                fragments=group,
                center_y=sum(_bbox_center_y(item.local_bbox) for item in group) / len(group),
                bbox=bbox,
                visual_row_id=next(iter(visual_row_ids)) if len(visual_row_ids) == 1 else None,
            )
        )
    rows.sort(key=lambda row: row.center_y)
    return rows


def _build_rule_table_candidates(
    rows: list[_VisualRow],
    lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    axis_lines: list[_LocalAxisLine],
    *,
    path_infos: list[PDFPathInfo] | None = None,
    excluded_bboxes: list[BBox] | None = None,
) -> list[_TableCandidate]:
    """枚举同跨度横线边界区间，再以连续多列文本分布确认表格。"""

    candidates: list[_TableCandidate] = []
    path_infos = path_infos or []
    excluded_bboxes = excluded_bboxes or []
    for rule_group in _group_long_horizontal_rules(axis_lines, median_height):
        for first_index, top_rule in enumerate(rule_group[:-1]):
            for bottom_index in range(first_index + 1, len(rule_group)):
                bottom_rule = rule_group[bottom_index]
                interval_rules = rule_group[first_index : bottom_index + 1]
                boundary_rules = [top_rule, bottom_rule]
                rule_bbox = _bbox_union_many([line.bbox for line in boundary_rules])
                core_rows = _rows_inside_rule_interval(
                    rows,
                    rule_bbox,
                    excluded_bboxes,
                )
                caption_line = _find_table_caption(
                    lines,
                    rule_bbox,
                    page_size,
                    angle,
                    median_height,
                )
                caption_anchored_compact_grid = (
                    caption_line is not None
                    and len(interval_rules) >= 3
                    and rule_bbox[3] - rule_bbox[1] <= 0.15 * (page_size[0] if angle in {90, 270} else page_size[1])
                )
                if (
                    not _every_rule_interval_has_multi_cell_row(
                        core_rows,
                        interval_rules,
                        median_height,
                    )
                    and not caption_anchored_compact_grid
                ):
                    continue
                if (
                    not _rule_intervals_are_column_compatible(
                        core_rows,
                        interval_rules,
                        median_height,
                    )
                    and not caption_anchored_compact_grid
                ):
                    continue

                fill_band_count = _count_repeated_fill_bands(
                    path_infos,
                    rule_bbox,
                    page_size,
                    angle,
                    median_height,
                )
                aligned_vertical_count = _count_aligned_vertical_rules(
                    axis_lines,
                    rule_bbox,
                    median_height,
                )

                row_segments = _continuous_table_row_segments(core_rows, median_height)
                accepted: tuple[list[_VisualRow], list[_VisualRow], int, float] | None = None
                for row_segment in row_segments:
                    dense_rows = [row for row in row_segment if len(row.fragments) >= 2]
                    compact_grid_columns = (
                        _compact_fully_ruled_grid_column_count(
                            row_segment,
                            dense_rows,
                            interval_rules,
                            axis_lines,
                            rule_bbox,
                            median_height,
                        )
                        if len(dense_rows) == 2
                        else 0
                    )
                    stable_columns, column_coverage = _count_stable_columns(
                        dense_rows,
                        median_height,
                    )
                    if compact_grid_columns > 0:
                        # 两行样本容易把左右/中心锚点误算成不同稳定列，使用物理网格列数。
                        stable_columns = compact_grid_columns
                    caption_supported_compact_rows = (
                        caption_anchored_compact_grid
                        and len(dense_rows) >= 2
                        and stable_columns >= 3
                        and column_coverage >= 0.5
                    )
                    if len(dense_rows) < 3 and compact_grid_columns == 0 and not caption_supported_compact_rows:
                        continue
                    # 真表格的多单元行会在整个数据带内反复出现；少数图题、图例和
                    # 坐标刻度偶然形成的多列行不能支撑一大片正文区域。
                    if len(dense_rows) / len(row_segment) < 0.2:
                        continue
                    if stable_columns < 2 or column_coverage < 0.5:
                        continue
                    if _looks_like_page_column_prose(
                        row_segment,
                        dense_rows,
                        stable_columns,
                        fill_band_count,
                        aligned_vertical_count,
                        rule_bbox,
                    ):
                        continue
                    if not _table_segment_reaches_boundaries(
                        row_segment,
                        rule_bbox,
                        median_height,
                    ):
                        continue
                    if not _table_rows_align_with_rule_span(
                        row_segment,
                        rule_bbox,
                        median_height,
                    ):
                        continue
                    result = (
                        row_segment,
                        dense_rows,
                        stable_columns,
                        column_coverage,
                    )
                    if accepted is None or (
                        len(row_segment),
                        len(dense_rows),
                        stable_columns,
                        column_coverage,
                    ) > (
                        len(accepted[0]),
                        len(accepted[1]),
                        accepted[2],
                        accepted[3],
                    ):
                        accepted = result
                if accepted is None:
                    continue

                accepted_rows, dense_rows, stable_columns, _coverage = accepted
                candidate = _expand_rule_table_candidate(
                    boundary_rules,
                    accepted_rows,
                    rows,
                    lines,
                    page_size,
                    angle,
                    median_height,
                    caption_line,
                )
                candidate.score = float(2 + len(dense_rows) + stable_columns + min(fill_band_count, 8))
                candidates.append(candidate)
    return _expand_candidates_to_connected_rule_grids(
        candidates,
        rows,
        page_size,
        angle,
        median_height,
        axis_lines,
        excluded_bboxes,
    )


def _expand_candidates_to_connected_rule_grids(
    candidates: list[_TableCandidate],
    rows: list[_VisualRow],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    axis_lines: list[_LocalAxisLine],
    excluded_bboxes: list[BBox],
) -> list[_TableCandidate]:
    """把已确认候选沿连续横边界和贯穿竖轨扩展到完整物理网格。"""

    grid_bboxes = [
        grid_bbox
        for grid_bbox in _connected_rule_grid_bboxes(axis_lines, median_height)
        if not any(_bbox_overlap_in_smaller(grid_bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes)
    ]
    if not grid_bboxes:
        return candidates

    tolerance = max(2.0, median_height)
    for candidate in candidates:
        if candidate.core_bbox is None:
            continue
        core_local_bbox = _rotate_bbox_to_upright(
            candidate.core_bbox,
            page_size,
            angle,
        )
        matches = [
            grid_bbox
            for grid_bbox in grid_bboxes
            if _bbox_axis_overlap_ratio(
                core_local_bbox,
                grid_bbox,
                axis="x",
            )
            >= 0.9
            and core_local_bbox[3] >= grid_bbox[1] - tolerance
            and core_local_bbox[1] <= grid_bbox[3] + tolerance
        ]
        if not matches:
            continue
        grid_bbox = max(
            matches,
            key=lambda bbox: (
                min(core_local_bbox[3], bbox[3]) - max(core_local_bbox[1], bbox[1]),
                _bbox_area(bbox),
            ),
        )
        expanded_core_bbox = _bbox_union(core_local_bbox, grid_bbox)
        candidate.local_bbox = _bbox_union(candidate.local_bbox, grid_bbox)
        candidate.core_bbox = _rotate_bbox_from_upright(
            expanded_core_bbox,
            page_size,
            angle,
        )
        candidate.bbox = _rotate_bbox_from_upright(
            candidate.local_bbox,
            page_size,
            angle,
        )
        for row in rows:
            if not expanded_core_bbox[1] <= row.center_y <= expanded_core_bbox[3]:
                continue
            candidate.line_indices.update(
                fragment.line_index
                for fragment in row.fragments
                if _point_in_bbox(
                    (
                        _bbox_center_x(fragment.local_bbox),
                        _bbox_center_y(fragment.local_bbox),
                    ),
                    expanded_core_bbox,
                )
            )
        for annotation in candidate.annotations:
            candidate.line_indices.difference_update(annotation.line_indices)
    return candidates


def _build_closed_rule_grid_candidates(
    rows: list[_VisualRow],
    lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    axis_lines: list[_LocalAxisLine],
    excluded_bboxes: list[BBox],
) -> list[_TableCandidate]:
    """用闭合物理网格接纳含空行或仅有表头文本的稀疏表格。"""

    candidates: list[_TableCandidate] = []
    for component in _connected_rule_grid_components(axis_lines, median_height):
        grid_bbox = _bbox_union_many([rule.bbox for rule in component])
        if any(_bbox_overlap_in_smaller(grid_bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes):
            continue
        core_rows = _rows_inside_rule_interval(
            rows,
            grid_bbox,
            excluded_bboxes,
        )
        if not core_rows:
            continue

        vertical_positions = _closed_grid_vertical_track_positions(
            component,
            axis_lines,
            median_height,
        )
        if len(vertical_positions) < 2:
            continue
        edge_tolerance = max(2.0, 0.25 * median_height)
        if (
            abs(vertical_positions[0] - grid_bbox[0]) > edge_tolerance
            or abs(vertical_positions[-1] - grid_bbox[2]) > edge_tolerance
        ):
            continue

        if len(component) == 2:
            if len(vertical_positions) < 3:
                continue
            occupied_columns = _count_occupied_closed_grid_columns(
                core_rows,
                vertical_positions,
            )
            if occupied_columns < 2:
                continue

        caption_line = _find_table_caption(
            lines,
            grid_bbox,
            page_size,
            angle,
            median_height,
        )
        candidate = _expand_rule_table_candidate(
            [component[0], component[-1]],
            core_rows,
            rows,
            lines,
            page_size,
            angle,
            median_height,
            caption_line,
        )
        candidate.score = float(100 + len(component) + len(vertical_positions))
        candidates.append(candidate)
    return candidates


def _closed_grid_vertical_track_positions(
    horizontal_rules: list[_LocalAxisLine],
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[float]:
    """收集覆盖首末横边界中心跨度至少九成的竖轨并合并重复路径。"""

    top = _bbox_center_y(horizontal_rules[0].bbox)
    bottom = _bbox_center_y(horizontal_rules[-1].bbox)
    grid_height = max(0.1, bottom - top)
    left = min(rule.bbox[0] for rule in horizontal_rules)
    right = max(rule.bbox[2] for rule in horizontal_rules)
    edge_tolerance = max(2.0, 0.25 * median_height)
    raw_positions = []
    for line in axis_lines:
        if line.orientation != "vertical":
            continue
        overlap = max(
            0.0,
            min(line.bbox[3], bottom) - max(line.bbox[1], top),
        )
        position = _bbox_center_x(line.bbox)
        if overlap / grid_height >= 0.9 and left - edge_tolerance <= position <= right + edge_tolerance:
            raw_positions.append(position)

    position_tolerance = max(1.0, 0.1 * median_height)
    position_groups: list[list[float]] = []
    for position in sorted(raw_positions):
        if position_groups and abs(position - statistics.mean(position_groups[-1])) <= position_tolerance:
            position_groups[-1].append(position)
        else:
            position_groups.append([position])
    return [statistics.mean(group) for group in position_groups]


def _count_occupied_closed_grid_columns(
    rows: list[_VisualRow],
    vertical_positions: list[float],
) -> int:
    """按文本片段中心统计闭合网格中实际有文字的物理列数。"""

    occupied_columns: set[int] = set()
    for row in rows:
        for fragment in row.fragments:
            center_x = _bbox_center_x(fragment.local_bbox)
            matching_columns = [
                index
                for index, (left, right) in enumerate(zip(vertical_positions, vertical_positions[1:]))
                if left < center_x < right
            ]
            if len(matching_columns) == 1:
                occupied_columns.add(matching_columns[0])
    return len(occupied_columns)


def _connected_rule_grid_bboxes(
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[BBox]:
    """把端点一致且由外轨或至少两条列轨贯穿的相邻横线组成网格框。"""

    return [
        _bbox_union_many([rule.bbox for rule in component])
        for component in _connected_rule_grid_components(
            axis_lines,
            median_height,
        )
    ]


def _connected_rule_grid_components(
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[list[_LocalAxisLine]]:
    """保留连续网格的横边界成员，供精确外轨和横边界数量校验。"""

    output: list[list[_LocalAxisLine]] = []
    for rule_group in _group_long_horizontal_rules(axis_lines, median_height):
        components: list[list[_LocalAxisLine]] = []
        for rule in rule_group:
            if not components or not _rule_bands_share_grid_tracks(
                components[-1][-1],
                rule,
                axis_lines,
                median_height,
            ):
                components.append([rule])
            else:
                components[-1].append(rule)
        output.extend(component for component in components if len(component) >= 2)
    return output


def _connected_horizontal_rule_bboxes(
    source: _PageSource,
) -> set[BBox]:
    """返回参与常规横排闭合网格的原始水平线框，供上游避免删除真实表格边界。"""

    angle_lines = [line for line in source.lines if line.angle == 0]
    fragments = _build_fragments(angle_lines, source.page_size)
    if not fragments:
        return set()
    local_axis_lines = _transform_axis_lines(
        source.drawing_lines,
        source.page_size,
        0,
    )
    return {
        rule.original_bbox
        for component in _connected_rule_grid_components(
            local_axis_lines,
            _median_fragment_height(fragments),
        )
        for rule in component
        if rule.orientation == "horizontal"
    }


def _rule_bands_share_grid_tracks(
    top_rule: _LocalAxisLine,
    bottom_rule: _LocalAxisLine,
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> bool:
    """校验相邻横边界的跨度，并确认其间存在连续外框或稳定列分隔线。"""

    top_width = max(0.1, top_rule.bbox[2] - top_rule.bbox[0])
    bottom_width = max(0.1, bottom_rule.bbox[2] - bottom_rule.bbox[0])
    overlap_left = max(top_rule.bbox[0], bottom_rule.bbox[0])
    overlap_right = min(top_rule.bbox[2], bottom_rule.bbox[2])
    overlap_width = max(0.0, overlap_right - overlap_left)
    endpoint_tolerance = max(4.0, 2.0 * median_height)
    if (
        overlap_width / max(top_width, bottom_width) < 0.9
        or abs(top_rule.bbox[0] - bottom_rule.bbox[0]) > endpoint_tolerance
        or abs(top_rule.bbox[2] - bottom_rule.bbox[2]) > endpoint_tolerance
    ):
        return False

    top_y = _bbox_center_y(top_rule.bbox)
    bottom_y = _bbox_center_y(bottom_rule.bbox)
    track_tolerance = max(1.0, 0.25 * median_height)
    raw_positions = [
        _bbox_center_x(line.bbox)
        for line in axis_lines
        if line.orientation == "vertical"
        and line.bbox[1] <= top_y + track_tolerance
        and line.bbox[3] >= bottom_y - track_tolerance
        and overlap_left - track_tolerance <= _bbox_center_x(line.bbox) <= overlap_right + track_tolerance
    ]
    position_groups: list[list[float]] = []
    for position in sorted(raw_positions):
        if position_groups and abs(position - statistics.mean(position_groups[-1])) <= track_tolerance:
            position_groups[-1].append(position)
        else:
            position_groups.append([position])
    positions = [statistics.mean(group) for group in position_groups]
    has_outer_tracks = any(abs(position - overlap_left) <= endpoint_tolerance for position in positions) and any(
        abs(position - overlap_right) <= endpoint_tolerance for position in positions
    )
    interior_tracks = [
        position for position in positions if overlap_left + track_tolerance < position < overlap_right - track_tolerance
    ]
    return has_outer_tracks or len(interior_tracks) >= 2


def _group_long_horizontal_rules(
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[list[_LocalAxisLine]]:
    """按近似左右端点聚合长横线，并去除同位置重复路径。"""

    minimum_length = max(40.0, 10.0 * median_height)
    horizontal_lines = [
        line for line in axis_lines if line.orientation == "horizontal" and line.bbox[2] - line.bbox[0] >= minimum_length
    ]
    endpoint_tolerance = max(4.0, 2.0 * median_height)
    span_groups: list[list[_LocalAxisLine]] = []
    for line in sorted(horizontal_lines, key=lambda item: (item.bbox[0], item.bbox[2], item.bbox[1])):
        target = next(
            (
                group
                for group in span_groups
                if abs(line.bbox[0] - group[0].bbox[0]) <= endpoint_tolerance
                and abs(line.bbox[2] - group[0].bbox[2]) <= endpoint_tolerance
            ),
            None,
        )
        if target is None:
            span_groups.append([line])
        else:
            target.append(line)

    output: list[list[_LocalAxisLine]] = []
    for span_group in span_groups:
        unique_lines: list[_LocalAxisLine] = []
        for line in sorted(span_group, key=lambda item: _bbox_center_y(item.bbox)):
            if any(abs(_bbox_center_y(line.bbox) - _bbox_center_y(item.bbox)) <= 1.0 for item in unique_lines):
                continue
            unique_lines.append(line)
        if len(unique_lines) >= 2:
            output.append(unique_lines)
    return output


def _rows_inside_rule_interval(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    excluded_bboxes: list[BBox],
) -> list[_VisualRow]:
    """截取边界走廊内文本行，并移除已由强图形核心覆盖的片段。"""

    output: list[_VisualRow] = []
    for row in rows:
        clipped_row = _clip_visual_row_to_corridor(row, rule_bbox, margin=0.0)
        if clipped_row is None or not rule_bbox[1] <= clipped_row.center_y <= rule_bbox[3]:
            continue
        fragments = [
            fragment
            for fragment in clipped_row.fragments
            if not any(
                _point_in_bbox(
                    (
                        _bbox_center_x(fragment.local_bbox),
                        _bbox_center_y(fragment.local_bbox),
                    ),
                    excluded_bbox,
                )
                for excluded_bbox in excluded_bboxes
            )
        ]
        if not fragments:
            continue
        output.append(
            _VisualRow(
                fragments=fragments,
                center_y=sum(_bbox_center_y(fragment.local_bbox) for fragment in fragments) / len(fragments),
                bbox=_bbox_union_many([fragment.local_bbox for fragment in fragments]),
                visual_row_id=clipped_row.visual_row_id,
            )
        )
    return output


def _every_rule_interval_has_multi_cell_row(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
    median_height: float,
) -> bool:
    """要求候选跨过的每个相邻横线区间都存在至少一行多单元文本。"""

    if len(rule_group) < 2:
        return False
    for interval_index, (top_rule, bottom_rule) in enumerate(zip(rule_group, rule_group[1:])):
        top = _bbox_center_y(top_rule.bbox)
        bottom = _bbox_center_y(bottom_rule.bbox)
        interval_rows = [row for row in rows if top <= row.center_y <= bottom]
        if any(len(row.fragments) >= 2 for row in interval_rows):
            continue
        # 紧邻顶边界的合并表头可能由 pdftext 输出为一个短 fragment；
        # 只放宽高度很小的首区间，避免把远处章节标题接到表格上。
        if interval_index == 0 and interval_rows and bottom - top <= 2.5 * median_height:
            continue
        return False
    return True


def _rule_intervals_are_column_compatible(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
    median_height: float,
) -> bool:
    """拒绝跨过长篇栏式正文、导致稳定列数明显塌缩的多表合并区间。"""

    profiles: list[tuple[int, float, int, float]] = []
    for top_rule, bottom_rule in zip(rule_group, rule_group[1:]):
        top = _bbox_center_y(top_rule.bbox)
        bottom = _bbox_center_y(bottom_rule.bbox)
        interval_rows = [row for row in rows if top <= row.center_y <= bottom and len(row.fragments) >= 2]
        stable_columns, column_coverage = _count_stable_columns(
            interval_rows,
            median_height,
        )
        profiles.append(
            (
                stable_columns,
                bottom - top,
                len(interval_rows),
                column_coverage,
            )
        )
    maximum_columns = max(
        (columns for columns, _height, _row_count, _coverage in profiles),
        default=0,
    )
    for interval_index, (columns, interval_height, row_count, coverage) in enumerate(profiles):
        # 紧凑首区间可能只是跨列表头；一旦区间明显高于普通表头，
        # 也必须具有连续多单元格行，不能无条件跨过正文连接两张表。
        if interval_index == 0 and interval_height <= 2.5 * median_height:
            continue
        if interval_height <= 6.0 * median_height:
            continue
        minimum_rows = max(2, int(interval_height / max(8.0 * median_height, 0.1)))
        if row_count < minimum_rows or coverage < 0.5:
            return False
        if columns < max(2, int(0.5 * maximum_columns)):
            return False
    return True


def _continuous_table_row_segments(
    rows: list[_VisualRow],
    median_height: float,
) -> list[list[_VisualRow]]:
    """按物理行距切分边界区间，保留单元格换行参与连续性判断。"""

    segments: list[list[_VisualRow]] = []
    for row in sorted(rows, key=lambda item: item.center_y):
        if not segments or max(0.0, row.bbox[1] - segments[-1][-1].bbox[3]) > 3.0 * median_height:
            segments.append([row])
        else:
            segments[-1].append(row)
    return segments


def _table_segment_reaches_boundaries(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    median_height: float,
) -> bool:
    """要求数据行链分别贴近最近的上下边界，排除页眉线和远处章节标题。"""

    if not rows:
        return False
    maximum_gap = 2.5 * median_height
    top_gap = max(0.0, rows[0].bbox[1] - rule_bbox[1])
    bottom_gap = max(0.0, rule_bbox[3] - rows[-1].bbox[3])
    return top_gap <= maximum_gap and bottom_gap <= maximum_gap


def _table_rows_align_with_rule_span(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    median_height: float,
) -> bool:
    """校验数据行总体跨度与横线走廊重叠，拒绝仅在边缘偶遇的多列文本。"""

    if not rows:
        return False
    rows_bbox = _bbox_union_many([row.bbox for row in rows])
    rule_width = max(0.1, rule_bbox[2] - rule_bbox[0])
    rows_width = max(0.1, rows_bbox[2] - rows_bbox[0])
    overlap = max(
        0.0,
        min(rows_bbox[2], rule_bbox[2]) - max(rows_bbox[0], rule_bbox[0]),
    )
    return overlap / min(rule_width, rows_width) >= 0.9 and rows_width >= max(8.0 * median_height, 0.25 * rule_width)


def _count_aligned_vertical_rules(
    axis_lines: list[_LocalAxisLine],
    rule_bbox: BBox,
    median_height: float,
) -> int:
    """统计贯穿候选主要高度且位于横线跨度内的竖向分隔线。"""

    required_height = max(4.0 * median_height, 0.5 * (rule_bbox[3] - rule_bbox[1]))
    return sum(
        line.orientation == "vertical"
        and rule_bbox[0] - median_height <= _bbox_center_x(line.bbox) <= rule_bbox[2] + median_height
        and line.bbox[3] - line.bbox[1] >= required_height
        and _bbox_axis_overlap_ratio(line.bbox, rule_bbox, axis="y") >= 0.8
        for line in axis_lines
    )


def _compact_fully_ruled_grid_column_count(
    row_segment: list[_VisualRow],
    dense_rows: list[_VisualRow],
    interval_rules: list[_LocalAxisLine],
    axis_lines: list[_LocalAxisLine],
    rule_bbox: BBox,
    median_height: float,
) -> int:
    """以完整横竖边界确认两行紧凑网格，并返回物理列数，失败时返回零。"""

    rule_height = max(0.1, rule_bbox[3] - rule_bbox[1])
    if len(row_segment) != 2 or len(dense_rows) != 2 or len(interval_rules) < 3 or rule_height > 6.0 * median_height:
        return 0

    vertical_positions = _full_height_vertical_rule_positions(
        axis_lines,
        rule_bbox,
        median_height,
    )
    if len(vertical_positions) < 3:
        return 0

    edge_tolerance = max(1.5, 0.25 * median_height)
    left_boundary = min(
        vertical_positions,
        key=lambda position: abs(position - rule_bbox[0]),
    )
    right_boundary = min(
        vertical_positions,
        key=lambda position: abs(position - rule_bbox[2]),
    )
    if (
        abs(left_boundary - rule_bbox[0]) > edge_tolerance
        or abs(right_boundary - rule_bbox[2]) > edge_tolerance
        or right_boundary <= left_boundary
    ):
        return 0

    grid_boundaries = [position for position in vertical_positions if left_boundary <= position <= right_boundary]
    if len(grid_boundaries) < 3:
        return 0
    grid_intervals = list(zip(grid_boundaries, grid_boundaries[1:]))

    occupied_columns: list[set[int]] = []
    for row in dense_rows:
        row_columns: list[int] = []
        for fragment in row.fragments:
            fragment_center = _bbox_center_x(fragment.local_bbox)
            matching_columns = [index for index, (left, right) in enumerate(grid_intervals) if left <= fragment_center <= right]
            if len(matching_columns) != 1:
                return 0
            column_index = matching_columns[0]
            if column_index in row_columns:
                return 0
            row_columns.append(column_index)
        if len(row_columns) < 2:
            return 0
        occupied_columns.append(set(row_columns))

    if len(occupied_columns[0] & occupied_columns[1]) < 2:
        return 0
    return len(grid_intervals)


def _full_height_vertical_rule_positions(
    axis_lines: list[_LocalAxisLine],
    rule_bbox: BBox,
    median_height: float,
) -> list[float]:
    """收集覆盖紧凑候选主要高度的竖线中心，并合并同位置重复路径。"""

    rule_height = max(0.1, rule_bbox[3] - rule_bbox[1])
    raw_positions: list[float] = []
    for line in axis_lines:
        if line.orientation != "vertical":
            continue
        overlap = max(
            0.0,
            min(line.bbox[3], rule_bbox[3]) - max(line.bbox[1], rule_bbox[1]),
        )
        if (
            overlap / rule_height < 0.8
            or line.bbox[3] - line.bbox[1] < 0.8 * rule_height
            or not rule_bbox[0] - median_height <= _bbox_center_x(line.bbox) <= rule_bbox[2] + median_height
        ):
            continue
        raw_positions.append(_bbox_center_x(line.bbox))

    deduplicated: list[list[float]] = []
    position_tolerance = max(1.0, 0.1 * median_height)
    for position in sorted(raw_positions):
        if deduplicated and abs(position - statistics.mean(deduplicated[-1])) <= position_tolerance:
            deduplicated[-1].append(position)
        else:
            deduplicated.append([position])
    return [statistics.mean(group) for group in deduplicated]


def _looks_like_page_column_prose(
    rows: list[_VisualRow],
    dense_rows: list[_VisualRow],
    stable_columns: int,
    fill_band_count: int,
    aligned_vertical_count: int,
    rule_bbox: BBox,
) -> bool:
    """用双栏占宽率识别夹在远横线间的普通并排正文。"""

    if stable_columns != 2 or fill_band_count >= 2 or aligned_vertical_count > 0 or len(dense_rows) / len(rows) < 0.55:
        return False
    corridor_width = max(0.1, rule_bbox[2] - rule_bbox[0])
    occupied_ratios = [
        sum(fragment.local_bbox[2] - fragment.local_bbox[0] for fragment in row.fragments) / corridor_width
        for row in dense_rows
    ]
    return statistics.median(occupied_ratios) >= 0.75


def _count_repeated_fill_bands(
    path_infos: list[PDFPathInfo],
    rule_bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
) -> int:
    """统计区间内左右端点和高度重复的填充行带，并对重叠 Path 去重。"""

    minimum_width = max(8.0 * median_height, 0.3 * (rule_bbox[2] - rule_bbox[0]))
    candidates: list[BBox] = []
    for path_info in path_infos:
        if path_info.form_depth != 0 or not path_info.fill_visible:
            continue
        bbox = _rotate_bbox_to_upright(path_info.bbox, page_size, angle)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if (
            width < minimum_width
            or not 0.25 * median_height <= height <= 3.0 * median_height
            or _bbox_center_y(bbox) < rule_bbox[1]
            or _bbox_center_y(bbox) > rule_bbox[3]
            or _bbox_axis_overlap_ratio(bbox, rule_bbox, axis="x") < 0.8
        ):
            continue
        if any(_bbox_overlap_in_smaller(bbox, item) >= 0.9 for item in candidates):
            continue
        candidates.append(bbox)

    endpoint_tolerance = max(3.0, median_height)
    groups: list[list[BBox]] = []
    for bbox in candidates:
        target = next(
            (
                group
                for group in groups
                if abs(bbox[0] - group[0][0]) <= endpoint_tolerance
                and abs(bbox[2] - group[0][2]) <= endpoint_tolerance
                and abs((bbox[3] - bbox[1]) - (group[0][3] - group[0][1])) <= endpoint_tolerance
            ),
            None,
        )
        if target is None:
            groups.append([bbox])
        else:
            target.append(bbox)
    return max((len(group) for group in groups), default=0)


def _longest_dense_multi_cell_rows(
    rows: list[_VisualRow],
    median_height: float,
) -> list[_VisualRow]:
    """返回行距不超过四倍行高的最长连续多单元格文本段。"""

    segments: list[list[_VisualRow]] = []
    for row in (item for item in rows if len(item.fragments) >= 2):
        if not segments or row.center_y - segments[-1][-1].center_y > 4.0 * median_height:
            segments.append([row])
        else:
            segments[-1].append(row)
    return max(segments, key=len, default=[])


def _expand_rule_table_candidate(
    rule_group: list[_LocalAxisLine],
    core_rows: list[_VisualRow],
    all_rows: list[_VisualRow],
    all_lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    caption_line: _LineItem | None,
) -> _TableCandidate:
    """合并横线核心与上下注释，并保留注释的独立行身份。"""

    rule_bbox = _bbox_union_many([line.bbox for line in rule_group])
    core_line_indices = {fragment.line_index for row in core_rows for fragment in row.fragments}
    caption_rows = _collect_caption_rows(all_rows, caption_line, rule_bbox, median_height)
    footnote_rows = _collect_footnote_rows(
        all_rows,
        all_lines,
        rule_bbox,
        median_height,
        core_line_indices,
        page_size,
        angle,
    )
    core_local_bbox = _bbox_union(rule_bbox, _bbox_union_many([row.bbox for row in core_rows]))
    caption_annotation = _build_table_annotation(
        "caption",
        caption_rows,
        excluded_line_indices=core_line_indices,
        excluded_local_bbox=core_local_bbox,
    )
    footnote_annotation = _build_table_annotation(
        "footnote",
        footnote_rows,
        excluded_line_indices=core_line_indices,
    )
    annotations = [annotation for annotation in (caption_annotation, footnote_annotation) if annotation is not None]
    annotation_line_indices = (
        set().union(
            *(annotation.line_indices for annotation in annotations),
        )
        if annotations
        else set()
    )
    included_rows = [*caption_rows, *core_rows, *footnote_rows]
    local_bbox = _bbox_union(core_local_bbox, _bbox_union_many([row.bbox for row in included_rows]))
    return _TableCandidate(
        bbox=_rotate_bbox_from_upright(local_bbox, page_size, angle),
        local_bbox=local_bbox,
        angle=angle,
        score=0.0,
        core_bbox=_rotate_bbox_from_upright(core_local_bbox, page_size, angle),
        # 表体成员与注释成员保持互斥；物化失败时会显式把无效注释放回表体投影。
        line_indices=core_line_indices - annotation_line_indices,
        annotations=annotations,
    )


def _count_stable_columns(
    rows: list[_VisualRow],
    median_height: float,
) -> tuple[int, float]:
    """分别聚类片段左边界、中心和右边界，返回最稳定的列分布。"""

    tolerance = max(3.0, median_height * 0.75)
    best_result = (0, 0.0)
    # 三种对齐方式分别聚类，避免把同一片段的不同锚点混算为多列。
    for alignment in ("left", "center", "right"):
        clusters: list[dict[str, Any]] = []
        for row_index, row in enumerate(rows):
            for fragment in row.fragments:
                left, _top, right, _bottom = fragment.local_bbox
                if alignment == "left":
                    anchor = left
                elif alignment == "center":
                    anchor = (left + right) / 2
                else:
                    anchor = right
                cluster = next(
                    (item for item in clusters if abs(anchor - float(item["mean"])) <= tolerance),
                    None,
                )
                if cluster is None:
                    clusters.append({"mean": anchor, "values": [anchor], "rows": {row_index}})
                else:
                    cluster["values"].append(anchor)
                    cluster["rows"].add(row_index)
                    cluster["mean"] = sum(cluster["values"]) / len(cluster["values"])
        stable_coverages = [len(cluster["rows"]) / len(rows) for cluster in clusters if len(cluster["rows"]) / len(rows) >= 0.5]
        result = (
            len(stable_coverages),
            min(stable_coverages) if stable_coverages else 0.0,
        )
        # 仅在结果严格更优时更新，平局时保留既有的左对齐优先级。
        if result > best_result:
            best_result = result
    return best_result


def _merge_table_candidates(candidates: list[_TableCandidate]) -> list[_TableCandidate]:
    """合并同方向且明显重叠的横线候选，避免同一表格重复输出。"""

    merged: list[_TableCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        target = next(
            (
                item
                for item in merged
                if item.angle == candidate.angle and _bbox_overlap_in_smaller(candidate.bbox, item.bbox) >= 0.2
            ),
            None,
        )
        if target is None:
            merged.append(candidate)
            continue
        target.bbox = _bbox_union(target.bbox, candidate.bbox)
        target.local_bbox = _bbox_union(target.local_bbox, candidate.local_bbox)
        if target.core_bbox is None:
            target.core_bbox = candidate.core_bbox
        elif candidate.core_bbox is not None:
            target.core_bbox = _bbox_union(target.core_bbox, candidate.core_bbox)
        target.line_indices.update(candidate.line_indices)
        _merge_table_candidate_annotations(target, candidate)
        target.score = max(target.score, candidate.score)
    return sorted(merged, key=lambda item: (item.bbox[1], item.bbox[0]))


def _median_fragment_height(fragments: list[_Fragment]) -> float:
    """返回正向文本片段高度的中位数。"""

    heights = [
        fragment.local_bbox[3] - fragment.local_bbox[1]
        for fragment in fragments
        if fragment.local_bbox[3] > fragment.local_bbox[1]
    ]
    return max(0.1, float(statistics.median(heights)) if heights else 1.0)
