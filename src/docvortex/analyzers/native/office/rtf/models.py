"""Explicit semantic model between RTF parser and DocVortex raw-block converter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias, Union


@dataclass(frozen=True, slots=True)
class RtfTextStyle:
    """Saves a RTF character run in a style that projects to Middle JSON."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False
    code: bool = False


@dataclass(frozen=True, slots=True)
class RtfTextRun:
    """Saves text, styles, and optional safe links for completed code page decoding."""

    text: str
    style: RtfTextStyle = RtfTextStyle()
    hyperlink: str | None = None


@dataclass(frozen=True, slots=True)
class RtfInlineEquation:
    """Save inline LaTeX without delimiters."""

    latex: str


@dataclass(frozen=True, slots=True)
class RtfImage:
    """Save RTF pict payload and verifiable source information."""

    data: bytes
    content_type: str
    part_name: str
    alt: str = ""


@dataclass(frozen=True, slots=True)
class RtfNoteReference:
    """Internally stable id that preserves footnote or endnote references."""

    note_id: str


@dataclass(frozen=True, slots=True)
class RtfAnchor:
    """Save in-paragraph bookmark anchor, converter Exposed only on title."""

    name: str


@dataclass(frozen=True, slots=True)
class RtfLineBreak:
    """Represents RTF Semantic wrapping carried by row, column, or explicit paging control."""


RtfInline: TypeAlias = Union[
    RtfTextRun,
    RtfInlineEquation,
    RtfImage,
    RtfNoteReference,
    RtfAnchor,
    RtfLineBreak,
]


@dataclass(frozen=True, slots=True)
class RtfListInfo:
    """Saves the identity, hierarchy, numbering type and precise label of a list paragraph."""

    identity: int
    level: int
    ordered: bool
    marker: str = "decimal"
    start: int = 1
    label: str | None = None


@dataclass(slots=True)
class RtfParagraph:
    """Saves a RTF semantic paragraph and its block-level attributes."""

    inlines: list[RtfInline] = field(default_factory=list)
    style_name: str = ""
    outline_level: int | None = None
    is_title: bool = False
    block_style: Literal["normal", "code", "quote"] = "normal"
    list_info: RtfListInfo | None = None


@dataclass(slots=True)
class RtfDisplayEquation:
    """Save RTF Office Math interline formula."""

    latex: str


@dataclass(slots=True)
class RtfTableCell:
    """Save the semantic content and merge tags of table origin cell."""

    blocks: list[RtfBlock] = field(default_factory=list)
    horizontal_merge: Literal["none", "start", "continue"] = "none"
    vertical_merge: Literal["none", "start", "continue"] = "none"
    right_boundary: int | None = None


@dataclass(slots=True)
class RtfTableRow:
    """Save a row and header mark of the RTF table."""

    cells: list[RtfTableCell] = field(default_factory=list)
    header: bool = False


@dataclass(slots=True)
class RtfTable:
    """Saves the RTF table rows in source order."""

    rows: list[RtfTableRow] = field(default_factory=list)


RtfBlock: TypeAlias = Union[RtfParagraph, RtfDisplayEquation, RtfTable]


@dataclass(slots=True)
class RtfNote:
    """Save footnotes, endnotes, or comment text."""

    id: str
    kind: Literal["footnote", "endnote", "annotation"]
    blocks: list[RtfBlock] = field(default_factory=list)


@dataclass(slots=True)
class RtfMetadata:
    """Save Document properties allowed to be exposed in RTF info destination."""

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None


@dataclass(slots=True)
class RtfDocument:
    """Saves a single logical page RTF document, ancillary content, annotations, footage and metadata."""

    blocks: list[RtfBlock] = field(default_factory=list)
    notes: list[RtfNote] = field(default_factory=list)
    headers: list[RtfBlock] = field(default_factory=list)
    footers: list[RtfBlock] = field(default_factory=list)
    metadata: RtfMetadata = field(default_factory=RtfMetadata)


__all__ = [
    "RtfAnchor",
    "RtfBlock",
    "RtfDisplayEquation",
    "RtfDocument",
    "RtfImage",
    "RtfInline",
    "RtfInlineEquation",
    "RtfLineBreak",
    "RtfListInfo",
    "RtfMetadata",
    "RtfNote",
    "RtfNoteReference",
    "RtfParagraph",
    "RtfTable",
    "RtfTableCell",
    "RtfTableRow",
    "RtfTextRun",
    "RtfTextStyle",
]
