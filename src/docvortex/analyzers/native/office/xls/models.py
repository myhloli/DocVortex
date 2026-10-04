"""Legacy Excel Internal semantic model used by the parsing phase."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class XlsFontStyle:
    """Inline text styles expressible in a BIFF FONT record."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False


@dataclass(frozen=True, slots=True)
class XlsRichRun:
    """Rich text range represented by Python character index."""

    start: int
    end: int
    style: XlsFontStyle


@dataclass(frozen=True, slots=True)
class XlsRichText:
    """Cell or drawing text and its rich text range."""

    text: str
    runs: tuple[XlsRichRun, ...] = ()


@dataclass(slots=True)
class XlsCell:
    """A visible semantic cell in the worksheet."""

    row: int
    col: int
    value: XlsRichText
    hyperlink: str | None = None


@dataclass(frozen=True, slots=True)
class XlsImage:
    """Serialized picture bound to worksheet cell anchor."""

    row: int
    col: int
    image_base64: str


@dataclass(frozen=True, slots=True)
class XlsEquation:
    """Native or picture comment formula bound to worksheet cell anchor."""

    row: int
    col: int
    latex: str


@dataclass(frozen=True, slots=True)
class XlsChart:
    """Source data coordinates recovered from embedded chart reference."""

    row: int
    col: int
    source_rows: tuple[int, ...]
    source_cols: tuple[int, ...]
    image_base64: str | None = None


@dataclass(slots=True)
class XlsSheet:
    """A worksheet cell, merge range, and drawing resource."""

    name: str
    visible: bool
    order: int = -1
    cells: dict[tuple[int, int], XlsCell] = field(default_factory=dict)
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    images: list[XlsImage] = field(default_factory=list)
    equations: list[XlsEquation] = field(default_factory=list)
    charts: list[XlsChart] = field(default_factory=list)
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class XlsChartSheet:
    """A standalone chart sheet and its source worksheet selection range."""

    name: str
    visible: bool
    order: int
    source_sheet_name: str | None
    source_rows: tuple[int, ...] = ()
    source_cols: tuple[int, ...] = ()


@dataclass(slots=True)
class XlsWorkbook:
    """Excel 97–2003 Internal pagination representation of a workbook."""

    sheets: list[XlsSheet]
    chart_sheets: list[XlsChartSheet] = field(default_factory=list)
    active_sheet_index: int | None = None
