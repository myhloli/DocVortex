"""PDF rule line and text line candidates; retain the original claiming order and judgment rules."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
import statistics
import math
import sys
from collections import OrderedDict
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
from .models import _Fragment, _LineItem, _LocalAxisLine, _PageSource, _SharedLineIndexSet, _TableCandidate, _VisualRow
from .table_annotations import (
    _PreparedTableNoteBodyMetrics,
    _PreparedTableCoreRows,
    _PreparedMarkerCache,
    _PreparedTableNoteRow,
    _build_table_annotation,
    _prepare_annotation_geometry,
    _collect_caption_rows,
    _collect_footnote_rows,
    _find_table_caption,
    _merge_table_candidate_annotations,
    _prepare_table_note_body_metrics,
    _prepare_table_note_rows,
    _prepare_table_core_rows,
    _prepare_marker_line_context,
)
from .table_rows import _clip_visual_row_to_corridor


@dataclass(slots=True)
class _RuleCorridorRow:
    """Save the exact transverse corridor cropped rows, as well as the geometry used by the original algorithm for longitudinal admission."""

    source_bbox: BBox
    gate_center_y: float
    row: _VisualRow


@dataclass(slots=True)
class _RuleIntervalPrefix:
    """Only an exact copy of the row sequence and adjacent interval allocation below the current starting point of the horizontal line is retained."""

    rows: tuple[_VisualRow, ...]
    rules: tuple[_LocalAxisLine, ...]
    groups: list[list[_VisualRow]]


@dataclass(slots=True)
class _RuleBandIndex:
    """Save the original row identity of the same corridor and its first adjacent interval in the complete horizontal line group."""

    rows: tuple[_VisualRow, ...]
    positions: dict[int, int]
    bands: tuple[int, ...]
    on_rule: tuple[bool, ...]
    rule_count: int
    native: Any = None
    shared_core_rows: Any = None

    def interval(self, rows: list[_VisualRow]) -> tuple[int, int] | None:
        """Only row references that are identical to the original corridor, continuous and in order are allowed."""

        if not rows:
            return 0, 0
        start = self.positions.get(id(rows[0]))
        if start is None or start + len(rows) > len(self.rows):
            return None
        if any(row is not self.rows[start + offset] for offset, row in enumerate(rows)):
            return None
        return start, start + len(rows)


class _RuleIntervalGroups(list):
    """Preserve the normal grouped list behavior and carry the interval text evidence produced by the same native scan."""

    def __init__(self, groups, rows, rules, height, accepted):
        """The evidence is only reused by the original input list and the same row of high queries, and the cross-call state of external mutable objects is not cached."""
        super().__init__(groups)
        self.source_rows = rows
        self.source_rules = rules
        self.height = height
        self.accepted = accepted


@dataclass(slots=True)
class _StableColumnPrefixState:
    """Saves the complete prefix state of stable column clustering for continued calculation with strict prefix input."""

    row_ids: tuple[int, ...]
    clusters_by_alignment: Any


@dataclass(slots=True)
class _StableColumnCache:
    """Save the precise results of the stable column and the prefix status that can be safely continued at the current starting point."""

    results: dict[tuple[float, tuple[int, ...]], tuple[int, float]] = field(default_factory=dict)
    prefixes: dict[tuple[float, int], _StableColumnPrefixState] = field(default_factory=dict)
    prepared_rows: dict[int, tuple[_VisualRow, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class _RuleCandidateContext:
    """The same candidate group shares read-only input and grid indexes, avoiding duplication of page members for each interval."""

    rows: list
    lines: list
    page_size: tuple
    angle: int
    median_height: float
    axis_lines: list
    excluded_bboxes: list
    marker_cache: dict
    body_metrics: _PreparedTableNoteBodyMetrics
    marker_prepared: _PreparedMarkerCache = field(default_factory=_PreparedMarkerCache)
    grids: list | None = None
    grid_components: list | None = None
    grid_members: dict = field(default_factory=dict)
    core_indexes: dict = field(default_factory=dict)
    marker_line_context: tuple[bool, dict] | None = None
    row_bounds: dict = field(default_factory=dict)
    annotation_geometry: Any = None


@dataclass(slots=True)
class _RuleCandidateDraft:
    """Save lightweight candidate descriptions and create annotations and member collections after scoring and sorting."""

    context: _RuleCandidateContext
    boundaries: list
    rows: list
    caption: _LineItem | None
    note_rows: list
    has_notes: bool
    score: float
    core_query: Any = None
    prepared_candidate: Any = None

    def materialize(self) -> _TableCandidate:
        """The current candidate is materialized according to the reference rules, and the grid members of this group are reused and immediately handed over to the combiner."""
        context = self.context
        if context.annotation_geometry is None:
            context.annotation_geometry = _prepare_annotation_geometry(context.rows) or False
        candidate = _expand_rule_table_candidate(
            self.boundaries,
            self.rows,
            context.rows,
            context.lines,
            context.page_size,
            context.angle,
            context.median_height,
            self.caption,
            self.has_notes,
            context.marker_cache,
            self.note_rows,
            context.body_metrics,
            self.core_query,
            context.annotation_geometry or None,
            self.prepared_candidate,
        )
        candidate.score = self.score
        if context.grids is None:
            context.grids = [
                box
                for box in _connected_rule_grid_bboxes(
                    context.axis_lines,
                    context.median_height,
                    components=context.grid_components,
                )
                if not any(_bbox_overlap_in_smaller(box, excluded) >= 0.5 for excluded in context.excluded_bboxes)
            ]
        return _expand_candidates_to_connected_rule_grids(
            [candidate],
            context.rows,
            context.page_size,
            context.angle,
            context.median_height,
            context.axis_lines,
            context.excluded_bboxes,
            prepared_grid_bboxes=context.grids,
            grid_member_cache=context.grid_members,
        )[0]


def _build_fragments(
    lines: list[_LineItem],
    page_size: tuple[float, float],
) -> list[_Fragment]:
    """Convert refined native run into table cell candidates."""

    fragments: list[_Fragment] = []
    for line in lines:
        local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, line.angle)
        fragments.append(
            _Fragment(
                text=line.text,
                bbox=line.bbox,
                local_bbox=local_bbox,
                line_index=line.source_index,
                # Reuse native thick line identities to avoid different characters within the same line cell
                # Incorrectly split into multiple lines due to slight baseline differences.
                visual_row_id=line.visual_row_id,
            )
        )
    return fragments


def _cluster_fragment_rows(
    fragments: list[_Fragment],
    median_height: float,
) -> list[_VisualRow]:
    """Prioritize the reuse of native visual row identities, and cluster the remaining fragments into table rows based on centerline tolerance."""

    tolerance = max(2.0, median_height * 0.5)
    native_groups: dict[int, list[_Fragment]] = {}
    geometric_fragments: list[_Fragment] = []
    for fragment in fragments:
        if fragment.visual_row_id is None:
            geometric_fragments.append(fragment)
        else:
            native_groups.setdefault(fragment.visual_row_id, []).append(fragment)

    # First lock the run detached from the same native thick row, and then allow different thick rows to be merged according to the baseline geometry;
    # Rotating tables often divides each cell of the same data row into multiple pdftext thick rows, and cannot rely only on row id.
    seed_groups = [*native_groups.values(), *[[fragment] for fragment in geometric_fragments]]
    prepared_seed_groups = [
        (
            seed_group,
            statistics.fmean(_bbox_center_y(item.local_bbox) for item in seed_group),
            min(item.local_bbox[0] for item in seed_group),
        )
        for seed_group in seed_groups
    ]
    prepared_seed_groups.sort(key=lambda item: (item[1], item[2]))
    grouped: list[list[_Fragment]] = []
    group_centers: list[float] = []
    for seed_group, center_y, _left in prepared_seed_groups:
        target_index: int | None = None
        for group_index, group_center in enumerate(group_centers):
            if abs(center_y - group_center) <= tolerance:
                target_index = group_index
                break
        if target_index is None:
            grouped.append(list(seed_group))
            group_centers.append(center_y)
        else:
            target_group = grouped[target_index]
            target_group.extend(seed_group)
            # Only when the group members change, the original fmean is used for recalculation, and subsequent comparisons directly reuse the accurate results.
            group_centers[target_index] = statistics.fmean(_bbox_center_y(item.local_bbox) for item in target_group)

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
    caption_candidates: list[tuple[_LineItem, BBox]] | None = None,
    defer_materialization: bool = False,
) -> list[_TableCandidate] | list[_RuleCandidateDraft]:
    """Enumerate horizontal line boundary intervals with the same span, and then confirm the table with continuous multi-column text distribution."""

    candidates: list[_TableCandidate] = []
    path_infos = path_infos or []
    excluded_bboxes = excluded_bboxes or []
    stable_column_cache = _StableColumnCache()
    prepared_path_infos = _prepare_fill_band_infos(path_infos, page_size, angle)
    vertical_axis_lines = [line for line in axis_lines if line.orientation == "vertical"]
    corridor_cache: dict[tuple[float, float], list[_RuleCorridorRow]] = {}
    note_corridor_cache: dict[tuple[float, float], tuple[list[_PreparedTableNoteRow], bool]] = {}
    prepared_note_body_metrics = _prepare_table_note_body_metrics(
        lines,
        page_size,
        angle,
    )
    marker_cache: dict[tuple[int, str], bool] = {}
    drafts: list[_RuleCandidateDraft] = []
    context = _RuleCandidateContext(
        rows,
        lines,
        page_size,
        angle,
        median_height,
        axis_lines,
        excluded_bboxes,
        marker_cache,
        prepared_note_body_metrics,
    )
    for rule_group in _group_long_horizontal_rules(axis_lines, median_height):
        active_first_index = -1
        interval_prefix: _RuleIntervalPrefix | None = None
        band_indexes: OrderedDict[tuple[float, float], _RuleBandIndex | None] = OrderedDict()
        # The candidate set and enumeration order of the historical algorithm are retained; performance optimization only reuses the pure calculation results within the candidates.
        for first_index, bottom_index in _iter_rule_spans(len(rule_group)):
            if first_index != active_first_index:
                stable_column_cache.prefixes.clear()
                interval_prefix = None
                active_first_index = first_index
            top_rule = rule_group[first_index]
            bottom_rule = rule_group[bottom_index]
            interval_rules = rule_group[first_index : bottom_index + 1]
            boundary_rules = [top_rule, bottom_rule]
            rule_bbox = _bbox_union_many([line.bbox for line in boundary_rules])
            core_rows = _rows_inside_rule_interval(
                rows,
                rule_bbox,
                excluded_bboxes,
                corridor_cache=corridor_cache,
            )
            caption_line = _find_table_caption(
                lines,
                rule_bbox,
                page_size,
                angle,
                median_height,
                caption_candidates,
            )
            caption_anchored_compact_grid = (
                caption_line is not None
                and len(interval_rules) >= 3
                and rule_bbox[3] - rule_bbox[1] <= 0.15 * (page_size[0] if angle in {90, 270} else page_size[1])
            )
            corridor_key = (rule_bbox[0], rule_bbox[2])
            if corridor_key not in band_indexes:
                corridor_rows = [item.row for item in corridor_cache[corridor_key]]
                band_indexes[corridor_key] = (
                    _prepare_rule_band_index(corridor_rows, rule_group)
                    if len(corridor_rows) >= 8 and len(rule_group) >= 4
                    else None
                )
                if len(band_indexes) > 16:
                    band_indexes.popitem(last=False)
            else:
                band_indexes.move_to_end(corridor_key)
            interval_row_groups = _partition_rows_by_prepared_bands(
                core_rows, interval_rules, first_index, band_indexes[corridor_key], median_height
            )
            if interval_row_groups is None:
                interval_row_groups, interval_prefix = _partition_rows_by_rule_intervals_cached(
                    core_rows, interval_rules, interval_prefix
                )
            if (
                not _every_rule_interval_has_multi_cell_row(
                    core_rows,
                    interval_rules,
                    median_height,
                    interval_row_groups,
                )
                and not caption_anchored_compact_grid
            ):
                continue
            if (
                not _rule_intervals_are_column_compatible(
                    core_rows,
                    interval_rules,
                    median_height,
                    stable_column_cache,
                    interval_row_groups,
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
                prepared_path_infos,
            )
            aligned_vertical_count = _count_aligned_vertical_rules(
                axis_lines,
                rule_bbox,
                median_height,
                vertical_axis_lines,
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
                    stable_column_cache,
                    allow_prefix_reuse=True,
                )
                if compact_grid_columns > 0:
                    # Two rows of samples easily miscalculate the left and right/center anchor points into different stable columns, using the physical grid column number.
                    stable_columns = compact_grid_columns
                caption_supported_compact_rows = (
                    caption_anchored_compact_grid and len(dense_rows) >= 2 and stable_columns >= 3 and column_coverage >= 0.5
                )
                if len(dense_rows) < 3 and compact_grid_columns == 0 and not caption_supported_compact_rows:
                    continue
                # True tables have multi-cell rows that recur throughout the data band; a few figure titles, legends, and
                # The accidental multi-column rows of the coordinate scale cannot support a large text area.
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
                corridor_key = (rule_bbox[0], rule_bbox[2])
                if corridor_key not in context.row_bounds:
                    context.row_bounds[corridor_key] = _RuleRowBounds([item.row for item in corridor_cache[corridor_key]])
                if not _table_rows_align_with_rule_span(
                    row_segment,
                    rule_bbox,
                    median_height,
                    context.row_bounds[corridor_key],
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
            corridor_key = (rule_bbox[0], rule_bbox[2])
            note_context = note_corridor_cache.get(corridor_key)
            if note_context is None:
                prepared_note_rows = _prepare_table_note_rows(
                    rows,
                    lines,
                    rule_bbox,
                    median_height,
                    page_size,
                    angle,
                )
                note_context = (
                    prepared_note_rows,
                    any(row.explicit_note or row.auxiliary_marker is not None for row in prepared_note_rows),
                )
                note_corridor_cache[corridor_key] = note_context
            prepared_note_rows, has_possible_table_notes = note_context
            score = float(2 + len(dense_rows) + stable_columns + min(fill_band_count, 8))
            if defer_materialization:
                band_index = band_indexes[corridor_key]
                if corridor_key not in context.core_indexes:
                    if context.marker_line_context is None:
                        context.marker_line_context = _prepare_marker_line_context(lines)
                    marker_safe, marker_source_lines = context.marker_line_context
                    # The internal row sequence is frozen as tuple during the build phase, allowing two read-only indexes to safely share slice parity.
                    corridor_rows = (
                        band_index.rows if band_index is not None else tuple(item.row for item in corridor_cache[corridor_key])
                    )
                    context.core_indexes[corridor_key] = _prepare_table_core_rows(
                        corridor_rows,
                        lines,
                        prepared_note_body_metrics,
                        context.marker_prepared,
                        marker_safe=marker_safe,
                        source_lines=marker_source_lines,
                    )
                core_index = context.core_indexes[corridor_key]
                interval = core_index.interval(accepted_rows) if core_index is not None else None
                core_query = (core_index, *interval) if interval is not None else None
                drafts.append(
                    _RuleCandidateDraft(
                        context,
                        boundary_rules,
                        accepted_rows,
                        caption_line,
                        prepared_note_rows,
                        has_possible_table_notes,
                        score,
                        core_query,
                        _prepared_rule_candidate_query(band_indexes[corridor_key], accepted_rows, core_query),
                    )
                )
                continue
            candidate = _expand_rule_table_candidate(
                boundary_rules,
                accepted_rows,
                rows,
                lines,
                page_size,
                angle,
                median_height,
                caption_line,
                has_possible_table_notes,
                marker_cache,
                prepared_note_rows,
                prepared_note_body_metrics,
                prepared_candidate=_prepared_rule_candidate_query(band_indexes[corridor_key], accepted_rows),
            )
            candidate.score = score
            candidates.append(candidate)
    if defer_materialization:
        # During delayed materialization, grid connected components are first frozen for closed grid detection and subsequent owned
        # The results are merged once; pages with no candidates remain scanned with zero extra effort.
        if drafts:
            context.grid_components = _connected_rule_grid_components(axis_lines, median_height)
        return drafts
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
    *,
    prepared_grid_bboxes: list[BBox] | None = None,
    grid_member_cache: dict[BBox, frozenset[int]] | None = None,
) -> list[_TableCandidate]:
    """Expand identified candidates to the complete physical grid along continuous horizontal boundaries and through vertical rails."""

    grid_bboxes = (
        prepared_grid_bboxes
        if prepared_grid_bboxes is not None
        else [
            grid_bbox
            for grid_bbox in _connected_rule_grid_bboxes(axis_lines, median_height)
            if not any(_bbox_overlap_in_smaller(grid_bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes)
        ]
    )
    if not grid_bboxes:
        return candidates

    tolerance = max(2.0, median_height)
    if grid_member_cache is None:
        grid_member_cache = {}
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
        grid_members = grid_member_cache.get(expanded_core_bbox)
        if grid_members is None:
            grid_members = frozenset(
                fragment.line_index
                for row in rows
                if expanded_core_bbox[1] <= row.center_y <= expanded_core_bbox[3]
                for fragment in row.fragments
                if _point_in_bbox(
                    (
                        _bbox_center_x(fragment.local_bbox),
                        _bbox_center_y(fragment.local_bbox),
                    ),
                    expanded_core_bbox,
                )
            )
            grid_member_cache[expanded_core_bbox] = grid_members
        candidate.line_indices = _SharedLineIndexSet(
            grid_members,
            candidate.line_indices,
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
    caption_candidates: list[tuple[_LineItem, BBox]] | None = None,
    *,
    grid_components: list[list[_LocalAxisLine]] | None = None,
) -> list[_TableCandidate]:
    """Use a closed physical grid to accommodate sparse tables with empty rows or header text only."""

    candidates: list[_TableCandidate] = []
    row_interval_index = _build_row_interval_index(rows)
    components = grid_components if grid_components is not None else _connected_rule_grid_components(axis_lines, median_height)
    # The components passed in by the multiplexer are still generated by the above call/detection boundary; here it is only iterated and the vertical track is not scanned repeatedly.
    for component in components:
        grid_bbox = _bbox_union_many([rule.bbox for rule in component])
        if any(_bbox_overlap_in_smaller(grid_bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes):
            continue
        core_rows = _rows_inside_rule_interval(
            rows,
            grid_bbox,
            excluded_bboxes,
            row_interval_index,
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
            caption_candidates,
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
    """Collect vertical rails covering at least 90% of the center span of the first and last horizontal boundaries and merge duplicate paths."""

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
    """Count the number of physical columns actually containing text in the closed grid based on the center of the text fragment."""

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
    *,
    components: list[list[_LocalAxisLine]] | None = None,
) -> list[BBox]:
    """A grid frame is formed by adjacent horizontal lines whose endpoints are consistent and penetrated by an outer rail or at least two rails."""

    # components is a frozen private multiplex input within the same page; retains the original signature when not provided
    # Compared with the original calculation, the test/old calling path that directly calls this function will not be affected.
    if components is None:
        components = _connected_rule_grid_components(axis_lines, median_height)
    return [_bbox_union_many([rule.bbox for rule in component]) for component in components]


def _connected_rule_grid_components(
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[list[_LocalAxisLine]]:
    """The transverse boundary members of the continuous grid are retained for accurate outer track and transverse boundary quantity verification."""

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
    """Returns the original horizontal wireframe participating in the regular horizontal closed grid, for upstream to avoid deleting the true table boundaries."""

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
    """Verify the span of adjacent horizontal boundaries and confirm that there is a continuous outline or stable column divider in between."""

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


def _iter_rule_spans(rule_count: int):
    """Enumerate all horizontal line boundary combinations in historical order without deleting any internal sub-candidates."""

    for first_index in range(max(0, rule_count - 1)):
        for bottom_index in range(first_index + 1, rule_count):
            yield first_index, bottom_index


def _group_long_horizontal_rules(
    axis_lines: list[_LocalAxisLine],
    median_height: float,
) -> list[list[_LocalAxisLine]]:
    """Aggregate long horizontal lines based on approximate left and right endpoints, and remove duplicate paths at the same location."""

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


def _build_rule_corridor_rows(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    excluded_bboxes: list[BBox],
) -> list[_RuleCorridorRow]:
    """Visual rows are pre-cropped according to the exact left and right boundaries, retaining the intermediate geometry required for vertical admission by the original algorithm."""

    output: list[_RuleCorridorRow] = []
    for row in rows:
        clipped_row = _clip_visual_row_to_corridor(row, rule_bbox, margin=0.0)
        if clipped_row is None:
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
            _RuleCorridorRow(
                source_bbox=row.bbox,
                gate_center_y=clipped_row.center_y,
                row=_VisualRow(
                    fragments=fragments,
                    center_y=sum(_bbox_center_y(fragment.local_bbox) for fragment in fragments) / len(fragments),
                    bbox=_bbox_union_many([fragment.local_bbox for fragment in fragments]),
                    visual_row_id=clipped_row.visual_row_id,
                ),
            )
        )
    return output


def _rows_inside_rule_interval(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    excluded_bboxes: list[BBox],
    row_interval_index: tuple[list[tuple[int, _VisualRow]], list[float]] | None = None,
    corridor_cache: dict[tuple[float, float], list[_RuleCorridorRow]] | None = None,
) -> list[_VisualRow]:
    """Intercept lines of text within bounding corridors and remove segments already covered by strong graphics cores."""

    if corridor_cache is not None:
        corridor_key = (rule_bbox[0], rule_bbox[2])
        corridor_rows = corridor_cache.get(corridor_key)
        if corridor_rows is None:
            corridor_rows = _build_rule_corridor_rows(
                rows,
                rule_bbox,
                excluded_bboxes,
            )
            corridor_cache[corridor_key] = corridor_rows
        return [
            item.row
            for item in corridor_rows
            if item.source_bbox[1] <= rule_bbox[3]
            and item.source_bbox[3] >= rule_bbox[1]
            and rule_bbox[1] <= item.gate_center_y <= rule_bbox[3]
        ]

    output: list[_VisualRow] = []
    if row_interval_index is None:
        indexed_rows = list(enumerate(rows))
    else:
        records, starts = row_interval_index
        upper = bisect_right(starts, rule_bbox[3])
        indexed_rows = [(index, row) for index, row in records[:upper] if row.bbox[3] >= rule_bbox[1]]
        indexed_rows.sort(key=lambda item: item[0])
    for _index, row in indexed_rows:
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


def _build_row_interval_index(
    rows: list[_VisualRow],
) -> tuple[list[tuple[int, _VisualRow]], list[float]]:
    """Create a safe superset index sorted by the upper boundary of the rows, preserving the original row order for subsequent processing."""
    records = sorted(
        enumerate(rows),
        key=lambda item: (item[1].bbox[1], item[1].bbox[3], item[0]),
    )
    return records, [row.bbox[1] for _index, row in records]


def _partition_rows_by_rule_intervals(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
) -> list[list[_VisualRow]]:
    """Allocate candidate rows to adjacent horizontal line closed intervals at a time, keeping shared boundary rows belonging to both sides of the interval."""

    if len(rule_group) < 2:
        return []
    centers = [_bbox_center_y(rule.bbox) for rule in rule_group]
    groups: list[list[_VisualRow]] = [[] for _ in range(len(rule_group) - 1)]
    for row in rows:
        center_y = row.center_y
        position = bisect_left(centers, center_y)
        if position < len(centers) and centers[position] == center_y:
            if position > 0:
                groups[position - 1].append(row)
            if position < len(groups):
                groups[position].append(row)
            continue
        interval_index = position - 1
        if 0 <= interval_index < len(groups):
            groups[interval_index].append(row)
    return groups


def _prepare_rule_band_index(rows: list[_VisualRow], rules: list[_LocalAxisLine]) -> _RuleBandIndex | None:
    """Calculates the first interval of a complete horizontal line group for an exact corridor, with fallback for abnormal and repeated horizontal lines."""

    if len({id(row) for row in rows}) != len(rows) or any(
        type(row.center_y) is not float or not math.isfinite(row.center_y) for row in rows
    ):
        return None
    centers = []
    for rule in rules:
        box = rule.bbox
        if (
            type(box) not in (tuple, list)
            or len(box) != 4
            or any(type(value) is not float or not math.isfinite(value) for value in box)
        ):
            return None
        center = _bbox_center_y(box)
        if not math.isfinite(center) or centers and center <= centers[-1]:
            return None
        centers.append(center)
    native = _prepare_native_rule_candidates(rows, centers, rules)
    bands = [bisect_left(centers, row.center_y) for row in rows] if native is None else []
    return _RuleBandIndex(
        tuple(rows),
        {id(row): index for index, row in enumerate(rows)},
        tuple(bands),
        tuple(position < len(centers) and centers[position] == row.center_y for position, row in zip(bands, rows))
        if native is None
        else (),
        len(centers),
        native,
    )


def _prepare_native_rule_candidates(rows, centers, rules):
    """Only shared native corridors are prepared for common limited data, special objects are left as reference paths before calculations begin."""
    from ...._compute_backend import get_native

    native = get_native()
    if native is None or not hasattr(native, "PreparedRuleCandidates"):
        return None
    if any(type(rule) is not _LocalAxisLine for rule in rules):
        return None
    packed = []
    for row in rows:
        if (
            type(row) is not _VisualRow
            or type(row.fragments) is not list
            or type(row.bbox) not in (tuple, list)
            or len(row.bbox) != 4
            or any(type(value) is not float or not math.isfinite(value) for value in row.bbox)
        ):
            return None
        members = []
        for fragment in row.fragments:
            if (
                type(fragment) is not _Fragment
                or type(fragment.line_index) is not int
                or not -(2**63) <= fragment.line_index < 2**63
            ):
                return None
            members.append(fragment.line_index)
        packed.append((row.center_y, len(row.fragments), row.bbox, members))
    return native.PreparedRuleCandidates(centers, packed)


def _prepared_rule_candidate_query(index, rows, prepared_core=None):
    """The identity of the shared corridor is only verified once, and the order-preserving slices that have been verified by core.interval are subsequently reused."""
    if index is None or index.native is None:
        return None
    if prepared_core is not None:
        core, start, end = prepared_core
        if type(core) is _PreparedTableCoreRows and type(core.rows) is tuple:
            shared = index.shared_core_rows
            if shared is None or shared[0] is not core or shared[1] is not core.rows:
                # Only internal immutable tuple is cached; field replacement will be re-validated due to identity changes, and external mutable lists are not allowed.
                same_rows = core.rows is index.rows or (
                    len(core.rows) == len(index.rows)
                    and all(actual is expected for actual, expected in zip(core.rows, index.rows, strict=True))
                )
                shared = index.shared_core_rows = (core, core.rows, same_rows)
            if shared[2]:
                return (index, start, end) if start < end else None
    interval = index.interval(rows)
    return (index, *interval) if interval is not None and interval[0] < interval[1] else None


def _prepared_rule_candidate_core(prepared, rules, *, owned=False):
    """By merging core members and coordinate sources at once, Python retains the original coordinate identity and collection contract."""
    if prepared is None or not rules:
        return None
    boxes = [rule.bbox for rule in rules]
    if any(
        type(box) not in (tuple, list)
        or len(box) != 4
        or any(type(value) is not float or not math.isfinite(value) for value in box)
        for box in boxes
    ):
        return None
    index, start, end = prepared
    members, sources = (index.native.owned_core if owned else index.native.core)(start, end, boxes)
    bbox = tuple(
        (boxes[source] if source < len(boxes) else index.rows[source - len(boxes)].bbox)[axis]
        for axis, source in enumerate(sources)
    )
    if owned:
        from ._native_table_merge import _NativeCoreLineSet

        return _NativeCoreLineSet(members), bbox
    return {member for member in members}, bbox


def _partition_rows_by_prepared_bands(
    rows: list[_VisualRow],
    rules: list[_LocalAxisLine],
    first_index: int,
    index: _RuleBandIndex | None,
    median_height: float | None = None,
) -> list[list[_VisualRow]] | None:
    """Only the first interval is reused for consecutive and order-preserving corridor rows, and boundary rows are still given dual ownership."""

    if index is None or first_index < 0 or first_index + len(rules) > index.rule_count:
        return None
    interval = index.interval(rows)
    if interval is None:
        return None
    start, end = interval
    if index.native is not None:
        evidence_safe = type(median_height) is float and math.isfinite(median_height)
        packed_groups, accepted = index.native.partition(
            start, end, first_index, len(rules), median_height if evidence_safe else 0.0
        )
        groups = [[index.rows[position] for position in group] for group in packed_groups]
        return _RuleIntervalGroups(groups, rows, rules, median_height, accepted) if evidence_safe else groups
    groups: list[list[_VisualRow]] = [[] for _ in range(max(0, len(rules) - 1))]
    for row, band, on_rule in zip(rows, index.bands[start:end], index.on_rule[start:end], strict=True):
        local = band - first_index
        if on_rule:
            if 0 <= local - 1 < len(groups):
                groups[local - 1].append(row)
            if 0 <= local < len(groups):
                groups[local].append(row)
        elif 0 <= local - 1 < len(groups):
            groups[local - 1].append(row)
    return groups


def _partition_rows_by_rule_intervals_cached(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
    previous: _RuleIntervalPrefix | None,
) -> tuple[list[list[_VisualRow]], _RuleIntervalPrefix]:
    """When strictly the same line sequence and increasing horizontal line prefix are used, only the last interval is supplemented, and the original allocation is performed for the rest."""

    reusable = (
        previous is not None
        and len(rule_group) == len(previous.rules) + 1
        and len(rows) == len(previous.rows)
        and all(row is old for row, old in zip(rows, previous.rows, strict=True))
        and all(rule is old for rule, old in zip(rule_group[:-1], previous.rules, strict=True))
        and all(type(row.center_y) is float and math.isfinite(row.center_y) for row in rows)
        and all(
            type(rule.bbox) in (tuple, list)
            and len(rule.bbox) == 4
            and all(type(value) is float and math.isfinite(value) for value in rule.bbox)
            for rule in rule_group
        )
    )
    if reusable:
        centers = [_bbox_center_y(rule.bbox) for rule in rule_group]
        reusable = all(first < second for first, second in zip(centers, centers[1:]))
    if reusable:
        low, high = centers[-2:]
        groups = [*previous.groups, [row for row in rows if low <= row.center_y <= high]]
    else:
        groups = _partition_rows_by_rule_intervals(rows, rule_group)
    return groups, _RuleIntervalPrefix(tuple(rows), tuple(rule_group), groups)


def _every_rule_interval_has_multi_cell_row(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
    median_height: float,
    interval_row_groups: list[list[_VisualRow]] | None = None,
) -> bool:
    """It is required that there is at least one line of multi-unit text for each adjacent horizontal line interval that the candidate spans."""

    if len(rule_group) < 2:
        return False
    if (
        type(median_height) is float
        and type(interval_row_groups) is _RuleIntervalGroups
        and interval_row_groups.source_rows is rows
        and interval_row_groups.source_rules is rule_group
        and interval_row_groups.height == median_height
    ):
        return interval_row_groups.accepted
    groups = interval_row_groups
    if groups is None:
        groups = _partition_rows_by_rule_intervals(rows, rule_group)
    for interval_index, ((top_rule, bottom_rule), interval_rows) in enumerate(zip(zip(rule_group, rule_group[1:]), groups)):
        top = _bbox_center_y(top_rule.bbox)
        bottom = _bbox_center_y(bottom_rule.bbox)
        if any(len(row.fragments) >= 2 for row in interval_rows):
            continue
        # Merge headers immediately adjacent to the top boundary may be output by pdftext as a short fragment;
        # Only widen the first interval with a small height to avoid connecting distant chapter titles to the table.
        if interval_index == 0 and interval_rows and bottom - top <= 2.5 * median_height:
            continue
        return False
    return True


def _rule_intervals_are_column_compatible(
    rows: list[_VisualRow],
    rule_group: list[_LocalAxisLine],
    median_height: float,
    stable_column_cache: _StableColumnCache | None = None,
    interval_row_groups: list[list[_VisualRow]] | None = None,
) -> bool:
    """Multi-table merge ranges that span long columnar texts and result in significant collapse of the number of stable columns are rejected."""

    interval_heights = [
        _bbox_center_y(bottom_rule.bbox) - _bbox_center_y(top_rule.bbox)
        for top_rule, bottom_rule in zip(rule_group, rule_group[1:])
    ]
    # The original logic will unconditionally skip subsequent compatibility checks for all intervals no higher than six times the row height;
    # Therefore, when all are short ranges, you can return directly to avoid first calculating column statistics that will never participate in the decision.
    if interval_heights and all(height <= 6.0 * median_height for height in interval_heights):
        return True

    groups = interval_row_groups
    if groups is None:
        groups = _partition_rows_by_rule_intervals(rows, rule_group)
    profiles: list[tuple[int, float, int, float]] = []
    for band_rows, interval_height in zip(groups, interval_heights):
        interval_rows = [row for row in band_rows if len(row.fragments) >= 2]
        if _is_multiline_description_cell_band(band_rows, median_height):
            profiles.append((2, interval_height, len(band_rows), 1.0))
            continue
        stable_columns, column_coverage = _count_stable_columns(
            interval_rows,
            median_height,
            stable_column_cache,
        )
        profiles.append(
            (
                stable_columns,
                interval_height,
                len(interval_rows),
                column_coverage,
            )
        )
    maximum_columns = max(
        (columns for columns, _height, _row_count, _coverage in profiles),
        default=0,
    )
    for interval_index, (columns, interval_height, row_count, coverage) in enumerate(profiles):
        # The compact first interval may only span the column header; once the interval is significantly higher than the ordinary table header,
        # It must also have continuous multi-cell rows and cannot unconditionally connect two tables across the body.
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


def _is_multiline_description_cell_band(rows: list[_VisualRow], height: float) -> bool:
    """Confirm short left cells and multi-row right cells, and do not use physical row continuation times to negate logical table rows."""
    if len(rows) < 3 or len(rows[0].fragments) != 2:
        return False
    first, second = sorted(rows[0].fragments, key=lambda fragment: fragment.local_bbox[0])
    left, right = first.local_bbox, second.local_bbox
    if left[2] - left[0] > 0.45 * (right[2] - left[0]) or right[0] - left[2] < 0.5 * height:
        return False
    for previous, row in zip(rows, rows[1:]):
        if row.center_y - previous.center_y > 2.0 * height:
            return False
        if len(row.fragments) != 1:
            return False
        bbox = row.fragments[0].local_bbox
        if abs(bbox[0] - right[0]) > 0.75 * height or bbox[2] > right[2] + height:
            return False
    return True


def _continuous_table_row_segments(
    rows: list[_VisualRow],
    median_height: float,
) -> list[list[_VisualRow]]:
    """Divide the boundary interval according to the physical line spacing, and retain cell line breaks to participate in continuity judgment."""

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
    """Data row chains are required to be close to the nearest upper and lower boundaries respectively, excluding header lines and distant chapter titles."""

    if not rows:
        return False
    maximum_gap = 2.5 * median_height
    top_gap = max(0.0, rows[0].bbox[1] - rule_bbox[1])
    bottom_gap = max(0.0, rule_bbox[3] - rows[-1].bbox[3])
    return top_gap <= maximum_gap and bottom_gap <= maximum_gap


class _RuleRowBounds:
    """Save the corridor row box index and only accept queries where the identity and sequence are completely consecutive."""

    def __init__(self, rows):
        """A limited row box extremum tree is constructed at one time, and special values and small pages are not indexed."""
        from ...._compute_backend import get_native

        self.rows = rows
        self.positions = {id(row): i for i, row in enumerate(rows)}
        self.native = None
        native = get_native()
        if (
            native is not None
            and len(rows) >= 32
            and len(self.positions) == len(rows)
            and all(
                type(row.bbox) in (tuple, list)
                and len(row.bbox) == 4
                and all(type(v) is float and math.isfinite(v) for v in row.bbox)
                for row in rows
            )
        ):
            self.native = native.TableRowGeometry([row.bbox for row in rows])

    def bbox(self, rows):
        """The original traversal of repeated and non-consecutive rows is maintained; the source of extreme values still refers to the original coordinate object."""
        if self.native is None or not rows:
            return None
        start = self.positions.get(id(rows[0]))
        if (
            start is None
            or start + len(rows) > len(self.rows)
            or any(row is not self.rows[start + offset] for offset, row in enumerate(rows))
        ):
            return None
        indices = self.native.union_indices(start, start + len(rows))
        return tuple(self.rows[index].bbox[axis] for axis, index in enumerate(indices))


def _table_rows_align_with_rule_span(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    median_height: float,
    prepared_bounds: _RuleRowBounds | None = None,
) -> bool:
    """Verify that the overall span of data lines overlaps with horizontal corridors, rejecting multiple columns of text that are only encountered incidentally at the edges."""

    if not rows:
        return False
    rows_bbox = prepared_bounds.bbox(rows) if prepared_bounds is not None else None
    if rows_bbox is None:
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
    vertical_axis_lines: list[_LocalAxisLine] | None = None,
) -> int:
    """Count the vertical dividing lines that run through the candidate's main height and are within the span of the horizontal line."""

    required_height = max(4.0 * median_height, 0.5 * (rule_bbox[3] - rule_bbox[1]))
    candidates = vertical_axis_lines if vertical_axis_lines is not None else axis_lines
    return sum(
        line.orientation == "vertical"
        and rule_bbox[0] - median_height <= _bbox_center_x(line.bbox) <= rule_bbox[2] + median_height
        and line.bbox[3] - line.bbox[1] >= required_height
        and _bbox_axis_overlap_ratio(line.bbox, rule_bbox, axis="y") >= 0.8
        for line in candidates
    )


