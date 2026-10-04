"""Cross-page table HTML content, row and column structure, and cell semantic merging."""

from __future__ import annotations

from copy import deepcopy

from bs4 import Tag

from ...schema import BlockType

from .blocks import _build_post_body_child_index, _build_table_state, _table_children
from .html import (
    _colspan,
    _refresh_table_state_metrics,
    _rowspan,
    _scan_rows,
    _serialize_table_state_html,
    build_visual_col_mapping,
    calculate_row_columns,
    calculate_visual_columns,
)
from .models import BlockDict, TableMergeState
from .structure import _expand_header_count_by_rowspan, can_merge_tables, check_row_columns_match, detect_table_headers


def adjust_table_rows_colspan(
    rows: list[Tag],
    start_idx: int,
    end_idx: int,
    row_effective_cols: list[int],
    reference_structure: list[int],
    reference_visual_cols: int,
    target_cols: int,
    match_reference_row: Tag,
) -> None:
    """Adjust the table row's colspan attribute to match the target number of columns."""
    reference_row_copy = deepcopy(match_reference_row)

    for row_idx in range(start_idx, end_idx):
        row = rows[row_idx]
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        current_row_effective_cols = row_effective_cols[row_idx]
        current_row_cols = calculate_row_columns(row)

        if current_row_effective_cols >= target_cols or current_row_cols >= target_cols:
            continue

        if calculate_visual_columns(row) == reference_visual_cols and check_row_columns_match(row, reference_row_copy):
            if len(cells) <= len(reference_structure):
                for cell_idx, cell in enumerate(cells):
                    if cell_idx < len(reference_structure) and reference_structure[cell_idx] > 1:
                        cell["colspan"] = str(reference_structure[cell_idx])
        else:
            cols_diff = target_cols - current_row_effective_cols
            if cols_diff > 0:
                last_cell = cells[-1]
                current_last_span = _colspan(last_cell)
                last_cell["colspan"] = str(current_last_span + cols_diff)


def _cell_has_semantic_content(cell: Tag) -> bool:
    """Determine whether the cell still contains semantic content visible to the user."""
    if cell.get_text(strip=True):
        return True

    return cell.find(["img", "svg", "math", "eq", "table", "figure", "object", "embed", "canvas"]) is not None


def _row_has_semantic_content(row: Tag) -> bool:
    """Determine whether the entire line still retains unmerged semantic content."""
    return any(_cell_has_semantic_content(cell) for cell in row.find_all(["td", "th"]))


def _insert_cell_before_visual_column(rows: list[Tag], target_row_index: int, start_vcol: int, cell: Tag) -> None:
    """Inserts the cell into the target row before the corresponding visual column."""
    target_row = rows[target_row_index]
    target_cells = target_row.find_all(["td", "th"])
    target_vcol_map = build_visual_col_mapping(rows, target_row_index)

    for idx, target_start_vcol in enumerate(target_vcol_map):
        if target_start_vcol >= start_vcol:
            target_cells[idx].insert_before(cell)
            return

    target_row.append(cell)


def _carry_rowspan_structure_to_next_row(rows: list[Tag], row_idx: int) -> None:
    """Sink the blank structure placeholder cells to avoid destroying the alignment of subsequent columns after deleting the current row."""
    next_row_idx = row_idx + 1
    if next_row_idx >= len(rows):
        return

    current_row = rows[row_idx]
    current_cells = current_row.find_all(["td", "th"])
    current_vcol_map = build_visual_col_mapping(rows, row_idx)
    carried_cells = []

    for cell, start_vcol in zip(current_cells, current_vcol_map):
        rowspan = _rowspan(cell)
        if rowspan <= 1 or _cell_has_semantic_content(cell):
            continue

        carried_cell = deepcopy(cell)
        new_rowspan = rowspan - 1
        if new_rowspan > 1:
            carried_cell["rowspan"] = str(new_rowspan)
        else:
            carried_cell.attrs.pop("rowspan", None)
        carried_cells.append((start_vcol, carried_cell))

    for start_vcol, carried_cell in sorted(carried_cells, key=lambda item: item[0], reverse=True):
        _insert_cell_before_visual_column(rows, next_row_idx, start_vcol, carried_cell)


