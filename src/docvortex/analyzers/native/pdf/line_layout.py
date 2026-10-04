"""Provides shared layout decisions for text column bands, leading, and line connections."""

from __future__ import annotations

import math
import re
import statistics
from typing import Sequence

from ....foundation._text import is_hyphen_at_line_end
from ....schema import BBox
from .geometry import (
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_intersects,
    _bbox_union_many,
    _clip_bbox,
    _coerce_bbox,
)
from .models import _LineItem, _LocalAxisLine, _TextLane
from .inline.types import PDF_FONT_ITALIC_FLAG
from .typography import _normalized_font_family

_TIGHT_OUTPUT_PADDING = 1.0


def _title_fonts_compatible(first: _LineItem, second: _LineItem) -> bool:
    """Check that title fonts and weights are compatible; evidence of reliable weights is retained at low font coverage."""

    font_conflicts = (
        first.font_signature is not None
        and second.font_signature is not None
        and first.font_coverage >= 0.75
        and second.font_coverage >= 0.75
        and first.font_signature != second.font_signature
    )
    weight_conflicts = _font_weights_conflict(first, second)
    return not (font_conflicts or weight_conflicts)


def _font_signatures_share_family(
    first: tuple[str, int] | None,
    second: tuple[str, int] | None,
) -> bool:
    """Determines whether two reliable font signatures differ only by the PDF subset prefix or description flag."""

    first_family = _normalized_font_family(first)
    second_family = _normalized_font_family(second)
    return first_family is not None and second_family is not None and first_family == second_family


def _font_signatures_share_emphasis_family(first: tuple[str, int], second: tuple[str, int]) -> bool:
    """Ignore the universal weight/italic suffix only in confirmed body text emphasis continuations, without changing headings or other font compatibility rules."""
    style_suffix = r"(?:semibold|demibold|regular|regu|roman|bold|light|medium|italic|ital|oblique)+$"
    first_family = re.sub(style_suffix, "", _normalized_font_family(first) or "")
    second_family = re.sub(style_suffix, "", _normalized_font_family(second) or "")
    return bool(first_family) and first_family == second_family


def _font_weights_conflict(first: _LineItem, second: _LineItem) -> bool:
    """Determine whether the weight difference between the two lines is significant enough to form a hard boundary between the paragraphs."""

    return (
        first.dominant_font_weight is not None
        and second.dominant_font_weight is not None
        and abs(first.dominant_font_weight - second.dominant_font_weight) >= 100.0
        and max(first.dominant_font_weight, second.dominant_font_weight)
        >= 1.15 * min(first.dominant_font_weight, second.dominant_font_weight)
    )


def _should_connect_semantic_rows(
    previous: tuple[_LineItem, BBox],
    current: tuple[_LineItem, BBox],
    lane: _TextLane,
    regular_gap: float,
    table_bboxes: list[BBox],
    axis_lines: list[_LocalAxisLine],
) -> bool:
    """Only use geometry, fonts and barriers to connect semantic lines of the same type to avoid title content affecting aggregation."""

    previous_line, previous_bbox = previous
    current_line, current_bbox = current
    if previous_line.semantic_type != current_line.semantic_type:
        return False
    if (
        current_line.semantic_type == "paragraph_title"
        and current_line.explicit_section_title
        and current_line.title_band_id is not None
        and current_line.title_band_id != previous_line.title_band_id
    ):
        # The confirmed independent numbered title strips form a permanent boundary, and two consecutive levels of titles cannot be stuck together due to tight arrangement.
        return False
    if _connection_crosses_table(previous_line.bbox, current_line.bbox, table_bboxes):
        return False
    if _horizontal_rule_separates_rows(previous_bbox, current_bbox, lane, axis_lines):
        return False
    previous_height = _line_effective_height(previous_line, previous_bbox)
    current_height = _line_effective_height(current_line, current_bbox)
    pair_height = max(previous_height, current_height)
    if max(previous_height, current_height) / min(previous_height, current_height) > 1.35:
        return False
    vertical_gap = _effective_text_row_gap(previous, current)
    if not -0.25 * pair_height <= vertical_gap <= max(1.25 * pair_height, regular_gap + 0.75 * pair_height):
        return False
    shared_title_band = previous_line.title_band_id is not None and previous_line.title_band_id == current_line.title_band_id
    if previous_line.semantic_type == "paragraph_title" and vertical_gap > 0.5 * pair_height and not shared_title_band:
        return False
    font_conflicts = (
        previous_line.font_signature is not None
        and current_line.font_signature is not None
        and previous_line.font_coverage >= 0.75
        and current_line.font_coverage >= 0.75
        and previous_line.font_signature != current_line.font_signature
    )
    uncertain_document_title_font = (
        previous_line.semantic_type == "doc_title"
        and min(previous_line.font_coverage, current_line.font_coverage) < 0.85
        and (
            previous_line.dominant_font_weight is None
            or current_line.dominant_font_weight is None
            or abs(previous_line.dominant_font_weight - current_line.dominant_font_weight) < 100.0
        )
    )
    if (
        font_conflicts
        and not uncertain_document_title_font
        and not (shared_title_band and previous_line.structural_title and current_line.structural_title)
    ):
        return False
    lane_width = max(0.1, lane.right - lane.left)
    centered_pair = abs(_bbox_center_x(previous_bbox) - _bbox_center_x(current_bbox)) <= 0.15 * lane_width
    aligned_pair = abs(previous_bbox[0] - current_bbox[0]) <= 0.75 * pair_height
    overlapping_pair = _bbox_axis_overlap_ratio(previous_bbox, current_bbox, axis="x") >= 0.35
    return centered_pair or aligned_pair or overlapping_pair


def _line_style_scale(line: _LineItem, local_bbox: BBox) -> float:
    """Returns the canonical font size, compatible with the old effective line height and local bbox when missing."""

    return max(
        0.1,
        line.em_height
        if line.style_scale_repaired and line.em_height > 0
        else line.effective_height or (local_bbox[3] - local_bbox[1]),
    )


def _line_canonical_style_scale(line: _LineItem, local_bbox: BBox) -> float:
    """Ignore the semantic selection flag and directly return the calibrated font scale of tight/origin."""

    return max(
        0.1,
        line.em_height if line.em_height > 0 else line.effective_height or (local_bbox[3] - local_bbox[1]),
    )


