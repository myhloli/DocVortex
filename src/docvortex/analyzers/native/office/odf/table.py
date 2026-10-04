"""Construct restricted ODF table grid and serialize to secure HTML."""

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
    region_semantic_positions,
    select_best_gap_candidate,
)
from .constants import MAX_EXPANSION_TEXT_BYTES, qname
from .errors import OdfResourceLimitError
from .models import GridCell, TableGrid


CellRenderer = Callable[[etree._Element], str]
_HTML_TEXT_RE = re.compile(r"<[^>]+>")
_CELL_ADDRESS_RE = re.compile(r"\$?(?P<col>[A-Za-z]+)\$?(?P<row>[1-9][0-9]*)")


@dataclass(slots=True)
class OdfTableExpansionBudget:
    """Cumulative grid and repeated text bloat for all tables in a single ODF document."""

    used_grid_slots: int = 0
    used_duplicated_text_bytes: int = 0

    def start_table(self) -> _TableGridReservation:
        """Create a local reservation that only accumulates the area of the newly added rectangle for a table parsing."""
        return _TableGridReservation(self)

    def charge_grid_slots(self, additional_slots: int) -> None:
        """Accumulate document-level slots before expanding the table grid."""
        if additional_slots < 0 or self.used_grid_slots > MAX_GRID_SLOTS - additional_slots:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")
        self.used_grid_slots += additional_slots

    def charge_duplicated_text(self, byte_count: int) -> None:
        """Accumulate document-level bloat bytes before copying cell text."""
        if byte_count < 0 or self.used_duplicated_text_bytes > MAX_EXPANSION_TEXT_BYTES - byte_count:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_expansion_text_bytes={MAX_EXPANSION_TEXT_BYTES}")
        self.used_duplicated_text_bytes += byte_count


@dataclass(slots=True)
class _TableGridReservation:
    """Record the maximum rectangular area of the current table that has been included in the document budget."""

    budget: OdfTableExpansionBudget
    reserved_slots: int = 0

    def reserve(self, row_count: int, width: int) -> None:
        """Only current table growth is included in the document-level budget."""
        projected_slots = row_count * max(width, 1)
        if projected_slots <= self.reserved_slots:
            return
        self.budget.charge_grid_slots(projected_slots - self.reserved_slots)
        self.reserved_slots = projected_slots


def _positive_int(value: str | None, default: int = 1) -> int:
    """Constrain the ODF count attribute before conversion and roll back the corrupted value to at least one."""
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
    """Recursively output ordinary rows and header rows in the order of ODF containers."""
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
    """When there is no paragraph displayed in the cell, convert ODF typed cached value into text."""
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
    """Determine whether cell HTML contains non-empty text or picture/formula structure."""
    if any(token in value.casefold() for token in ("<img", "<eq", "<table", "<ul", "<ol")):
        return True
    return bool(html.unescape(_HTML_TEXT_RE.sub("", value)).strip())


def _validate_grid_extent(row_count: int, width: int) -> None:
    """Verify that the expected rectangle will not exceed the shared grid budget before allocating or traversing."""
    projected_width = max(width, 1)
    if row_count > MAX_GRID_SLOTS // projected_width:
        raise OdfResourceLimitError(f"ODF resource limit exceeded: max_grid_slots={MAX_GRID_SLOTS}")


def _ensure_row(
    grid: TableGrid,
    row_index: int,
    width: int = 0,
    reservation: _TableGridReservation | None = None,
) -> list[GridCell | None]:
    """Make sure the grid exists for the specified rows and minimum column widths."""
    _validate_grid_extent(max(len(grid.rows), row_index + 1), max(grid.width, width))
    if reservation is not None:
        reservation.reserve(max(len(grid.rows), row_index + 1), max(grid.width, width))
    while len(grid.rows) <= row_index:
        grid.rows.append([])
    row = grid.rows[row_index]
    if len(row) < width:
        row.extend([None] * (width - len(row)))
    return row


def _charge_grid(grid: TableGrid) -> None:
    """Check the maximum grid slot by the current rectangular bounds."""
    _validate_grid_extent(len(grid.rows), grid.width)