def _clip_overlapped_blank_rowspan_cells(
    rows: list[Tag],
    initial_occupied: dict[int, set[int]],
) -> bool:
    """Crop the blank structure occupancy of the current page that is covered by the previous page rowspan.

    In a cross-page table, the unfinished rowspan on the previous page will be occupied by initial_occupied
    The visual column at the beginning of the current page. If table recognition on the current page generates a blank space at the same position
    rowspan cell, this cell is just a structure placeholder; direct splicing will result in the same visual column
    As two columns. Here, only blank placeholders without semantic content are cropped, and cells with real content are not processed.
    """
    if not rows or not initial_occupied:
        return False

    cells_to_remove = []
    cells_to_move = []

    for row_idx, row in enumerate(rows):
        cells = row.find_all(["td", "th"])
        visual_col_map = build_visual_col_mapping(rows, row_idx)
        for cell, start_vcol in zip(cells, visual_col_map):
            rowspan = _rowspan(cell)
            if rowspan <= 1 or _cell_has_semantic_content(cell):
                continue

            colspan = _colspan(cell)
            occupied_cols = set(range(start_vcol, start_vcol + colspan))
            if not occupied_cols:
                continue

            overlap_rows = 0
            while overlap_rows < rowspan:
                covered_cols = initial_occupied.get(row_idx + overlap_rows, set())
                if not occupied_cols.issubset(covered_cols):
                    break
                overlap_rows += 1

            if overlap_rows == 0:
                continue

            remaining_rowspan = rowspan - overlap_rows
            target_row_idx = row_idx + overlap_rows
            if remaining_rowspan > 0 and target_row_idx >= len(rows):
                continue

            cells_to_remove.append(cell)
            if remaining_rowspan > 0:
                moved_cell = deepcopy(cell)
                if remaining_rowspan > 1:
                    moved_cell["rowspan"] = str(remaining_rowspan)
                else:
                    moved_cell.attrs.pop("rowspan", None)
                cells_to_move.append((target_row_idx, start_vcol, moved_cell))

    if not cells_to_remove:
        return False

    for cell in cells_to_remove:
        cell.extract()

    for target_row_idx, start_vcol, moved_cell in sorted(
        cells_to_move,
        key=lambda item: (item[0], item[1]),
        reverse=True,
    ):
        _insert_cell_before_visual_column(rows, target_row_idx, start_vcol, moved_cell)

    return True


def _apply_cell_merge(
    previous_state: TableMergeState,
    current_state: TableMergeState,
    header_count: int,
) -> bool:
    """Apply cell_merge semantic merging.

    When the value in cell_merge is 1, the contents of the cell corresponding to the first data row in the following table
    Append to the corresponding cell in the last row of the table above. Delete the data row when all are 1,
    Clears the contents of merged cells but retains rows when blending.

    cell_merge align by visual column index, build visual column map to match correctly
    Two tables may have different numbers of rows due to rowspan <td> elements.
    Metadata is read from the root block of the current page table, HTML and the column structure is still provided by the only table body.
    """
    current_table_block = current_state.owner_block
    if not isinstance(current_table_block, dict):
        return False

    cell_merge = current_table_block.get("cell_merge")
    if not isinstance(cell_merge, list) or not cell_merge:
        return False

    rows2 = current_state.rows
    if header_count >= len(rows2):
        return False
    if not previous_state.rows:
        return False

    first_data_row = rows2[header_count]
    last_row = previous_state.rows[-1]

    cells1 = last_row.find_all(["td", "th"])
    cells2 = first_data_row.find_all(["td", "th"])

    # Construct a visual column to cell index mapping
    last_row_idx = len(previous_state.rows) - 1
    vcol_map1 = build_visual_col_mapping(previous_state.rows, last_row_idx)
    current_merge_rows = rows2[header_count:]
    vcol_map2 = build_visual_col_mapping(
        current_merge_rows,
        0,
        initial_occupied=previous_state.tail_occupied,
    )

    # Build a reverse mapping of visual columns -> cell indexes (expand colspan)
    vcol_to_cell1: dict[int, int] = {}
    for ci, start_vcol in enumerate(vcol_map1):
        colspan = int(cells1[ci].get("colspan", 1))
        for c in range(start_vcol, start_vcol + colspan):
            vcol_to_cell1[c] = ci
    vcol_to_cell2: dict[int, int] = {}
    for ci, start_vcol in enumerate(vcol_map2):
        colspan = int(cells2[ci].get("colspan", 1))
        for c in range(start_vcol, start_vcol + colspan):
            vcol_to_cell2[c] = ci

    # Execute a transfer by unique (src_cell_idx, dst_cell_idx) pair to avoid repeated processing of colspan
    transferred_pairs: set[tuple[int, int]] = set()
    for vi, merge_flag in enumerate(cell_merge):
        if merge_flag == 1:
            ci1 = vcol_to_cell1.get(vi)
            ci2 = vcol_to_cell2.get(vi)
            if ci1 is not None and ci2 is not None:
                pair = (ci1, ci2)
                if pair not in transferred_pairs:
                    for child in list(cells2[ci2].children):
                        cells1[ci1].append(child.extract())
                    transferred_pairs.add(pair)

    # Only clear source cells that have been successfully transferred
    cleared_ci2: set[int] = set()
    for vi, merge_flag in enumerate(cell_merge):
        if merge_flag == 1:
            ci1 = vcol_to_cell1.get(vi)
            ci2 = vcol_to_cell2.get(vi)
            if ci1 is not None and ci2 is not None and ci2 not in cleared_ci2:
                cells2[ci2].clear()
                cleared_ci2.add(ci2)

    if not _row_has_semantic_content(first_data_row):
        _carry_rowspan_structure_to_next_row(rows2, header_count)
        first_data_row.extract()
        if first_data_row in rows2:
            rows2.remove(first_data_row)

    return bool(transferred_pairs)