def _line_effective_height(line: _LineItem, local_bbox: BBox) -> float:
    """Compatible with existing layout calls and uniformly forwarded to canonical font scale."""

    return _line_style_scale(line, local_bbox)


def _line_layout_height(_line: _LineItem, local_bbox: BBox) -> float:
    """Returns the layout envelope height of canonical, which is used for formula and visual container space judgment."""

    return max(0.1, local_bbox[3] - local_bbox[1])


def _line_tight_output_bbox(
    line: _LineItem,
    page_size: tuple[float, float],
) -> BBox | None:
    """Combine the reliable tight glyph, expand it by 1pt on each side, and crop it to the page range."""

    ink_bbox = _coerce_bbox(line.ink_bbox)
    if ink_bbox is None:
        return None
    return _clip_bbox(
        (
            ink_bbox[0] - _TIGHT_OUTPUT_PADDING,
            ink_bbox[1] - _TIGHT_OUTPUT_PADDING,
            ink_bbox[2] + _TIGHT_OUTPUT_PADDING,
            ink_bbox[3] + _TIGHT_OUTPUT_PADDING,
        ),
        page_size,
    )


def _lines_tight_output_bbox(
    lines: Sequence[_LineItem],
    page_size: tuple[float, float],
) -> BBox | None:
    """Merge multiple lines of tight+1pt candidates; members missing tight continue to use the original layout bbox."""

    output_bboxes: list[BBox] = []
    changed = False
    for line in lines:
        candidate = _line_tight_output_bbox(line, page_size)
        output_bboxes.append(candidate or line.bbox)
        changed = changed or candidate is not None
    if not changed or not output_bboxes:
        return None
    return _bbox_union_many(output_bboxes)


def _effective_text_row_gap(
    previous: tuple[_LineItem, BBox],
    current: tuple[_LineItem, BBox],
) -> float:
    """Calculate headroom based on the top edge of the previous line and the effective line height to avoid elongating the bottom edge of high math glyphs bbox."""

    previous_line, previous_bbox = previous
    _current_line, current_bbox = current
    if previous_line.restored_inline_cluster:
        # The bbox bottom edge of the two-dimensional text cluster is the true denominator boundary; at the same time, the depth overlap is cut off to prevent adjacent fractions from becoming paragraph barriers to each other.
        return max(
            current_bbox[1] - previous_bbox[3],
            -0.25 * _line_effective_height(previous_line, previous_bbox),
        )
    return current_bbox[1] - (previous_bbox[1] + _line_effective_height(previous_line, previous_bbox))


def _effective_body_text_row_gap(
    previous: tuple[_LineItem, BBox],
    current: tuple[_LineItem, BBox],
) -> float:
    """The main text connection uses the origin baseline rhythm first, and falls back to the existing bbox clearance when evidence is lacking."""

    previous_line, previous_bbox = previous
    current_line, _current_bbox = current
    if (
        previous_line.baseline is not None
        and current_line.baseline is not None
        and current_line.baseline > previous_line.baseline
    ):
        previous_scale = _line_effective_height(previous_line, previous_bbox)
        current_scale = _line_effective_height(*current)
        pitch = current_line.baseline - previous_line.baseline
        if (
            0.5 * min(previous_scale, current_scale)
            <= pitch
            <= 3.0
            * max(
                previous_scale,
                current_scale,
            )
        ):
            return pitch - previous_scale
    return _effective_text_row_gap(previous, current)


