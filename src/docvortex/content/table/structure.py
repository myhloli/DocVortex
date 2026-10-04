"""Determination of header, width and boundary row structure of cross-page tables."""

from __future__ import annotations

from typing import Any

from bs4 import Tag

from ...schema import BlockType

from .blocks import _bbox_for_calculation, _is_continuation_caption, _is_post_table_non_continuation_caption, _table_children
from .html import _colspan, _rowspan, calculate_row_rendered_segments
from .models import MAX_HEADER_ROWS, TableMergeState


def detect_table_headers(
    state1: TableMergeState, state2: TableMergeState, max_header_rows: int = MAX_HEADER_ROWS
) -> tuple[int, bool, list[list[str]]]:
    """Detect and compare the headers of two tables, scanning only the first few rows."""
    front_rows1 = state1.front_header_info[:max_header_rows]
    front_rows2 = state2.front_header_info[:max_header_rows]

    min_rows = min(len(front_rows1), len(front_rows2), max_header_rows)
    header_rows = 0
    headers_match = True
    header_texts = []

    for row_idx in range(min_rows):
        row1 = front_rows1[row_idx]
        row2 = front_rows2[row_idx]
        structure_match = (
            row1.cell_count == row2.cell_count
            and row1.effective_cols == row2.effective_cols
            and row1.colspans == row2.colspans
            and row1.rowspans == row2.rowspans
            and row1.normalized_texts == row2.normalized_texts
        )

        if structure_match:
            header_rows += 1
            header_texts.append(list(row1.display_texts))
        else:
            headers_match = header_rows > 0
            break

    if header_rows == 0:
        header_rows, headers_match, header_texts = _detect_table_headers_visual(state1, state2, max_header_rows=max_header_rows)

    return header_rows, headers_match, header_texts


def _detect_table_headers_visual(
    state1: TableMergeState,
    state2: TableMergeState,
    max_header_rows: int = MAX_HEADER_ROWS,
) -> tuple[int, bool, list[list[str]]]:
    """Detect header based on visual consistency (only compare text content, ignore colspan/rowspan differences)."""
    front_rows1 = state1.front_header_info[:max_header_rows]
    front_rows2 = state2.front_header_info[:max_header_rows]

    min_rows = min(len(front_rows1), len(front_rows2), max_header_rows)
    header_rows = 0
    headers_match = True
    header_texts = []

    for row_idx in range(min_rows):
        row1 = front_rows1[row_idx]
        row2 = front_rows2[row_idx]
        # OCR colspan/rowspan may be lost when recognizing the header. Here, the number of rendering segments is used to constrain visual consistency.
        rendered_segments1 = calculate_row_rendered_segments(state1.rows, row_idx)
        rendered_segments2 = calculate_row_rendered_segments(state2.rows, row_idx)
        if row1.normalized_texts == row2.normalized_texts and rendered_segments1 == rendered_segments2:
            header_rows += 1
            header_texts.append(list(row1.display_texts))
        else:
            headers_match = header_rows > 0
            break

    if header_rows == 0:
        headers_match = False

    return header_rows, headers_match, header_texts


def _expand_header_count_by_rowspan(rows: list[Tag], header_count: int) -> int:
    """Number of skipped rows by header rowspan coverage extension.

    The header of the first row of a cross-page continuation table may contain rowspan. If only the first matched line is skipped,
    Subsequent header rows covered by this rowspan will lose their placeholder sources and form a half header after merging.
    Therefore, when skipping repeated headers, you need to cover all rows occupied by skipped header rows.
    """
    if header_count <= 0 or not rows:
        return header_count

    expanded_header_count = min(header_count, len(rows))
    row_idx = 0
    while row_idx < expanded_header_count:
        row = rows[row_idx]
        for cell in row.find_all(["td", "th"]):
            rowspan = _rowspan(cell)
            if rowspan > 1:
                expanded_header_count = max(expanded_header_count, row_idx + rowspan)
                expanded_header_count = min(expanded_header_count, len(rows))
        row_idx += 1

    return expanded_header_count


