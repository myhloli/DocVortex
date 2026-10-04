"""Bounded HTML table placeholder grid parsing common to multiple formats renderer."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias

from bs4 import BeautifulSoup, Tag

MAX_NESTED_TABLE_DEPTH = 4
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 100
MAX_TABLE_SLOTS = 10_000
_POSITIVE_INTEGER_RE = re.compile(r"[0-9]+")

HtmlTableSource: TypeAlias = str | BeautifulSoup | Tag


class HtmlTableError(ValueError):
    """Means HTML table cannot be parsed safely to a strictly rectangular grid."""


@dataclass(frozen=True, slots=True)
class HtmlTableCell:
    """Saves the position of the original HTML cell in the logical placeholder grid."""

    tag: Tag
    row: int
    column: int
    rowspan: int
    colspan: int
    is_header: bool

    @property
    def end_row(self) -> int:
        """Returns the last row index occupied by the cell."""
        return self.row + self.rowspan - 1

    @property
    def end_column(self) -> int:
        """Returns the last column index occupied by the cell."""
        return self.column + self.colspan - 1


@dataclass(frozen=True, slots=True)
class HtmlTableGrid:
    """Saves a HTML table grid with overlap, bounds, scale and rectangle validation."""

    tag: Tag
    row_count: int
    column_count: int
    cells: tuple[HtmlTableCell, ...]
    header_rows: tuple[int, ...]


def parse_html_tables(source: HtmlTableSource) -> tuple[HtmlTableGrid, ...]:
    """Resolve one or more top-level tables in a source relative to the current context."""
    root = BeautifulSoup(source, "html.parser") if isinstance(source, str) else source
    if not isinstance(root, (BeautifulSoup, Tag)):
        raise HtmlTableError("HTML table source must be a string or BeautifulSoup Tag")
    if isinstance(root, Tag) and root.name == "table":
        table_tags = (root,)
    else:
        parent_table = root.find_parent("table") if isinstance(root, Tag) else None
        table_tags = tuple(table for table in root.find_all("table") if table.find_parent("table") is parent_table)
    if not table_tags:
        raise HtmlTableError("HTML does not contain a top-level table")
    return tuple(_parse_html_table(table) for table in table_tags)


def _parse_html_table(table: Tag) -> HtmlTableGrid:
    """Parse a single table tag into a strictly rectangular grid of placeholders."""
    if table.name != "table":
        raise HtmlTableError("Expected a <table> tag")
    rows = tuple(row for row in table.find_all("tr") if row.find_parent("table") is table)
    if not rows or len(rows) > MAX_TABLE_ROWS:
        raise HtmlTableError(f"Table row count must be between 1 and {MAX_TABLE_ROWS}")

    occupied: dict[tuple[int, int], HtmlTableCell] = {}
    cells: list[HtmlTableCell] = []
    for row_index, row in enumerate(rows):
        column_index = 0
        for source_cell in row.find_all(("td", "th"), recursive=False):
            while (row_index, column_index) in occupied:
                column_index += 1
            rowspan = _parse_span(source_cell, "rowspan")
            colspan = _parse_span(source_cell, "colspan")
            if row_index + rowspan > len(rows):
                raise HtmlTableError(f"rowspan exceeds table bounds at row={row_index}, column={column_index}")
            if column_index + colspan > MAX_TABLE_COLUMNS:
                raise HtmlTableError(f"Table column count exceeds {MAX_TABLE_COLUMNS}")
            coordinates = tuple(
                (target_row, target_column)
                for target_row in range(row_index, row_index + rowspan)
                for target_column in range(column_index, column_index + colspan)
            )
            overlap = next((coordinate for coordinate in coordinates if coordinate in occupied), None)
            if overlap is not None:
                raise HtmlTableError(f"Cell span overlaps row={overlap[0]}, column={overlap[1]}")
            placement = HtmlTableCell(
                tag=source_cell,
                row=row_index,
                column=column_index,
                rowspan=rowspan,
                colspan=colspan,
                is_header=source_cell.name == "th",
            )
            cells.append(placement)
            occupied.update(dict.fromkeys(coordinates, placement))
            if len(occupied) > MAX_TABLE_SLOTS:
                raise HtmlTableError(f"Table occupancy exceeds {MAX_TABLE_SLOTS} slots")
            column_index += colspan

    if not occupied:
        raise HtmlTableError("Table must contain at least one cell")
    column_count = max(column for _, column in occupied) + 1
    missing = next(
        (
            (row_index, column_index)
            for row_index in range(len(rows))
            for column_index in range(column_count)
            if (row_index, column_index) not in occupied
        ),
        None,
    )
    if missing is not None:
        raise HtmlTableError(f"Table occupancy is not rectangular at row={missing[0]}, column={missing[1]}")
    header_rows = tuple(
        row_index
        for row_index, row in enumerate(rows)
        if _row_belongs_to_thead(row, table)
        or all(occupied[(row_index, column_index)].is_header for column_index in range(column_count))
    )
    return HtmlTableGrid(
        tag=table,
        row_count=len(rows),
        column_count=column_count,
        cells=tuple(cells),
        header_rows=header_rows,
    )


def _parse_span(cell: Tag, attribute: str) -> int:
    """Reads a strictly positive integer rowspan/colspan, returning one if missing."""
    raw_value = cell.get(attribute, "1")
    if isinstance(raw_value, list):
        raise HtmlTableError(f"Invalid {attribute}: {raw_value!r}")
    value = str(raw_value).strip()
    if _POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise HtmlTableError(f"Invalid {attribute}: {raw_value!r}")
    span = int(value)
    if span < 1 or span > MAX_TABLE_SLOTS:
        raise HtmlTableError(f"Invalid {attribute}: {raw_value!r}")
    return span


def _row_belongs_to_thead(row: Tag, table: Tag) -> bool:
    """Determine whether tr is located within thead of the current table."""
    parent = row.parent
    while isinstance(parent, Tag) and parent is not table:
        if parent.name == "thead":
            return True
        parent = parent.parent
    return False


__all__ = [
    "HtmlTableCell",
    "HtmlTableError",
    "HtmlTableGrid",
    "HtmlTableSource",
    "MAX_NESTED_TABLE_DEPTH",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "MAX_TABLE_SLOTS",
    "parse_html_tables",
]
