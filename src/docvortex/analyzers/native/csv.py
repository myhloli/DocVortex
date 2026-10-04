"""Convert delimiter text CSV to DocVortex single page table model-list."""

from __future__ import annotations

import codecs
import csv as csv_module
import html
import re
from collections import Counter
from io import StringIO
from typing import Any, BinaryIO, Final, Literal, TypeAlias

from ftfy.badness import badness

from ...schema import BlockType

MAX_CSV_BYTES: Final = 200 * 1024 * 1024
MAX_CSV_ROWS: Final = 1_048_576
MAX_CSV_COLUMNS: Final = 16_384
# CSV will materialize each slot into a HTML/DOM node, and the budget needs to be significantly lower than the upper limit of the sparse spreadsheet projection.
MAX_CSV_GRID_SLOTS: Final = 250_000
MAX_CSV_RENDERED_BYTES: Final = 256 * 1024 * 1024

_DELIMITER_CANDIDATES: Final = (",", ";", "\t", "|")
_DELIMITER_SAMPLE_RECORDS: Final = 20
_HEADER_SAMPLE_ROWS: Final = 50
_HEADER_KIND_DOMINANCE_NUM: Final = 9
_HEADER_KIND_DOMINANCE_DEN: Final = 10
_MAX_HEADER_LABEL_CHARS: Final = 64
_SEP_DIRECTIVE_RE = re.compile(r"\Asep=(?P<delimiter>[,;\t|])(?:\r\n|\n|\r|$)", re.IGNORECASE)
_DISALLOWED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_DATE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?$")

CsvValueKind: TypeAlias = Literal["number", "boolean", "date", "text"]


def _text_quality_score(text: str) -> int:
    """Calculate the abnormal character score of the candidate decoded text, the lower the score, the more credible it is."""
    control_penalty = len(_DISALLOWED_CONTROL_RE.findall(text)) * 10
    return badness(text) + control_penalty


def _decode_csv_bytes(file_bytes: bytes) -> str:
    """Strictly decode CSV in the fixed order of BOM, UTF-8, GB18030, Windows-1252."""
    if file_bytes.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError("Unsupported CSV encoding: UTF-32")
    if file_bytes.startswith(codecs.BOM_UTF8):
        return file_bytes.decode("utf-8-sig", errors="strict")
    if file_bytes.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return file_bytes.decode("utf-16", errors="strict")
    try:
        return file_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass

    candidates: dict[str, str] = {}
    for encoding in ("gb18030", "cp1252"):
        try:
            candidates[encoding] = file_bytes.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    if not candidates:
        raise ValueError("Unsupported CSV encoding; expected UTF-8, UTF-16, GB18030, or Windows-1252")
    return min(
        candidates.items(),
        key=lambda item: (
            _text_quality_score(item[1]),
            0 if item[0] == "cp1252" else 1,
        ),
    )[1]


def _extract_sep_directive(text: str) -> tuple[str, str | None]:
    """Extract the sep instruction in Excel style and remove the physical row from subsequent CSV data."""
    match = _SEP_DIRECTIVE_RE.match(text)
    if match is None:
        return text, None
    return text[match.end() :], match.group("delimiter")


def _sample_record_widths(text: str, delimiter: str) -> list[int]:
    """Read a limited number of complete logical records using candidate delimiters and return the number of fields in each record."""
    reader = csv_module.reader(
        StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        skipinitialspace=False,
        strict=True,
    )
    widths: list[int] = []
    try:
        for record in reader:
            widths.append(max(1, len(record)))
            if len(widths) >= _DELIMITER_SAMPLE_RECORDS:
                break
    except csv_module.Error:
        return []
    return widths


def _sniff_delimiter(text: str) -> str:
    """Select delimiters for consistency in logical record column widths, with commas preferred in the event of a complete tie."""
    best_delimiter = ","
    best_score = (0, 0, 0)
    for preference, delimiter in enumerate(_DELIMITER_CANDIDATES):
        widths = _sample_record_widths(text, delimiter)
        if not widths:
            continue
        width_counts = Counter(widths)
        modal_width, frequency = max(width_counts.items(), key=lambda item: (item[1], item[0]))
        if modal_width < 2:
            continue
        score = (frequency, modal_width, -preference)
        if score > best_score:
            best_delimiter = delimiter
            best_score = score
    return best_delimiter


