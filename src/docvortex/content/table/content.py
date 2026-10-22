"""跨页表格 HTML 内容、行列结构和单元格语义合并。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from bs4 import Tag

from ...schema import BlockType

from .blocks import _build_post_body_child_index, _build_table_state, _table_children
from .grid import (
    _TableColumnMapping,
    _align_table_columns,
    _compose_column_mappings,
    _identity_column_mapping,
    _project_cell_merge_flags,
    _snapshot_table_grid,
)
from .html import (
    _colspan,
    _refresh_table_state_metrics,
    _rowspan,
    _serialize_table_state_html,
    build_visual_col_mapping,
)
from .models import BlockDict, TableMergeState
from .structure import _expand_header_count_by_rowspan, can_merge_tables, detect_table_headers


@dataclass(frozen=True)
class _TableContentMergeResult:
    """返回合并副本及当前原页到结果网格的映射，不向公共协议写入状态。"""

    table: BlockDict
    current_columns: _TableColumnMapping


def _cell_has_semantic_content(cell: Tag) -> bool:
    """判断单元格是否仍包含用户可见的语义内容。"""
    if cell.get_text(strip=True):
        return True

    return cell.find(["img", "svg", "math", "eq", "table", "figure", "object", "embed", "canvas"]) is not None


def _row_has_semantic_content(row: Tag) -> bool:
    """判断整行是否仍保留未并回的语义内容。"""
    return any(_cell_has_semantic_content(cell) for cell in row.find_all(["td", "th"]))


def _insert_cell_before_visual_column(rows: list[Tag], target_row_index: int, start_vcol: int, cell: Tag) -> None:
    """将单元格插入到目标行中对应视觉列之前。"""
    target_row = rows[target_row_index]
    target_cells = target_row.find_all(["td", "th"])
    target_vcol_map = build_visual_col_mapping(rows, target_row_index)

    for idx, target_start_vcol in enumerate(target_vcol_map):
        if target_start_vcol >= start_vcol:
            target_cells[idx].insert_before(cell)
            return

    target_row.append(cell)


def _carry_rowspan_structure_to_next_row(rows: list[Tag], row_idx: int) -> None:
    """下沉空白结构占位单元格，避免删除当前行后破坏后续列对齐。"""
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
    """裁剪被上页 rowspan 覆盖的当前页空白结构占位。

    跨页表格中，上一页未结束的 rowspan 会通过 initial_occupied 占住
    当前页开头的视觉列。如果当前页表格识别又生成了同位置的空白
    rowspan 单元格，这个单元格只是结构占位；直接拼接会把同一视觉列
    当成两列。这里仅裁剪无语义内容的空白占位，真实内容单元格不处理。
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
    cell_merge: list[int] | None,
) -> bool:
    """只在统一网格中按完全相同的列区间一对一转移内容，包含继承单元格。"""
    if cell_merge is None or len(cell_merge) != previous_state.total_cols:
        return False

    rows2 = current_state.rows
    if header_count >= len(rows2):
        return False
    if not previous_state.rows:
        return False

    first_data_row = rows2[header_count]
    previous_row_count = len(previous_state.rows)
    combined_grid = _snapshot_table_grid(
        [*previous_state.rows, *rows2[header_count:]],
        previous_state.total_cols,
    )
    previous_owners = combined_grid.row_owners[previous_row_count - 1]
    transferred_sources: set[int] = set()
    transferred_targets: set[int] = set()
    for source in combined_grid.cells:
        if source.row_index != previous_row_count or not _cell_has_semantic_content(source.cell):
            continue
        if any(flag != 1 for flag in cell_merge[source.start_col : source.end_col]):
            continue
        target = previous_owners.get(source.start_col)
        if target is None or (target.start_col, target.end_col) != (source.start_col, source.end_col):
            continue
        if id(source.cell) in transferred_sources or id(target.cell) in transferred_targets:
            continue
        for child in list(source.cell.children):
            target.cell.append(child.extract())
        transferred_sources.add(id(source.cell))
        transferred_targets.add(id(target.cell))

    if transferred_sources and not _row_has_semantic_content(first_data_row):
        _carry_rowspan_structure_to_next_row(rows2, header_count)
        first_data_row.extract()
        rows2.pop(header_count)

    return bool(transferred_sources)