def can_merge_by_structure(
    current_state: TableMergeState,
    previous_state: TableMergeState,
    current_bbox: Any = None,
    previous_bbox: Any = None,
) -> bool:
    """Determine whether merging is possible based on table structure only (caption/footnote is not checked).

    Called by external tools, ignoring caption and footnote checks.
    """
    if (
        current_bbox is not None
        and previous_bbox is not None
        and not _table_widths_are_compatible(
            current_bbox,
            previous_bbox,
        )
    ):
        return False

    if (
        previous_state.total_cols <= 0
        or current_state.total_cols <= 0
        or previous_state.last_data_row_metrics is None
        or current_state.last_data_row_metrics is None
    ):
        return False

    if previous_state.total_cols == current_state.total_cols:
        return True

    return check_rows_match(previous_state, current_state)


def _table_widths_are_compatible(current_bbox: Any, previous_bbox: Any) -> bool:
    """Use thousandths bbox to determine whether the relative difference in width between the two tables is less than ten percent."""
    current_calc_bbox = _bbox_for_calculation(current_bbox)
    previous_calc_bbox = _bbox_for_calculation(previous_bbox)
    if current_calc_bbox is None or previous_calc_bbox is None:
        return False

    current_width = current_calc_bbox[2] - current_calc_bbox[0]
    previous_width = previous_calc_bbox[2] - previous_calc_bbox[0]
    min_width = min(current_width, previous_width)
    return min_width > 0 and abs(current_width - previous_width) / min_width < 0.1


def can_merge_tables(current_state: TableMergeState, previous_state: TableMergeState) -> bool:
    """Determine whether it can be merged based on the auxiliary text, width and HTML structure of the dict table."""
    current_table_block = current_state.owner_block
    previous_table_block = previous_state.owner_block

    if not isinstance(previous_table_block, dict) or not isinstance(current_table_block, dict):
        return False

    previous_children = _table_children(previous_table_block)
    current_children = _table_children(current_table_block)
    footnote_count = sum(1 for block in previous_children if block.get("type") == BlockType.TABLE_FOOTNOTE)
    caption_blocks = [block for block in current_children if block.get("type") == BlockType.TABLE_CAPTION]
    merge_caption_blocks = [
        block for block in caption_blocks if not _is_post_table_non_continuation_caption(current_table_block, block)
    ]
    if merge_caption_blocks:
        has_continuation_marker = any(_is_continuation_caption(block) for block in merge_caption_blocks)

        if not has_continuation_marker:
            return False

        if footnote_count > 1:
            return False
    elif footnote_count > 0:
        return False

    if not _table_widths_are_compatible(current_table_block.get("bbox"), previous_table_block.get("bbox")):
        return False

    return can_merge_by_structure(current_state, previous_state)


def check_rows_match(previous_state: TableMergeState, current_state: TableMergeState) -> bool:
    """Check if table boundary rows match."""
    last_row_metrics = previous_state.last_data_row_metrics
    if last_row_metrics is None:
        return False

    header_count, _, _ = detect_table_headers(previous_state, current_state)
    header_count = _expand_header_count_by_rowspan(current_state.rows, header_count)
    first_data_row_metrics = current_state.front_first_data_row_metrics.get(header_count)
    if first_data_row_metrics is None:
        return False

    previous_rendered_segments = calculate_row_rendered_segments(previous_state.rows, last_row_metrics.row_idx)
    current_rendered_segments = calculate_row_rendered_segments(current_state.rows, first_data_row_metrics.row_idx)

    return (
        last_row_metrics.effective_cols == first_data_row_metrics.effective_cols
        or last_row_metrics.actual_cols == first_data_row_metrics.actual_cols
        or previous_rendered_segments == current_rendered_segments
    )


def check_row_columns_match(row1: Tag, row2: Tag) -> bool:
    """Determine whether the number of explicit cells in two rows is consistent with the colspan structure."""
    cells1 = row1.find_all(["td", "th"])
    cells2 = row2.find_all(["td", "th"])
    if len(cells1) != len(cells2):
        return False
    for cell1, cell2 in zip(cells1, cells2):
        colspan1 = _colspan(cell1)
        colspan2 = _colspan(cell2)
        if colspan1 != colspan2:
            return False
    return True
