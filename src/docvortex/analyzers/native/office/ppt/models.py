"""Legacy PPT Internal semantic model used by the parsing phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class PptTextRun:
    """A text fragment with resolved styles and hyperlinks."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    baseline: int | None = None
    hyperlink: str | None = None


@dataclass(frozen=True, slots=True)
class PptParagraph:
    """A paragraph and its list level and numbering properties."""

    runs: tuple[PptTextRun, ...]
    depth: int = 0
    list_kind: Literal["ordered", "unordered"] | None = None
    start: int | None = None
    pp9rt: int = 0


@dataclass(frozen=True, slots=True)
class PptTextElement:
    """Text shape with slide coordinates."""

    paragraphs: tuple[PptParagraph, ...]
    text_type: int
    bbox: tuple[float, float, float, float]
    order: int
    shape_offset: int
    is_placeholder: bool = False


@dataclass(frozen=True, slots=True)
class PptImageElement:
    """An image that has been bound to a specific slide shape."""

    image_base64: str
    bbox: tuple[float, float, float, float]
    order: int
    shape_offset: int


@dataclass(frozen=True, slots=True)
class PptEquationElement:
    """The LaTeX formula has been restored from the native object or picture comment."""

    latex: str
    bbox: tuple[float, float, float, float]
    order: int
    shape_offset: int


@dataclass(frozen=True, slots=True)
class PptChartElement:
    """Editable OLE chart already bound to slide shape."""

    content: str
    image_base64: str | None
    bbox: tuple[float, float, float, float]
    order: int
    shape_offset: int


@dataclass(frozen=True, slots=True)
class PptTableCell:
    """The table origin cell and its range across rows and columns."""

    row: int
    col: int
    row_span: int
    col_span: int
    paragraphs: tuple[PptParagraph, ...]


@dataclass(frozen=True, slots=True)
class PptTableElement:
    """Regular grid reconstructed from OfficeArt table group."""

    rows: int
    cols: int
    cells: tuple[PptTableCell, ...]
    bbox: tuple[float, float, float, float]
    order: int
    shape_offsets: frozenset[tuple[int, ...]]


PptSlideElement: TypeAlias = PptTextElement | PptImageElement | PptEquationElement | PptChartElement | PptTableElement


@dataclass(slots=True)
class PptSlide:
    """Semantic content and notes of a slide."""

    slide_id: int | None
    elements: list[PptSlideElement] = field(default_factory=list)
    notes: list[PptParagraph] = field(default_factory=list)
    hidden: bool = False


@dataclass(slots=True)
class PptPresentation:
    """Paginated internal representation of legacy PPT."""

    slides: list[PptSlide]
    width: int = 5760
    height: int = 4320
