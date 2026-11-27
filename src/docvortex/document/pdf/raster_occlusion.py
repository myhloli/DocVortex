"""收集后画实心位图完全遮住前一层的源证据。"""

from collections import Counter
from contextlib import closing
import ctypes
from dataclasses import replace
from functools import lru_cache
from math import ceil, floor

import numpy as np

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from .drawing_visibility import DrawingIsolationError, hide_drawings, render_source_patch
from .raster import DEFAULT_MAX_RENDER_EDGE, DEFAULT_PDF_IMAGE_DPI
from .raster_composites import confirmed_card_pairs

_BOX_TOLERANCE = 0.05
_MAX_PROOFS_PER_PAGE = 4
_MAX_CROP_PIXELS = 1_300_000


def _dimensions(bbox):
    """返回视觉矩形的宽和高。"""
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _source_pixel_size(member):
    """只读取资源的原始像素宽高，避开图像解码。"""
    width, height = ctypes.c_uint(), ctypes.c_uint()
    if raw.FPDFImageObj_GetImagePixelSize(member.raw, ctypes.byref(width), ctypes.byref(height)):
        return width.value, height.value
    return 0, 0


def _is_active(obj):
    """在证明期间只处理真实参与渲染的对象。"""
    active = ctypes.c_int()
    return raw.FPDFPageObj_GetIsActive(obj, ctypes.byref(active)) and active.value


def _axis_aligned_leaf(member):
    """仅允许无父 Form、无旋转反射的页面首层位图。"""
    a, b, c, d, _, _ = member.matrix
    return member.depth == 0 and a > 0 and d > 0 and max(abs(b), abs(c)) <= 1e-6


def _geometry_can_cover(inner, outer):
    """核对完整内含，仅给 PDF 量化造成的亚像素边界差留出容差。"""
    gaps = (inner[0] - outer[0], inner[1] - outer[1], outer[2] - inner[2], outer[3] - inner[3])
    return min(gaps) >= -_BOX_TOLERANCE and sum(gaps) > 0.5


def _isolated_alpha(member, page):
    """读取有界 PDFium 独立回绘的完整 alpha，保留蒙版实际形状。"""
    width, height = _source_pixel_size(member)
    a, _, _, d, _, _ = member.matrix
    if not (0 < width * height <= 4_000_000 and ceil(a) * ceil(d) <= 1_000_000):
        return None
    obj = member.raw
    if not _is_active(obj) or raw.FPDFPageObj_HasTransparency(obj):
        return None
    handle = raw.FPDFImageObj_GetRenderedBitmap(page.pdf, page, obj)
    if not handle:
        return None
    with closing(pdfium.PdfBitmap.from_raw(handle)) as bitmap, bitmap.to_pil() as image:
        if image.mode != "RGBA":
            return None
        return image.getchannel("A")


def _opaque_over_lower(alpha, upper, lower_bbox, page_bbox):
    """只检查下层在前层中的投影，外缘真透明且不干涉下层时仍可保留。"""
    if alpha is None:
        return False
    _, _, _, d, e, f = upper.matrix
    origin_x, origin_y = e - page_bbox[0], page_bbox[3] - f - d
    x1, y1, x2, y2 = lower_bbox
    projection = (
        max(0, floor(x1 - origin_x)),
        max(0, floor(y1 - origin_y)),
        min(alpha.width, ceil(x2 - origin_x)),
        min(alpha.height, ceil(y2 - origin_y)),
    )
    left, top, right, bottom = projection
    if left >= right or top >= bottom:
        return False
    with alpha.crop(projection) as region:
        return region.getextrema() == (255, 255)


def _crop_pixels(page, crop, scale):
    """在最终成品使用的页面采样相位上取得一块像素。"""
    with closing(page.render(scale=scale, crop=crop)) as bitmap, bitmap.to_pil() as image:
        return image.tobytes()


def _unchanged_with_inner_disabled(page, inner, bbox, size):
    """在实际裁图比例和更高分辨率逐像素对照，退出时恢复源绘制状态。"""
    if not _is_active(inner.raw):
        return False
    x1, y1, x2, y2 = bbox
    width, height = size
    crop = tuple(max(0, margin - 1) for margin in (x1, height - y2, width - x2, y1))
    primary = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(size))
    scales = (primary, 6)
    if max((ceil((x2 - x1 + 2) * scale) * ceil((y2 - y1 + 2) * scale) for scale in scales)) > _MAX_CROP_PIXELS:
        return False
    original = [_crop_pixels(page, crop, scale) for scale in scales]
    if not raw.FPDFPageObj_SetIsActive(inner.raw, 0):
        return False
    try:
        return all(pixels == _crop_pixels(page, crop, scale) for pixels, scale in zip(original, scales, strict=True))
    finally:
        if not raw.FPDFPageObj_SetIsActive(inner.raw, 1):
            raise RuntimeError("无法恢复用于遮挡验证的 PDF 位图")


