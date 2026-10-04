"""HTML Table parsing, row and column scanning and structure status caching."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup, Tag

from ...foundation._text import full_to_half
from .models import MAX_HEADER_ROWS, RenderedCellSegment, RowMetrics, RowScanResult, RowSignature, TableMergeState


def _colspan(cell: Any) -> int:
    """Read cell HTML colspan, illegal values are handed over to the upper layer for security downgrade."""
    val = cell.get("colspan", "1")
    assert isinstance(val, str)
    return int(val)


def _rowspan(cell: Any) -> int:
    """Read cell HTML rowspan, illegal values are handed over to the upper layer for security downgrade."""
    val = cell.get("rowspan", "1")
    assert isinstance(val, str)
    return int(val)


def _normalize_cell_text(cell: Tag) -> str:
    """Generate table header matching using half-width text without white space."""
    return "".join(full_to_half(cell.get_text()).split())


def _display_cell_text(cell: Tag) -> str:
    """Generates half-width display text that preserves internal whitespace."""
    return full_to_half(cell.get_text().strip())


def _scan_rows(rows: list[Tag], initial_occupied: dict[int, set[int]] | None = None, start_row_idx: int = 0) -> RowScanResult:
    """A single scan of the HTML row and cache of valid columns, explicit columns, and cross-row placeholder metrics.

    ``initial_occupied`` records future line occupancies using offsets relative to the first line, thereby preserving spanning
    rowspan structure for front and rear table boundaries.
    """
    occupied: dict[int, dict[int, bool]] = {}
    max_cols = 0

    for row_offset, cols in (initial_occupied or {}).items():
        if not cols:
            continue
        occupied[row_offset] = dict.fromkeys(cols, True)
        max_cols = max(max_cols, max(cols) + 1)

    row_effective_cols: list[int] = []
    row_metrics: list[RowMetrics] = []
    last_nonempty_row_metrics: RowMetrics | None = None

    for local_idx, row in enumerate(rows):
        occupied_row = occupied.setdefault(local_idx, {})
        col_idx = 0
        cells = row.find_all(["td", "th"])
        actual_cols = 0

        for cell in cells:
            while col_idx in occupied_row:
                col_idx += 1

            colspan = _colspan(cell)
            rowspan = _rowspan(cell)
            actual_cols += colspan

            for row_offset in range(rowspan):
                target_idx = local_idx + row_offset
                occupied_target = occupied.setdefault(target_idx, {})
                for col in range(col_idx, col_idx + colspan):
                    occupied_target[col] = True

            col_idx += colspan
            max_cols = max(max_cols, col_idx)

        effective_cols = max(occupied_row.keys()) + 1 if occupied_row else 0
        row_effective_cols.append(effective_cols)
        max_cols = max(max_cols, effective_cols)

        metrics = RowMetrics(
            row_idx=start_row_idx + local_idx,
            effective_cols=effective_cols,
            actual_cols=actual_cols,
            visual_cols=len(cells),
        )
        row_metrics.append(metrics)
        if cells:
            last_nonempty_row_metrics = metrics

    tail_occupied = {
        row_idx - len(rows): set(cols.keys()) for row_idx, cols in occupied.items() if row_idx >= len(rows) and cols
    }

    return RowScanResult(
        row_effective_cols=row_effective_cols,
        row_metrics=row_metrics,
        total_cols=max_cols,
        last_nonempty_row_metrics=last_nonempty_row_metrics,
        tail_occupied=tail_occupied,
    )


def _build_row_signature(row: Tag, effective_cols: int) -> RowSignature:
    """Build the row structure and text signature used by header detection."""
    cells = row.find_all(["td", "th"])
    return RowSignature(
        effective_cols=effective_cols,
        colspans=tuple(_colspan(cell) for cell in cells),
        rowspans=tuple(_rowspan(cell) for cell in cells),
        normalized_texts=tuple(_normalize_cell_text(cell) for cell in cells),
        display_texts=tuple(_display_cell_text(cell) for cell in cells),
    )


def _build_front_cache(
    rows: list[Tag], max_header_rows: int = MAX_HEADER_ROWS
) -> tuple[list[RowSignature], dict[int, RowMetrics]]:
    """Cache the table header signature and the first batch of data row indicators."""
    front_limit = min(len(rows), max_header_rows + 1)
    front_rows = rows[:front_limit]
    front_scan = _scan_rows(front_rows)

    front_header_info = [
        _build_row_signature(front_rows[idx], front_scan.row_effective_cols[idx])
        for idx in range(min(len(front_rows), max_header_rows))
    ]
    front_first_data_row_metrics = dict(enumerate(front_scan.row_metrics))
    return front_header_info, front_first_data_row_metrics


def _refresh_table_state_metrics(state: TableMergeState) -> None:
    """HTML Table status indicator is recalculated after structural adjustment."""
    scan = _scan_rows(state.rows)
    state.row_effective_cols = scan.row_effective_cols
    state.total_cols = scan.total_cols
    state.last_data_row_metrics = scan.last_nonempty_row_metrics
    state.tail_occupied = scan.tail_occupied
    state.front_header_info, state.front_first_data_row_metrics = _build_front_cache(state.rows)


def build_table_state_from_html(
    html: str,
    max_header_rows: int = MAX_HEADER_ROWS,
) -> TableMergeState | None:
    """TableMergeState is built from the original HTML and does not rely on the DocVortex block structure.

    Called by external tools (such as mineru-vl-utils) for cross-page table structure detection.
    The returned state is used by the HTML-only structure helper and does not contain the DocVortex block owner.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody") or soup.find("table")
    rows = soup.find_all("tr")
    if tbody is None or not rows:
        return None

    try:
        scan = _scan_rows(rows)
        front_header_info, front_first_data_row_metrics = _build_front_cache(
            rows,
            max_header_rows=max_header_rows,
        )
    except (AssertionError, TypeError, ValueError):
        return None
    if scan.total_cols <= 0 or scan.last_nonempty_row_metrics is None:
        return None

    return TableMergeState(
        owner_block=None,
        body_block=None,
        soup=soup,
        tbody=tbody,
        rows=rows,
        total_cols=scan.total_cols,
        front_header_info=front_header_info,
        front_first_data_row_metrics=front_first_data_row_metrics,
        last_data_row_metrics=scan.last_nonempty_row_metrics,
        row_effective_cols=scan.row_effective_cols,
        tail_occupied=scan.tail_occupied,
    )