def _infer_text_lanes(
    line_geometry: list[tuple[_LineItem, BBox]],
    local_page_width: float,
    median_height: float,
    *,
    recalculate_intervals: bool = True,
) -> list[_TextLane]:
    """Stable bars are inferred from repeated left and right edges, and boundaries are recalculated with assigned members as needed."""

    anchor_tolerance = max(3.0, 0.75 * median_height)
    anchor_geometry = [
        item for item in line_geometry if item[0].semantic_type not in {"header", "footer", "page_number", "aside_text"}
    ]
    regular_lines = [
        item
        for item in anchor_geometry
        if item[1][2] - item[1][0] >= max(4.0 * _line_effective_height(*item), 0.15 * local_page_width)
    ]
    left_clusters: list[list[tuple[_LineItem, BBox]]] = []
    for item in sorted(regular_lines, key=lambda value: value[1][0]):
        if not left_clusters:
            left_clusters.append([item])
            continue
        cluster_left = statistics.median(member[1][0] for member in left_clusters[-1])
        if abs(item[1][0] - cluster_left) <= anchor_tolerance:
            left_clusters[-1].append(item)
        else:
            left_clusters.append([item])

    supported_intervals = [
        (
            statistics.median(item[1][0] for item in cluster),
            statistics.median(item[1][2] for item in cluster),
            len(cluster),
        )
        for cluster in left_clusters
        if len(cluster) >= 3
    ]
    supported_intervals.sort(key=lambda interval: interval[0])
    filtered_intervals: list[tuple[float, float, int]] = []
    for interval in supported_intervals:
        if not filtered_intervals:
            filtered_intervals.append(interval)
            continue
        previous = filtered_intervals[-1]
        minimum_gutter = max(6.0, 0.75 * median_height)
        if interval[0] - previous[1] >= minimum_gutter:
            filtered_intervals.append(interval)
        elif interval[2] > previous[2]:
            filtered_intervals[-1] = interval

    if not filtered_intervals:
        source = regular_lines or line_geometry
        filtered_intervals = [
            (
                min(item[1][0] for item in source),
                max(item[1][2] for item in source),
                len(source),
            )
        ]

    nested_column_band = None
    if len(filtered_intervals) == 1 and anchor_geometry:
        nested_outer_interval = (
            (
                min(item[1][0] for item in anchor_geometry),
                max(item[1][2] for item in anchor_geometry),
            )
            if recalculate_intervals
            else filtered_intervals[0][:2]
        )
        nested_column_band = _infer_nested_column_band(
            anchor_geometry,
            local_page_width,
            median_height,
            nested_outer_interval,
            enhanced=recalculate_intervals,
        )
    if nested_column_band is not None:
        nested_lanes, band_top, band_bottom = nested_column_band
        nested_ordered_lanes = sorted(nested_lanes, key=lambda lane: lane.left)
        fallback_lane = _TextLane(
            left=filtered_intervals[0][0],
            right=filtered_intervals[0][1],
        )
        span_lines: list[tuple[_LineItem, BBox]] = []
        for item in line_geometry:
            line, bbox = item
            center_y = _bbox_center_y(bbox)
            if (
                line.semantic_type in {"header", "footer", "page_number", "page_footnote", "aside_text"}
                or not band_top <= center_y <= band_bottom
            ):
                fallback_lane.lines.append(item)
                continue
            best_lane, best_coverage, second_coverage = _best_lane_coverage(
                bbox,
                nested_lanes,
            )
            fits_only_one_lane = _fits_only_one_lane_ordered(
                bbox,
                best_lane,
                nested_ordered_lanes,
                anchor_tolerance,
            )
            if second_coverage >= 0.2 and not fits_only_one_lane:
                span_lines.append(item)
                continue
            if best_coverage >= 0.5 or fits_only_one_lane:
                best_lane.lines.append(item)
            else:
                span_lines.append(item)
        lanes = [lane for lane in nested_lanes if lane.lines]
        if recalculate_intervals:
            _expand_nested_lane_intervals_from_members(
                lanes,
                anchor_tolerance,
            )
        if fallback_lane.lines:
            lanes.append(fallback_lane)
        if span_lines:
            lanes.append(
                _TextLane(
                    left=min(item[1][0] for item in span_lines),
                    right=max(item[1][2] for item in span_lines),
                    lines=span_lines,
                    is_span=True,
                )
            )
        _reattach_span_lane_continuations(lanes, median_height)
        _reattach_cross_lane_short_tails(lanes, median_height)
        return lanes

    lanes = [_TextLane(left=left, right=right) for left, right, _support in filtered_intervals]
    ordered_lanes = sorted(lanes, key=lambda lane: lane.left)
    span_lines: list[tuple[_LineItem, BBox]] = []
    for item in line_geometry:
        bbox = item[1]
        best_lane, best_coverage, second_coverage = _best_lane_coverage(bbox, lanes)
        fits_only_one_lane = _fits_only_one_lane_ordered(
            bbox,
            best_lane,
            ordered_lanes,
            anchor_tolerance,
        )
        # Lines that cover two stable text columns at the same time are still cross-column content; lines that only enter a single side column and do not cross the column gap
        # Wide text lines are returned to this column to prevent narrow legends from erroneously squeezing text into span lane.
        if second_coverage >= 0.2 and not fits_only_one_lane:
            span_lines.append(item)
            continue
        if len(lanes) == 1 or fits_only_one_lane:
            best_lane.lines.append(item)
        else:
            span_lines.append(item)

    if recalculate_intervals:
        _expand_nested_lane_intervals_from_members(
            lanes,
            anchor_tolerance,
        )
    if span_lines:
        lanes.append(
            _TextLane(
                left=min(item[1][0] for item in span_lines),
                right=max(item[1][2] for item in span_lines),
                lines=span_lines,
                is_span=True,
            )
        )
    _reattach_span_lane_continuations(lanes, median_height)
    _reattach_cross_lane_short_tails(lanes, median_height)
    return lanes


def _best_lane_coverage(
    bbox: BBox,
    lanes: list[_TextLane],
) -> tuple[_TextLane, float, float]:
    """Find the best column coverage and the second highest coverage in one traversal, and retain the original first best column in a tie."""

    line_width = max(0.1, bbox[2] - bbox[0])
    best_lane = lanes[0]
    best_coverage = -1.0
    second_coverage = -1.0
    for lane in lanes:
        coverage = max(0.0, min(bbox[2], lane.right) - max(bbox[0], lane.left)) / line_width
        if coverage > best_coverage:
            second_coverage = best_coverage
            best_coverage = coverage
            best_lane = lane
        elif coverage > second_coverage:
            second_coverage = coverage
    return best_lane, best_coverage, second_coverage


def _fits_only_one_lane_ordered(
    bbox: BBox,
    best_lane: _TextLane,
    ordered: list[_TextLane],
    tolerance: float,
) -> bool:
    """Determines whether wide rows only stay in one column on the caller's presorted column sequence."""

    lane_index = ordered.index(best_lane)
    if lane_index > 0 and bbox[0] < ordered[lane_index - 1].right - tolerance:
        return False
    if lane_index + 1 < len(ordered) and bbox[2] > ordered[lane_index + 1].left - 0.25 * tolerance:
        return False
    return best_lane.left - tolerance <= _bbox_center_x(bbox) <= best_lane.right + max(tolerance, bbox[2] - best_lane.right)


def _fits_only_one_lane(
    bbox: BBox,
    best_lane: _TextLane,
    lanes: list[_TextLane],
    tolerance: float,
) -> bool:
    """The old internal entry is retained; independent calls still sort themselves, and the output is exactly the same as the presorted version."""

    return _fits_only_one_lane_ordered(
        bbox,
        best_lane,
        sorted(lanes, key=lambda lane: lane.left),
        tolerance,
    )


def _expand_nested_lane_intervals_from_members(
    lanes: list[_TextLane],
    tolerance: float,
) -> None:
    """Expand local column boundaries by owned members and preserve stable column grooves until adjacent columns intersect."""

    for lane in lanes:
        alignment_tolerance = max(1.0, 0.5 * tolerance)
        body_members = [bbox for line, bbox in lane.lines if line.semantic_type is None]
        if not body_members:
            continue
        aligned_members = [
            bbox
            for bbox in body_members
            if abs(bbox[0] - lane.left) <= alignment_tolerance or abs(bbox[2] - lane.right) <= alignment_tolerance
        ]
        if not aligned_members:
            continue
        # Headers, page numbers and titles are not involved; at the same time, only the main text with stable anchor points on at least one side is allowed to expand the column width.
        # Prevent another layout section later on the page from widening the current partial column as a whole.
        lane.left = min(lane.left, min(bbox[0] for bbox in aligned_members))
        lane.right = max(lane.right, max(bbox[2] for bbox in aligned_members))
    ordered = sorted(lanes, key=lambda lane: lane.left)
    for left_lane, right_lane in zip(ordered, ordered[1:]):
        if left_lane.right < right_lane.left - tolerance:
            continue
        midpoint = (left_lane.right + right_lane.left) / 2.0
        left_lane.right = min(left_lane.right, midpoint)
        right_lane.left = max(right_lane.left, midpoint)