def _compact_fully_ruled_grid_column_count(
    row_segment: list[_VisualRow],
    dense_rows: list[_VisualRow],
    interval_rules: list[_LocalAxisLine],
    axis_lines: list[_LocalAxisLine],
    rule_bbox: BBox,
    median_height: float,
) -> int:
    """Confirms a two-row compact grid with full horizontal and vertical boundaries and returns the physical column number, or zero on failure."""

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
    """Collect vertical line centers covering the main heights of compact candidates and merge duplicate paths at the same location."""

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
    """Use double column width to identify ordinary side-by-side text sandwiched between far horizontal lines."""

    if stable_columns != 2 or fill_band_count >= 2 or aligned_vertical_count > 0 or len(dense_rows) / len(rows) < 0.55:
        return False
    corridor_width = max(0.1, rule_bbox[2] - rule_bbox[0])
    occupied_ratios = [
        sum(fragment.local_bbox[2] - fragment.local_bbox[0] for fragment in row.fragments) / corridor_width
        for row in dense_rows
    ]
    return statistics.median(occupied_ratios) >= 0.75


def _prepare_fill_band_infos(
    path_infos: list[PDFPathInfo],
    page_size: tuple[float, float],
    angle: int,
) -> list[tuple[PDFPathInfo, BBox]]:
    """Pre-convert the coordinates of the visible filled Path to avoid repeated rotation of each horizontal line interval."""
    return [
        (path_info, _rotate_bbox_to_upright(path_info.bbox, page_size, angle))
        for path_info in path_infos
        if path_info.form_depth == 0 and path_info.fill_visible
    ]


