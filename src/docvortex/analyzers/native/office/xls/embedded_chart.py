"""Restore HTML data tables from Excel.Chart or MSGraph.Chart OLE objects."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from loguru import logger

from ..legacy.ole import BoundedOleReader
from ..legacy.binary import get_f64, get_u16
from ..errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from ..limits import MAX_GRID_SLOTS
from .models import XlsCell, XlsRichText, XlsSheet, XlsWorkbook
from .number_format import builtin_number_format, format_number
from .parser import parse_xls_workbook
from .records import BOF, EOF, iter_records, record_at
from .strings import clean_text
from .xls_converter import render_xls_chart_html

GRAPH_VERSION = 0x0680
GRAPH_WORKBOOK_SUBSTREAM = 0x0005
GRAPH_CHART_SUBSTREAM = 0x8000
GRAPH_BLANK = 0x0001
GRAPH_NUMBER = 0x0003
GRAPH_LABEL = 0x0204
GRAPH_DATE1904 = 0x0022
GRAPH_FORMAT = 0x041E
GRAPH_BOF_DATASHEET = 0x1052
GRAPH_EXCLUDE_ROWS = 0x1053
GRAPH_EXCLUDE_COLUMNS = 0x1054
GRAPH_MAX_ROWS = 4_000
GRAPH_MAX_COLS = 256


@dataclass(frozen=True, slots=True)
class EmbeddedChartData:
    """Embedded chart Resolved workbook and unique table selection."""

    workbook: XlsWorkbook
    sheet_name: str
    rows: tuple[int, ...]
    cols: tuple[int, ...]
    source_kind: str


def _nonempty_selection(sheet: XlsSheet) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """Returns a stable set of rows and columns covered by non-empty cells in the worksheet."""

    if not sheet.cells:
        return None
    rows = tuple(sorted({row for row, _col in sheet.cells}))
    cols = tuple(sorted({col for _row, col in sheet.cells}))
    if len(rows) * len(cols) > MAX_GRID_SLOTS:
        raise LegacyOfficeResourceLimitError(f"workbook extent exceeds max_grid_slots={MAX_GRID_SLOTS}")
    return rows, cols


def _excel_chart_data(workbook_stream: bytes) -> EmbeddedChartData | None:
    """Resolve Excel.Chart activity independent chart sheet in workbook."""

    workbook = parse_xls_workbook(workbook_stream)
    visible_charts = [chart for chart in workbook.chart_sheets if chart.visible]
    selected = next(
        (
            chart
            for chart in visible_charts
            if workbook.active_sheet_index is not None and chart.order == workbook.active_sheet_index
        ),
        None,
    )
    if selected is None and len(visible_charts) == 1:
        selected = visible_charts[0]
    if selected is not None and selected.source_sheet_name:
        if selected.source_rows and selected.source_cols:
            return EmbeddedChartData(
                workbook=workbook,
                sheet_name=selected.source_sheet_name,
                rows=selected.source_rows,
                cols=selected.source_cols,
                source_kind="excel",
            )

    visible_sheets = [sheet for sheet in workbook.sheets if sheet.visible and sheet.cells]
    if len(visible_sheets) != 1:
        return None
    fallback = _nonempty_selection(visible_sheets[0])
    if fallback is None:
        return None
    rows, cols = fallback
    return EmbeddedChartData(
        workbook=workbook,
        sheet_name=visible_sheets[0].name,
        rows=rows,
        cols=cols,
        source_kind="excel",
    )


def _decode_short_unicode(payload: bytes, offset: int) -> str | None:
    """Read MS-OGRAPH ShortXLUnicodeString."""

    if offset < 0 or offset + 2 > len(payload):
        return None
    count = payload[offset]
    flags = payload[offset + 1]
    if flags & 0xFE:
        return None
    width = 2 if flags & 0x01 else 1
    start = offset + 2
    end = start + count * width
    if end > len(payload):
        return None
    encoding = "utf-16le" if width == 2 else "cp1252"
    try:
        return clean_text(payload[start:end].decode(encoding, errors="strict"))
    except UnicodeDecodeError:
        return None


def _decode_unicode_min2(payload: bytes, offset: int) -> str | None:
    """Read MS-OGRAPH XLUnicodeStringMin2 with length u16."""

    count = get_u16(payload, offset)
    if count is None or offset + 3 > len(payload):
        return None
    flags = payload[offset + 2]
    if flags & 0xFE:
        return None
    width = 2 if flags & 0x01 else 1
    start = offset + 3
    end = start + int(count) * width
    if end > len(payload):
        return None
    encoding = "utf-16le" if width == 2 else "cp1252"
    try:
        return clean_text(payload[start:end].decode(encoding, errors="strict"))
    except UnicodeDecodeError:
        return None


def _graph_included_indices(
    candidates: tuple[int, ...],
    payload: bytes | None,
) -> tuple[int, ...]:
    """Filters by MS-OGRAPH alternating boundaries the rows or columns that chart actually contains."""

    if not payload:
        return candidates
    if len(payload) % 2:
        return candidates
    boundaries = [int(struct.unpack_from("<H", payload, offset)[0]) for offset in range(0, len(payload), 2)]
    if len(boundaries) % 2:
        return candidates
    included = tuple(
        value for value in candidates if any(start <= value < end for start, end in zip(boundaries[::2], boundaries[1::2]))
    )
    return included or candidates


def _graph_chart_data(workbook_stream: bytes) -> EmbeddedChartData | None:
    """Parse MS-OGRAPH chart sheet embedded datasheet."""

    records = list(iter_records(workbook_stream))
    bof_records = [record for record in records if record.record_type == BOF]
    if len(bof_records) != 2:
        return None
    first, second = bof_records
    if (
        get_u16(first.payload, 0) != GRAPH_VERSION
        or get_u16(first.payload, 2) != GRAPH_WORKBOOK_SUBSTREAM
        or get_u16(second.payload, 0) != GRAPH_VERSION
        or get_u16(second.payload, 2) != GRAPH_CHART_SUBSTREAM
    ):
        return None

    date1904 = False
    custom_formats: dict[int, str] = {}
    in_chart = False
    in_datasheet = False
    cells: dict[tuple[int, int], XlsCell] = {}
    excluded_rows: bytes | None = None
    excluded_cols: bytes | None = None
    for record in records:
        if record is second:
            in_chart = True
            continue
        if not in_chart:
            if record.record_type == GRAPH_DATE1904:
                date1904 = get_u16(record.payload, 0) == 1
            elif record.record_type == GRAPH_FORMAT:
                format_id = get_u16(record.payload, 0)
                format_code = _decode_unicode_min2(record.payload, 2)
                if format_id is not None and format_code:
                    custom_formats[int(format_id)] = format_code
            continue
        if record.record_type == EOF:
            break
        if record.record_type == GRAPH_BOF_DATASHEET:
            in_datasheet = True
            continue
        if record.record_type == GRAPH_EXCLUDE_ROWS:
            excluded_rows = record.payload
            continue
        if record.record_type == GRAPH_EXCLUDE_COLUMNS:
            excluded_cols = record.payload
            continue
        if not in_datasheet or len(record.payload) < 7:
            continue
        row = get_u16(record.payload, 0)
        col = get_u16(record.payload, 2)
        format_id = get_u16(record.payload, 5)
        if row is None or col is None or row >= GRAPH_MAX_ROWS or col >= GRAPH_MAX_COLS:
            continue
        text: str | None = None
        if record.record_type == GRAPH_LABEL:
            text = _decode_short_unicode(record.payload, 7)
        elif record.record_type == GRAPH_NUMBER:
            value = get_f64(record.payload, 7)
            if value is not None:
                format_code = custom_formats.get(int(format_id or 0)) or builtin_number_format(int(format_id or 0))
                text = format_number(value, format_code, date1904=date1904)
        elif record.record_type == GRAPH_BLANK:
            text = ""
        if text is None:
            continue
        cells[(int(row), int(col))] = XlsCell(
            row=int(row),
            col=int(col),
            value=XlsRichText(text),
        )

    sheet = XlsSheet(name="Data", visible=True, order=0, cells=cells)
    selection = _nonempty_selection(sheet)
    if selection is None:
        return None
    rows, cols = selection
    rows = _graph_included_indices(rows, excluded_rows)
    cols = _graph_included_indices(cols, excluded_cols)
    if not rows or not cols or len(rows) * len(cols) > MAX_GRID_SLOTS:
        return None
    return EmbeddedChartData(
        workbook=XlsWorkbook(sheets=[sheet]),
        sheet_name=sheet.name,
        rows=rows,
        cols=cols,
        source_kind="graph",
    )


def extract_embedded_chart_data(workbook_stream: bytes) -> EmbeddedChartData | None:
    """Select Excel BIFF or MS-OGRAPH chart parser by BOF version."""

    first = record_at(workbook_stream, 0)
    if first is None or first.record_type != BOF:
        return None
    version = get_u16(first.payload, 0)
    try:
        if version in {0x0500, 0x0600}:
            return _excel_chart_data(workbook_stream)
        if version == GRAPH_VERSION:
            return _graph_chart_data(workbook_stream)
    except LegacyOfficeResourceLimitError:
        raise
    except (LegacyOfficeMalformedError, ValueError, struct.error) as exc:
        logger.warning("LEGACY_CHART_DATA_FALLBACK: {}", exc)
    return None


def extract_embedded_chart_html(workbook_stream: bytes) -> str | None:
    """Restore a renderable HTML data table from Workbook/Book stream."""

    chart = extract_embedded_chart_data(workbook_stream)
    if chart is None:
        return None
    return render_xls_chart_html(
        chart.workbook,
        chart.sheet_name,
        chart.rows,
        chart.cols,
    )


def extract_embedded_chart_html_from_storage(storage: bytes) -> str | None:
    """Read Workbook/Book from the standalone OLE CFB object and restore the data table."""

    try:
        with BoundedOleReader(storage) as ole:
            if ole.has_stream("Workbook"):
                workbook_stream = ole.read_stream("Workbook")
            elif ole.has_stream("Book"):
                workbook_stream = ole.read_stream("Book")
            else:
                return None
    except LegacyOfficeResourceLimitError:
        raise
    except ValueError:
        return None
    return extract_embedded_chart_html(workbook_stream)