def _infer_nested_column_band(
    line_geometry: list[tuple[_LineItem, BBox]],
    local_page_width: float,
    median_height: float,
    outer_interval: tuple[float, float],
    *,
    enhanced: bool = False,
) -> tuple[list[_TextLane], float, float] | None:
    """Find side-by-side text columns that occupy only a partial vertical range within the full-width format."""

    outer_width = max(0.1, outer_interval[1] - outer_interval[0])
    candidates = [
        item
        for item in line_geometry
        if item[0].semantic_type is None
        and max(
            4.0 * _line_effective_height(*item),
            0.12 * local_page_width,
        )
        <= item[1][2] - item[1][0]
        <= 0.62 * outer_width
    ]
    if len(candidates) < 6:
        return None

    center_tolerance = max(2.0 * median_height, 0.06 * local_page_width)
    center_clusters: list[list[tuple[_LineItem, BBox]]] = []
    for item in sorted(candidates, key=lambda value: _bbox_center_x(value[1])):
        center = _bbox_center_x(item[1])
        target = next(
            (
                cluster
                for cluster in center_clusters
                if abs(center - statistics.median(_bbox_center_x(member[1]) for member in cluster)) <= center_tolerance
            ),
            None,
        )
        if target is None:
            center_clusters.append([item])
        else:
            target.append(item)

    supported = [cluster for cluster in center_clusters if len(cluster) >= 3]
    supported.sort(key=lambda cluster: statistics.median(_bbox_center_x(item[1]) for item in cluster))
    best_pair: (
        tuple[
            tuple[int, float, float],
            list[tuple[_LineItem, BBox]],
            list[tuple[_LineItem, BBox]],
            tuple[float, float],
            tuple[float, float],
        ]
        | None
    ) = None
    cluster_pairs = (
        [
            (left_cluster, right_cluster)
            for left_index, left_cluster in enumerate(supported[:-1])
            for right_cluster in supported[left_index + 1 :]
        ]
        if enhanced
        else list(zip(supported, supported[1:]))
    )
    for left_cluster, right_cluster in cluster_pairs:
        left_interval = (
            statistics.median(item[1][0] for item in left_cluster),
            statistics.median(item[1][2] for item in left_cluster),
        )
        right_interval = (
            statistics.median(item[1][0] for item in right_cluster),
            statistics.median(item[1][2] for item in right_cluster),
        )
        gutter = right_interval[0] - left_interval[1]
        common_top = max(
            min(item[1][1] for item in left_cluster),
            min(item[1][1] for item in right_cluster),
        )
        common_bottom = min(
            max(item[1][3] for item in left_cluster),
            max(item[1][3] for item in right_cluster),
        )
        combined_width = right_interval[1] - left_interval[0]
        if (
            gutter < max(6.0, 0.75 * median_height)
            or common_bottom - common_top < 2.0 * median_height
            or combined_width < 0.55 * outer_width
        ):
            continue
        score = (
            min(len(left_cluster), len(right_cluster)),
            common_bottom - common_top,
            gutter,
        )
        candidate_pair = (
            score,
            left_cluster,
            right_cluster,
            left_interval,
            right_interval,
        )
        if best_pair is None or candidate_pair[0] > best_pair[0]:
            best_pair = candidate_pair
    if best_pair is None:
        return None

    _score, left_cluster, right_cluster, left_interval, right_interval = best_pair
    band_top = min(item[1][1] for item in [*left_cluster, *right_cluster]) - 0.5 * median_height
    band_bottom = max(item[1][3] for item in [*left_cluster, *right_cluster]) + 0.5 * median_height
    return (
        [
            _TextLane(left=left_interval[0], right=left_interval[1]),
            _TextLane(left=right_interval[0], right=right_interval[1]),
        ],
        band_top,
        band_bottom,
    )


def _reattach_span_lane_continuations(
    lanes: list[_TextLane],
    median_height: float,
) -> None:
    """Move the single-column-wide short trailing line immediately following the stable hurdle multiline back to the corresponding span lane."""

    regular_lanes = [lane for lane in lanes if not lane.is_span]
    span_lanes = [lane for lane in lanes if lane.is_span]
    if len(regular_lanes) < 2 or not span_lanes:
        return

    for span_lane in span_lanes:
        _reattach_repeated_indented_span_tails(
            span_lane,
            regular_lanes,
            median_height,
        )
        while True:
            span_lane.lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))
            candidates: list[
                tuple[
                    float,
                    float,
                    _TextLane,
                    tuple[_LineItem, BBox],
                ]
            ] = []
            for regular_lane in regular_lanes:
                for candidate in regular_lane.lines:
                    candidate_line, candidate_bbox = candidate
                    preceding = [
                        item
                        for item in span_lane.lines
                        if item[0].semantic_type == candidate_line.semantic_type and item[1][1] < candidate_bbox[1]
                    ]
                    if len(preceding) < 2:
                        continue
                    previous, last = preceding[-2:]
                    previous_height = _line_effective_height(*previous)
                    last_height = _line_effective_height(*last)
                    if (
                        abs(previous[1][0] - last[1][0]) > 0.75 * median_height
                        or max(previous_height, last_height) / min(previous_height, last_height) > 1.35
                        or not _title_fonts_compatible(previous[0], last[0])
                        or not -0.25 * median_height <= _effective_text_row_gap(previous, last) <= 0.75 * median_height
                    ):
                        continue
                    gap = _effective_text_row_gap(last, candidate)
                    candidate_height = _line_effective_height(*candidate)
                    if (
                        not -0.25 * median_height <= gap <= 0.75 * median_height
                        or abs(candidate_bbox[0] - last[1][0]) > 0.75 * median_height
                        or max(last_height, candidate_height) / min(last_height, candidate_height) > 1.35
                        or not _title_fonts_compatible(last[0], candidate_line)
                    ):
                        continue
                    has_parallel_peer = any(
                        other_line is not candidate_line
                        and _bbox_axis_overlap_ratio(
                            candidate_bbox,
                            other_bbox,
                            axis="y",
                        )
                        >= 0.5
                        for lane in regular_lanes
                        for other_line, other_bbox in lane.lines
                    )
                    if has_parallel_peer:
                        continue
                    candidates.append(
                        (
                            candidate_bbox[1],
                            max(0.0, gap),
                            regular_lane,
                            candidate,
                        )
                    )
            if not candidates:
                break
            _top, _gap, regular_lane, candidate = min(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            regular_lane.lines.remove(candidate)
            span_lane.lines.append(candidate)
            span_lane.left = min(span_lane.left, candidate[1][0])
            span_lane.right = max(span_lane.right, candidate[1][2])
            span_lane.lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))


