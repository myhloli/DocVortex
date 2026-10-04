"""Format-neutral flood filling of spreadsheet data region discovery with the gap candidate scoring algorithm."""

from __future__ import annotations

import collections
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias


ContentPredicate: TypeAlias = Callable[[int, int], bool]
SpanLookup: TypeAlias = Callable[[int, int], tuple[int, int]]
RegionFactory: TypeAlias = Callable[[int], list["ConnectedRegion"]]

AUTO_GAP_TOLERANCE_CANDIDATES: tuple[int, ...] = (0, 1, 2)
AUTO_GAP_TOLERANCE_PREFERENCE: dict[int, int] = {1: 0, 0: 1, 2: 2}
AUTO_GAP_TOLERANCE_PREFERENCE_MARGIN = 0.15


@dataclass(frozen=True, slots=True)
class ConnectedRegion:
    """Save the set of connected content cells and their bounding boxes obtained by flooding once."""

    cells: frozenset[tuple[int, int]]
    row_start: int
    row_end: int
    col_start: int
    col_end: int

    @property
    def num_rows(self) -> int:
        """Returns the number of rows of the region's bounding box."""
        return self.row_end - self.row_start + 1

    @property
    def num_cols(self) -> int:
        """Returns the number of columns of the region's bounding box."""
        return self.col_end - self.col_start + 1


def discover_connected_regions(
    has_content: ContentPredicate,
    start_positions: Sequence[tuple[int, int]],
    max_row: int,
    max_col: int,
    gap_tolerance: int,
) -> list[ConnectedRegion]:
    """Flood-fill the unvisited content cells in reading order and return the connected areas of each starting point."""
    visited: set[tuple[int, int]] = set()
    regions: list[ConnectedRegion] = []
    for start_row, start_col in start_positions:
        if (start_row, start_col) in visited:
            continue
        region = _flood_fill_region(has_content, start_row, start_col, max_row, max_col, gap_tolerance)
        visited.update(region.cells)
        regions.append(region)
    return regions


def _flood_fill_region(
    has_content: ContentPredicate,
    start_row: int,
    start_col: int,
    max_row: int,
    max_col: int,
    gap_tolerance: int,
) -> ConnectedRegion:
    """Use the flood filling (BFS) strategy to determine the connected grid and bounding box of a table area.

    Neighbors in four directions can connect content across blank spaces within the tolerance distance. When encountering content that already belongs to the current area,
    The coordinates immediately stop expanding in this direction, ensuring that only the closest content cells are bridged in the same direction.
    """
    queue: collections.deque[tuple[int, int]] = collections.deque([(start_row, start_col)])
    table_cells: set[tuple[int, int]] = {(start_row, start_col)}

    min_r, max_r = start_row, start_row
    min_c, max_c = start_col, start_col

    def in_bounds_content(r: int, c: int) -> bool:
        """First perform bounding box-level out-of-bounds interception, and then entrust the caller with semantic content determination."""
        if r < 0 or c < 0 or r > max_row or c > max_col:
            return False
        return has_content(r, c)

    while queue:
        curr_r, curr_c = queue.popleft()

        min_r = min(min_r, curr_r)
        max_r = max(max_r, curr_r)
        min_c = min(min_c, curr_c)
        max_c = max(max_c, curr_c)

        directions = [
            (0, 1),  # right
            (0, -1),  # Left
            (1, 0),  # Down
            (-1, 0),  # superior
        ]

        for dr, dc in directions:
            # Neighbors are checked incrementally within a tolerated distance (nearest ones first).
            for step in range(1, gap_tolerance + 2):
                nr, nc = curr_r + (dr * step), curr_c + (dc * step)

                if (nr, nc) in table_cells:
                    break  # It already belongs to the current table, so the search will continue without crossing over.

                if in_bounds_content(nr, nc):
                    table_cells.add((nr, nc))
                    queue.append((nr, nc))
                    # Find the connection point in that direction and stop extending the interval.
                    break

    return ConnectedRegion(
        cells=frozenset(table_cells),
        row_start=min_r,
        row_end=max_r,
        col_start=min_c,
        col_end=max_c,
    )


