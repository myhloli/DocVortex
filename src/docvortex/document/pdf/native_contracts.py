"""PDF 原生页面快照、数据类型与固定常量，保持原生提取算法与资源语义。"""

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
    rectangle_bboxes: tuple[BBox, ...] = ()


@dataclass(frozen=True)
class PDFImageInfo:
    """PDF 点阵图的页面几何与稳定内容指纹，指纹读取失败时为 None。"""

    bbox: BBox
    fingerprint: str | None
    smooth_background: bool = False
    # 图片自身像素证明的白色/透明顶边，仅供原生布局排除相邻大标题使用。
    blank_top_bbox: BBox | None = None
    composite_inner_bboxes: tuple[BBox, ...] = ()
    shadow_ink_bbox: BBox | None = None
    quiet_band_backdrop: bool = False
    occluded_inner_bboxes: tuple[BBox, ...] = ()
    # 原图的大部分被同槽后画版本重绘，合并裁片时保留两个框的并集。
    repainted_inner_bboxes: tuple[BBox, ...] = ()
    # 源绘制证实的搭档及保留裁片。两原候选均须通过最终容器筛选。
    raster_pair: tuple[BBox, BBox] | None = None
    # 本页 Form 家族内的实际 Image 放置编号；独立位图不需要该编号。
    form_raster_id: int | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class PDFPageVectorGeometry:
    """保存一次 Path 遍历物化的线与路径摘要，不持有 PDFium 句柄。"""

    drawing_lines: tuple[PDFDrawingLine, ...] = ()
    path_infos: tuple[PDFPathInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class _PDFFormInfo:
    """一个 Form 调用的自有证据；来源编号保持原字符与 Path 的遍历身份。"""

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
    parallel_rules_only: bool = False
    image_source_ids: tuple[int | None, ...] = ()
    declared_watermark: bool = False
    has_untracked_content: bool = False


@dataclass(frozen=True, slots=True, init=False)
class _PDFPageSnapshot:
    """保存单次页面提取的自有原生文本或兼容证据，不持有 PDFium 子对象。"""

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
    text_visibility_uncertain: bool = False

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
        text_visibility_uncertain: bool = False,
    ) -> None:
        """保留既有text_geometry构造关键字和位置顺序，内部字段仅用于惰性兼容缓存。"""
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
            ("text_visibility_uncertain", text_visibility_uncertain),
        ):
            object.__setattr__(self, name, value)

    @property
    def text_geometry(self) -> PDFPageTextGeometry:
        """旧私有消费者显式访问时物化一次；Flash原生管线不提前创建兼容字符。"""
        if self._text_geometry is None and self.native_text is not None:
            object.__setattr__(self, "_text_geometry", self.native_text.materialize_geometry())
        return self._text_geometry


@dataclass
class _PathSubpath:
    """保存一个 PDF Path 子路径的点、直线段和闭合状态。"""

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
