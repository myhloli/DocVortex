"""仅为分类核验图像上的正文是否由原生文字实际绘制，不读取旧 OCR 内容作正文。"""

from __future__ import annotations

from ctypes import byref, c_float

import pypdfium2.raw as raw

from .native_coordinates import _apply_pdf_matrix, _drawing_page_size, _transform_drawing_point
from .native_objects import (
    _clipped_objects_of_type,
    _intersect_object_bbox,
    _text_object_visibility,
    _transform_object_bbox,
)

_IMAGE_TEXT_OVERLAP = 0.8
_UNPAINTED_TEXT_RATIO = 0.5
_PROBE_MAX_SIDE = 1024


def _area(box: tuple) -> float:
    """计算有效矩形面积，空裁剪按零处理。"""
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _union_area(boxes: list[tuple]) -> float:
    """沿横轴切片合并纵向区间，避免重叠图片重复计入覆盖率。"""
    boxes = [box for box in boxes if _area(box) > 0]
    edges = sorted({box[index] for box in boxes for index in (0, 2)})
    total = 0.0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((box[1], box[3]) for box in boxes if box[0] < right and box[2] > left)
        top, height = float("-inf"), 0.0
        for bottom, end in intervals:
            height += max(0.0, end - max(bottom, top))
            top = max(top, end)
        total += (right - left) * height
    return total


def _image_rectangles(page) -> list[tuple]:
    """将图片变换到页面坐标并应用累计裁剪及边界，遍历深度与原生解析保持一致。"""
    boxes = []
    frame = page.get_bbox()
    for member in _clipped_objects_of_type(page, raw.FPDF_PAGEOBJ_IMAGE):
        values = [c_float() for _ in range(4)]
        if not raw.FPDFPageObj_GetBounds(member.raw, *(byref(value) for value in values)):
            continue
        box = _transform_object_bbox(
            tuple(value.value for value in values), lambda point: _apply_pdf_matrix(point, member.parent_matrix)
        )
        box = _intersect_object_bbox(_intersect_object_bbox(box, member.clip), frame)
        if _area(box) > 0:
            boxes.append(box)
    return boxes


def page_image_coverage(page, images=None) -> float:
    """按页面内图片矩形并集计算覆盖率，图片白边和透明像素仅作为保守外框。"""
    page_area = _area(page.get_bbox())
    return min(_union_area(_image_rectangles(page) if images is None else images) / page_area, 1.0) if page_area else 0.0


def _render_rgb(page, scale: float):
    """取得独立 RGB 副本后释放 PDFium 位图，避免探测像素引用已关闭缓冲区。"""
    bitmap = page.render(scale=scale, may_draw_forms=False)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def _text_paint_difference(page):
    """临时关闭文字绘制并保留文字裁剪，比较像素；无论失败与否都恢复原渲染模式。"""
    from PIL import ImageChops

    scale = min(1.5, _PROBE_MAX_SIDE / max(page.get_size()))
    before = _render_rgb(page, scale)
    changed = []
    after = None
    try:
        for member in _clipped_objects_of_type(page, raw.FPDF_PAGEOBJ_TEXT):
            mode = raw.FPDFTextObj_GetTextRenderMode(member.raw)
            if mode in {0, 1, 2, 4, 5, 6}:
                # 模式 4–6 同时参与裁剪；改为 7 保留裁剪，不能改为 3 改变图片显示。
                if not raw.FPDFTextObj_SetTextRenderMode(member.raw, 7 if mode >= 4 else 3):
                    raise ValueError("Cannot suppress PDF text painting")
                changed.append((member.raw, mode))
        after = _render_rgb(page, scale)
        difference = ImageChops.difference(before, after)
        try:
            bands = difference.split()
            partial = ImageChops.lighter(bands[0], bands[1])
            maximum = ImageChops.lighter(partial, bands[2])
            partial.close()
            try:
                # 同一页面、同一渲染器的确定性差分保留弱对比度正文，不能只接受深色字。
                return maximum.point(lambda value: 255 if value else 0)
            finally:
                maximum.close()
                for band in bands:
                    band.close()
        finally:
            difference.close()
    finally:
        restored = True
        for obj, mode in reversed(changed):
            restored = bool(raw.FPDFTextObj_SetTextRenderMode(obj, mode)) and restored
        before.close()
        if after is not None:
            after.close()
        if not restored:
            raise ValueError("Cannot restore PDF text painting")


def image_text_signal(page, cleaned_char_count: int, min_chars: int, images=None) -> dict:
    """共享当前页图像框并批量核验正文，只有候选数可能达到 OCR 门槛时才进行像素探测。"""
    from .classification_bridge import read_image_text_snapshot

    result = {"image_backed_ocr": False, "image_text_chars": 0, "painted_image_text_chars": 0, "pixel_probe_performed": False}
    images = _image_rectangles(page) if images is None else images
    if not images or cleaned_char_count < min_chars:
        return result
    frame, rotation = page.get_bbox(), page.get_rotation()
    width, height = _drawing_page_size(frame, rotation)
    images = [_transform_object_bbox(box, lambda point: _transform_drawing_point(point, frame, rotation)) for box in images]
    visibility = _text_object_visibility(page, frame, rotation)
    textpage = page.get_textpage()
    snapshot = None
    try:
        snapshot = read_image_text_snapshot(textpage, frame, rotation, images, visibility, _IMAGE_TEXT_OVERLAP)
        count, paints = snapshot.count, snapshot.paints
    finally:
        textpage.close()
    result["image_text_chars"] = count
    # 不可见字符数不可能超过候选数；严格小于任一门槛时，后续渲染绝不可能改变分类。
    if count < max(min_chars, cleaned_char_count * _UNPAINTED_TEXT_RATIO):
        return result
    if paints:
        difference = _text_paint_difference(page)
        try:
            result["pixel_probe_performed"] = True
            result["painted_image_text_chars"] = snapshot.painted_count(
                difference.tobytes(), difference.width, difference.height, width, height
            )
        finally:
            difference.close()
    unpainted = count - result["painted_image_text_chars"]
    result["image_backed_ocr"] = unpainted >= min_chars and unpainted >= cleaned_char_count * _UNPAINTED_TEXT_RATIO
    return result
