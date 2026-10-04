"""Internal type contract for DocVortex HTML v1 canonical wire."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, Union

from lxml import etree  # type: ignore[reportMissingImports]

from ...schema import BlockType


DOCVORTEX_HTML_VERSION = "1"
WIRE_BLOCK_CLASS = "docvortex-block"
WIRE_DOCUMENT_CLASS = "docvortex-document"
WIRE_INDEX_CLASS = "docvortex-index"
WIRE_LIST_CONTENT_CLASS = "docvortex-list-content"
WIRE_LIST_MARKER_CLASS = "docvortex-list-marker"
WIRE_PAGE_BREAK_CLASS = "docvortex-page-break"
WIRE_PAGE_CLASS = "docvortex-page"
WIRE_VISUAL_BODY_CLASS = "docvortex-visual-body"
WireRenderMode: TypeAlias = Literal["default", "full"]
WireFallbackReason: TypeAlias = Literal["unsupported_version", "non_canonical_wire"]


@dataclass(frozen=True, slots=True)
class TextWireSpec:
    """Save the canonical node and metadata of a text class top-level block."""

    wrapper: etree._Element
    content_root: etree._Element
    block_type: BlockType
    page_idx: int
    block_index: int | None


@dataclass(frozen=True, slots=True)
class EquationWireSpec:
    """Save an inline formula or formula picture carrier."""

    wrapper: etree._Element
    content_root: etree._Element
    page_idx: int
    block_index: int | None


@dataclass(frozen=True, slots=True)
class AnnotationWireSpec:
    """canonical in-row container holding visual caption/footnote."""

    element: etree._Element
    block_type: BlockType


@dataclass(frozen=True, slots=True)
class RichVisualBodyWireSpec:
    """Save the main image and the normalized rich content fragment of the normal image/chart body."""

    element: etree._Element
    parent_type: BlockType
    sub_type: str
    primary_image: etree._Element | None
    content_fragment: etree._Element | None


@dataclass(frozen=True, slots=True)
class FlowchartBodyWireSpec:
    """Save flowchart source code and optional original image (raster fallback image in the old shell)."""

    element: etree._Element
    source_element: etree._Element
    primary_image: etree._Element | None


@dataclass(frozen=True, slots=True)
class TableBodyWireSpec:
    """Save the unique canonical payload of table body."""

    element: etree._Element
    kind: Literal["empty", "html", "text", "image"]
    payload_element: etree._Element | None


@dataclass(frozen=True, slots=True)
class CodeBodyWireSpec:
    """The only canonical content carrier that holds code/algorithm body."""

    element: etree._Element
    kind: Literal["code", "algorithm"]
    content_element: etree._Element


VisualBodyWireSpec: TypeAlias = Union[
    RichVisualBodyWireSpec,
    FlowchartBodyWireSpec,
    TableBodyWireSpec,
    CodeBodyWireSpec,
]
VisualChildWireSpec: TypeAlias = Union[VisualBodyWireSpec, AnnotationWireSpec]


@dataclass(frozen=True, slots=True)
class VisualWireSpec:
    """Save visual top-level block with child nodes that have been resolved in DOM order."""

    wrapper: etree._Element
    content_root: etree._Element
    block_type: BlockType
    page_idx: int
    block_index: int | None
    sub_type: str
    guess_lang: str
    children: tuple[VisualChildWireSpec, ...]


@dataclass(frozen=True, slots=True)
class ListLeafWireSpec:
    """Saves the contents of a list leaf carrier, marker with public types."""

    block_type: BlockType
    block_index: int | None
    content_element: etree._Element | None
    marker: str


@dataclass(frozen=True, slots=True)
class ListWireSpec:
    """Saves a list of canonicals and their recursive subkeys."""

    element: etree._Element
    block_index: int | None
    ordered: bool
    start: int
    sub_type: str
    classes: frozenset[str]
    children: tuple[Union[ListLeafWireSpec, "ListWireSpec"], ...]


@dataclass(frozen=True, slots=True)
class ListBlockWireSpec:
    """Save the top level ListBlock wrapper with the list tree."""

    wrapper: etree._Element
    page_idx: int
    block_index: int | None
    root: ListWireSpec


@dataclass(frozen=True, slots=True)
class IndexLeafWireSpec:
    """Saves the canonical contents of a directory leaf carrier with metadata."""

    block_type: BlockType
    block_index: int | None
    content_element: etree._Element | None
    anchor: str
    level: int | None


@dataclass(frozen=True, slots=True)
class IndexWireSpec:
    """Saves a list of canonical directories and their recursive subkeys."""

    element: etree._Element
    block_index: int | None
    children: tuple[Union[IndexLeafWireSpec, "IndexWireSpec"], ...]


@dataclass(frozen=True, slots=True)
class IndexBlockWireSpec:
    """Save the top-level IndexBlock wrapper with the directory tree."""

    wrapper: etree._Element
    page_idx: int
    block_index: int | None
    root: IndexWireSpec


PageWireSpec: TypeAlias = Union[
    TextWireSpec,
    EquationWireSpec,
    VisualWireSpec,
    ListBlockWireSpec,
    IndexBlockWireSpec,
]


@dataclass(frozen=True, slots=True)
class DocVortexHtmlWirePlan:
    """Save the complete canonical wire parsing result once without resource side effects."""

    root: etree._Element
    render_mode: WireRenderMode
    blocks: tuple[PageWireSpec, ...]


@dataclass(frozen=True, slots=True)
class WireDecodeResult:
    """Distinguish between a wire miss, an exact decode success, and a universal fallback required."""

    blocks: list[dict[str, object]] | None
    fallback_reason: WireFallbackReason | None = None


__all__ = [
    "AnnotationWireSpec",
    "CodeBodyWireSpec",
    "EquationWireSpec",
    "FlowchartBodyWireSpec",
    "IndexBlockWireSpec",
    "IndexLeafWireSpec",
    "IndexWireSpec",
    "ListBlockWireSpec",
    "ListLeafWireSpec",
    "ListWireSpec",
    "DOCVORTEX_HTML_VERSION",
    "DocVortexHtmlWirePlan",
    "PageWireSpec",
    "RichVisualBodyWireSpec",
    "TableBodyWireSpec",
    "TextWireSpec",
    "VisualBodyWireSpec",
    "VisualChildWireSpec",
    "VisualWireSpec",
    "WireDecodeResult",
    "WireFallbackReason",
    "WireRenderMode",
    "WIRE_BLOCK_CLASS",
    "WIRE_DOCUMENT_CLASS",
    "WIRE_INDEX_CLASS",
    "WIRE_LIST_CONTENT_CLASS",
    "WIRE_LIST_MARKER_CLASS",
    "WIRE_PAGE_BREAK_CLASS",
    "WIRE_PAGE_CLASS",
    "WIRE_VISUAL_BODY_CLASS",
]