def _count_repeated_fill_bands(
    path_infos: list[PDFPathInfo],
    rule_bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    prepared_path_infos: list[tuple[PDFPathInfo, BBox]] | None = None,
) -> int:
    """Count left and right endpoints and highly repeated padded row bands within the interval, and deduplicate overlapping Path."""

    minimum_width = max(8.0 * median_height, 0.3 * (rule_bbox[2] - rule_bbox[0]))
    candidates: list[BBox] = []
    prepared = prepared_path_infos
    if prepared is None:
        prepared = _prepare_fill_band_infos(path_infos, page_size, angle)
    for _path_info, bbox in prepared:
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
    """Returns the longest continuous multi-cell text segment with line spacing no greater than four times the line height."""

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
    has_possible_table_notes: bool = True,
    marker_cache: dict[tuple[int, str], bool] | None = None,
    prepared_note_rows: list[_PreparedTableNoteRow] | None = None,
    prepared_note_body_metrics: _PreparedTableNoteBodyMetrics | None = None,
    prepared_core: Any = None,
    prepared_annotation_geometry: Any = None,
    prepared_candidate: Any = None,
    *,
    owned: bool = False,
) -> _TableCandidate:
    """Merges the horizontal core with upper and lower comments and preserves the independent line identity of the comments."""

    rule_bbox = _bbox_union_many([line.bbox for line in rule_group])
    native_core = (
        _prepared_rule_candidate_core(prepared_candidate, rule_group, owned=True)
        if owned
        else _prepared_rule_candidate_core(prepared_candidate, rule_group)
    )
    if owned and native_core is None:
        raise ValueError("owned candidate requires a prepared core")
    core_line_indices = (
        native_core[0] if native_core is not None else {fragment.line_index for row in core_rows for fragment in row.fragments}
    )
    caption_rows = _collect_caption_rows(all_rows, caption_line, rule_bbox, median_height)
    footnote_rows = (
        _collect_footnote_rows(
            all_rows,
            all_lines,
            rule_bbox,
            median_height,
            core_line_indices,
            page_size,
            angle,
            marker_cache,
            prepared_note_rows,
            prepared_note_body_metrics,
            prepared_core,
        )
        if has_possible_table_notes
        else []
    )
    if native_core is not None:
        core_local_bbox = native_core[1]
        indexed_bbox = True
    else:
        core_rows_bbox = prepared_core[0].bbox(prepared_core[1], prepared_core[2]) if prepared_core is not None else None
        indexed_bbox = core_rows_bbox is not None
        if core_rows_bbox is None:
            core_rows_bbox = _bbox_union_many([row.bbox for row in core_rows])
        core_local_bbox = _bbox_union(rule_bbox, core_rows_bbox)
    caption_annotation = _build_table_annotation(
        "caption",
        caption_rows,
        excluded_line_indices=core_line_indices,
        excluded_local_bbox=core_local_bbox,
        prepared_geometry=prepared_annotation_geometry,
    )
    footnote_annotation = _build_table_annotation(
        "footnote",
        footnote_rows,
        excluded_line_indices=core_line_indices,
        prepared_geometry=prepared_annotation_geometry,
    )
    annotations = [annotation for annotation in (caption_annotation, footnote_annotation) if annotation is not None]
    annotation_line_indices = (
        set().union(
            *(annotation.line_indices for annotation in annotations),
        )
        if annotations
        else set()
    )
    if indexed_bbox and all(
        type(value) is float and math.isfinite(value) for row in (*caption_rows, *footnote_rows) for value in row.bbox
    ):
        # The finite extreme value union is idempotent, and the outer core always participates in the comparison first, retaining the original object with equal coordinates.
        local_bbox = core_local_bbox
        for row in (*caption_rows, *footnote_rows):
            local_bbox = _bbox_union(local_bbox, row.bbox)
    else:
        included_rows = [*caption_rows, *core_rows, *footnote_rows]
        local_bbox = _bbox_union(core_local_bbox, _bbox_union_many([row.bbox for row in included_rows]))
    if owned:
        return (
            _rotate_bbox_from_upright(local_bbox, page_size, angle),
            local_bbox,
            _rotate_bbox_from_upright(core_local_bbox, page_size, angle),
            core_line_indices,
            annotations,
        )
    return _TableCandidate(
        bbox=_rotate_bbox_from_upright(local_bbox, page_size, angle),
        local_bbox=local_bbox,
        angle=angle,
        score=0.0,
        core_bbox=_rotate_bbox_from_upright(core_local_bbox, page_size, angle),
        # Table body members and annotation members remain mutually exclusive; when materialization fails, invalid annotations are explicitly put back into the table body projection.
        line_indices=core_line_indices - annotation_line_indices,
        annotations=annotations,
    )