def _lane_accepts_short_tail(
    candidate: tuple[_LineItem, BBox],
    lane: _TextLane,
    median_height: float,
) -> bool:
    """Use the same set of preceding lines of evidence to determine whether the current column or other columns can accommodate the text short tail."""

    candidate_line, candidate_bbox = candidate
    preceding = [
        item for item in lane.lines if item[0].semantic_type == candidate_line.semantic_type and item[1][1] < candidate_bbox[1]
    ]
    if not preceding:
        return False
    # For the previous rows at the same position, the smaller source_index is used in stable sorting in the column to prevent subsequent insertions from triggering sorting and changing the used evidence.
    previous = max(preceding, key=lambda item: (item[1][1], item[1][0], -item[0].source_index))
    return _short_tail_accepts_previous(candidate, lane, median_height, previous)


def _short_tail_accepts_previous(candidate, lane, median_height, previous) -> bool:
    """Reuse the determined preamble lines of the original rules, keeping the font, spacing, and visual line thresholds unchanged."""
    candidate_line, candidate_bbox = candidate
    previous_line, previous_bbox = previous
    pair_height = max(
        _line_effective_height(*previous),
        _line_effective_height(*candidate),
        median_height,
    )
    lane_width = max(0.1, lane.right - lane.left)
    if (
        previous_bbox[2] - previous_bbox[0] < 0.65 * lane_width
        or candidate_bbox[2] - candidate_bbox[0] > 0.85 * lane_width
        or candidate_bbox[0] < lane.left - 0.75 * pair_height
        or candidate_bbox[2] > lane.right + 0.75 * pair_height
        or abs(candidate_bbox[0] - previous_bbox[0]) > 0.75 * pair_height
        or not _title_fonts_compatible(previous_line, candidate_line)
    ):
        return False
    gap = _effective_body_text_row_gap(previous, candidate)
    if not -0.25 * pair_height <= gap <= 0.9 * pair_height:
        return False
    if (
        previous_line.visual_row_id is not None
        and candidate_line.visual_row_id is not None
        and not 0 < candidate_line.visual_row_id - previous_line.visual_row_id <= 2
    ):
        return False
    return True


def _reattach_cross_lane_short_tails(
    lanes: list[_TextLane],
    median_height: float,
) -> None:
    """The most recent previous row of each column is reused according to vertical events; special input is implemented by the original sequential scan."""
    # When there is only one column, there is no "cross-column" attribution, and the input checksum pre-sequence scan is skipped directly;
    # This does not change members, order, or boundaries, and also allows common single-column pages to avoid full-page repeats.
    if len(lanes) < 2:
        return
    pending = []
    seen_lines, seen_indices = set(), set()
    for lane_index, lane in enumerate(lanes):
        for ordinal, row in enumerate(lane.lines):
            line, bbox = row
            if (
                type(line) is not _LineItem
                or type(line.source_index) is not int
                or id(line) in seen_lines
                or line.source_index in seen_indices
                or type(bbox) not in (tuple, list)
                or len(bbox) != 4
                or (line.semantic_type is not None and type(line.semantic_type) is not str)
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox)
            ):
                return _reattach_cross_lane_short_tails_python(lanes, median_height)
            seen_lines.add(id(line))
            seen_indices.add(line.source_index)
            if line.semantic_type is None:
                pending.append((lane_index, row, ordinal))
    pending.sort(key=lambda item: (item[1][1][1], item[1][1][0], item[1][0].source_index))
    previous = [None] * len(lanes)
    previous_keys = [None] * len(lanes)
    append_ordinal = sum(len(lane.lines) for lane in lanes)
    position = 0
    while position < len(pending):
        end = position + 1
        top = pending[position][1][1][1]
        while end < len(pending) and pending[end][1][1][1] == top:
            end += 1
        settled = []
        for source, candidate, ordinal in pending[position:end]:
            matches = [
                index
                for index, lane in enumerate(lanes)
                if previous[index] is not None and _short_tail_accepts_previous(candidate, lane, median_height, previous[index])
            ]
            target = matches[0] if len(matches) == 1 else source
            if target != source:
                lanes[source].lines.remove(candidate)
                lanes[target].lines.append(candidate)
                lanes[target].lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))
                ordinal = append_ordinal
                append_ordinal += 1
            settled.append((target, candidate, ordinal))
        # Only the y that is strictly lower can become the preamble row of the next group. Candidates with the same high level do not affect each other.
        for target, row, ordinal in settled:
            key = (row[1][1], row[1][0], -row[0].source_index, -ordinal)
            if previous_keys[target] is None or key > previous_keys[target]:
                previous_keys[target], previous[target] = key, row
        position = end


def _reattach_cross_lane_short_tails_python(
    lanes: list[_TextLane],
    median_height: float,
) -> None:
    """Move the short tail of the text with unique column ownership back to the corresponding column in the preorder dependency order."""

    # Column boundaries are fixed, and matching only relies on y0 strictly smaller rows; each row is migrated at most once after the upper row is determined first.
    # The original object and its source column are retained, and different lines of text cannot be merged or skipped with source_index.
    pending = [(lane, candidate) for lane in lanes for candidate in lane.lines if candidate[0].semantic_type is None]
    pending.sort(key=lambda item: (item[1][1][1], item[1][1][0], item[1][0].source_index))
    for source_lane, candidate in pending:
        # The current column must also participate in the uniqueness judgment; if multiple columns can be accepted, it will remain in place to avoid two-way migration.
        matches = [lane for lane in lanes if _lane_accepts_short_tail(candidate, lane, median_height)]
        if len(matches) != 1 or matches[0] is source_lane:
            continue
        target_lane = matches[0]
        source_lane.lines.remove(candidate)
        target_lane.lines.append(candidate)
        target_lane.lines.sort(
            key=lambda item: (item[1][1], item[1][0], item[0].source_index),
        )