def count_max_consecutive_true(flags: Sequence[bool]) -> int:
    """Returns the length of the longest consecutive true value in a Boolean sequence."""
    max_count = 0
    current = 0
    for flag in flags:
        if flag:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def _build_region_content_mask(
    region: ConnectedRegion,
    has_semantic_content: ContentPredicate,
    span_at: SpanLookup,
) -> list[list[bool]]:
    """Constructs a region semantic content mask containing merged spans."""
    mask = [[False for _ in range(region.num_cols)] for _ in range(region.num_rows)]
    for row in range(region.row_start, region.row_end + 1):
        for col in range(region.col_start, region.col_end + 1):
            if not has_semantic_content(row, col):
                continue
            row_span, col_span = span_at(row, col)
            for row_idx in range(row, min(row + row_span, region.row_end + 1)):
                for col_idx in range(col, min(col + col_span, region.col_end + 1)):
                    mask[row_idx - region.row_start][col_idx - region.col_start] = True
    return mask


def _is_real_singleton_region(region: ConnectedRegion, span_at: SpanLookup) -> bool:
    """Determine whether the candidate is a real table with a single cell and cannot be further split."""
    if region.num_rows != 1 or region.num_cols != 1:
        return False
    row_span, col_span = span_at(region.row_start, region.col_start)
    return row_span == 1 and col_span == 1


def _summarize_single_region(
    region: ConnectedRegion,
    has_semantic_content: ContentPredicate,
    span_at: SpanLookup,
) -> dict[str, float | int | bool]:
    """Single-region morphology metric used to calculate gap candidate scores."""
    table_area = region.num_rows * region.num_cols
    content_mask = _build_region_content_mask(region, has_semantic_content, span_at)
    content_area = sum(sum(1 for flag in row if flag) for row in content_mask)
    blank_ratio = 1.0 - (content_area / max(table_area, 1))

    interior_blank_rows = [not any(content_mask[row_idx]) for row_idx in range(1, max(region.num_rows - 1, 1))]
    interior_blank_cols = [
        not any(content_mask[row_idx][col_idx] for row_idx in range(region.num_rows))
        for col_idx in range(1, max(region.num_cols - 1, 1))
    ]
    if region.num_rows <= 2:
        interior_blank_rows = []
    if region.num_cols <= 2:
        interior_blank_cols = []

    interior_blank_row_count = sum(interior_blank_rows)
    interior_blank_col_count = sum(interior_blank_cols)
    max_consecutive_interior_blank_lines = max(
        count_max_consecutive_true(interior_blank_rows),
        count_max_consecutive_true(interior_blank_cols),
    )

    return {
        "table_area": table_area,
        "content_area": content_area,
        "blank_ratio": blank_ratio,
        "interior_blank_row_count": interior_blank_row_count,
        "interior_blank_col_count": interior_blank_col_count,
        "max_consecutive_interior_blank_lines": max_consecutive_interior_blank_lines,
        "real_singleton": _is_real_singleton_region(region, span_at),
    }


def summarize_connected_regions(
    regions: Sequence[ConnectedRegion],
    has_semantic_content: ContentPredicate,
    span_at: SpanLookup,
) -> dict[str, float | int]:
    """Summarize the penalty metrics for a set of gap candidate regions."""
    table_count = len(regions)
    real_singleton_count = 0
    severe_separator_count = 0
    sparse_large_table_count = 0
    total_area = 0
    weighted_blank_numerator = 0.0
    total_interior_blank_lines = 0
    total_possible_interior_lines = 0
    row_cover_count: collections.Counter[int] = collections.Counter()

    for region in regions:
        region_summary = _summarize_single_region(region, has_semantic_content, span_at)
        table_area = int(region_summary["table_area"])
        blank_ratio = float(region_summary["blank_ratio"])
        interior_blank_row_count = int(region_summary["interior_blank_row_count"])
        interior_blank_col_count = int(region_summary["interior_blank_col_count"])
        max_consecutive_interior_blank_lines = int(region_summary["max_consecutive_interior_blank_lines"])

        total_area += table_area
        weighted_blank_numerator += table_area * blank_ratio
        total_interior_blank_lines += interior_blank_row_count + interior_blank_col_count
        total_possible_interior_lines += max(region.num_rows - 2, 0) + max(region.num_cols - 2, 0)
        for row_idx in range(region.row_start, region.row_start + region.num_rows):
            row_cover_count[row_idx] += 1

        if bool(region_summary["real_singleton"]):
            real_singleton_count += 1
        if table_area >= 6 and blank_ratio > 0.35:
            sparse_large_table_count += 1
        if max_consecutive_interior_blank_lines >= 2:
            severe_separator_count += 1

    occupied_row_count = max(len(row_cover_count), 1)
    row_overlap_excess_ratio = sum(max(0, count - 1) for count in row_cover_count.values()) / occupied_row_count

    return {
        "real_singleton_ratio": real_singleton_count / max(table_count, 1),
        "weighted_blank_ratio": weighted_blank_numerator / max(total_area, 1),
        "interior_blank_line_ratio": total_interior_blank_lines / max(total_possible_interior_lines, 1),
        "sparse_large_table_ratio": sparse_large_table_count / max(table_count, 1),
        "severe_separator_count": severe_separator_count,
        "row_overlap_excess_ratio": row_overlap_excess_ratio,
    }