def _read_csv_rows(text: str, delimiter: str) -> list[list[str]]:
    """Strictly read all CSV records while enforcing row number, column number and grid size restrictions."""
    reader = csv_module.reader(
        StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        skipinitialspace=False,
        strict=True,
    )
    rows: list[list[str]] = []
    max_columns = 0
    try:
        for record in reader:
            row = list(record) or [""]
            next_row_count = len(rows) + 1
            if next_row_count > MAX_CSV_ROWS:
                raise ValueError(f"CSV exceeds max_rows={MAX_CSV_ROWS}")
            next_max_columns = max(max_columns, len(row))
            if next_max_columns > MAX_CSV_COLUMNS:
                raise ValueError(f"CSV exceeds max_columns={MAX_CSV_COLUMNS}")
            if next_row_count * next_max_columns > MAX_CSV_GRID_SLOTS:
                raise ValueError(f"CSV exceeds max_grid_slots={MAX_CSV_GRID_SLOTS}")
            rows.append(row)
            max_columns = next_max_columns
    except csv_module.Error as exc:
        raise ValueError(f"Malformed CSV near physical line {reader.line_num}: {exc}") from exc
    return rows


def _classify_value(value: str) -> CsvValueKind | None:
    """The non-empty fields are roughly divided into numbers, Boolean, dates or text for header voting."""
    normalized = value.strip()
    if not normalized:
        return None
    numeric = normalized.removesuffix("%")
    compact_numeric = "".join(char for char in numeric if char not in {",", " ", "_", "\u00a0"})
    if any(char.isascii() and char.isdigit() for char in compact_numeric):
        try:
            float(compact_numeric)
        except ValueError:
            pass
        else:
            return "number"
    if normalized.casefold() in {"true", "false", "yes", "no"}:
        return "boolean"
    if _DATE_RE.fullmatch(normalized) or _TIME_RE.fullmatch(normalized):
        return "date"
    return "text"


def _dominant_kind(values: list[str]) -> CsvValueKind | None:
    """Return the field type that covers at least 90% of the non-empty subject values, and return empty when there is no advantage type."""
    kinds = [kind for value in values if (kind := _classify_value(value)) is not None]
    if not kinds:
        return None
    counts = Counter(kinds)
    for kind in ("number", "boolean", "date", "text"):
        if counts[kind] * _HEADER_KIND_DOMINANCE_DEN >= len(kinds) * _HEADER_KIND_DOMINANCE_NUM:
            return kind
    return None


def _fold_header_value(value: str) -> str:
    """Generate header comparison values that ignore leading and trailing blanks and case."""
    return value.strip().casefold()


def _modal_row_width(rows: list[list[str]]) -> int:
    """Return the row width mode, and select a wider record when the frequency is the same."""
    if not rows:
        return 0
    counts = Counter(len(row) for row in rows)
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _infer_header_row(rows: list[list[str]]) -> bool:
    """Conservatively judge whether CSV has a row of headers based on the first row label form and body column type."""
    if len(rows) < 2:
        return False
    body = rows[1 : _HEADER_SAMPLE_ROWS + 1]
    if len(rows[0]) != _modal_row_width(body):
        return False

    header = rows[0]
    seen_labels: set[str] = set()
    for column, value in enumerate(header):
        folded = _fold_header_value(value)
        if not folded:
            if column == 0:
                continue
            return False
        if "\n" in value or "\r" in value or len(value) > _MAX_HEADER_LABEL_CHARS:
            return False
        if folded in seen_labels:
            return False
        seen_labels.add(folded)

    header_votes = 0
    data_votes = 0
    for column, label in enumerate(header):
        values = [row[column].strip() for row in body if column < len(row) and row[column].strip()]
        if not values:
            continue
        label_kind = _classify_value(label)
        dominant_kind = _dominant_kind(values)
        if dominant_kind is not None and dominant_kind != "text":
            if label_kind == "text" or (column == 0 and not label.strip()):
                header_votes += 1
            else:
                data_votes += 1
            continue
        folded_label = _fold_header_value(label)
        if folded_label and any(_fold_header_value(value) == folded_label for value in values):
            data_votes += 1

    if header_votes or data_votes:
        return header_votes > data_votes
    return True


def _normalize_row_widths(rows: list[list[str]]) -> list[list[str]]:
    """On the premise of not changing the existing field content, short records are filled to the maximum column width."""
    if not rows:
        return []
    max_columns = max(len(row) for row in rows)
    return [row + [""] * (max_columns - len(row)) for row in rows]


