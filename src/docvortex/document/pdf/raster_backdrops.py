"""只从源图自身颜色、裁剪和前景对象确认无独立内容的印刷色带。"""

from dataclasses import replace
import ctypes

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from .native_coordinates import _drawing_page_size
from .raster_composites import _isolated_alpha


def _is_pale_gradient(member, page):
    """从完整原像素检查色差和局部边缘，避免缩略图把小字或标志抹平。"""
    try:
        data = raw.FPDF_IMAGEOBJ_METADATA()
        if not raw.FPDFImageObj_GetImageMetadata(member.raw, page, ctypes.byref(data)):
            return False
        if not 1 < data.width * data.height <= 500_000 or min(data.width, data.height) <= 1:
            return False
        handle = raw.FPDFImageObj_GetBitmap(member.raw)
        if not handle:
            return False
        bitmap = pdfium.PdfBitmap.from_raw(handle)
        try:
            with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                pixels = np.asarray(rgb, dtype=np.int16)
                if pixels.min() < 192:
                    return False
                spacing = max(1, min(pixels.shape[:2]) // 32)
                return (
                    all(np.max(np.abs(np.diff(pixels, axis=axis))) <= 12 for axis in (0, 1))
                    and np.max(np.abs(pixels[spacing:] - pixels[:-spacing])) <= 14
                    and np.max(np.abs(pixels[:, spacing:] - pixels[:, :-spacing])) <= 14
                )
        finally:
            bitmap.close()
    except Exception:
        return False


def _open_field_is_opaque(member, page, box, page_bbox, later_boxes):
    """有效裁剪的透明孔必须完全落在后画图层下；外露底面保持不透明。"""
    alpha = _isolated_alpha(member, page, box, page_bbox)
    if alpha is None:
        return False
    rows, cols = alpha.shape
    dx, dy = (box[2] - box[0]) / cols, (box[3] - box[1]) / rows
    xs = box[0] + dx * (np.arange(cols)[None, :] + 0.5)
    ys = box[1] + dy * (np.arange(rows)[:, None] + 0.5)
    covered = np.zeros(alpha.shape, dtype=bool)
    for x0, y0, x1, y1 in later_boxes:
        covered |= (x0 - dx <= xs) & (xs <= x1 + dx) & (y0 - dy <= ys) & (ys <= y1 + dy)
    open_field = alpha[1:-1, 1:-1][~covered[1:-1, 1:-1]]
    return open_field.size >= 0.4 * alpha.size and np.all(open_field == 255)


def attach_raster_backdrops(page, page_bbox, rotation, members, infos):
    """只检查横跨版面且位于多组已证实原生卡片下方的浅色源位图。"""
    if rotation or sum(bool(info.composite_inner_bboxes) for info in infos) < 3:
        return
    width, height = _drawing_page_size(page_bbox, rotation)
    for index, (member, info) in enumerate(zip(members, infos, strict=True)):
        left, top, right, bottom = info.bbox
        a, b, c, d, _, _ = member.matrix
        if not (
            member.depth == 0
            and abs(b) + abs(c) <= 1e-6
            and 0 < min(abs(a), abs(d)) <= max(abs(a), abs(d)) <= 1600
            and left <= 0.02 * width
            and right >= 0.98 * width
            and 0.15 * height <= bottom - top <= 0.5 * height
        ):
            continue
        later = infos[index + 1 :]
        panels = [
            candidate.bbox
            for candidate in later
            if candidate.composite_inner_bboxes and top <= candidate.bbox[1] < candidate.bbox[3] <= bottom
        ]
        if len(panels) < 3 or not _is_pale_gradient(member, page):
            continue
        visible_overlays = (later_info.bbox for later_info in later if later_info.bbox[1] < bottom and later_info.bbox[3] > top)
        if _open_field_is_opaque(member, page, info.bbox, page_bbox, visible_overlays):
            infos[index] = replace(info, quiet_band_backdrop=True)