def _new_stable_column_clusters() -> dict[str, list[dict[str, Any]]]:
    """Create three sets of empty cluster states consistent with the original stable column algorithm."""

    return {alignment: [] for alignment in ("left", "center", "right")}


def _extend_stable_column_clusters(
    clusters_by_alignment: dict[str, list[dict[str, Any]]],
    rows: list[_VisualRow],
    *,
    start_row_index: int,
    tolerance: float,
) -> None:
    """Add new rows to the existing clustering state in the original order without changing the mean value and first hit rule."""

    for alignment in ("left", "center", "right"):
        clusters = clusters_by_alignment[alignment]
        for row_index, row in enumerate(rows, start=start_row_index):
            for fragment in row.fragments:
                left, _top, right, _bottom = fragment.local_bbox
                if alignment == "left":
                    anchor = left
                elif alignment == "center":
                    anchor = (left + right) / 2
                else:
                    anchor = right
                cluster: dict[str, Any] | None = None
                for item in clusters:
                    if abs(anchor - float(item["mean"])) <= tolerance:
                        cluster = item
                        break
                if cluster is None:
                    clusters.append({"mean": anchor, "values": [anchor], "rows": {row_index}})
                else:
                    cluster["values"].append(anchor)
                    cluster["rows"].add(row_index)
                    # The original implemented sum/len calculation method is retained to avoid changing the critical floating point clustering affiliation.
                    cluster["mean"] = sum(cluster["values"]) / len(cluster["values"])