def _shifted_repaint(inner, outer):
    """只提名两个边界交错、覆盖对方绝大部分面积的重绘槽，排除内嵌图。"""
    left, top = max(inner[0], outer[0]), max(inner[1], outer[1])
    right, bottom = min(inner[2], outer[2]), min(inner[3], outer[3])
    shared = max(0.0, right - left) * max(0.0, bottom - top)
    iw, ih = _dimensions(inner)
    ow, oh = _dimensions(outer)
    return shared >= 0.92 * max(iw * ih, ow * oh) and shared < min(iw * ih, ow * oh) - 0.5


def _mostly_repainted(page, low, high, box, size):
    """量出早图在后图关闭时的可见贡献，以及两层按原序绘制后真实剩余的贡献。"""
    x1, y1, x2, y2 = box
    width, height = size
    scale = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(size))
    if (ceil((x2 - x1 + 2) * scale) * ceil((y2 - y1 + 2) * scale)) > _MAX_CROP_PIXELS:
        return False
    if not all(_is_active(m.raw) and raw.FPDFPageObj_GetMarkedContentID(m.raw) < 0 for m in (low, high)):
        return False
    crop = tuple(max(0, margin - 1) for margin in (x1, height - y2, width - x2, y1))

    def render():
        """以同一实际裁图比例和位置读取 RGB，在任意分支结束后交还页的源状态。"""
        with render_source_patch(page, scale, crop) as image, image.convert("RGB") as rgb:
            return np.array(rgb, dtype=np.int16)

    def changed(a, b):
        """用通道强度差排除重复编码及抗锯齿噪声。"""
        return np.max(np.abs(a - b), axis=2) > 12

    paper = render()
    with hide_drawings((low.raw,)):
        without_low = render()
    with hide_drawings((high.raw,)):
        without_high = render()
        with hide_drawings((low.raw,)):
            without_both = render()
    latent = changed(without_high, without_both)
    later = changed(paper, without_high)
    surviving = changed(paper, without_low)
    # 后图确有不同的大块内容，同时旧图原能显示的墨迹几乎已不再参与成品。
    return np.count_nonzero(latent & later & ~surviving) >= 0.08 * latent.size and (
        np.count_nonzero(surviving) <= 0.05 * np.count_nonzero(latent)
    )


def _geometry_nominees(infos, members):
    """仅对近同框图体做遮挡核验，整页扫描中的独立小图继续按原位公开。"""
    multiplicity = Counter(info.bbox for info in infos)
    grouped_pairs = confirmed_card_pairs(infos)
    boxes = [info.bbox for info in infos]
    for low, inner in enumerate(boxes[:-1]):
        if multiplicity[inner] != 1 or not _axis_aligned_leaf(members[low]):
            continue
        iw, ih = _dimensions(inner)
        for high in range(low + 1, min(len(boxes), low + 9)):
            outer = boxes[high]
            if multiplicity[outer] != 1 or not _axis_aligned_leaf(members[high]):
                continue
            contained = _geometry_can_cover(inner, outer)
            if not contained and (
                not _shifted_repaint(inner, outer) or members[low].clip is None or members[high].clip is None
            ):
                continue
            ow, oh = _dimensions(outer)
            ratio = iw * ih / (ow * oh)
            if ratio >= 0.55 and frozenset((inner, outer)) not in grouped_pairs:
                yield low, high


def attach_raster_occlusion(page, page_bbox, size, rotation, members, infos):
    """为有限个几何候选补充原件遮挡证据，不在提取阶段改变位图集合。"""
    if rotation != 0:
        return

    @lru_cache(maxsize=_MAX_PROOFS_PER_PAGE)
    def isolated(index):
        """同一后画图遮住多层时只检查一次独立绘制。"""
        return _isolated_alpha(members[index], page)

    attempts = Counter()
    for low, high in _geometry_nominees(infos, members):
        contained = _geometry_can_cover(infos[low].bbox, infos[high].bbox)
        if attempts[contained] >= _MAX_PROOFS_PER_PAGE:
            continue
        attempts[contained] += 1
        try:
            if not contained:
                if _mostly_repainted(page, members[low], members[high], infos[low].bbox, size):
                    upper = infos[high]
                    infos[high] = replace(upper, repainted_inner_bboxes=(*upper.repainted_inner_bboxes, infos[low].bbox))
                continue
            confirmed = _opaque_over_lower(isolated(high), members[high], infos[low].bbox, page_bbox) and (
                _unchanged_with_inner_disabled(page, members[low], infos[low].bbox, size)
            )
        except (OSError, ValueError, DrawingIsolationError):
            # 无法按原坐标可靠回绘时让既有布局继续处理该候选。
            continue
        if confirmed:
            upper = infos[high]
            infos[high] = replace(upper, occluded_inner_bboxes=(*upper.occluded_inner_bboxes, infos[low].bbox))
