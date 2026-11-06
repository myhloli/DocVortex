"""在原始 PDF 上标注严格 Middle JSON 的布局，共享实现不负责文件读写。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import cast
import warnings

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import IndirectObject, NameObject, NullObject, RectangleObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from .schema import PAGE_AUXILIARY_BLOCK_TYPES, BBox, BlockBase, BlockType, PageInfo

_VISUAL_PARENT_TYPES = {BlockType.IMAGE, BlockType.TABLE, BlockType.CHART, BlockType.CODE}

# 当前布局使用实色边框和同色类型标签；页面脚注独立着色，页面辅助块统一为灰色。
_BLOCK_COLORS: dict[str, tuple[float, float, float]] = {
    "text": (0.60, 0.05, 0.30),
    "ref_text": (0.45, 0.20, 0.65),
    "doc_title": (0.20, 0.20, 0.80),
    "paragraph_title": (0.20, 0.40, 0.90),
    "equation": (0.00, 0.60, 0.10),
    "list": (0.10, 0.60, 0.35),
    "index": (0.10, 0.60, 0.35),
    "image_body": (0.30, 0.85, 0.05),
    "image_caption": (0.10, 0.55, 0.90),
    "image_footnote": (0.95, 0.45, 0.10),
    "table_body": (0.80, 0.80, 0.00),
    "table_caption": (1.00, 0.85, 0.10),
    "table_footnote": (0.55, 0.90, 0.25),
    "chart_body": (0.30, 0.85, 0.05),
    "chart_caption": (0.10, 0.55, 0.90),
    "chart_footnote": (0.95, 0.45, 0.10),
    "code_body": (0.40, 0.00, 0.80),
    "algorithm_body": (0.40, 0.00, 0.80),
    "code_caption": (0.70, 0.35, 0.95),
    "code_footnote": (0.85, 0.70, 0.95),
    "page_footnote": (0, 128 / 255, 128 / 255),
    **{str(kind): (158 / 255, 158 / 255, 158 / 255) for kind in PAGE_AUXILIARY_BLOCK_TYPES},
}

_LABEL_FONT = "Helvetica"
_LABEL_FONT_SIZE = 7
_LABEL_PADDING = 1.0
_LABEL_GAP = 1.0


@dataclass(frozen=True, slots=True)
class LayoutRegion:
    """共享 PDF 标注与交互预览的原始区域，子路径与 HTML 来源映射使用相同规则。"""

    page_idx: int
    block_index: int
    child_path: tuple[int, ...]
    bbox: BBox
    type: str
    index: int | None
    color: tuple[float, float, float]


def collect_layout_regions(pages: Sequence[PageInfo]) -> list[LayoutRegion]:
    """按原始页和块树顺序收集布局，保留辅助块、子块编号及显示方向下的归一化坐标。"""
    if any(not isinstance(page, PageInfo) for page in pages):
        raise TypeError("pages must contain PageInfo instances")
    if len({page.page_idx for page in pages}) != len(pages):
        raise ValueError("pages must have unique page_idx values")
    return [
        region
        for page in pages
        for block in page.blocks
        for region in _iter_layout_regions(block, page.page_idx, cast(int, block.index))
    ]


def _iter_layout_regions(
    block: BlockBase, page_idx: int, block_index: int, child_path: tuple[int, ...] = ()
) -> Iterable[LayoutRegion]:
    """视觉父块由子块提供标注，列表保留层级。无坐标节点仍继续遍历其子树。"""
    if block.bbox is not None and block.type not in _VISUAL_PARENT_TYPES:
        kind = str(block.type)
        yield LayoutRegion(
            page_idx=page_idx,
            block_index=block_index,
            child_path=child_path,
            bbox=block.bbox,
            type=kind,
            index=block.index,
            color=_BLOCK_COLORS.get(kind, (0.90, 0.10, 0.10)),
        )
    content = getattr(block, "content", None)
    children = [child for child in content if isinstance(child, BlockBase)] if isinstance(content, list) else []
    for position, child in enumerate(children):
        yield from _iter_layout_regions(child, page_idx, block_index, (*child_path, position))


def render_layout_pdf(
    pdf_bytes: bytes,
    pages: Sequence[PageInfo],
    *,
    page_indices: Sequence[int] | None = None,
) -> bytes:
    """把当前 PageInfo 布局画到 PDF，返回新字节且不修改解析结果。

    bbox 使用页面显示方向下、以 CropBox 与 MediaBox 有效交集左上角为原点的归一化坐标。
    page_indices 的第 i 项是输入 PDF 第 i 页对应的原始 page_idx；
    完整原文可省略映射，抽页或重排后的 PDF 必须显式提供映射。
    没有对应解析结果的页面原样保留，映射长度错误时抛出 ValueError。
    每个边框标注自身类型和原始 index，缺失 index 显示 -；标签文字可被 PDF 提取。
    """
    regions_by_page: dict[int, list[LayoutRegion]] = {}
    for region in collect_layout_regions(pages):
        regions_by_page.setdefault(region.page_idx, []).append(region)

    reader = PdfReader(BytesIO(pdf_bytes))
    indices = tuple(range(len(reader.pages))) if page_indices is None else tuple(page_indices)
    if len(indices) != len(reader.pages):
        raise ValueError("page_indices length must match the PDF page count")
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices):
        raise ValueError("page_indices must contain non-negative integers")

    # 损坏的文档信息不参与可见页渲染；只省略不可读的 /Info，不回退为丢失表单的逐页复制。
    if "/Info" in reader.trailer:
        info_reference = reader.trailer.raw_get("/Info")
        try:
            reader.metadata
        except PdfReadError:
            # 完整对象加载也会遍历未被目录引用的 xref；缓存仅此已确认损坏的元数据，避免再次读取。
            if isinstance(info_reference, IndirectObject):
                reader.cache_indirect_object(info_reference.generation, info_reference.idnum, NullObject())
            del reader.trailer[NameObject("/Info")]
            warnings.warn("Omitting unreadable PDF /Info metadata from layout copy", RuntimeWarning, stacklevel=2)
    # 页树之外的 AcroForm 也参与 Widget 外观渲染，必须一并复制文档目录。
    try:
        writer = PdfWriter(clone_from=reader)
    except (RecursionError, PdfReadError) as error:
        # pypdf 可能包装递归异常；只接受已证明由递归溢出导致的读取失败。
        if isinstance(error, PdfReadError) and not isinstance(error.__cause__, RecursionError):
            raise
        # 带深结构树的文档先循环加载全部对象，避免递归克隆，不改全局递归上限或丢弃目录。
        warnings.warn("Loading all PDF objects after recursive layout clone exceeded its limit", RuntimeWarning, stacklevel=2)
        writer = PdfWriter(reader, full=True)
    for page_copy, original_index in zip(writer.pages, indices):
        regions = regions_by_page.get(original_index)
        if regions:
            overlay = _build_page_overlay(page_copy, regions)
            if overlay is not None:
                page_copy.merge_page(overlay)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _effective_page_box(page: PageObject) -> RectangleObject:
    """与 PDFium 显示页框一致取裁剪框和媒体框交集，不改写原框或重复缩放 UserUnit。"""
    return RectangleObject(
        (
            max(page.cropbox.left, page.mediabox.left),
            max(page.cropbox.bottom, page.mediabox.bottom),
            min(page.cropbox.right, page.mediabox.right),
            min(page.cropbox.top, page.mediabox.top),
        )
    )


def _build_page_overlay(page: PageObject, boxes: Sequence[LayoutRegion]) -> PageObject | None:
    """先绘制边框，再按显示方向放置标签，保留原页方向和页面框。"""
    visible_box = _effective_page_box(page)
    if visible_box.width <= 0 or visible_box.height <= 0:
        return None
    packet = BytesIO()
    painter = canvas.Canvas(packet, pagesize=(float(page.mediabox.width), float(page.mediabox.height)))
    painter.setLineWidth(1.0)
    for region in boxes:
        x0, y0, x1, y1 = _normalized_bbox_to_pdf(region.bbox, page)
        painter.setStrokeColorRGB(*region.color)
        painter.rect(x0, y0, max(0.5, x1 - x0), max(0.5, y1 - y0), stroke=1, fill=0)
    _draw_overlay_labels(painter, page, boxes)
    painter.save()
    packet.seek(0)
    overlay = PdfReader(packet).pages[0]
    # merge_page 按 overlay 的 CropBox 裁剪，必须同步原页偏移，支持负坐标。
    overlay.mediabox = RectangleObject(page.mediabox)
    overlay.cropbox = visible_box
    return overlay


def _draw_overlay_labels(painter: canvas.Canvas, page: PageObject, boxes: Sequence[LayoutRegion]) -> None:
    """在显示方向的左下原点坐标系中绘制正向标签，并用白底保护文字可读性。"""
    visible_box = _effective_page_box(page)
    left, bottom = float(visible_box.left), float(visible_box.bottom)
    width, height = float(visible_box.width), float(visible_box.height)
    rotation = page.rotation % 360
    transforms = {
        0: (1, 0, 0, 1, left, bottom),
        90: (0, 1, -1, 0, left + width, bottom),
        180: (-1, 0, 0, -1, left + width, bottom + height),
        270: (0, -1, 1, 0, left, bottom + height),
    }
    painter.saveState()
    painter.transform(*transforms[rotation])
    if rotation in (90, 270):
        width, height = height, width
    ascent, descent = pdfmetrics.getAscentDescent(_LABEL_FONT, _LABEL_FONT_SIZE)
    label_height = ascent - descent + 2 * _LABEL_PADDING
    for region in boxes:
        label = f"{region.type}: {region.index if region.index is not None else '-'}"
        text_width = pdfmetrics.stringWidth(label, _LABEL_FONT, _LABEL_FONT_SIZE)
        label_width = min(text_width + 2 * _LABEL_PADDING, width)
        rect = _place_label(region.bbox[0] * width, (1 - region.bbox[1]) * height, label_width, label_height, width, height)
        x0, y0, x1, y1 = rect
        painter.setFillColorRGB(1, 1, 1)
        painter.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
        painter.setFillColorRGB(*region.color)
        text = painter.beginText(x0 + _LABEL_PADDING, y0 + _LABEL_PADDING - descent)
        text.setFont(_LABEL_FONT, _LABEL_FONT_SIZE)
        # 极窄页面仅压缩标签的水平宽度，不让右侧文字越过 CropBox。
        text.setHorizScale(100 * min(1, max(0, label_width - 2 * _LABEL_PADDING) / text_width))
        text.textOut(label)
        painter.drawText(text)
    painter.restoreState()


def _place_label(
    left: float,
    top: float,
    width: float,
    height: float,
    page_width: float,
    page_height: float,
) -> BBox:
    """固定贴着所属框上方，仅在页面边缘就近收拢，不因其他标签碰撞而跨行移动。"""
    left = max(0, min(left, page_width - width))
    bottom = max(0, min(top + _LABEL_GAP, page_height - height))
    return left, bottom, left + width, min(bottom + height, page_height)


def _normalized_bbox_to_pdf(bbox: BBox, page: PageObject) -> BBox:
    """反转显示方向的直角旋转，并把有效可见页框内的归一化框还原为 PDF 坐标。"""
    visible_box = _effective_page_box(page)
    left, bottom = float(visible_box.left), float(visible_box.bottom)
    width, height = float(visible_box.width), float(visible_box.height)
    x0, y0, x1, y1 = bbox
    rotation = page.rotation % 360
    if rotation == 90:
        return left + y0 * width, bottom + x0 * height, left + y1 * width, bottom + x1 * height
    if rotation == 180:
        return left + (1 - x1) * width, bottom + y0 * height, left + (1 - x0) * width, bottom + y1 * height
    if rotation == 270:
        return left + (1 - y1) * width, bottom + (1 - x1) * height, left + (1 - y0) * width, bottom + (1 - x0) * height
    if rotation != 0:
        raise ValueError("PDF page rotation must be a multiple of 90 degrees")
    return left + x0 * width, bottom + (1 - y1) * height, left + x1 * width, bottom + (1 - y0) * height


__all__ = ["LayoutRegion", "collect_layout_regions", "render_layout_pdf"]