def _stable_column_result(
    clusters_by_alignment: dict[str, list[dict[str, Any]]],
    row_count: int,
) -> tuple[int, float]:
    """Calculate the original stable column number and minimum coverage from the complete clustering state."""

    if row_count <= 0:
        return 0, 0.0

    best_result = (0, 0.0)
    for alignment in ("left", "center", "right"):
        clusters = clusters_by_alignment[alignment]
        stable_coverages: list[float] = []
        for cluster in clusters:
            coverage = len(cluster["rows"]) / row_count
            if coverage >= 0.5:
                stable_coverages.append(coverage)
        result = (
            len(stable_coverages),
            min(stable_coverages) if stable_coverages else 0.0,
        )
        # Only updates if the result is strictly superior, retaining the existing left-aligned priority in case of a tie.
        if result > best_result:
            best_result = result
    return best_result


def _count_stable_columns(
    rows: list[_VisualRow],
    median_height: float,
    cache: _StableColumnCache | None = None,
    *,
    allow_prefix_reuse: bool = False,
) -> tuple[int, float]:
    """Reuse native accumulation state of corridor anchors with strict prefixes, special values retain Python summing behavior."""
    from ...._compute_backend import get_native

    native = get_native()
    if native is None or sys.implementation.name != "cpython" or not (3, 10) <= sys.version_info[:2] <= (3, 14):
        return _count_stable_columns_python(rows, median_height, cache, allow_prefix_reuse=allow_prefix_reuse)
    if type(median_height) is not float or not math.isfinite(median_height):
        return _count_stable_columns_python(rows, median_height)
    row_ids = tuple(id(row) for row in rows)
    key = (median_height, row_ids)
    if cache is not None and key in cache.results:
        return cache.results[key]
    prefix_key = (median_height, row_ids[0]) if row_ids and allow_prefix_reuse else None
    prefix = cache.prefixes.get(prefix_key) if cache is not None and prefix_key is not None else None
    reuse = (
        prefix is not None
        and isinstance(prefix.clusters_by_alignment, native.StableColumnClusters)
        and len(prefix.row_ids) < len(row_ids)
        and row_ids[: len(prefix.row_ids)] == prefix.row_ids
    )
    start = len(prefix.row_ids) if reuse else 0
    packed = []
    for row in rows[start:]:
        existing = cache.prepared_rows.get(id(row)) if cache is not None else None
        if existing is None:
            values = []
            for fragment in row.fragments:
                left, _top, right, _bottom = fragment.local_bbox
                if type(left) is not float or type(right) is not float or not math.isfinite(left) or not math.isfinite(right):
                    values = None
                    break
                values.append((left, right))
            if cache is not None:
                # Strong references are limited to this build to prevent the address from being reused by another object after the row object is destroyed.
                cache.prepared_rows[id(row)] = (row, values)
        else:
            values = existing[1]
        if values is None:
            return _count_stable_columns_python(rows, median_height)
        packed.append(values)
    state = prefix.clusters_by_alignment if reuse else native.StableColumnClusters(sys.version_info >= (3, 12))
    result = state.extend(packed, max(3.0, median_height * 0.75))
    if result is None:
        if cache is not None and prefix_key is not None:
            cache.prefixes.pop(prefix_key, None)
        return _count_stable_columns_python(rows, median_height)
    if cache is not None:
        cache.results[key] = result
        if prefix_key is not None and (prefix is None or reuse):
            cache.prefixes[prefix_key] = _StableColumnPrefixState(row_ids, state)
    return result


