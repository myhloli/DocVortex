"""PDF native page snapshots, data types and fixed constants, maintaining native extraction algorithms and resource semantics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from PIL import Image

from ...foundation.type_identity import preserve_type_module
from ...schema import BBox
from .text._contracts import Char

logger = logging.getLogger("docvortex.document.pdf._document")

POINTS_PER_INCH: int = 72


DEFAULT_RENDER_DPI: int = 200


DEFAULT_RENDER_SCALE: float = DEFAULT_RENDER_DPI / POINTS_PER_INCH


DEFAULT_RENDER_MAX_EDGE: int = 3500


DRAWING_FORM_MAX_DEPTH = 15


DRAWING_LINE_MERGE_TOLERANCE = 2.0


DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE = 1.0


DRAWING_LINE_AXIS_RATIO_TOLERANCE = 0.02


DRAWING_LINE_MIN_LENGTH = 1.0


DRAWING_THIN_RECT_MAX_THICKNESS = 2.0


DRAWING_THIN_RECT_MIN_ASPECT_RATIO = 4.0


PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES = 16 * 1024 * 1024


_PDF_EXTERNAL_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})


PDFMetadataKey: TypeAlias = Literal[
    "Title",
    "Author",
    "Subject",
    "Keywords",
    "Creator",
    "Producer",
    "CreationDate",
    "ModDate",
]


class PDFPageImage:
    """Saves the page pixels and their scaling relative to the PDF point coordinates."""

    def __init__(self, pil_image: Image.Image, scale: float) -> None:
        """Holds an independent Pillow image and scaling value provided by the caller."""
        self.pil_image = pil_image
        self.scale = scale


@dataclass(frozen=True, slots=True)
class PDFPageTextGeometry:
    """Save original characters and loose/tight/origin three sets of visual geometry."""

    chars: list[Char]
    tight_bboxes: dict[int, BBox]
    origins: dict[int, tuple[float, float]]
    loose_bboxes: dict[int, BBox] = field(default_factory=dict)


@dataclass(frozen=True)
class PDFDrawingLine:
    """PDF Horizontal or vertical drawing line visible on the page, coordinates use PDF point from the upper left origin of the page."""

    start: tuple[float, float]
    end: tuple[float, float]
    bbox: BBox
    width: float
    orientation: Literal["horizontal", "vertical"]


@dataclass(frozen=True)
class PDFLinkAnnotation:
    """PDF External URI Link Note, the area coordinates use the PDF point of the upper left origin of the page."""

    target: str
    bboxes: tuple[BBox, ...]
    source_index: int


@dataclass(frozen=True)
class PDFPathInfo:
    """PDF Visible geometry and drawing features of Path, bbox uses the coordinates of the upper left origin of the page."""

    bbox: BBox
    segment_count: int
    fill_visible: bool
    stroke_visible: bool
    form_depth: int
    source_index: int
    fill_rgba: tuple[int, int, int, int] | None = None
    rectangle_bboxes: tuple[BBox, ...] = ()


@dataclass(frozen=True)
class PDFImageInfo:
    """PDF The page geometry and stable content fingerprint of the bitmap, None when the fingerprint reading fails."""

    bbox: BBox
    fingerprint: str | None
    smooth_background: bool = False
    # The pixel-proven white/transparent top edge of the image itself is only used by the native layout to exclude adjacent large titles.
    blank_top_bbox: BBox | None = None


@dataclass(frozen=True, slots=True)
class PDFPageVectorGeometry:
    """Save a Path traversal of the materialized line and path summary, without holding a PDFium handle."""

    drawing_lines: tuple[PDFDrawingLine, ...] = ()
    path_infos: tuple[PDFPathInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class _PDFFormInfo:
    """Self-evident evidence of a Form call; the source number retains the traversal identity of the original character and Path."""

    instance_id: int
    parent_id: int | None
    occurrence: tuple[int, ...]
    bbox: BBox
    declared_bbox: BBox | None
    media_bbox: BBox
    matrix: tuple[float, ...]
    clip: BBox | None
    paint_order: int
    text_indices: frozenset[int]
    path_indices: frozenset[int]
    image_bboxes: tuple[BBox, ...]
    structure_valid: bool


@dataclass(frozen=True, slots=True, init=False)
class _PDFPageSnapshot:
    """Saves own native text or compatibility evidence for a single page extraction, does not hold the PDFium sub-object."""

    page_size: tuple[float, float]
    rotation: Literal[0, 90, 180, 270]
    _text_geometry: PDFPageTextGeometry | None
    drawing_lines: list[PDFDrawingLine]
    path_infos: list[PDFPathInfo]
    image_infos: list[PDFImageInfo]
    form_bboxes: list[BBox]
    signature_bboxes: list[BBox]
    link_annotations: list[PDFLinkAnnotation]
    native_text: Any = None
    form_infos: tuple[_PDFFormInfo, ...] = ()

    def __init__(
        self,
        page_size: tuple[float, float],
        rotation: Literal[0, 90, 180, 270],
        text_geometry: PDFPageTextGeometry | None,
        drawing_lines: list[PDFDrawingLine],
        path_infos: list[PDFPathInfo],
        image_infos: list[PDFImageInfo],
        form_bboxes: list[BBox],
        signature_bboxes: list[BBox],
        link_annotations: list[PDFLinkAnnotation],
        native_text: Any = None,
        form_infos: tuple[_PDFFormInfo, ...] = (),
    ) -> None:
        """The existing text_geometry construction keywords and positional order are retained, and internal fields are only used for lazy-compatible caching."""
        for name, value in (
            ("page_size", page_size),
            ("rotation", rotation),
            ("_text_geometry", text_geometry),
            ("drawing_lines", drawing_lines),
            ("path_infos", path_infos),
            ("image_infos", image_infos),
            ("form_bboxes", form_bboxes),
            ("signature_bboxes", signature_bboxes),
            ("link_annotations", link_annotations),
            ("native_text", native_text),
            ("form_infos", form_infos),
        ):
            object.__setattr__(self, name, value)

    @property
    def text_geometry(self) -> PDFPageTextGeometry:
        """The old private consumer is materialized once when explicitly accessed; Flash native pipeline does not create compatible characters in advance."""
        if self._text_geometry is None and self.native_text is not None:
            object.__setattr__(self, "_text_geometry", self.native_text.materialize_geometry())
        return self._text_geometry


@dataclass
class _PathSubpath:
    """Saves the points, straight segments, and closure status of a PDF Path subpath."""

    points: list[tuple[float, float]]
    straight_segments: list[tuple[tuple[float, float], tuple[float, float]]]
    closed: bool = False


preserve_type_module(PDFPageImage, "docvortex.document.pdf._document")
preserve_type_module(PDFPageTextGeometry, "docvortex.document.pdf._document")
preserve_type_module(PDFDrawingLine, "docvortex.document.pdf._document")
preserve_type_module(PDFLinkAnnotation, "docvortex.document.pdf._document")
preserve_type_module(PDFPathInfo, "docvortex.document.pdf._document")
preserve_type_module(PDFImageInfo, "docvortex.document.pdf._document")
preserve_type_module(_PDFPageSnapshot, "docvortex.document.pdf._document")
preserve_type_module(_PathSubpath, "docvortex.document.pdf._document")
