"""PDF 表格检测编排与续表标题；保留原有认领顺序与判定规则。"""

from __future__ import annotations
from ....schema import BBox
from .models import _PageSource, _TableAnnotation, _TableCandidate
from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_overlap_in_smaller,
    _bbox_union,
    _expand_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
    _transform_axis_lines,
)

from .table_constants import _TABLE_CONTINUATION_RE
from .table_filled_grid import _detect_filled_grid_table_candidates
from .table_rules import (
    _build_closed_rule_grid_candidates,
    _build_fragments,
    _build_rule_table_candidates,
    _cluster_fragment_rows,
    _connected_rule_grid_bboxes,
    _median_fragment_height,
    _merge_table_candidates,
)


def _detect_table_candidates(
    source: _PageSource,
    excluded_bboxes: list[BBox] | None = None,
) -> list[_TableCandidate]:
    """按文本方向融合横线区间、规则文本和填充行带，生成表格候选。"""

    excluded_bboxes = excluded_bboxes or []
    filled_grid_candidates = _detect_filled_grid_table_candidates(
        source.path_infos,
        source.page_size,
        excluded_bboxes,
    )
    filled_grid_bboxes = [candidate.bbox for candidate in filled_grid_candidates]
    rule_candidates: list[_TableCandidate] = []
    angles = sorted({line.angle for line in source.lines})
    for angle in angles:
        angle_lines = [line for line in source.lines if line.angle == angle]
        if not angle_lines:
            continue
        fragments = _build_fragments(angle_lines, source.page_size)
        if not fragments:
            continue
        median_height = _median_fragment_height(fragments)
        rows = _cluster_fragment_rows(fragments, median_height)
        local_axis_lines = _transform_axis_lines(
            source.drawing_lines,
            source.page_size,
            angle,
        )
        local_excluded_bboxes = [
            _expand_bbox(
                _rotate_bbox_to_upright(bbox, source.page_size, angle),
                2.5 * median_height,
            )
            for bbox in [*excluded_bboxes, *filled_grid_bboxes]
        ]
        local_closed_grid_excluded_bboxes = [
            *local_excluded_bboxes,
            *[_rotate_bbox_to_upright(bbox, source.page_size, angle) for bbox in source.form_bboxes],
        ]
        rule_candidates.extend(
            _build_rule_table_candidates(
                rows,
                angle_lines,
                source.page_size,
                angle,
                median_height,
                local_axis_lines,
                path_infos=source.path_infos,
                excluded_bboxes=local_excluded_bboxes,
            )
        )
        rule_candidates.extend(
            _build_closed_rule_grid_candidates(
                rows,
                angle_lines,
                source.page_size,
                angle,
                median_height,
                local_axis_lines,
                local_closed_grid_excluded_bboxes,
            )
        )
    merged_rule_candidates = [
        candidate
        for candidate in _merge_table_candidates(rule_candidates)
        if not any(_bbox_overlap_in_smaller(candidate.bbox, filled_bbox) >= 0.2 for filled_bbox in filled_grid_bboxes)
    ]
    candidates = [*filled_grid_candidates, *merged_rule_candidates]
    _externalize_table_continuation_captions(
        source,
        candidates,
    )
    return sorted(
        candidates,
        key=lambda candidate: (candidate.bbox[1], candidate.bbox[0]),
    )


def _externalize_table_continuation_captions(
    source: _PageSource,
    candidates: list[_TableCandidate],
) -> None:
    """以连通物理网格收紧表体，并把其上方续表标记外置为 caption。"""

    for candidate in candidates:
        angle_lines = [line for line in source.lines if line.angle == candidate.angle]
        if not angle_lines:
            continue
        fragments = _build_fragments(
            angle_lines,
            source.page_size,
        )
        if not fragments:
            continue
        median_height = _median_fragment_height(fragments)
        local_axis_lines = _transform_axis_lines(
            source.drawing_lines,
            source.page_size,
            candidate.angle,
        )
        grid_matches = [
            grid_bbox
            for grid_bbox in _connected_rule_grid_bboxes(
                local_axis_lines,
                median_height,
            )
            if _bbox_axis_overlap_ratio(
                grid_bbox,
                candidate.local_bbox,
                axis="x",
            )
            >= 0.9
            and _bbox_overlap_in_smaller(
                grid_bbox,
                candidate.local_bbox,
            )
            >= 0.9
        ]
        if not grid_matches:
            continue
        grid_bbox = max(
            grid_matches,
            key=lambda bbox: _bbox_area(bbox),
        )
        continuation_lines = []
        for line in angle_lines:
            if (
                _TABLE_CONTINUATION_RE.fullmatch(
                    line.text.strip(),
                )
                is None
            ):
                continue
            local_bbox = _rotate_bbox_to_upright(
                line.bbox,
                source.page_size,
                candidate.angle,
            )
            gap = grid_bbox[1] - local_bbox[3]
            if (
                local_bbox[3] <= grid_bbox[1] + 0.25 * median_height
                and -0.25 * median_height <= gap <= 3.0 * median_height
                and _bbox_axis_overlap_ratio(
                    local_bbox,
                    grid_bbox,
                    axis="x",
                )
                >= 0.8
            ):
                continuation_lines.append(
                    (line, local_bbox),
                )
        if len(continuation_lines) != 1:
            continue
        continuation_line, continuation_local_bbox = continuation_lines[0]
        continuation_bbox = continuation_line.bbox
        existing_caption = next(
            (annotation for annotation in candidate.annotations if annotation.kind == "caption"),
            None,
        )
        if existing_caption is None:
            candidate.annotations.append(
                _TableAnnotation(
                    kind="caption",
                    bbox=continuation_bbox,
                    line_indices={continuation_line.source_index},
                    line_bboxes={
                        continuation_line.source_index: continuation_bbox,
                    },
                )
            )
        else:
            existing_caption.bbox = _bbox_union(
                existing_caption.bbox,
                continuation_bbox,
            )
            existing_caption.line_indices.add(
                continuation_line.source_index,
            )
            existing_caption.line_bboxes[continuation_line.source_index] = continuation_bbox
        candidate.line_indices.discard(
            continuation_line.source_index,
        )
        core_bbox = _rotate_bbox_from_upright(
            grid_bbox,
            source.page_size,
            candidate.angle,
        )
        candidate.core_bbox = core_bbox
        candidate.local_bbox = _bbox_union(
            grid_bbox,
            continuation_local_bbox,
        )
        candidate.bbox = _bbox_union(
            core_bbox,
            continuation_bbox,
        )