def _count_stable_columns_python(
    rows: list[_VisualRow],
    median_height: float,
    cache: _StableColumnCache | None = None,
    *,
    allow_prefix_reuse: bool = False,
) -> tuple[int, float]:
    """Cluster the fragment left, center, and right boundaries separately, and continue the existing state for strictly prefixed input."""

    row_ids = tuple(id(row) for row in rows)
    cache_key = (median_height, row_ids)
    if cache is not None:
        cached = cache.results.get(cache_key)
        if cached is not None:
            return cached

    tolerance = max(3.0, median_height * 0.75)
    clusters_by_alignment: dict[str, list[dict[str, Any]]]
    start_row_index = 0
    prefix_key: tuple[float, int] | None = None
    prefix_state: _StableColumnPrefixState | None = None
    reused_prefix = False

    if cache is not None and allow_prefix_reuse and row_ids:
        prefix_key = (median_height, row_ids[0])
        prefix_state = cache.prefixes.get(prefix_key)
        if (
            prefix_state is not None
            and len(prefix_state.row_ids) < len(row_ids)
            and row_ids[: len(prefix_state.row_ids)] == prefix_state.row_ids
        ):
            clusters_by_alignment = prefix_state.clusters_by_alignment
            start_row_index = len(prefix_state.row_ids)
            reused_prefix = True
        else:
            clusters_by_alignment = _new_stable_column_clusters()
    else:
        clusters_by_alignment = _new_stable_column_clusters()

    _extend_stable_column_clusters(
        clusters_by_alignment,
        rows[start_row_index:],
        start_row_index=start_row_index,
        tolerance=tolerance,
    )
    result = _stable_column_result(clusters_by_alignment, len(rows))

    if cache is not None:
        cache.results[cache_key] = result
        if allow_prefix_reuse and row_ids and prefix_key is not None:
            if prefix_state is None or reused_prefix:
                cache.prefixes[prefix_key] = _StableColumnPrefixState(
                    row_ids=row_ids,
                    clusters_by_alignment=clusters_by_alignment,
                )
    return result


