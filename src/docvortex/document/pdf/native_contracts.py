"""PDF 原生页面快照、数据类型与固定常量，保持原生提取算法与资源语义。"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from .text.contracts import Char
from PIL import Image
from ...schema import BBox


logger = logging.getLogger("docvortex.document.pdf.document")

POINTS_PER_INCH: int = 72


DEFAULT_RENDER_DPI: int = 200


DEFAULT_RENDER_SCALE: float = DEFAULT_RENDER_DPI / POINTS_PER_INCH


DEFAULT_RENDER_MAX_EDGE: int = 3500


NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE = 1.0


OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE = 2.5


OFFSET_DUPLICATE_TRANSLATION_TOLERANCE = 0.1


OFFSET_DUPLICATE_MIN_BBOX_OVERLAP_RATIO = 0.45


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
    """保存页面像素及其相对 PDF 点坐标的缩放比例。"""

    def __init__(self, pil_image: Image.Image, scale: float) -> None:
        """持有调用者提供的独立 Pillow 图片与缩放值。"""
        self.pil_image = pil_image
        self.scale = scale


@dataclass(frozen=True, slots=True)
class PDFPageTextGeometry:
    """保存原始字符及 loose/tight/origin 三套视觉几何。"""

    chars: list[Char]
    tight_bboxes: dict[int, BBox]
    origins: dict[int, tuple[float, float]]
    loose_bboxes: dict[int, BBox] = field(default_factory=dict)


@dataclass(frozen=True)
class PDFDrawingLine:
    """PDF 页面中可见的水平或竖直绘图线，坐标使用页面左上原点的 PDF point。"""

    start: tuple[float, float]
    end: tuple[float, float]
    bbox: BBox
    width: float
    orientation: Literal["horizontal", "vertical"]


@dataclass(frozen=True)
class PDFLinkAnnotation:
    """PDF 外部 URI Link 注解，区域坐标使用页面左上原点的 PDF point。"""

    target: str
    bboxes: tuple[BBox, ...]
    source_index: int


@dataclass(frozen=True)
class PDFPathInfo:
    """PDF Path 的可见几何和绘制特征，bbox 使用页面左上原点坐标。"""

    bbox: BBox
    segment_count: int
    fill_visible: bool
    stroke_visible: bool
    form_depth: int
    source_index: int
    fill_rgba: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class PDFImageInfo:
    """PDF 点阵图的页面几何与稳定内容指纹，指纹读取失败时为 None。"""

    bbox: BBox
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class _PDFPageSnapshot:
    """保存单次页面生命周期提取的纯 Python 数据，不持有 PDFium 子对象。"""

    page_size: tuple[float, float]
    rotation: Literal[0, 90, 180, 270]
    text_geometry: PDFPageTextGeometry
    drawing_lines: list[PDFDrawingLine]
    path_infos: list[PDFPathInfo]
    image_infos: list[PDFImageInfo]
    form_bboxes: list[BBox]
    signature_bboxes: list[BBox]
    link_annotations: list[PDFLinkAnnotation]


@dataclass
class _PathSubpath:
    """保存一个 PDF Path 子路径的点、直线段和闭合状态。"""

    points: list[tuple[float, float]]
    straight_segments: list[tuple[tuple[float, float], tuple[float, float]]]
    closed: bool = False


PDFPageImage.__module__ = "docvortex.document.pdf.document"
PDFPageTextGeometry.__module__ = "docvortex.document.pdf.document"
PDFDrawingLine.__module__ = "docvortex.document.pdf.document"
PDFLinkAnnotation.__module__ = "docvortex.document.pdf.document"
PDFPathInfo.__module__ = "docvortex.document.pdf.document"
PDFImageInfo.__module__ = "docvortex.document.pdf.document"
_PDFPageSnapshot.__module__ = "docvortex.document.pdf.document"
_PathSubpath.__module__ = "docvortex.document.pdf.document"
