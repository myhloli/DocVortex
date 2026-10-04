"""Implement XYCut++ reading order sorting that does not rely on semantic models based on geometric information."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Any, Final, Sequence


DEFAULT_BETA: Final = 1.3
DEFAULT_DENSITY_THRESHOLD: Final = 0.9
OVERLAP_THRESHOLD: Final = 0.1
MIN_OVERLAP_COUNT: Final = 2
MIN_GAP_THRESHOLD: Final = 5.0
NARROW_ELEMENT_WIDTH_RATIO: Final = 0.1


@dataclass(frozen=True)
class _CutInfo:
    """Record the projection segmentation position and the corresponding blank spacing."""

    position: float
    gap: float


@dataclass(frozen=True)
class _SortableEntry:
    """Save objects to be sorted, original serial numbers and standardized absolute coordinates."""

    index: int
    payload: dict[str, Any]
    bbox: tuple[float, float, float, float]

    @property
    def left(self) -> float:
        """Return left boundary."""
        return self.bbox[0]

    @property
    def top(self) -> float:
        """Return to the upper boundary."""
        return self.bbox[1]

    @property
    def right(self) -> float:
        """Return to the right boundary."""
        return self.bbox[2]

    @property
    def bottom(self) -> float:
        """Return to the lower boundary."""
        return self.bbox[3]

    @property
    def width(self) -> float:
        """Returns the element width."""
        return self.right - self.left

    @property
    def height(self) -> float:
        """Return element height."""
        return self.bottom - self.top

    @property
    def area(self) -> float:
        """Return the element bounding box area."""
        return self.width * self.height

    @property
    def center_x(self) -> float:
        """Returns the horizontal center of the element."""
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        """Returns the longitudinal center of the element."""
        return (self.top + self.bottom) / 2.0


def sort_entries(
    entries: Sequence[dict[str, Any]],
    *,
    beta: float = DEFAULT_BETA,
    density_threshold: float = DEFAULT_DENSITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Perform a deterministic XYCut++ sorting of dictionary objects by absolute coordinates bbox.

    bbox Invalid objects will not participate in geometric sorting and are stably retained at the end of the result in input relative order.
    ``MIN_GAP_THRESHOLD`` uses absolute coordinate units such as PDF and point, and the caller must not normalize in advance.
    """
    sortable_entries, invalid_entries = _build_sortable_entries(entries)
    sorted_entries = _recursive_segment(
        sortable_entries,
        beta=beta,
        density_threshold=density_threshold,
    )
    return [entry.payload for entry in sorted_entries] + invalid_entries


def _build_sortable_entries(
    entries: Sequence[dict[str, Any]],
) -> tuple[list[_SortableEntry], list[dict[str, Any]]]:
    """Split bbox valid sorting objects and invalid objects that need stable tailing."""
    sortable_entries: list[_SortableEntry] = []
    invalid_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        bbox = _normalize_bbox(entry.get("bbox"))
        if bbox is None:
            invalid_entries.append(entry)
            continue
        sortable_entries.append(_SortableEntry(index=index, payload=entry, bbox=bbox))
    return sortable_entries, invalid_entries


