"""构造受限 ODF 表格网格并序列化为安全 HTML。"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from lxml import etree  # type: ignore[reportMissingImports]

from ..limits import MAX_GRID_SLOTS
from ..spreadsheet.region_discovery import (
    discover_connected_regions,
    keep_maximal_by_semantic_sets,
    native_region_contract_unchanged,
    region_semantic_positions,
    select_best_gap_candidate,
    select_sparse_regions,
)
from .constants import MAX_EXPANSION_TEXT_BYTES, qname
from .errors import OdfResourceLimitError
from .models import GridCell, TableGrid


CellRenderer = Callable[[etree._Element], str]
_HTML_TEXT_RE = re.compile(r"<[^>]+>")
_CELL_ADDRESS_RE = re.compile(r"\$?(?P<col>[A-Za-z]+)\$?(?P<row>[1-9][0-9]*)")


@dataclass(slots=True)
class OdfTableExpansionBudget:
    """累计单个 ODF 文档全部表格的网格与重复文本膨胀。"""

    used_grid_slots: int = 0
    used_duplicated_text_bytes: int = 0

    def start_table(self) -> _TableGridReservation:
        """为一次表格解析创建只累计新增矩形面积的局部预留。"""
        return _TableGridReservation(self)

    def charge_grid_slots(self, additional_slots: int) -> None:
        """在表格网格扩容前累计文档级槽位。"""
        if additional_slots < 0 or self.used_grid_slots > MAX_GRID_SLOTS - additional_slots:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")
        self.used_grid_slots += additional_slots

    def charge_duplicated_text(self, byte_count: int) -> None:
        """在复制单元格文本前累计文档级膨胀字节。"""
        if byte_count < 0 or self.used_duplicated_text_bytes > MAX_EXPANSION_TEXT_BYTES - byte_count:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_expansion_text_bytes={MAX_EXPANSION_TEXT_BYTES}")
        self.used_duplicated_text_bytes += byte_count


@dataclass(slots=True)
class _TableGridReservation:
    """记录当前表格已经计入文档预算的最大矩形面积。"""

    budget: OdfTableExpansionBudget
    reserved_slots: int = 0

    def reserve(self, row_count: int, width: int) -> None:
        """仅把当前表格增长部分计入文档级预算。"""
        projected_slots = row_count * max(width, 1)
        if projected_slots <= self.reserved_slots:
            return
        self.budget.charge_grid_slots(projected_slots - self.reserved_slots)
        self.reserved_slots = projected_slots


class _BuildingTableGrid(TableGrid):
    """仅在解析器独占构建期间记录单调增长的宽度，避免逐格扫描已有行。"""

    __slots__ = ("_building_width",)

    def __init__(self) -> None:
        """从空网格开始构建，最终裁剪仍返回支持任意行修改的普通网格。"""
        super().__init__()
        self._building_width = 0

    @property
    def width(self) -> int:
        """返回经统一扩容入口更新的构建期宽度。"""
        return self._building_width


def _positive_int(value: str | None, default: int = 1) -> int:
    """在转换前约束 ODF 计数属性，并把损坏值回退为至少一。"""
    if value is None:
        return default
    normalized = value.strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    if not normalized.isascii() or not normalized.isdigit():
        return default
    limit_text = str(MAX_GRID_SLOTS)
    if len(normalized) > len(limit_text):
        raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")
    significant = normalized.lstrip("0") or "0"
    if len(significant) > len(limit_text) or (len(significant) == len(limit_text) and significant > limit_text):
        raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")
    return max(1, int(significant))


def _iter_rows(container: etree._Element, *, header: bool = False) -> Iterator[tuple[etree._Element, bool]]:
    """按 ODF 容器顺序递归产出普通行和表头行。"""
    for child in container:
        if not isinstance(child.tag, str):
            continue
        if child.tag == qname("table", "table-row"):
            yield child, header
        elif child.tag == qname("table", "table-header-rows"):
            yield from _iter_rows(child, header=True)
        elif child.tag in {qname("table", "table-rows"), qname("table", "table-row-group")}:
            yield from _iter_rows(child, header=header)


def _typed_value_text(cell: etree._Element) -> str:
    """在单元格无显示段落时把 ODF typed cached value 转为文本。"""
    value_type = cell.get(qname("office", "value-type"), "")
    if value_type == "percentage":
        try:
            return f"{float(cell.get(qname('office', 'value'), '0')) * 100:g}%"
        except ValueError:
            return ""
    if value_type == "currency":
        value = cell.get(qname("office", "value"), "")
        currency = cell.get(qname("office", "currency"), "")
        return f"{value} {currency}".strip()
    if value_type == "float":
        value = cell.get(qname("office", "value"), "")
        try:
            return f"{float(value):g}"
        except ValueError:
            return value
    if value_type == "date":
        return cell.get(qname("office", "date-value"), "")
    if value_type == "time":
        return cell.get(qname("office", "time-value"), "")
    if value_type == "boolean":
        return "TRUE" if cell.get(qname("office", "boolean-value"), "false").casefold() == "true" else "FALSE"
    if value_type == "string":
        return cell.get(qname("office", "string-value"), "")
    return ""


def _has_visible_html(value: str) -> bool:
    """判断单元格 HTML 是否包含非空文本或图片/公式结构。"""
    if any(token in value.casefold() for token in ("<img", "<eq", "<table", "<ul", "<ol")):
        return True
    return bool(html.unescape(_HTML_TEXT_RE.sub("", value)).strip())


def _validate_grid_extent(row_count: int, width: int) -> None:
    """在分配或遍历前校验预计矩形不会超过共享网格预算。"""
    projected_width = max(width, 1)
    if row_count > MAX_GRID_SLOTS // projected_width:
        raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")


def _ensure_row(
    grid: TableGrid,
    row_index: int,
    width: int = 0,
    reservation: _TableGridReservation | None = None,
) -> list[GridCell | None]:
    """确保网格存在指定行和最小列宽。"""
    _validate_grid_extent(max(len(grid.rows), row_index + 1), max(grid.width, width))
    if reservation is not None:
        reservation.reserve(max(len(grid.rows), row_index + 1), max(grid.width, width))
    while len(grid.rows) <= row_index:
        grid.rows.append([])
    row = grid.rows[row_index]
    if len(row) < width:
        row.extend([None] * (width - len(row)))
        if isinstance(grid, _BuildingTableGrid):
            grid._building_width = max(grid._building_width, width)
    return row


def _charge_grid(grid: TableGrid) -> None:
    """按当前矩形边界检查最大网格槽位。"""
    _validate_grid_extent(len(grid.rows), grid.width)


def parse_table_grid(
    table: etree._Element,
    render_cell: CellRenderer,
    *,
    expansion_budget: OdfTableExpansionBudget | None = None,
) -> TableGrid:
    """展开受限重复行列与合并单元格，构造规范二维网格。"""
    grid = _BuildingTableGrid()
    reservation = expansion_budget.start_table() if expansion_budget is not None else None
    duplicated_text_bytes = 0
    row_index = 0
    pending_empty_rows = 0

    def ensure_row(target_row: int, width: int = 0) -> list[GridCell | None]:
        """在兼容独立表格调用的同时应用可选文档级预留。"""
        if reservation is None:
            return _ensure_row(grid, target_row, width)
        return _ensure_row(grid, target_row, width, reservation)

    for row_element, header in _iter_rows(table):
        row_repeat = _positive_int(row_element.get(qname("table", "number-rows-repeated")))
        cell_templates: list[tuple[bool, int, GridCell | None]] = []
        for cell in row_element:
            if not isinstance(cell.tag, str):
                continue
            if cell.tag == qname("table", "covered-table-cell"):
                cell_templates.append((True, _positive_int(cell.get(qname("table", "number-columns-repeated"))), None))
                continue
            if cell.tag != qname("table", "table-cell"):
                continue
            column_repeat = _positive_int(cell.get(qname("table", "number-columns-repeated")))
            row_span = _positive_int(cell.get(qname("table", "number-rows-spanned")))
            col_span = _positive_int(cell.get(qname("table", "number-columns-spanned")))
            _validate_grid_extent(row_span, col_span)
            cell_html = render_cell(cell)
            if not _has_visible_html(cell_html):
                typed_value = _typed_value_text(cell)
                cell_html = html.escape(typed_value) if typed_value else ""
            duplicated_bytes = len(cell_html.encode("utf-8")) * max(0, row_repeat * column_repeat - 1)
            if expansion_budget is None:
                duplicated_text_bytes += duplicated_bytes
                if duplicated_text_bytes > MAX_EXPANSION_TEXT_BYTES:
                    raise OdfResourceLimitError(
                        f"ODF resource limit exceeded: max_expansion_text_bytes={MAX_EXPANSION_TEXT_BYTES}"
                    )
            else:
                expansion_budget.charge_duplicated_text(duplicated_bytes)
            cell_templates.append(
                (
                    False,
                    column_repeat,
                    GridCell(html=cell_html, row_span=row_span, col_span=col_span, header=header),
                )
            )
        row_has_content = header or any(
            template is not None and (template.has_content or template.row_span > 1 or template.col_span > 1)
            for _, _, template in cell_templates
        )
        if not row_has_content:
            pending_empty_rows += row_repeat
            continue
        if pending_empty_rows:
            _validate_grid_extent(row_index + pending_empty_rows, grid.width)
            for _ in range(pending_empty_rows):
                ensure_row(row_index, grid.width)
                row_index += 1
            pending_empty_rows = 0
        _validate_grid_extent(row_index + row_repeat, grid.width)
        for _ in range(row_repeat):
            row = ensure_row(row_index)
            col_index = 0
            pending_empty_columns = 0
            for is_covered, repeat, template in cell_templates:
                if repeat > MAX_GRID_SLOTS:
                    raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")
                if (
                    not is_covered
                    and template is not None
                    and not template.has_content
                    and template.row_span == 1
                    and template.col_span == 1
                ):
                    pending_empty_columns += repeat
                    continue
                if pending_empty_columns:
                    col_index += pending_empty_columns
                    ensure_row(row_index, col_index)
                    pending_empty_columns = 0
                if not is_covered:
                    _validate_grid_extent(row_index + 1, max(grid.width, col_index + repeat))
                for _ in range(repeat):
                    if is_covered:
                        if (row_index, col_index) in grid.covered:
                            ensure_row(row_index, col_index + 1)
                            col_index += 1
                        continue
                    while (row_index, col_index) in grid.covered:
                        col_index += 1
                    assert template is not None
                    placed = GridCell(
                        html=template.html,
                        row_span=template.row_span,
                        col_span=template.col_span,
                        header=template.header,
                    )
                    _validate_grid_extent(
                        row_index + placed.row_span,
                        max(grid.width, col_index + placed.col_span),
                    )
                    row = ensure_row(row_index, col_index + placed.col_span)
                    row[col_index] = placed
                    for row_offset in range(placed.row_span):
                        covered_row = ensure_row(row_index + row_offset, col_index + placed.col_span)
                        for col_offset in range(placed.col_span):
                            if row_offset == 0 and col_offset == 0:
                                continue
                            grid.covered.add((row_index + row_offset, col_index + col_offset))
                            if covered_row[col_index + col_offset] is not None:
                                covered_row[col_index + col_offset] = None
                    col_index += placed.col_span
            if header:
                grid.header_rows = max(grid.header_rows, row_index + 1)
            _charge_grid(grid)
            row_index += 1
    return trim_table_grid(grid)


def trim_table_grid(grid: TableGrid) -> TableGrid:
    """移除尾部全空行列，同时保留已用范围内部的空白坐标。"""
    last_row = -1
    last_col = -1
    for row_index, row in enumerate(grid.rows):
        for col_index, cell in enumerate(row):
            if cell is not None and (cell.has_content or cell.row_span > 1 or cell.col_span > 1):
                last_row = max(last_row, row_index + cell.row_span - 1)
                last_col = max(last_col, col_index + cell.col_span - 1)
    if last_row < 0 or last_col < 0:
        return TableGrid()
    rows = []
    for row_index in range(min(last_row + 1, len(grid.rows))):
        row = list(grid.rows[row_index][: last_col + 1])
        if len(row) < last_col + 1:
            row.extend([None] * (last_col + 1 - len(row)))
        rows.append(row)
    covered = {(row, col) for row, col in grid.covered if row <= last_row and col <= last_col}
    return TableGrid(rows=rows, header_rows=min(grid.header_rows, len(rows)), covered=covered)


def crop_table_grid(grid: TableGrid, bounds: tuple[int, int, int, int]) -> TableGrid:
    """按闭区间行列边界裁剪网格并重映射合并占位。"""
    row_start, row_end, col_start, col_end = bounds
    if row_start < 0 or col_start < 0 or row_end < row_start or col_end < col_start:
        return TableGrid()
    rows: list[list[GridCell | None]] = []
    for source_row in range(row_start, min(row_end + 1, len(grid.rows))):
        row = grid.rows[source_row]
        selected = list(row[col_start : col_end + 1])
        if len(selected) < col_end - col_start + 1:
            selected.extend([None] * (col_end - col_start + 1 - len(selected)))
        rows.append(selected)
    covered = {
        (row - row_start, col - col_start)
        for row, col in grid.covered
        if row_start <= row <= row_end and col_start <= col <= col_end
    }
    header_rows = max(0, min(grid.header_rows - row_start, len(rows)))
    return trim_table_grid(TableGrid(rows=rows, header_rows=header_rows, covered=covered))


def _grid_cell_at(grid: TableGrid, row: int, col: int) -> GridCell | None:
    """安全返回网格坐标上的原点单元格，越界或未物化位置返回 None。"""
    cells = grid.rows[row] if 0 <= row < len(grid.rows) else []
    return cells[col] if 0 <= col < len(cells) else None


def split_table_regions(grid: TableGrid) -> list[TableGrid]:
    """洪水填充发现离散数据区域，并按 gap 候选评分选择稳定分割。

    与 XLS/XLSX 投影器共用区域发现算法：连通性掩码把可见内容、合并跨度和
    covered 占位都视为内容格，BFS 四向连通并在容忍距离内跨越空白；对候选
    tolerance 按惩罚指标选优后，逐区域裁剪出包围盒网格。
    """
    max_row = len(grid.rows) - 1
    max_col = grid.width - 1
    if max_row < 0 or max_col < 0:
        return []

    def has_content(row: int, col: int) -> bool:
        """判断坐标是否有可见内容或属于合并占位（对齐 Excel 投影语义）。"""
        if (row, col) in grid.covered:
            return True
        cell = _grid_cell_at(grid, row, col)
        return cell is not None and (cell.has_content or cell.row_span > 1 or cell.col_span > 1)

    # 仅从有可见内容的坐标出发，对齐 Excel 投影只以有值格为起点的行为。
    starts = [
        (row, col)
        for row, cells in enumerate(grid.rows)
        for col, cell in enumerate(cells)
        if cell is not None and cell.has_content
    ]
    if not starts:
        return []

    def has_semantic_content(row: int, col: int) -> bool:
        """判断坐标是否包含可见或结构化 HTML 语义。"""
        cell = _grid_cell_at(grid, row, col)
        return cell is not None and cell.has_content

    def span_at(row: int, col: int) -> tuple[int, int]:
        """返回原点单元格的合并跨度，非原点或空位返回 1x1。"""
        cell = _grid_cell_at(grid, row, col)
        return (cell.row_span, cell.col_span) if cell is not None else (1, 1)

    snapshot = _snapshot_grid_regions(grid, starts, max_row, max_col)
    if snapshot is not None:
        content_points, spans = snapshot
        _, _, best_regions, kept_indices = select_sparse_regions(starts, content_points, [], starts, spans, max_row, max_col)
        # 选中区域及成员已独立返回，裁剪网格前释放输入快照，避免两份大坐标集合同时驻留。
        del snapshot, starts, content_points, spans
    else:
        _, _, best_regions = select_best_gap_candidate(
            lambda tolerance: discover_connected_regions(has_content, starts, max_row, max_col, tolerance),
            has_semantic_content,
            span_at,
        )
        semantic_sets = [region_semantic_positions(region, has_semantic_content) for region in best_regions]
        kept_indices = keep_maximal_by_semantic_sets(semantic_sets)
    regions: list[TableGrid] = []
    for index in kept_indices:
        region = best_regions[index]
        cropped = crop_table_grid(grid, (region.row_start, region.row_end, region.col_start, region.col_end))
        if cropped.rows:
            regions.append(cropped)
    return regions


def _snapshot_grid_regions(
    grid: TableGrid,
    starts: list[tuple[int, int]],
    max_row: int,
    max_col: int,
) -> tuple[list[tuple[int, int]], list[tuple[int, int, int, int]]] | None:
    """把已展开网格的连接占位与原点跨度一次送入原生层，保留自定义回调语义。"""
    if not native_region_contract_unchanged() or any(
        globals()[name] is not original for name, original in _DEFAULT_GRID_REGION_HELPERS.items()
    ):
        return None
    if any(not (0 <= row <= max_row and 0 <= col <= max_col) for row, col in starts):
        return None
    if any(
        cell is not None and getattr(type(cell), "has_content", None) is not _DEFAULT_GRID_CELL_HAS_CONTENT
        for cells in grid.rows
        for cell in cells
    ):
        return None
    content_points = {(row, col) for row, col in grid.covered if 0 <= row <= max_row and 0 <= col <= max_col}
    spans = []
    for row, cells in enumerate(grid.rows):
        for col, cell in enumerate(cells):
            if cell is None:
                continue
            # 手工网格的非整数或超大跨度仍交给原回调裁剪，避免原生整数边界改变既有行为。
            if (
                type(cell.row_span) is not int
                or type(cell.col_span) is not int
                or not 1 <= cell.row_span <= 2**63 - 1
                or not 1 <= cell.col_span <= 2**63 - 1
            ):
                return None
            if not (0 <= row <= max_row and 0 <= col <= max_col):
                continue
            if cell.has_content or cell.row_span > 1 or cell.col_span > 1:
                content_points.add((row, col))
            if cell.row_span != 1 or cell.col_span != 1:
                spans.append((row, col, cell.row_span, cell.col_span))
    return list(content_points), spans


_DEFAULT_GRID_REGION_HELPERS = {
    "discover_connected_regions": discover_connected_regions,
    "select_best_gap_candidate": select_best_gap_candidate,
    "region_semantic_positions": region_semantic_positions,
    "keep_maximal_by_semantic_sets": keep_maximal_by_semantic_sets,
    "_grid_cell_at": _grid_cell_at,
}
_DEFAULT_GRID_CELL_HAS_CONTENT = GridCell.has_content


def _render_html_row(grid: TableGrid, row_index: int, *, header: bool) -> str:
    """把网格中的一行序列化为 tr，并跳过合并占位。"""
    row = grid.rows[row_index]
    tag = "th" if header else "td"
    parts = ["<tr>"]
    for col_index in range(grid.width):
        if (row_index, col_index) in grid.covered:
            continue
        cell = row[col_index] if col_index < len(row) else None
        attrs = ""
        content = ""
        if cell is not None:
            if cell.row_span > 1:
                attrs += f' rowspan="{cell.row_span}"'
            if cell.col_span > 1:
                attrs += f' colspan="{cell.col_span}"'
            content = cell.html
        parts.append(f"<{tag}{attrs}>{content}</{tag}>")
    parts.append("</tr>")
    return "".join(parts)


def table_grid_to_html(grid: TableGrid) -> str:
    """把规范网格稳定序列化为带 thead/tbody 和跨度的 HTML 表格。"""
    if not grid.rows:
        return ""
    header_rows = min(grid.header_rows, len(grid.rows))
    parts = ["<table>"]
    if header_rows:
        parts.append("<thead>")
        parts.extend(_render_html_row(grid, row_index, header=True) for row_index in range(header_rows))
        parts.append("</thead>")
    if header_rows < len(grid.rows):
        parts.append("<tbody>")
        parts.extend(_render_html_row(grid, row_index, header=False) for row_index in range(header_rows, len(grid.rows)))
        parts.append("</tbody>")
    parts.append("</table>")
    return "".join(parts)


def _column_index(label: str) -> int | None:
    """在共享网格预算内把 A1 地址中的列字母转换为零基列号。"""
    max_label_length = 0
    remaining = MAX_GRID_SLOTS
    while remaining > 0:
        max_label_length += 1
        remaining = (remaining - 1) // 26
    if not label or len(label) > max_label_length:
        return None
    result = 0
    for char in label.upper():
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1 if result <= MAX_GRID_SLOTS else None


def _row_index(label: str) -> int | None:
    """在整数转换前把 A1 地址中的行号约束到共享网格预算。"""
    normalized = label.lstrip("0")
    if not normalized or len(normalized) > len(str(MAX_GRID_SLOTS)):
        return None
    result = int(normalized)
    return result - 1 if result <= MAX_GRID_SLOTS else None


def parse_cell_range_bounds(address: str) -> tuple[int, int, int, int] | None:
    """从 ODF cell-range-address 中提取零基闭区间边界。"""
    matches = list(_CELL_ADDRESS_RE.finditer(address or ""))
    if not matches:
        return None
    first = matches[0]
    last = matches[-1]
    row_start = _row_index(first.group("row"))
    col_start = _column_index(first.group("col"))
    row_end = _row_index(last.group("row"))
    col_end = _column_index(last.group("col"))
    if row_start is None or col_start is None or row_end is None or col_end is None:
        return None
    return min(row_start, row_end), max(row_start, row_end), min(col_start, col_end), max(col_start, col_end)


def union_bounds(bounds: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    """返回多个表格范围的最小包围矩形。"""
    if not bounds:
        return None
    return (
        min(item[0] for item in bounds),
        max(item[1] for item in bounds),
        min(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


__all__ = [
    "OdfTableExpansionBudget",
    "crop_table_grid",
    "parse_cell_range_bounds",
    "parse_table_grid",
    "split_table_regions",
    "table_grid_to_html",
    "trim_table_grid",
    "union_bounds",
]