def _perform_table_content_merge(
    previous_state: TableMergeState,
    current_state: TableMergeState,
    previous_table_block: BlockDict,
    current_table_block: BlockDict,
    previous_source_columns: _TableColumnMapping | None,
) -> _TableColumnMapping | None:
    """在两个克隆表格上执行 HTML、单元格和 footnote 的内容合并。"""
    header_count, _, _ = detect_table_headers(previous_state, current_state)
    header_count = _expand_header_count_by_rowspan(current_state.rows, header_count)

    rows1 = previous_state.rows
    rows2 = current_state.rows
    if not rows1 or header_count >= len(rows2):
        return None

    source_columns = previous_source_columns or _identity_column_mapping(previous_state.total_cols)
    previous_mapping, current_mapping = _align_table_columns(previous_state, current_state, header_count)
    source_to_merged = _compose_column_mappings(source_columns, previous_mapping)
    cell_merge = _project_cell_merge_flags(current_table_block.get("cell_merge"), source_to_merged)
    _refresh_table_state_metrics(previous_state)
    _refresh_table_state_metrics(current_state)
    target_columns = previous_mapping.target_columns
    _clip_overlapped_blank_rowspan_cells(rows2[header_count:], previous_state.tail_occupied)
    _snapshot_table_grid([*rows1, *rows2[header_count:]], target_columns)

    cell_merge_applied = _apply_cell_merge(previous_state, current_state, header_count, cell_merge)

    appended_rows = rows2[header_count:]
    merged_rows = []

    if previous_state.tbody is None or current_state.tbody is None:
        return None

    for row in appended_rows:
        row.extract()
        previous_state.tbody.append(row)
        merged_rows.append(row)

    if not merged_rows and not cell_merge_applied:
        return None

    previous_state.rows.extend(merged_rows)
    _snapshot_table_grid(previous_state.rows, target_columns)
    _refresh_table_state_metrics(previous_state)
    if previous_state.total_cols != target_columns:
        return None

    previous_content = previous_table_block.get("content")
    if not isinstance(previous_content, list):
        return None

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
    return current_mapping if _serialize_table_state_html(previous_state) else None


def merge_table_content(previous_table: BlockDict, current_table: BlockDict) -> BlockDict | None:
    """纯函数式合并两张跨页表格的内容，失败时返回 ``None``。

    两个输入都会先深拷贝；返回块保留前表外层信息，只改克隆表体 HTML
    并用当前表 footnote 替换前表 footnote，不会修改任何输入对象。
    """
    result = _merge_table_content_with_columns(previous_table, current_table)
    return result.table if result is not None else None


def _merge_table_content_with_columns(
    previous_table: BlockDict,
    current_table: BlockDict,
    previous_source_columns: _TableColumnMapping | None = None,
) -> _TableContentMergeResult | None:
    """供渲染器合并多页链，返回最近原页的网格映射且不改变公共函数签名。"""
    if (
        not isinstance(previous_table, dict)
        or not isinstance(current_table, dict)
        or previous_table.get("type") != BlockType.TABLE
        or current_table.get("type") != BlockType.TABLE
    ):
        return None

    # 合并会丢弃续表标题并替换前表脚注；有独立书签时保留原说明及跳转位置。
    for table in (previous_table, current_table):
        if any(
            child.get("type") in {BlockType.TABLE_CAPTION, BlockType.TABLE_FOOTNOTE}
            and isinstance(child.get("anchor"), str)
            and child["anchor"].strip()
            for child in _table_children(table)
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
        current_columns = _perform_table_content_merge(
            previous_state,
            current_state,
            previous_clone,
            current_clone,
            previous_source_columns,
        )
        if current_columns is None:
            return None
    except (AssertionError, TypeError, ValueError):
        return None

    return _TableContentMergeResult(previous_clone, current_columns)