def _reattach_repeated_indented_span_tails(
    span_lane: _TextLane,
    regular_lanes: list[_TextLane],
    median_height: float,
) -> None:
    """Identify the repeated first line of the hurdle and the indented short tail, and move the short tail back to the hurdle band."""

    span_rows = sorted(
        span_lane.lines,
        key=lambda item: (item[1][1], item[1][0], item[0].source_index),
    )
    matches: list[tuple[_TextLane, tuple[_LineItem, BBox], tuple[_LineItem, BBox], float]] = []
    for span_index, span_row in enumerate(span_rows):
        span_line, span_bbox = span_row
        next_span_top = span_rows[span_index + 1][1][1] if span_index + 1 < len(span_rows) else float("inf")
        for regular_lane in regular_lanes:
            for candidate in regular_lane.lines:
                candidate_line, candidate_bbox = candidate
                if candidate_line.semantic_type != span_line.semantic_type:
                    continue
                gap = _effective_text_row_gap(span_row, candidate)
                indent = candidate_bbox[0] - span_bbox[0]
                span_height = _line_effective_height(*span_row)
                candidate_height = _line_effective_height(*candidate)
                if (
                    candidate_bbox[1] <= span_bbox[1]
                    or candidate_bbox[1] >= next_span_top
                    or not -0.25 * median_height <= gap <= 0.75 * median_height
                    or not 0.75 * median_height <= indent <= 6.0 * median_height
                    or max(span_height, candidate_height) / min(span_height, candidate_height) > 1.35
                    or not _title_fonts_compatible(span_line, candidate_line)
                ):
                    continue
                has_parallel_peer = any(
                    other_line is not candidate_line and _bbox_axis_overlap_ratio(candidate_bbox, other_bbox, axis="y") >= 0.5
                    for lane in regular_lanes
                    for other_line, other_bbox in lane.lines
                )
                if not has_parallel_peer:
                    matches.append((regular_lane, candidate, span_row, indent))

    if len(matches) < 2:
        return
    median_indent = statistics.median(match[3] for match in matches)
    supported = [match for match in matches if abs(match[3] - median_indent) <= max(0.75 * median_height, 0.25 * median_indent)]
    if len(supported) < 2:
        return
    for regular_lane, candidate, _span_row, _indent in supported:
        if candidate not in regular_lane.lines:
            continue
        regular_lane.lines.remove(candidate)
        span_lane.lines.append(candidate)
        span_lane.left = min(span_lane.left, candidate[1][0])
        span_lane.right = max(span_lane.right, candidate[1][2])
    span_lane.lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))


def _estimate_lane_gap(lane: _TextLane) -> tuple[float, float]:
    """The row height is only extracted once for this line spacing estimation, and the sorting still applies to the original column member list."""
    from ...._compute_backend import get_native

    native = get_native()
    if native is None:
        return _estimate_lane_gap_python(lane)
    lane.lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))
    result = native.lane_gap(
        [bbox for _line, bbox in lane.lines],
        [_line_effective_height(line, bbox) for line, bbox in lane.lines],
        [line.restored_inline_cluster for line, _bbox in lane.lines],
        [
            previous[0].visual_row_id == current[0].visual_row_id and (previous[0].split_from_row or current[0].split_from_row)
            for previous, current in zip(lane.lines, lane.lines[1:])
        ],
    )
    return result if result is not None else _estimate_lane_gap_python(lane)


def _estimate_lane_gap_python(lane: _TextLane) -> tuple[float, float]:
    """Estimating conventional headroom and MAD from clusters of smaller gaps compatible with adjacent rows within the pen strip."""

    lane.lines.sort(key=lambda item: (item[1][1], item[1][0], item[0].source_index))
    heights = [_line_effective_height(line, bbox) for line, bbox in lane.lines]
    median_height = statistics.median(heights) if heights else 1.0
    gaps: list[float] = []
    for previous, current in zip(lane.lines, lane.lines[1:]):
        previous_line, previous_bbox = previous
        current_line, current_bbox = current
        if previous_line.visual_row_id == current_line.visual_row_id and (
            previous_line.split_from_row or current_line.split_from_row
        ):
            continue
        previous_height = _line_effective_height(previous_line, previous_bbox)
        current_height = _line_effective_height(current_line, current_bbox)
        if max(previous_height, current_height) / min(previous_height, current_height) > 1.35:
            continue
        pair_height = max(previous_height, current_height)
        gap = _effective_text_row_gap(previous, current)
        if gap < -0.25 * pair_height or gap > 2.0 * pair_height:
            continue
        if (
            _bbox_axis_overlap_ratio(previous_bbox, current_bbox, axis="x") < 0.5
            and abs(previous_bbox[0] - current_bbox[0]) > 1.5 * median_height
        ):
            continue
        # PDF Character boxes often produce minimal overlap between adjacent baselines; zero headroom is included in regular line spacing statistics.
        gaps.append(max(0.0, gap))

    if not gaps:
        return 0.35 * median_height, 0.0
    sorted_gaps = sorted(gaps)
    lower_count = max(1, math.ceil(len(sorted_gaps) * 0.6))
    lower_gaps = sorted_gaps[:lower_count]
    regular_gap = statistics.median(lower_gaps)
    gap_mad = statistics.median(abs(gap - regular_gap) for gap in lower_gaps)
    return regular_gap, gap_mad


def _is_structural_typography_gap(
    previous_height: float,
    current_height: float,
    vertical_gap: float,
    regular_gap: float,
    gap_mad: float,
    *,
    reliable_style_change: bool = False,
) -> bool:
    """Determine whether the abnormal inter-segment headroom also has line height or reliable font level changes."""

    pair_height = max(previous_height, current_height)
    minimum_height = max(0.1, min(previous_height, current_height))
    prominent_gap = vertical_gap > regular_gap + max(
        0.75 * pair_height,
        3.0 * gap_mad,
    )
    return prominent_gap and (pair_height / minimum_height >= 1.12 or reliable_style_change)