def gap_candidate_penalty(summary: dict[str, float | int]) -> float:
    """A set of regional morphological indicators is converted into gap candidate penalty points according to fixed weights."""
    return (
        6.0 * int(summary["severe_separator_count"])
        + 2.5 * float(summary["interior_blank_line_ratio"])
        + 1.5 * float(summary["sparse_large_table_ratio"])
        + 1.0 * float(summary["real_singleton_ratio"])
        + 0.5 * float(summary["weighted_blank_ratio"])
        + 1.0 * float(summary["row_overlap_excess_ratio"])
    )


def select_best_gap_candidate(
    discover_with_tolerance: RegionFactory,
    has_semantic_content: ContentPredicate,
    span_at: SpanLookup,
) -> tuple[int, float, list[ConnectedRegion]]:
    """Select the most stable gap tolerance in fixed candidate and preference order."""
    candidates = []
    for gap_tolerance in AUTO_GAP_TOLERANCE_CANDIDATES:
        regions = discover_with_tolerance(gap_tolerance)
        summary = summarize_connected_regions(regions, has_semantic_content, span_at)
        candidates.append(
            {
                "gap_tolerance": gap_tolerance,
                "penalty": gap_candidate_penalty(summary),
                "regions": regions,
                **summary,
            }
        )

    min_penalty = min(float(candidate["penalty"]) for candidate in candidates)
    near_best_candidates = [
        candidate
        for candidate in candidates
        if float(candidate["penalty"]) <= (min_penalty + AUTO_GAP_TOLERANCE_PREFERENCE_MARGIN)
    ]

    best_candidate = min(
        near_best_candidates,
        key=lambda candidate: (
            int(candidate["severe_separator_count"]),
            AUTO_GAP_TOLERANCE_PREFERENCE[int(candidate["gap_tolerance"])],
            float(candidate["interior_blank_line_ratio"]),
            float(candidate["penalty"]),
        ),
    )
    return (
        int(best_candidate["gap_tolerance"]),
        float(best_candidate["penalty"]),
        best_candidate["regions"],
    )


def region_semantic_positions(region: ConnectedRegion, has_semantic_content: ContentPredicate) -> set[tuple[int, int]]:
    """Returns the coordinates with semantic content within the region's bounding box."""
    return {
        (row, col)
        for row in range(region.row_start, region.row_end + 1)
        for col in range(region.col_start, region.col_end + 1)
        if has_semantic_content(row, col)
    }


def keep_maximal_by_semantic_sets(semantic_sets: Sequence[set[tuple[int, int]]]) -> list[int]:
    """Returns a subscript whose semantic coordinate set is not strictly contained by other candidates."""
    kept_indices: list[int] = []
    for index, semantic_set in enumerate(semantic_sets):
        if any(semantic_set < semantic_sets[other_index] for other_index in range(len(semantic_sets)) if other_index != index):
            continue
        kept_indices.append(index)
    return kept_indices


__all__ = [
    "AUTO_GAP_TOLERANCE_CANDIDATES",
    "AUTO_GAP_TOLERANCE_PREFERENCE",
    "AUTO_GAP_TOLERANCE_PREFERENCE_MARGIN",
    "ConnectedRegion",
    "count_max_consecutive_true",
    "discover_connected_regions",
    "gap_candidate_penalty",
    "keep_maximal_by_semantic_sets",
    "region_semantic_positions",
    "select_best_gap_candidate",
    "summarize_connected_regions",
]