def _normalize_bbox(
    bbox: Any,
) -> tuple[float, float, float, float] | None:
    """Convert legal quaternary bbox into finite floating point absolute coordinates."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None

    try:
        normalized = tuple(float(value) for value in bbox)
    except (TypeError, ValueError):
        return None

    if not all(math.isfinite(value) for value in normalized):
        return None

    x0, y0, x1, y1 = normalized
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _recursive_segment(
    entries: Sequence[_SortableEntry],
    *,
    beta: float,
    density_threshold: float,
) -> list[_SortableEntry]:
    """Re-identify hurdle blocks in the current local area, calculate density and recursively segment."""
    if len(entries) <= 1:
        return list(entries)

    cross_layout_entries = _identify_cross_layout_elements(entries, beta)
    cross_layout_ids = {entry.index for entry in cross_layout_entries}
    main_entries = [entry for entry in entries if entry.index not in cross_layout_ids]

    if not main_entries:
        return _sort_by_y_then_x(cross_layout_entries)

    sorted_main = _segment_main_entries(
        main_entries,
        beta=beta,
        density_threshold=density_threshold,
    )
    return _merge_cross_layout_elements(sorted_main, cross_layout_entries)


def _segment_main_entries(
    entries: Sequence[_SortableEntry],
    *,
    beta: float,
    density_threshold: float,
) -> list[_SortableEntry]:
    """Select the preferred axis according to the current area density, and recursively process the sub-areas after segmentation."""
    if len(entries) <= 1:
        return list(entries)

    density_ratio = _compute_density_ratio(entries)
    prefer_horizontal_first = density_ratio > density_threshold
    horizontal_cut = _find_best_horizontal_cut_with_projection(entries)
    vertical_cut = _find_best_vertical_cut_with_projection(entries)

    preferred_cut = horizontal_cut if prefer_horizontal_first else vertical_cut
    fallback_cut = vertical_cut if prefer_horizontal_first else horizontal_cut
    use_horizontal_cut = prefer_horizontal_first

    if preferred_cut.gap < MIN_GAP_THRESHOLD:
        if fallback_cut.gap < MIN_GAP_THRESHOLD:
            return _sort_by_y_then_x(entries)
        preferred_cut = fallback_cut
        use_horizontal_cut = not use_horizontal_cut

    if use_horizontal_cut:
        groups = _split_by_horizontal_cut(entries, preferred_cut.position)
    else:
        groups = _split_by_vertical_cut(entries, preferred_cut.position)

    if len(groups) <= 1:
        return _sort_by_y_then_x(entries)

    result: list[_SortableEntry] = []
    for group in groups:
        result.extend(
            _recursive_segment(
                group,
                beta=beta,
                density_threshold=density_threshold,
            )
        )
    return result


def _identify_cross_layout_elements(entries: Sequence[_SortableEntry], beta: float) -> list[_SortableEntry]:
    """Identify wide elements spanning multiple columns with the current region's median width."""
    if len(entries) < 3:
        return []

    median_width = median(entry.width for entry in entries)
    threshold = beta * median_width
    return [entry for entry in entries if entry.width >= threshold and _has_minimum_overlaps(entry, entries, MIN_OVERLAP_COUNT)]


def _has_minimum_overlaps(
    entry: _SortableEntry,
    entries: Sequence[_SortableEntry],
    min_count: int,
) -> bool:
    """Determine whether the element covers at least a specified number of other elements in the horizontal direction."""
    overlap_count = 0
    for other in entries:
        if other.index == entry.index:
            continue
        if _calculate_horizontal_overlap_ratio(entry, other) < OVERLAP_THRESHOLD:
            continue
        overlap_count += 1
        if overlap_count >= min_count:
            return True
    return False


def _calculate_horizontal_overlap_ratio(
    entry1: _SortableEntry,
    entry2: _SortableEntry,
) -> float:
    """Calculate the lateral coverage ratio of two elements relative to the narrower element."""
    overlap_width = max(
        0.0,
        min(entry1.right, entry2.right) - max(entry1.left, entry2.left),
    )
    smaller_width = min(entry1.width, entry2.width)
    if overlap_width <= 0 or smaller_width <= 0:
        return 0.0
    return overlap_width / smaller_width


def _compute_density_ratio(entries: Sequence[_SortableEntry]) -> float:
    """Calculate the ratio of the sum of element areas in the current area to the area of the area."""
    region = _calculate_bounding_region(entries)
    if region is None:
        return 1.0

    region_area = (region[2] - region[0]) * (region[3] - region[1])
    if region_area <= 0:
        return 1.0

    content_area = sum(entry.area for entry in entries)
    return min(1.0, content_area / region_area)


def _calculate_bounding_region(
    entries: Sequence[_SortableEntry],
) -> tuple[float, float, float, float] | None:
    """Calculate the minimum enclosing rectangle of the current element set."""
    if not entries:
        return None

    left = min(entry.left for entry in entries)
    top = min(entry.top for entry in entries)
    right = max(entry.right for entry in entries)
    bottom = max(entry.bottom for entry in entries)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _find_best_vertical_cut_with_projection(
    entries: Sequence[_SortableEntry],
) -> _CutInfo:
    """Look for maximum projected white space vertically and try again ignoring narrow elements if necessary."""
    if len(entries) < 2:
        return _CutInfo(0.0, 0.0)

    edge_cut = _find_vertical_cut_by_edges(entries)
    if edge_cut.gap >= MIN_GAP_THRESHOLD or len(entries) < 3:
        return edge_cut

    region = _calculate_bounding_region(entries)
    if region is None:
        return edge_cut

    narrow_threshold = (region[2] - region[0]) * NARROW_ELEMENT_WIDTH_RATIO
    filtered_entries = [entry for entry in entries if entry.width >= narrow_threshold]
    if len(filtered_entries) < 2 or len(filtered_entries) == len(entries):
        return edge_cut

    filtered_cut = _find_vertical_cut_by_edges(filtered_entries)
    if filtered_cut.gap > edge_cut.gap and filtered_cut.gap >= MIN_GAP_THRESHOLD:
        return filtered_cut
    return edge_cut