def _serialize_table_state_html(state: TableMergeState) -> bool:
    """Write the merged BeautifulSoup back to the clone table body, and return failure if the table body is missing."""
    if state.body_block is None:
        return False
    state.body_block["content"] = str(state.soup)
    state.dirty = False
    return True


def calculate_table_total_columns(soup: BeautifulSoup) -> int:
    """Calculate the total number of columns of the table, processing rowspan and colspan by analyzing the entire table structure."""
    rows = soup.find_all("tr")
    return _scan_rows(rows).total_cols if rows else 0


def build_table_occupied_matrix(soup: BeautifulSoup) -> dict[int, int]:
    """Constructs the occupancy matrix of the table, returning the effective number of columns for each row."""
    rows = soup.find_all("tr")
    if not rows:
        return {}

    scan = _scan_rows(rows)
    return dict(enumerate(scan.row_effective_cols))


def calculate_row_effective_columns(soup: BeautifulSoup, row_idx: int) -> int:
    """Calculate the effective number of columns for the specified row (taking into account rowspan occupation)."""
    row_effective_cols = build_table_occupied_matrix(soup)
    return row_effective_cols.get(row_idx, 0)


def calculate_row_columns(row: Tag) -> int:
    """Calculate the actual number of columns for table rows, taking into account the colspan attribute."""
    cells = row.find_all(["td", "th"])
    column_count = 0

    for cell in cells:
        colspan = _colspan(cell)
        column_count += colspan

    return column_count


def calculate_visual_columns(row: Tag) -> int:
    """Calculate the number of visual columns for table rows (actual number of td/th cells, colspan is not taken into account)."""
    cells = row.find_all(["td", "th"])
    return len(cells)


def _scan_row_visual_sources(
    rows: list[Tag],
    target_row_index: int,
    initial_occupied: dict[int, set[int]] | None = None,
) -> tuple[dict[int, tuple[int, int]], int]:
    """Scan to the target row, recording which source cell is currently occupied by each visual column.

    initial_occupied represents the rowspan placeholder continued from the previous page, and the line number is relative
    rows[0] calculation. It only participates in column positioning as a virtual source cell and does not correspond to the actual current page.
    <td>/<th> elements.
    """
    if target_row_index < 0:
        target_row_index += len(rows)
    if target_row_index < 0 or target_row_index >= len(rows):
        return {}, 0

    # occupied[row_idx][col_idx] = (source_row_idx, source_cell_idx)
    occupied: dict[int, dict[int, tuple[int, int]]] = {}
    total_cols = 0
    for row_offset, cols in (initial_occupied or {}).items():
        if not cols:
            continue
        occupied[row_offset] = {col: (-1, col) for col in cols}
        total_cols = max(total_cols, max(cols) + 1)

    for r_idx in range(target_row_index + 1):
        occupied_row = occupied.setdefault(r_idx, {})
        col_idx = 0
        cells = rows[r_idx].find_all(["td", "th"])
        for cell_idx, cell in enumerate(cells):
            while col_idx in occupied_row:
                col_idx += 1
            colspan = _colspan(cell)
            rowspan = _rowspan(cell)
            source_marker = (r_idx, cell_idx)
            for ro in range(rowspan):
                target_idx = r_idx + ro
                occ = occupied.setdefault(target_idx, {})
                for c in range(col_idx, col_idx + colspan):
                    occ[c] = source_marker
            col_idx += colspan
            total_cols = max(total_cols, col_idx)

    return occupied.get(target_row_index, {}), total_cols