def _perform_table_content_merge(
    previous_state: TableMergeState,
    current_state: TableMergeState,
    previous_table_block: BlockDict,
    current_table_block: BlockDict,
) -> bool:
    """Perform a merge of the contents of HTML, cells, and footnote on the two cloned tables."""
    header_count, _, _ = detect_table_headers(previous_state, current_state)
    header_count = _expand_header_count_by_rowspan(current_state.rows, header_count)

    rows1 = previous_state.rows
    rows2 = current_state.rows
    if not rows1 or header_count >= len(rows2):
        return False

    previous_adjusted = False

    if header_count < len(rows2):
        current_merge_rows = rows2[header_count:]
        if _clip_overlapped_blank_rowspan_cells(current_merge_rows, previous_state.tail_occupied):
            _refresh_table_state_metrics(current_state)

    if rows1 and rows2 and header_count < len(rows2):
        last_row1 = rows1[-1]
        first_data_row2 = rows2[header_count]
        table_cols1 = previous_state.total_cols
        table_cols2 = current_state.total_cols

        if table_cols1 > table_cols2:
            reference_structure = [int(cell.get("colspan", 1)) for cell in last_row1.find_all(["td", "th"])]
            reference_visual_cols = calculate_visual_columns(last_row1)
            adjust_table_rows_colspan(
                rows2,
                header_count,
                len(rows2),
                current_state.row_effective_cols,
                reference_structure,
                reference_visual_cols,
                table_cols1,
                first_data_row2,
            )
        elif table_cols2 > table_cols1:
            reference_structure = [int(cell.get("colspan", 1)) for cell in first_data_row2.find_all(["td", "th"])]
            reference_visual_cols = calculate_visual_columns(first_data_row2)
            adjust_table_rows_colspan(
                rows1,
                0,
                len(rows1),
                previous_state.row_effective_cols,
                reference_structure,
                reference_visual_cols,
                table_cols2,
                last_row1,
            )
            previous_adjusted = True

    if previous_adjusted:
        _refresh_table_state_metrics(previous_state)

    cell_merge_applied = _apply_cell_merge(previous_state, current_state, header_count)

    appended_rows = rows2[header_count:]
    append_start_idx = len(previous_state.rows)
    merged_rows = []

    if previous_state.tbody is None or current_state.tbody is None:
        return False

    for row in appended_rows:
        row.extract()
        previous_state.tbody.append(row)
        merged_rows.append(row)

    if not merged_rows and not cell_merge_applied:
        return False

    previous_state.rows.extend(merged_rows)

    if merged_rows:
        appended_scan = _scan_rows(
            merged_rows,
            initial_occupied=previous_state.tail_occupied,
            start_row_idx=append_start_idx,
        )
        previous_state.row_effective_cols.extend(appended_scan.row_effective_cols)
        previous_state.total_cols = max(previous_state.total_cols, appended_scan.total_cols)
        if appended_scan.last_nonempty_row_metrics is not None:
            previous_state.last_data_row_metrics = appended_scan.last_nonempty_row_metrics
        previous_state.tail_occupied = appended_scan.tail_occupied

    previous_content = previous_table_block.get("content")
    if not isinstance(previous_content, list):
        return False

    previous_table_block["content"] = [
        block for block in previous_content if not isinstance(block, dict) or block.get("type") != BlockType.TABLE_FOOTNOTE
    ]
    current_footnotes = [
        block for block in _table_children(current_table_block) if block.get("type") == BlockType.TABLE_FOOTNOTE
    ]
    footnote_base_index = _build_post_body_child_index(previous_table_block, 0)
    for footnote_offset, table_footnote in enumerate(current_footnotes, start=1):
        temp_table_footnote = deepcopy(table_footnote)
        temp_table_footnote.pop("_cross_page", None)
        if footnote_base_index is None:
            temp_table_footnote["index"] = 0
        else:
            temp_table_footnote["index"] = footnote_base_index + footnote_offset
        previous_table_block["content"].append(temp_table_footnote)

    previous_state.dirty = True
    return _serialize_table_state_html(previous_state)


def merge_table_content(previous_table: BlockDict, current_table: BlockDict) -> BlockDict | None:
    """Purely functional method to merge the contents of two cross-page tables, returning ``None`` when failed.

    Both inputs will be deeply copied first; the returned block retains the outer information of the previous table and only changes the cloned table body HTML
    And replaces the previous table footnote with the current table footnote, without modifying any input objects.
    """
    if (
        not isinstance(previous_table, dict)
        or not isinstance(current_table, dict)
        or previous_table.get("type") != BlockType.TABLE
        or current_table.get("type") != BlockType.TABLE
    ):
        return None

    previous_clone = deepcopy(previous_table)
    current_clone = deepcopy(current_table)
    try:
        previous_state = _build_table_state(previous_clone)
        current_state = _build_table_state(current_clone)
        if previous_state is None or current_state is None:
            return None
        if not can_merge_tables(current_state, previous_state):
            return None
        if not _perform_table_content_merge(
            previous_state,
            current_state,
            previous_clone,
            current_clone,
        ):
            return None
    except (AssertionError, TypeError, ValueError):
        return None

    return previous_clone