def _should_connect_text_rows(
    previous: tuple[_LineItem, BBox],
    current: tuple[_LineItem, BBox],
    lane: _TextLane,
    regular_gap: float,
    gap_mad: float,
    table_bboxes: list[BBox],
    axis_lines: list[_LocalAxisLine],
) -> bool:
    """Determine whether two adjacent visual lines are in the same paragraph based on local spacing, first line indentation, fonts and obstacles."""

    previous_line, previous_bbox = previous
    current_line, current_bbox = current
    previous_height = _line_effective_height(previous_line, previous_bbox)
    current_height = _line_effective_height(current_line, current_bbox)
    pair_height = max(previous_height, current_height)
    lane_width = max(0.1, lane.right - lane.left)
    previous_width = previous_bbox[2] - previous_bbox[0]
    current_width = current_bbox[2] - current_bbox[0]
    vertical_gap = _effective_body_text_row_gap(previous, current)
    if previous_line.visual_row_id == current_line.visual_row_id and (
        previous_line.split_from_row or current_line.split_from_row
    ):
        return False
    if current_height < 0.88 * previous_height and vertical_gap > regular_gap + max(0.25 * previous_height, 3.0 * gap_mad):
        return False
    both_fill_lane = previous_width >= 0.8 * lane_width and current_width >= 0.8 * lane_width
    aligned_left_edges = abs(previous_bbox[0] - current_bbox[0]) <= 0.5 * pair_height
    current_returns_to_lane_left = (
        abs(current_bbox[0] - lane.left) <= 0.75 * pair_height
        and -0.5 * pair_height <= previous_bbox[0] - lane.left <= 2.0 * pair_height
    )
    reliable_font_match = (
        previous_line.font_signature is None
        or current_line.font_signature is None
        or previous_line.font_coverage < 0.75
        or current_line.font_coverage < 0.75
        or previous_line.font_signature == current_line.font_signature
        or _font_signatures_share_family(
            previous_line.font_signature,
            current_line.font_signature,
        )
        or (current_width <= 0.5 * lane_width and previous_line.font_signature[1] == current_line.font_signature[1])
    )
    previous_indent = previous_bbox[0] - lane.left
    # After the extreme font matrix correction, the height of the old font box no longer exaggerates the indentation; the distance between the left edge of the real line and the regular baseline still restricts line continuation.
    local_metric_pair = all(
        line.style_scale_repaired
        and line.source_bbox is not None
        and line.source_bbox[3] - line.source_bbox[1] >= 4 * _line_effective_height(line, bbox)
        for line, bbox in (previous, current)
    )
    repeated_indent_continuation = (
        previous_indent >= max(5.0, (1.25 if local_metric_pair else 1.8) * pair_height)
        and abs(current_bbox[0] - previous_bbox[0]) <= 0.5 * pair_height
        and reliable_font_match
        and -0.25 * pair_height <= vertical_gap <= regular_gap + max(0.75 * pair_height, 3.0 * gap_mad)
    )
    # Full-line text not terminated by commas can be connected to the bold enumeration short tail of the font family; real titles, white space, and font size changes are still rejected.
    emphasized_enumeration_tail = (
        previous_line.semantic_type is None
        and current_line.semantic_type is None
        and previous_line.text.rstrip().endswith((",", "，"))
        and (current_line.paragraph_terminal or bool(re.search(r"[.!?。！？]$", current_line.text.rstrip())))
        and len(current_line.text.split()) <= 10
        and previous_line.font_signature is not None
        and current_line.font_signature is not None
        and _font_signatures_share_emphasis_family(previous_line.font_signature, current_line.font_signature)
        and max(previous_height, current_height) <= 1.2 * min(previous_height, current_height)
        and -0.1 * pair_height <= vertical_gap <= 0.4 * pair_height
    )
    # After a full column of unfinished sentences, short italicized sentences starting with conjunctions or lowercase words are still text continuations; independent capital labels and formulas do not apply.
    italic_sentence_tail = (
        previous_line.semantic_type is None
        and current_line.semantic_type is None
        and not previous_line.paragraph_terminal
        and re.search(r"[.!?。！？:：;；]$", previous_line.text.rstrip()) is None
        and not previous_line.formula_candidate_only
        and not current_line.formula_candidate_only
        and not current_line.compact_formula_cluster
        and (current_line.paragraph_terminal or bool(re.search(r"[.!?。！？]$", current_line.text.rstrip())))
        and re.match(r"(?:[+&]\s+|[a-z])", current_line.text.strip()) is not None
        and 1 <= len(re.findall(r"[A-Za-z]{2,}", current_line.text)) <= 10
        and previous_line.font_signature is not None
        and current_line.font_signature is not None
        and current_line.font_signature[1] & PDF_FONT_ITALIC_FLAG
        and not previous_line.font_signature[1] & PDF_FONT_ITALIC_FLAG
        and _font_signatures_share_emphasis_family(previous_line.font_signature, current_line.font_signature)
        and not _font_weights_conflict(previous_line, current_line)
        and max(previous_height, current_height) <= 1.2 * min(previous_height, current_height)
        and -0.1 * pair_height <= vertical_gap <= 0.4 * pair_height
    )
    safe_short_tail = (
        previous_width >= 0.75 * lane_width
        and current_width <= 0.7 * lane_width
        and (aligned_left_edges or current_returns_to_lane_left)
        and (reliable_font_match or emphasized_enumeration_tail or italic_sentence_tail)
        and (not _font_weights_conflict(previous_line, current_line) or emphasized_enumeration_tail)
        and -0.25 * pair_height <= vertical_gap <= regular_gap + max(0.75 * pair_height, 3.0 * gap_mad)
    )
    height_ratio = max(previous_height, current_height) / min(previous_height, current_height)
    font_style_changed = (
        previous_line.font_signature is not None
        and current_line.font_signature is not None
        and previous_line.font_signature[1] != current_line.font_signature[1]
        and not _font_signatures_share_family(
            previous_line.font_signature,
            current_line.font_signature,
        )
    )
    reliable_style_conflict = (
        previous_line.font_signature is not None
        and current_line.font_signature is not None
        and previous_line.font_coverage >= 0.75
        and current_line.font_coverage >= 0.75
        and (
            (
                previous_line.font_signature != current_line.font_signature
                and not _font_signatures_share_family(
                    previous_line.font_signature,
                    current_line.font_signature,
                )
            )
            or _font_weights_conflict(previous_line, current_line)
        )
    )
    fallback_font_continuation = (
        previous_line.font_signature is not None
        and current_line.font_signature is not None
        and previous_line.font_coverage >= 0.75
        and current_line.font_coverage >= 0.75
        and previous_line.font_signature[0] != current_line.font_signature[0]
        and previous_line.font_signature[1] == current_line.font_signature[1]
        and aligned_left_edges
        and height_ratio <= 1.25
        and not _font_weights_conflict(previous_line, current_line)
        and -0.25 * pair_height <= vertical_gap <= regular_gap + max(0.35 * pair_height, 3.0 * gap_mad)
    )
    if (
        font_style_changed
        and _font_weights_conflict(previous_line, current_line)
        and not fallback_font_continuation
        and not emphasized_enumeration_tail
    ):
        # Explicit style bits and significant font weight changes at the same time are still hard boundaries and cannot be relaxed by full column geometry.
        return False
    if (
        not is_hyphen_at_line_end(previous_line.text)
        and _is_structural_typography_gap(
            previous_height,
            current_height,
            vertical_gap,
            regular_gap,
            gap_mad,
            reliable_style_change=reliable_style_conflict,
        )
        and not fallback_font_continuation
    ):
        # Even if the same column is full in the typesetting level transition such as legend to main text, it cannot be reabsorbed by the regular line continuation rules.
        return False
    full_width_continuation = (
        both_fill_lane
        and aligned_left_edges
        and not font_style_changed
        and vertical_gap <= regular_gap + max(0.75 * min(previous_height, current_height), 3.0 * gap_mad)
    )
    if height_ratio > 1.35 and not safe_short_tail and not full_width_continuation:
        # Full-column mixed fonts can be continued across font sizes, but explicit regular/italic style boundaries still maintain the original segmentation semantics.
        if not both_fill_lane or not aligned_left_edges or font_style_changed:
            return False

    if vertical_gap < -0.25 * pair_height:
        return False
    if (
        _bbox_axis_overlap_ratio(previous_bbox, current_bbox, axis="x") < 0.5
        and abs(previous_bbox[0] - current_bbox[0]) > 1.5 * pair_height
        and not safe_short_tail
    ):
        return False
    if _connection_crosses_table(previous_line.bbox, current_line.bbox, table_bboxes):
        return False
    if _horizontal_rule_separates_rows(previous_bbox, current_bbox, lane, axis_lines):
        return False

    gap_limit = max(
        regular_gap + max(0.5 * pair_height, 3.0 * gap_mad),
        1.1 * pair_height,
    )
    # Typographic breakers can skip indentation, font, and shortline rules, but must still be limited to adjacent physical lines.
    # Avoid the long distance "cross-" on the page from being mistakenly spelled into the same paragraph as the subsequent title.
    if is_hyphen_at_line_end(previous_line.text):
        return vertical_gap <= max(gap_limit, 1.8 * pair_height)
    if vertical_gap > gap_limit:
        return False

    terminal_previous = bool(re.search(r"[.!?。！？:：;；][\]\)}）】》”’'\"]*$", previous_line.text.rstrip()))
    sparse_lane = sum(line.semantic_type is None for line, _bbox in lane.lines) <= 6
    if (
        terminal_previous
        and not repeated_indent_continuation
        and ((sparse_lane and vertical_gap > 0.65 * pair_height) or vertical_gap > regular_gap + 0.5 * pair_height)
    ):
        return False

    # The local layout center may be further to the left than the inferred boundary of the entire column, and the indentation must also refer to the previous physical line.
    local_lane_left = min(lane.left, previous_bbox[0])
    local_lane_width = max(0.1, lane.right - local_lane_left)
    next_indent = current_bbox[0] - local_lane_left
    previous_fill = max(0.0, previous_bbox[2] - local_lane_left) / local_lane_width
    if (
        next_indent >= max(5.0, 0.65 * pair_height)
        and (previous_fill <= 0.8 or terminal_previous)
        and not safe_short_tail
        and not repeated_indent_continuation
    ):
        # Confirmed short tails with the same left margin are indented prior to the left margin of the column to prevent the last line after the reference colon from being cut off.
        return False

    abnormal_gap = vertical_gap > regular_gap + max(0.25 * pair_height, 3.0 * gap_mad)
    if (
        reliable_style_conflict
        and (abnormal_gap or min(previous_width, current_width) <= 0.7 * lane_width)
        and not both_fill_lane
        and not safe_short_tail
        and not fallback_font_continuation
    ):
        return False
    if abnormal_gap and min(previous_width, current_width) <= 0.65 * lane_width and not safe_short_tail:
        return False
    return True