def parse_table_grid(
    table: etree._Element,
    render_cell: CellRenderer,
    *,
    expansion_budget: OdfTableExpansionBudget | None = None,
) -> TableGrid:
    """Expand restricted repeated rows and columns and merge cells to construct a standardized two-dimensional grid."""
    grid = TableGrid()
    reservation = expansion_budget.start_table() if expansion_budget is not None else None
    duplicated_text_bytes = 0
    row_index = 0
    pending_empty_rows = 0

    def ensure_row(target_row: int, width: int = 0) -> list[GridCell | None]:
        """Apply optional document-level reservations while being compatible with standalone table calls."""
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
    """Removes trailing empty rows and columns while retaining empty coordinates within the used range."""
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
    """The grid is clipped according to the closed interval row and column boundaries and the merged occupancies are remapped."""
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
    """Safely returns the origin cell on grid coordinates, returns None for out-of-bounds or unmaterialized locations."""
    cells = grid.rows[row] if 0 <= row < len(grid.rows) else []
    return cells[col] if 0 <= col < len(cells) else None


def split_table_regions(grid: TableGrid) -> list[TableGrid]:
    """Flood filling finds discrete data regions and selects stable segmentations by gap candidate score.

    Shared region discovery algorithm with XLS/XLSX projector: connectivity mask combines visible content, merge span and
    covered placeholders are regarded as content cells, BFS is four-way connected and spans gaps within the tolerance distance; for candidates
    tolerance After selecting the best according to the penalty index, the bounding box grid is cropped area by area.
    """
    max_row = len(grid.rows) - 1
    max_col = grid.width - 1
    if max_row < 0 or max_col < 0:
        return []

    def has_content(row: int, col: int) -> bool:
        """Determine whether the coordinate has visible content or belongs to a merged placeholder (aligned with Excel projection semantics)."""
        if (row, col) in grid.covered:
            return True
        cell = _grid_cell_at(grid, row, col)
        return cell is not None and (cell.has_content or cell.row_span > 1 or cell.col_span > 1)

    # Alignment Excel Behavior of projection starting only from coordinates with visible content only.
    starts = [
        (row, col)
        for row, cells in enumerate(grid.rows)
        for col, cell in enumerate(cells)
        if cell is not None and cell.has_content
    ]
    if not starts:
        return []

    def has_semantic_content(row: int, col: int) -> bool:
        """Determine whether the coordinate contains visible or structured HTML semantics."""
        cell = _grid_cell_at(grid, row, col)
        return cell is not None and cell.has_content

    def span_at(row: int, col: int) -> tuple[int, int]:
        """Returns the merged span of the origin cell, and returns 1x1 for non-origin or empty cells."""
        cell = _grid_cell_at(grid, row, col)
        return (cell.row_span, cell.col_span) if cell is not None else (1, 1)

    _, _, best_regions = select_best_gap_candidate(
        lambda tolerance: discover_connected_regions(has_content, starts, max_row, max_col, tolerance),
        has_semantic_content,
        span_at,
    )
    semantic_sets = [region_semantic_positions(region, has_semantic_content) for region in best_regions]
    regions: list[TableGrid] = []
    for index in keep_maximal_by_semantic_sets(semantic_sets):
        region = best_regions[index]
        cropped = crop_table_grid(grid, (region.row_start, region.row_end, region.col_start, region.col_end))
        if cropped.rows:
            regions.append(cropped)
    return regions


def _render_html_row(grid: TableGrid, row_index: int, *, header: bool) -> str:
    """Serialize a row in the grid to tr and skip merging placeholders."""
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
    """Stable serialization of canonical grids into HTML tables with thead/tbody and spans."""
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
    """Convert column letters in the A1 address to zero-based column numbers within the shared grid budget."""
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
    """Constrain the row number in the A1 address to the shared grid budget before integer conversion."""
    normalized = label.lstrip("0")
    if not normalized or len(normalized) > len(str(MAX_GRID_SLOTS)):
        return None
    result = int(normalized)
    return result - 1 if result <= MAX_GRID_SLOTS else None


def parse_cell_range_bounds(address: str) -> tuple[int, int, int, int] | None:
    """Extract zero-based closed interval boundaries from ODF cell-range-address."""
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
    """Returns the smallest bounding rectangle of multiple table ranges."""
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
