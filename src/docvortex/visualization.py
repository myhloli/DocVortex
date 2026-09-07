"""在原始 PDF 上标注严格 Middle JSON 的布局，共享实现不负责文件读写。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from io import BytesIO

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import RectangleObject
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


def render_layout_pdf(
    pdf_bytes: bytes,
    pages: Sequence[PageInfo],
    *,
    page_indices: Sequence[int] | None = None,
) -> bytes:
    """把当前 PageInfo 布局画到 PDF，返回新字节且不修改解析结果。

    bbox 使用页面显示方向下、以 CropBox 左上角为原点的归一化坐标。
    page_indices 的第 i 项是输入 PDF 第 i 页对应的原始 page_idx；
    完整原文可省略映射，抽页或重排后的 PDF 必须显式提供映射。
    没有对应解析结果的页面原样保留，映射长度错误时抛出 ValueError。
    每个边框标注自身类型和原始 index，缺失 index 显示 -；标签文字可被 PDF 提取。
    """
    if any(not isinstance(page, PageInfo) for page in pages):
        raise TypeError("pages must contain PageInfo instances")
    pages_by_index = {page.page_idx: page for page in pages}
    if len(pages_by_index) != len(pages):
        raise ValueError("pages must have unique page_idx values")

    reader = PdfReader(BytesIO(pdf_bytes))
    indices = tuple(range(len(reader.pages))) if page_indices is None else tuple(page_indices)
    if len(indices) != len(reader.pages):
        raise ValueError("page_indices length must match the PDF page count")
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices):
        raise ValueError("page_indices must contain non-negative integers")

    writer = PdfWriter()
    for source_page, original_index in zip(reader.pages, indices):
        # 直接复制原页可保留 MediaBox、CropBox、Rotate 和原有批注。
        page_copy = writer.add_page(source_page)
        middle_page = pages_by_index.get(original_index)
        if middle_page is not None:
            overlay = _build_page_overlay(source_page, middle_page)
            if overlay is not None:
                page_copy.merge_page(overlay)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_page_overlay(page: PageObject, middle_page: PageInfo) -> PageObject | None:
    """先绘制边框，再按显示方向放置标签，保留原页方向和页面框。"""
    if page.cropbox.width <= 0 or page.cropbox.height <= 0:
        return None
    boxes = list(_iter_overlay_boxes(middle_page.blocks))
    if not boxes:
        return None

    packet = BytesIO()
    painter = canvas.Canvas(packet, pagesize=(float(page.mediabox.width), float(page.mediabox.height)))
    painter.setLineWidth(1.0)
    for block_type, bbox, _ in boxes:
        x0, y0, x1, y1 = _normalized_bbox_to_pdf(bbox, page)
        painter.setStrokeColorRGB(*_BLOCK_COLORS.get(block_type, (0.90, 0.10, 0.10)))
        painter.rect(x0, y0, max(0.5, x1 - x0), max(0.5, y1 - y0), stroke=1, fill=0)
    _draw_overlay_labels(painter, page, boxes)
    painter.save()
    packet.seek(0)
    overlay = PdfReader(packet).pages[0]
    # merge_page 按 overlay 的 CropBox 裁剪，必须同步原页偏移，支持负坐标。
    overlay.mediabox = RectangleObject(page.mediabox)
    overlay.cropbox = RectangleObject(page.cropbox)
    return overlay


def _iter_overlay_boxes(blocks: Iterable[BlockBase]) -> Iterable[tuple[str, BBox, int | None]]:
    """遍历当前 block 树并保留自身 index；视觉父块只画子块，列表保留层级框。"""
    for block in blocks:
        if block.bbox is not None and block.type not in _VISUAL_PARENT_TYPES:
            yield str(block.type), block.bbox, block.index
        content = getattr(block, "content", None)
        children = [child for child in content if isinstance(child, BlockBase)] if isinstance(content, list) else []
        if block.type in _VISUAL_PARENT_TYPES:
            for child in children:
                if child.bbox is not None:
                    yield str(child.type), child.bbox, child.index
        elif children:
            yield from _iter_overlay_boxes(children)


def _draw_overlay_labels(painter: canvas.Canvas, page: PageObject, boxes: Sequence[tuple[str, BBox, int | None]]) -> None:
    """在显示方向的左下原点坐标系中绘制正向标签，并用白底保护文字可读性。"""
    left, bottom = float(page.cropbox.left), float(page.cropbox.bottom)
    width, height = float(page.cropbox.width), float(page.cropbox.height)
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
    occupied: list[BBox] = []
    for block_type, bbox, index in boxes:
        label = f"{block_type}: {index if index is not None else '-'}"
        text_width = pdfmetrics.stringWidth(label, _LABEL_FONT, _LABEL_FONT_SIZE)
        label_width = min(text_width + 2 * _LABEL_PADDING, width)
        rect = _place_label(bbox[0] * width, (1 - bbox[1]) * height, label_width, label_height, width, height, occupied)
        occupied.append(rect)
        x0, y0, x1, y1 = rect
        painter.setFillColorRGB(1, 1, 1)
        painter.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
        painter.setFillColorRGB(*_BLOCK_COLORS.get(block_type, (0.90, 0.10, 0.10)))
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
    occupied: Sequence[BBox],
) -> BBox:
    """优先在框上方逐行避让，页顶受限时向框内下移；满页时选遮叠面积最小的位置。"""
    left = max(0, min(left, page_width - width))
    step = height + _LABEL_GAP
    candidates: list[BBox] = []
    y = top + _LABEL_GAP
    while y + height <= page_height:
        candidates.append((left, y, left + width, y + height))
        y += step
    y = min(top - _LABEL_GAP - height, page_height - height)
    while y >= 0:
        candidates.append((left, y, left + width, y + height))
        y -= step
    if not candidates:
        candidates.append((left, 0, left + width, min(height, page_height)))
    best = candidates[0]
    best_overlap = float("inf")
    for candidate in candidates:
        x0, y0, x1, y1 = candidate
        overlap = sum(
            max(0, min(x1, other[2]) - max(x0, other[0])) * max(0, min(y1, other[3]) - max(y0, other[1])) for other in occupied
        )
        if overlap == 0:
            return candidate
        if overlap < best_overlap:
            best, best_overlap = candidate, overlap
    return best


def _normalized_bbox_to_pdf(bbox: BBox, page: PageObject) -> BBox:
    """反转显示方向的直角旋转，并把 CropBox 内的归一化框还原为 PDF 坐标。"""
    left, bottom = float(page.cropbox.left), float(page.cropbox.bottom)
    width, height = float(page.cropbox.width), float(page.cropbox.height)
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


__all__ = ["render_layout_pdf"]