def _horizontal_rule_separates_rows(
    previous_bbox: BBox,
    current_bbox: BBox,
    lane: _TextLane,
    axis_lines: list[_LocalAxisLine],
) -> bool:
    """Checks whether there is a long horizontal rule line covering the current column band between two adjacent lines of text."""

    if current_bbox[1] <= previous_bbox[3]:
        return False
    lane_width = max(0.1, lane.right - lane.left)
    for axis_line in axis_lines:
        if axis_line.orientation != "horizontal":
            continue
        line_y = _bbox_center_y(axis_line.bbox)
        if not previous_bbox[3] <= line_y <= current_bbox[1]:
            continue
        overlap = max(0.0, min(axis_line.bbox[2], lane.right) - max(axis_line.bbox[0], lane.left))
        if overlap / lane_width >= 0.6:
            return True
    return False


def _connection_crosses_table(
    first_bbox: BBox,
    second_bbox: BBox,
    table_bboxes: list[BBox],
) -> bool:
    """Check whether the center connecting area of two rows crosses the confirmed form."""

    first_center = (_bbox_center_x(first_bbox), _bbox_center_y(first_bbox))
    second_center = (_bbox_center_x(second_bbox), _bbox_center_y(second_bbox))
    connector = _coerce_bbox(
        (
            min(first_center[0], second_center[0]) - 0.1,
            min(first_center[1], second_center[1]) - 0.1,
            max(first_center[0], second_center[0]) + 0.1,
            max(first_center[1], second_center[1]) + 0.1,
        )
    )
    return connector is not None and any(_bbox_intersects(connector, table_bbox) for table_bbox in table_bboxes)
