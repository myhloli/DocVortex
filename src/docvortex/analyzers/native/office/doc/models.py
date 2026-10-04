"""Internal semantic model between Word binary parser and Converter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class DocCharStyle:
    """The visible character pattern of a continuous DOC text run."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    emphasis: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False
    hidden: bool = False
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class DocTextRun:
    """A section of visible text with the same style and hyperlink target."""

    text: str
    style: DocCharStyle = DocCharStyle()
    hyperlink: str | None = None
    formula: bool = False


@dataclass(frozen=True, slots=True)
class DocListInfo:
    """Paragraph is the list information parsed from PlfLst/PlfLfo."""

    identity: int
    level: int
    ordered: bool
    start: int = 1
    label: str | None = None


@dataclass(frozen=True, slots=True)
class DocTableCellFormat:
    """The merge attribute of a Word table in the row definition."""

    right: int
    horizontal_first: bool = False
    horizontal_continue: bool = False
    vertical_first: bool = False
    vertical_continue: bool = False


@dataclass(frozen=True, slots=True)
class DocTableFormat:
    """A table row definition parsed from a TTP paragraph."""

    boundaries: tuple[int, ...] = ()
    cells: tuple[DocTableCellFormat, ...] = ()
    header: bool = False


@dataclass(slots=True)
class DocParagraph:
    """Complete the paragraph of style, field and paragraph attribute parsing."""

    cp_start: int
    cp_end: int
    runs: list[DocTextRun] = field(default_factory=list)
    images: list[DocVisualPayload] = field(default_factory=list)
    style_name: str = ""
    heading_level: int | None = None
    is_title: bool = False
    is_toc: bool = False
    toc_level: int | None = None
    is_caption: bool = False
    is_code: bool = False
    anchor: str | None = None
    list_info: DocListInfo | None = None
    in_table: bool = False
    table_depth: int = 0
    cell_mark: bool = False
    row_mark: bool = False
    table_format: DocTableFormat | None = None


@dataclass(frozen=True, slots=True)
class DocImagePayload:
    """A picture original load recovered from PICF/OfficeArt."""

    data: bytes
    extension: str
    content_type: str
    equation_latex: str | None = None
    render_size_emu: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class DocChartPayload:
    """An editable OLE HTML data and optional preview of chart."""

    content: str
    preview: DocImagePayload | None = None


DocVisualPayload: TypeAlias = DocImagePayload | DocChartPayload


@dataclass(slots=True)
class DocImage:
    """Inline or floating picture positioned by main story CP."""

    cp: int
    payload: DocImagePayload


@dataclass(slots=True)
class DocTableCell:
    """Word Table cells and their nested contents."""

    blocks: list[DocElement] = field(default_factory=list)
    row_span: int = 1
    col_span: int = 1


@dataclass(slots=True)
class DocTableRow:
    """Word A row of the table."""

    cells: list[DocTableCell] = field(default_factory=list)
    header: bool = False


@dataclass(slots=True)
class DocTable:
    """Ordinary or nested tables assembled in the order of CP."""

    cp_start: int
    cp_end: int
    rows: list[DocTableRow] = field(default_factory=list)


DocElement: TypeAlias = DocParagraph | DocImage | DocTable


@dataclass(slots=True)
class DocSection:
    """A Word section and its page auxiliary text."""

    cp_start: int
    cp_end: int
    elements: list[DocElement] = field(default_factory=list)
    headers: list[DocParagraph] = field(default_factory=list)
    footers: list[DocParagraph] = field(default_factory=list)
    footnotes: list[DocParagraph] = field(default_factory=list)


@dataclass(slots=True)
class DocDocument:
    """A DOC document that can be projected as model-list."""

    sections: list[DocSection] = field(default_factory=list)
