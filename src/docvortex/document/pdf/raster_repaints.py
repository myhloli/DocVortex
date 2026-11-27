"""以原件绘制次序与同相位纸面差分确认位图局部重绘。"""

from math import ceil

import numpy as np
import pypdfium2.raw as raw

from ...schema import BBox
from .drawing_visibility import DrawingIsolationError, hide_drawings, render_source_patch
from .native_coordinates import _normalize_pdf_page_bbox
from .native_objects import _clip_object_visual_bbox, _clipped_objects_of_type, _image_bbox_from_matrix
from .raster import DEFAULT_MAX_RENDER_EDGE, DEFAULT_PDF_IMAGE_DPI

_MAX_PROBE_PIXELS = 1_300_000
_PIXEL_DELTA = 12


def _read_repaint_objects(page, base: BBox, overlay: BBox):
    """按有效裁剪查找两处唯一的首层位图，确认覆盖图后画。"""
    page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
    wanted = (base, overlay)
    matches = {}
    for order, member in enumerate(_clipped_objects_of_type(page, raw.FPDF_PAGEOBJ_IMAGE)):
        box = _image_bbox_from_matrix(member.matrix, page_bbox, 0)
        if box is None:
            continue
        box = _clip_object_visual_bbox(box, member.clip, page_bbox, 0)
        if box not in wanted:
            continue
        a, b, c, d, _, _ = member.matrix
        if (
            box in matches
            or member.depth
            or min(a, d) <= 0
            or max(abs(b), abs(c)) > 1e-6
            or raw.FPDFPageObj_GetMarkedContentID(member.raw) >= 0
        ):
            return None
        matches[box] = order, member.raw
    if base not in matches or overlay not in matches or matches[base][0] >= matches[overlay][0]:
        return None
    return matches[base][1], matches[overlay][1]


def _region(xs, ys, box: BBox, padding=0):
    """以输出像素中心标记纸面矩形，可留出一像素的采样边缘。"""
    return ((xs >= box[0] - padding) & (xs < box[2] + padding))[None, :] & ((ys >= box[1] - padding) & (ys < box[3] + padding))[
        :, None
    ]


def _has_visible_ink(delta, region) -> bool:
    """要求区域内存在可辨识墨迹，忽略孤立采样误差。"""
    return bool(np.count_nonzero((delta > _PIXEL_DELTA) & region) >= max(16, 0.01 * np.count_nonzero(region)))


def verify_raster_repaint(page, base: BBox, overlay: BBox, vector: BBox) -> bool:
    """核对早层剩余墨迹、覆盖带与矢量候选跨出画框的真实像素。"""
    if page.get_rotation() != 0:
        return False
    objects = _read_repaint_objects(page, base, overlay)
    if objects is None:
        return False

    width, height = page.get_size()
    scale = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(width, height))
    frame = (min(base[0], overlay[0]), min(base[1], overlay[1]), max(base[2], overlay[2]), max(base[3], overlay[3]))
    # 取三处候选的并集，在与最终资产相同的相位验证落在框内和框外的绘制。
    left, top = min(frame[0], vector[0]), min(frame[1], vector[1])
    right, bottom = max(frame[2], vector[2]), max(frame[3], vector[3])
    fringe = 1 / scale
    crop = tuple(max(0, margin - fringe) for margin in (left, height - bottom, width - right, top))
    if ceil((right - left + 2 * fringe) * scale) * ceil((bottom - top + 2 * fringe) * scale) > _MAX_PROBE_PIXELS:
        return False

    def pixels():
        """取得同一白底纸面窗口的可作有符号差分的 RGB 通道。"""
        with render_source_patch(page, scale, crop) as patch, patch.convert("RGB") as rgb:
            return np.array(rgb, dtype=np.int16)

    try:
        paper = pixels()
        xs = (ceil(crop[0] * scale) + np.arange(paper.shape[1]) + 0.5) * width / ceil(width * scale)
        ys = (ceil(crop[3] * scale) + np.arange(paper.shape[0]) + 0.5) * height / ceil(height * scale)
        vector_region = _region(xs, ys, vector)
        outside = vector_region & ~_region(xs, ys, frame, fringe)
        if np.any(paper[outside] != 255):
            return False
        # 仅在覆盖带内部要求逐像素相同，保留原生圆角与裁剪边缘的细小可见过渡。
        cover_inset = 4 * fringe
        covered = _region(xs, ys, base, -cover_inset) & _region(xs, ys, overlay, -cover_inset)
        exposed = _region(xs, ys, base, -fringe) & ~_region(xs, ys, overlay, fringe)
        with hide_drawings((objects[0],)):
            lower_delta = np.max(np.abs(paper - pixels()), axis=2)
        if np.any(lower_delta[covered]) or not _has_visible_ink(lower_delta, exposed):
            return False
        with hide_drawings((objects[1],)):
            upper_delta = np.max(np.abs(paper - pixels()), axis=2)
        return _has_visible_ink(upper_delta, covered) and _has_visible_ink(upper_delta, covered & vector_region)
    except (OSError, ValueError, DrawingIsolationError):
        # 隔离失败时由既有布局继续裁决，绘制开关已经恢复。
        return False