def _render_field_html(value: str) -> str:
    """Escape a CSV field, normalize line breaks and replace control characters not allowed by HTML."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _DISALLOWED_CONTROL_RE.sub("\ufffd", normalized)
    return html.escape(normalized, quote=True).replace("\n", "<br>")


def _rendered_field_utf8_bytes(value: str, remaining_budget: int) -> int:
    """Calculate the number of UTF-8 bytes after field rendering without creating an escape string."""
    rendered_bytes = 0
    index = 0
    while index < len(value):
        char = value[index]
        codepoint = ord(char)
        if char == "\r":
            if index + 1 < len(value) and value[index + 1] == "\n":
                index += 1
            addition = len("<br>")
        elif char == "\n":
            addition = len("<br>")
        elif codepoint <= 0x08 or codepoint in {0x0B, 0x0C, 0x7F} or 0x0E <= codepoint <= 0x1F:
            addition = 3
        elif 0xD800 <= codepoint <= 0xDFFF:
            addition = 3
        elif char == "&":
            addition = len("&amp;")
        elif char in {"<", ">"}:
            addition = len("&lt;")
        elif char in {'"', "'"}:
            addition = len("&quot;")
        elif codepoint <= 0x7F:
            addition = 1
        elif codepoint <= 0x7FF:
            addition = 2
        elif codepoint <= 0xFFFF:
            addition = 3
        else:
            addition = 4
        rendered_bytes += addition
        if rendered_bytes > remaining_budget:
            raise ValueError(f"CSV exceeds max_rendered_bytes={MAX_CSV_RENDERED_BYTES}")
        index += 1
    return rendered_bytes


def _charge_rendered_bytes(used_bytes: int, additional_bytes: int) -> int:
    """Accumulate CSV HTML output budget, and reject over-limit content before writing to StringIO."""
    if additional_bytes < 0 or used_bytes > MAX_CSV_RENDERED_BYTES - additional_bytes:
        raise ValueError(f"CSV exceeds max_rendered_bytes={MAX_CSV_RENDERED_BYTES}")
    return used_bytes + additional_bytes


def _rows_to_html(rows: list[list[str]], *, has_header: bool) -> str:
    """Incrementally construct the security table HTML to avoid retaining separate string objects for each cell."""
    output = StringIO()
    rendered_bytes = 0
    rendered_bytes = _charge_rendered_bytes(rendered_bytes, len("<table>"))
    output.write("<table>")
    for row_index, row in enumerate(rows):
        tag = "th" if has_header and row_index == 0 else "td"
        row_prefix = "\n  <tr>"
        rendered_bytes = _charge_rendered_bytes(rendered_bytes, len(row_prefix))
        output.write(row_prefix)
        for value in row:
            cell_prefix = f"\n    <{tag}>"
            cell_suffix = f"</{tag}>"
            rendered_bytes = _charge_rendered_bytes(rendered_bytes, len(cell_prefix) + len(cell_suffix))
            remaining_budget = MAX_CSV_RENDERED_BYTES - rendered_bytes
            field_bytes = _rendered_field_utf8_bytes(value, remaining_budget)
            rendered_bytes = _charge_rendered_bytes(rendered_bytes, field_bytes)
            output.write(cell_prefix)
            output.write(_render_field_html(value))
            output.write(cell_suffix)
        row_suffix = "\n  </tr>"
        rendered_bytes = _charge_rendered_bytes(rendered_bytes, len(row_suffix))
        output.write(row_suffix)
    table_suffix = "\n</table>"
    _charge_rendered_bytes(rendered_bytes, len(table_suffix))
    output.write(table_suffix)
    return output.getvalue()


def convert_csv(file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
    """Read the CSV binary stream and return the single logical page table model-list."""
    file_bytes = file_binary.read(MAX_CSV_BYTES + 1)
    if len(file_bytes) > MAX_CSV_BYTES:
        raise ValueError(f"CSV exceeds max_bytes={MAX_CSV_BYTES}")

    text = _decode_csv_bytes(file_bytes)
    text, declared_delimiter = _extract_sep_directive(text)
    delimiter = declared_delimiter or _sniff_delimiter(text)
    rows = _read_csv_rows(text, delimiter)
    if not rows:
        return [[]]
    has_header = _infer_header_row(rows)
    normalized_rows = _normalize_row_widths(rows)
    table_html = _rows_to_html(normalized_rows, has_header=has_header)
    return [[{"type": BlockType.TABLE, "content": table_html}]]


__all__ = ["convert_csv"]