def _merge_table_candidates(candidates: list[_TableCandidate]) -> list[_TableCandidate]:
    """Merge horizontal line candidates with the same direction and obvious overlap to avoid repeated output of the same table."""

    merged: list[_TableCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if isinstance(candidate, _RuleCandidateDraft):
            candidate = candidate.materialize()
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
        if isinstance(candidate.line_indices, _SharedLineIndexSet) and not isinstance(
            target.line_indices,
            _SharedLineIndexSet,
        ):
            # The first high-scoring candidate may come from a closed grid path and hold a normal set; converted to candidate
            # After accurately sharing the basis representation, subsequent candidates on the same grid can merge the difference sets without rescanning all members.
            target.line_indices = _SharedLineIndexSet.from_exact_values(
                candidate.line_indices.base,
                target.line_indices,
            )
        target.line_indices.update(candidate.line_indices)
        _merge_table_candidate_annotations(target, candidate)
        target.score = max(target.score, candidate.score)
    return sorted(merged, key=lambda item: (item.bbox[1], item.bbox[0]))


def _median_fragment_height(fragments: list[_Fragment]) -> float:
    """Returns the median height of forward text fragments."""

    heights = [
        fragment.local_bbox[3] - fragment.local_bbox[1]
        for fragment in fragments
        if fragment.local_bbox[3] > fragment.local_bbox[1]
    ]
    return max(0.1, float(statistics.median(heights)) if heights else 1.0)


def _merge_owned_table_candidates(candidates):
    """The detector's exclusive candidate goes through its own native stream first, and the special input maintains the original variable object reference semantics."""
    from ._native_table_merge import merge_owned

    result = merge_owned(candidates)
    return _merge_table_candidates(candidates) if result is None else result


_NATIVE_DRAFT_MATERIALIZE = _RuleCandidateDraft.materialize
_NATIVE_MERGE_RULES = {
    function.__name__: function
    for function in (
        _merge_table_candidates,
        _expand_rule_table_candidate,
        _build_table_annotation,
        _collect_footnote_rows,
        _collect_caption_rows,
        _merge_table_candidate_annotations,
        _expand_candidates_to_connected_rule_grids,
        _prepared_rule_candidate_core,
        _bbox_area,
        _bbox_axis_overlap_ratio,
        _bbox_overlap_in_smaller,
        _bbox_union,
        _rotate_bbox_from_upright,
        _rotate_bbox_to_upright,
        _point_in_bbox,
        _bbox_center_x,
        _bbox_center_y,
    )
}
