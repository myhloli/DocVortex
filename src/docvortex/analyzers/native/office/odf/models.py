"""OpenDocument internal inline, style and table models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias, Union


@dataclass(frozen=True, slots=True)
class TextStyle:
    """Saves the inheritable ODF inline style final value."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    superscript: bool = False
    subscript: bool = False

    def names(self) -> tuple[str, ...]:
        """Returns enabled style names in stable order for the DocVortex inline protocol."""
        result: list[str] = []
        if self.bold:
            result.append("bold")
        if self.italic:
            result.append("italic")
        if self.underline:
            result.append("underline")
        if self.strikethrough:
            result.append("strikethrough")
        if self.superscript:
            result.append("superscript")
        if self.subscript:
            result.append("subscript")
        return tuple(result)


@dataclass(frozen=True, slots=True)
class TextStyleDelta:
    """Save ODF Tri-state fields in the style hierarchy that can be explicitly overridden."""

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    superscript: bool | None = None
    subscript: bool | None = None

    def merge(self, child: TextStyleDelta) -> TextStyleDelta:
        """Overrides the current style with the child style's non-empty fields."""
        return TextStyleDelta(
            bold=self.bold if child.bold is None else child.bold,
            italic=self.italic if child.italic is None else child.italic,
            underline=self.underline if child.underline is None else child.underline,
            strikethrough=self.strikethrough if child.strikethrough is None else child.strikethrough,
            superscript=self.superscript if child.superscript is None else child.superscript,
            subscript=self.subscript if child.subscript is None else child.subscript,
        )

    def resolve(self) -> TextStyle:
        """Process undeclared fields as closed and return the final style."""
        return TextStyle(
            bold=bool(self.bold),
            italic=bool(self.italic),
            underline=bool(self.underline),
            strikethrough=bool(self.strikethrough),
            superscript=bool(self.superscript),
            subscript=bool(self.subscript),
        )


@dataclass(frozen=True, slots=True)
class ListLevel:
    """Saves the universal numbering semantics of a ODF list hierarchy."""

    ordered: bool = False
    start: int = 1


@dataclass(frozen=True, slots=True)
class InlineText:
    """Saves inline text with styles and optional hyperlinks."""

    text: str
    style: TextStyle = TextStyle()
    hyperlink: str | None = None


@dataclass(frozen=True, slots=True)
class InlineMath:
    """Save inline LaTeX without peripheral markers."""

    latex: str


@dataclass(frozen=True, slots=True)
class InlineBreak:
    """Indicates an explicit line break within the paragraph."""


@dataclass(frozen=True, slots=True)
class InlineNote:
    """Save ODF note body which should belong with the current line content."""

    content: str


@dataclass(frozen=True, slots=True)
class InlineImage:
    """Saving images in table cells allows inline rendering data URI."""

    data_uri: str
    alt: str = ""


@dataclass(frozen=True, slots=True)
class InlineBlockGroup:
    """Save the out-of-segment block and its pairing with the inline picture in the inline stream."""

    blocks: tuple[dict[str, Any], ...]
    inline_image_rendered: bool = False


InlineAtom: TypeAlias = Union[
    InlineText,
    InlineMath,
    InlineBreak,
    InlineNote,
    InlineImage,
    InlineBlockGroup,
]


@dataclass(slots=True)
class GridCell:
    """Save ODF HTML table origin cell with span."""

    html: str = ""
    row_span: int = 1
    col_span: int = 1
    header: bool = False

    @property
    def has_content(self) -> bool:
        """Returns whether the cell contains visible or structured HTML."""
        return bool(self.html.strip())


@dataclass(slots=True)
class TableGrid:
    """Save the ODF 2D table with merged placeholders."""

    rows: list[list[GridCell | None]] = field(default_factory=list)
    header_rows: int = 0
    covered: set[tuple[int, int]] = field(default_factory=set)

    @property
    def width(self) -> int:
        """Returns the maximum number of visual columns in the grid."""
        return max((len(row) for row in self.rows), default=0)


__all__ = [
    "GridCell",
    "InlineAtom",
    "InlineBlockGroup",
    "InlineBreak",
    "InlineImage",
    "InlineMath",
    "InlineNote",
    "InlineText",
    "ListLevel",
    "TableGrid",
    "TextStyle",
    "TextStyleDelta",
]