def build_visual_col_mapping(
    rows: list[Tag],
    target_row_index: int,
    initial_occupied: dict[int, set[int]] | None = None,
) -> list[int]:
    """Constructs a mapping of each explicit <td>/<th> element in the target row to the visual column position.

    The mapping correctly takes into account the rowspan placeholder inherited from the preceding row.
    initial_occupied can additionally pass in the rowspan placeholder that continues from the previous page to the current slice.
    """
    if target_row_index < 0:
        target_row_index += len(rows)
    if target_row_index < 0 or target_row_index >= len(rows):
        return []

    target_occupied, _ = _scan_row_visual_sources(
        rows,
        target_row_index,
        initial_occupied=initial_occupied,
    )

    col_idx = 0
    mapping = []
    target_cells = rows[target_row_index].find_all(["td", "th"])
    for cell in target_cells:
        while col_idx in target_occupied and target_occupied[col_idx][0] < target_row_index:
            col_idx += 1
        mapping.append(col_idx)
        colspan = _colspan(cell)
        col_idx += colspan
    return mapping


def build_row_rendered_cell_segments(
    rows: list[Tag],
    target_row_index: int,
    initial_occupied: dict[int, set[int]] | None = None,
) -> list[RenderedCellSegment]:
    """Constructs rendered cell segments of the target row, preserving the visual column range covered by each segment.

    This function reuses the table row visual source scan results, and its semantics are the same as calculate_row_rendered_segments()
    To be consistent: colspan only counts as one rendering segment, and the cells continued from rowspan will also be used as
    The rendering segment of the target row is returned.
    """
    if target_row_index < 0:
        target_row_index += len(rows)
    if target_row_index < 0 or target_row_index >= len(rows):
        return []

    target_occupied, total_cols = _scan_row_visual_sources(
        rows,
        target_row_index,
        initial_occupied=initial_occupied,
    )
    if total_cols == 0:
        return []

    segments: list[RenderedCellSegment] = []
    current_marker: tuple[int, int] | None = None
    current_start_col: int | None = None
    current_text = ""

    # Consecutive visual columns from the same source cell are combined into one rendering segment.
    for col_idx in range(total_cols):
        marker = target_occupied.get(col_idx)
        if marker is None:
            if current_marker is not None and current_start_col is not None:
                segments.append(RenderedCellSegment(text=current_text, start_col=current_start_col, end_col=col_idx))
            current_marker = None
            current_start_col = None
            current_text = ""
            continue

        if marker != current_marker:
            if current_marker is not None and current_start_col is not None:
                segments.append(RenderedCellSegment(text=current_text, start_col=current_start_col, end_col=col_idx))
            current_marker = marker
            current_start_col = col_idx
            source_row_idx, source_cell_idx = marker
            current_text = ""
            if source_row_idx >= 0:
                source_cells = rows[source_row_idx].find_all(["td", "th"])
                if source_cell_idx < len(source_cells):
                    current_text = _display_cell_text(source_cells[source_cell_idx])

    if current_marker is not None and current_start_col is not None:
        segments.append(RenderedCellSegment(text=current_text, start_col=current_start_col, end_col=total_cols))

    return segments


def calculate_row_rendered_segments(rows: list[Tag], target_row_index: int) -> int:
    """Calculate the number of visual segments after rendering of the target row.

    The number of segments is counted according to "rendered cell blocks":
    - Each explicit cell in the current row counts as one section and is not expanded. colspan
    - The rowspan placeholder inherited from the previous sequence is also counted as a segment
    - Only consecutive columns and from the same source cell are considered the same segment
    """
    target_occupied, total_cols = _scan_row_visual_sources(rows, target_row_index)
    if total_cols == 0:
        return 0

    segment_count = 0
    previous_marker: tuple[int, int] | None = None

    for col_idx in range(total_cols):
        marker = target_occupied.get(col_idx)
        if marker is None:
            previous_marker = None
            continue
        if marker != previous_marker:
            segment_count += 1
            previous_marker = marker

    return segment_count
