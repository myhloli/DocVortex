"""OFD Deterministic data model used internally by the parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from lxml import etree  # type: ignore[reportMissingImports]

from ....schema import BBox
from .geometry import Affine, Point, Quad

if TYPE_CHECKING:
    from .vector import VectorPath, ClipPath


@dataclass(frozen=True, slots=True)
class FontResource:
    """Save font resources and optional embedded font member paths."""

    resource_id: int
    font_name: str
    family_name: str
    font_part: str | None
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True, slots=True)
class MediaResource:
    """Save member paths and declaration formats of multimedia resources."""

    resource_id: int
    media_type: str
    media_format: str
    media_part: str


@dataclass(frozen=True, slots=True)
class CompositeResource:
    """Save recursively expandable composite primitive resources."""

    resource_id: int
    width: float
    height: float
    element: etree._Element


@dataclass(slots=True)
class ResourceRegistry:
    """Save the resource index within the current document or page scope."""

    fonts: dict[int, FontResource] = field(default_factory=dict)
    media: dict[int, MediaResource] = field(default_factory=dict)
    composites: dict[int, CompositeResource] = field(default_factory=dict)
    draw_params: dict[int, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GlyphItem:
    """Save the geometry of a semantic character in page space."""

    text: str
    bbox: BBox
    quad: Quad
    origin: Point
    glyph_id: int | None = None


@dataclass(slots=True)
class TextLine:
    """Save a sortable text line recovered by TextCode."""

    text: str
    bbox: BBox
    glyphs: list[GlyphItem]
    angle: int
    font_size: float
    paint_order: int
    object_id: int | None
    layer_type: str
    template_id: int | None
    styles: tuple[str, ...] = ()
    runs: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class AxisLine:
    """Save a visible horizontal or vertical line in page space."""

    bbox: BBox
    orientation: str
    width: float
    paint_order: int
    template_id: int | None


@dataclass(frozen=True, slots=True)
class ImageItem:
    """Save page image load, geometry and drawing source."""

    bbox: BBox
    image_base64: str | None
    paint_order: int
    object_id: int | None
    layer_type: str
    template_id: int | None
    diagnostic: str | None = None


@dataclass(slots=True)
class OfdPageScene:
    """Save a page of OFD's native scenes and projectable objects."""

    page_idx: int
    physical_box: BBox
    content_box: BBox | None
    text_lines: list[TextLine] = field(default_factory=list)
    axis_lines: list[AxisLine] = field(default_factory=list)
    images: list[ImageItem] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    vector_paths: list[VectorPath] = field(default_factory=list)
    text_object_count: int = 0
    image_object_count: int = 0
    vector_unsupported: bool = False


@dataclass(frozen=True, slots=True)
class OfdDocumentRef:
    """Save the entry and metadata of a DocBody in OFD.xml."""

    document_part: str
    signatures_part: str | None
    metadata: dict[str, str]
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PageRef:
    """Save a page reference in the Document.xml page tree."""

    page_id: int | None
    page_part: str


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """Save the mapping of template ID to template page members."""

    template_id: int
    page_part: str


@dataclass(frozen=True, slots=True)
class PageBuildContext:
    """Save the parent state required for recursive primitive construction."""

    transform: Affine
    clip_bbox: BBox
    layer_type: str
    template_id: int | None
    draw_style: dict[str, str] = field(default_factory=dict)
    vector_clips: tuple[tuple[ClipPath, ...], ...] = ()


__all__ = [
    "AxisLine",
    "CompositeResource",
    "FontResource",
    "GlyphItem",
    "ImageItem",
    "MediaResource",
    "OfdDocumentRef",
    "OfdPageScene",
    "PageBuildContext",
    "PageRef",
    "ResourceRegistry",
    "TemplateRef",
    "TextLine",
]