def _find_vertical_cut_by_edges(
    entries: Sequence[_SortableEntry],
) -> _CutInfo:
    """Find the largest vertical segmentation blank according to the horizontal interval projection."""
    sorted_entries = sorted(
        entries,
        key=lambda entry: (entry.left, entry.right, entry.index),
    )
    largest_gap = 0.0
    cut_position = 0.0
    previous_right: float | None = None

    for entry in sorted_entries:
        if previous_right is not None and entry.left > previous_right:
            gap = entry.left - previous_right
            if gap > largest_gap:
                largest_gap = gap
                cut_position = (previous_right + entry.left) / 2.0
        previous_right = entry.right if previous_right is None else max(previous_right, entry.right)
    return _CutInfo(cut_position, largest_gap)


def _find_best_horizontal_cut_with_projection(
    entries: Sequence[_SortableEntry],
) -> _CutInfo:
    """Find the largest horizontal segmentation blank according to the longitudinal interval projection."""
    if len(entries) < 2:
        return _CutInfo(0.0, 0.0)

    sorted_entries = sorted(
        entries,
        key=lambda entry: (entry.top, entry.bottom, entry.index),
    )
    largest_gap = 0.0
    cut_position = 0.0
    previous_bottom: float | None = None

    for entry in sorted_entries:
        if previous_bottom is not None and entry.top > previous_bottom:
            gap = entry.top - previous_bottom
            if gap > largest_gap:
                largest_gap = gap
                cut_position = (previous_bottom + entry.top) / 2.0
        previous_bottom = entry.bottom if previous_bottom is None else max(previous_bottom, entry.bottom)
    return _CutInfo(cut_position, largest_gap)


def _split_by_horizontal_cut(
    entries: Sequence[_SortableEntry],
    cut_y: float,
) -> list[list[_SortableEntry]]:
    """Divide the element into upper and lower areas according to transverse tangent lines."""
    above = [entry for entry in entries if entry.center_y < cut_y]
    below = [entry for entry in entries if entry.center_y >= cut_y]
    return [group for group in (above, below) if group]


def _split_by_vertical_cut(
    entries: Sequence[_SortableEntry],
    cut_x: float,
) -> list[list[_SortableEntry]]:
    """Divide the element into two regions, left and right, according to longitudinal tangent lines."""
    left = [entry for entry in entries if entry.center_x < cut_x]
    right = [entry for entry in entries if entry.center_x >= cut_x]
    return [group for group in (left, right) if group]


def _merge_cross_layout_elements(
    sorted_main: Sequence[_SortableEntry],
    cross_layout_entries: Sequence[_SortableEntry],
) -> list[_SortableEntry]:
    """Insert hurdle elements according to geometric longitudinal division to avoid y coordinate reset in the column."""
    if not cross_layout_entries:
        return list(sorted_main)
    if not sorted_main:
        return _sort_by_y_then_x(cross_layout_entries)

    result: list[_SortableEntry] = []
    remaining_main = list(sorted_main)
    for cross_entry in _sort_by_y_then_x(cross_layout_entries):
        geometrically_above = [entry for entry in remaining_main if entry.bottom <= cross_entry.top]
        result.extend(geometrically_above)
        above_ids = {entry.index for entry in geometrically_above}
        remaining_main = [entry for entry in remaining_main if entry.index not in above_ids]
        result.append(cross_entry)

    result.extend(remaining_main)
    return result


def _sort_by_y_then_x(
    entries: Sequence[_SortableEntry],
) -> list[_SortableEntry]:
    """When the projection segmentation cannot be continued, the sorting is stable from top to bottom and from left to right."""
    return sorted(
        entries,
        key=lambda entry: (entry.top, entry.left, entry.index),
    )


__all__ = [
    "DEFAULT_BETA",
    "DEFAULT_DENSITY_THRESHOLD",
    "MIN_GAP_THRESHOLD",
    "MIN_OVERLAP_COUNT",
    "NARROW_ELEMENT_WIDTH_RATIO",
    "OVERLAP_THRESHOLD",
    "sort_entries",
]
