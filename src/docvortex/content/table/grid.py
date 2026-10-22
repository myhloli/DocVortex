"""跨页表格的占位校验、列边界映射与续接标志投影。"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import Tag

from .html import _colspan, _rowspan, build_row_rendered_cell_segments
from .models import TableMergeState


@dataclass(frozen=True)
class _TableCellPlacement:
    """保存改写前单元格的完整位置，包含继承到后续行的占位来源。"""

    cell: Tag
    row_index: int
    start_col: int
    end_col: int


@dataclass(frozen=True)
class _TableGridSnapshot:
    """保存所有显式单元格及每行逻辑列对应的真实所有者。"""

    cells: list[_TableCellPlacement]
    row_owners: list[dict[int, _TableCellPlacement]]


@dataclass(frozen=True)
class _TableColumnMapping:
    """记录某张原表到累计合并网格的列边界映射，仅在渲染期间使用。"""

    source_columns: int
    target_columns: int
    boundaries: dict[int, int]


def _snapshot_table_grid(rows: list[Tag], total_columns: int) -> _TableGridSnapshot:
    """先完整扫描原坐标，拒绝非正跨度、占位冲突和非矩形表格。"""
    if total_columns <= 0 or not rows:
        raise ValueError("Table grid must contain rows and columns")

    occupied: dict[int, dict[int, _TableCellPlacement]] = {}
    placements: list[_TableCellPlacement] = []
    row_owners: list[dict[int, _TableCellPlacement]] = []
    expected_columns = set(range(total_columns))

    for row_index, row in enumerate(rows):
        occupied_row = occupied.setdefault(row_index, {})
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            if cell.find("table") is not None:
                raise ValueError("Nested table grids cannot be aligned across pages")
            while column in occupied_row:
                column += 1
            colspan = _colspan(cell)
            rowspan = _rowspan(cell)
            if colspan <= 0 or rowspan <= 0 or column + colspan > total_columns:
                raise ValueError("Invalid table cell span")
            placement = _TableCellPlacement(cell, row_index, column, column + colspan)
            for offset in range(rowspan):
                occupied_target = occupied.setdefault(row_index + offset, {})
                for covered_column in range(placement.start_col, placement.end_col):
                    if covered_column in occupied_target:
                        raise ValueError("Table cells overlap")
                    occupied_target[covered_column] = placement
            placements.append(placement)
            column = placement.end_col
        if set(occupied_row) != expected_columns:
            raise ValueError("Table rows have inconsistent logical columns")
        row_owners.append(occupied_row)

    return _TableGridSnapshot(placements, row_owners)


def _identity_column_mapping(columns: int) -> _TableColumnMapping:
    """生成无需改变网格的完整列边界映射。"""
    return _TableColumnMapping(columns, columns, dict(zip(range(columns + 1), range(columns + 1))))


def _boundary_column_mapping(
    source: TableMergeState,
    source_row_index: int,
    target: TableMergeState,
    target_row_index: int,
) -> _TableColumnMapping:
    """按完整渲染段配对建立唯一保序映射，继承的 rowspan 也参与定位。"""
    source_segments = build_row_rendered_cell_segments(source.rows, source_row_index)
    target_segments = build_row_rendered_cell_segments(target.rows, target_row_index)
    if not source_segments or len(source_segments) != len(target_segments):
        raise ValueError("Table boundary segments cannot be paired")

    boundaries = {0: 0}
    source_end = target_end = 0
    for source_segment, target_segment in zip(source_segments, target_segments):
        if source_segment.start_col != source_end or target_segment.start_col != target_end:
            raise ValueError("Table boundary segments contain gaps")
        source_end = source_segment.end_col
        target_end = target_segment.end_col
        if source_end <= source_segment.start_col or target_end <= target_segment.start_col:
            raise ValueError("Table boundary segments contain invalid spans")
        boundaries[source_end] = target_end
    if source_end != source.total_cols or target_end != target.total_cols:
        raise ValueError("Table boundary does not cover its grid")
    if source.total_cols == target.total_cols:
        if any(source_column != target_column for source_column, target_column in boundaries.items()):
            raise ValueError("Equal-width table grids have different cell boundaries")
        return _identity_column_mapping(source.total_cols)
    return _TableColumnMapping(source.total_cols, target.total_cols, boundaries)


def _align_table_columns(
    previous: TableMergeState,
    current: TableMergeState,
    header_count: int,
) -> tuple[_TableColumnMapping, _TableColumnMapping]:
    """快照两表后将较窄网格映射到较宽网格，未知内部端点不作猜测。"""
    previous_grid = _snapshot_table_grid(previous.rows, previous.total_cols)
    current_grid = _snapshot_table_grid(current.rows, current.total_cols)
    if previous.total_cols >= current.total_cols:
        previous_mapping = _identity_column_mapping(previous.total_cols)
        current_mapping = _boundary_column_mapping(current, header_count, previous, len(previous.rows) - 1)
    else:
        previous_mapping = _boundary_column_mapping(previous, len(previous.rows) - 1, current, header_count)
        current_mapping = _identity_column_mapping(current.total_cols)

    for grid, mapping in ((previous_grid, previous_mapping), (current_grid, current_mapping)):
        for placement in grid.cells:
            if placement.start_col not in mapping.boundaries or placement.end_col not in mapping.boundaries:
                raise ValueError("Table contains an unmapped internal column boundary")
        for placement in grid.cells:
            mapped_span = mapping.boundaries[placement.end_col] - mapping.boundaries[placement.start_col]
            if mapped_span != placement.end_col - placement.start_col:
                placement.cell["colspan"] = str(mapped_span)
    return previous_mapping, current_mapping


def _compose_column_mappings(
    source_mapping: _TableColumnMapping,
    target_mapping: _TableColumnMapping,
) -> _TableColumnMapping:
    """把最近原页到累计网格的映射继续投影到本次扩宽后的网格。"""
    if source_mapping.target_columns != target_mapping.source_columns:
        raise ValueError("Table grid mapping dimensions do not match")
    # 原网格的逻辑单位可能只是 colspan 内部的虚拟格线；仅保留本次已有
    # 结构证据的边界，标志投影再要求缺少内部格线的整个区间取值一致。
    return _TableColumnMapping(
        source_mapping.source_columns,
        target_mapping.target_columns,
        {
            source: target_mapping.boundaries[target]
            for source, target in source_mapping.boundaries.items()
            if target in target_mapping.boundaries
        },
    )


def _project_cell_merge_flags(flags: object, mapping: _TableColumnMapping) -> list[int] | None:
    """按原前页网格投影零一标志，长度或区间语义不明确时仅放弃内容续接。"""
    if not isinstance(flags, list) or len(flags) != mapping.source_columns:
        return None
    if any(type(flag) is not int or flag not in (0, 1) for flag in flags):
        return None
    boundaries = sorted(mapping.boundaries)
    if not boundaries or boundaries[0] != 0 or boundaries[-1] != mapping.source_columns:
        return None
    projected = [0] * mapping.target_columns
    for start, end in zip(boundaries, boundaries[1:]):
        interval_flags = flags[start:end]
        if not interval_flags or any(flag != interval_flags[0] for flag in interval_flags):
            return None
        target_start = mapping.boundaries[start]
        target_end = mapping.boundaries[end]
        if not 0 <= target_start < target_end <= mapping.target_columns:
            return None
        projected[target_start:target_end] = [interval_flags[0]] * (target_end - target_start)
    return projected
