"""从独立 PDF 位图的像素和绘制次序识别局部合成图。"""

from collections import Counter
from dataclasses import replace
import ctypes
from functools import lru_cache
from itertools import combinations
from math import ceil, floor

import numpy as np
from PIL import Image
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ...schema import BBox
from .native_coordinates import _drawing_page_size
from .frame_apertures import find_frame_opening, screen_fits_opening

_MAX_RENDER_EDGE = 1600
_SAMPLE_EDGE = 64


def _candidate_margin(outer: BBox, inner: BBox) -> bool:
    """只比较四边近邻且完整包含的图层，保留小内嵌图和重合的独立放置。"""
    width, height = outer[2] - outer[0], outer[3] - outer[1]
    gaps = (inner[0] - outer[0], inner[1] - outer[1], outer[2] - inner[2], outer[3] - inner[3])
    return (
        sum(gaps) >= 1
        and all(0 <= gap <= 0.18 * span for gap, span in zip(gaps, (width, height) * 2))
        and (gaps[0] + gaps[2] <= 0.23 * width and gaps[1] + gaps[3] <= 0.23 * height)
    )


def _eligible_raster(member, bbox, page_bbox, size, rotation):
    """只检查有界的完整轴对齐放置；曲线 clip 可用，但页边截断或局部 Form 不推断归属。"""
    a, b, c, d, e, f = member.matrix
    return (
        rotation == 0
        and member.depth == 0
        and abs(b) + abs(c) <= 1e-6
        and 16 <= min(abs(a), abs(d))
        and max(abs(a), abs(d)) <= _MAX_RENDER_EDGE
        and page_bbox[0] <= min(e, e + a) <= max(e, e + a) <= page_bbox[2]
        and page_bbox[1] <= min(f, f + d) <= max(f, f + d) <= page_bbox[3]
        and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < 0.6 * size[0] * size[1]
    )


def _bitmap_sample(handle, mode, size=None):
    """只保留小型像素数组，立即释放由 PDFium 分配的原始缓冲。"""
    if not handle:
        return None
    bitmap = pdfium.PdfBitmap.from_raw(handle)
    try:
        with (
            bitmap.to_pil() as native,
            native.getchannel("A") if mode == "A" else native.convert(mode) as converted,
        ):
            if size is None:
                size = (_SAMPLE_EDGE, _SAMPLE_EDGE)
            with converted.resize(size, Image.Resampling.BOX) as sample:
                return np.array(sample)
    finally:
        bitmap.close()


def _intrinsic_sample(member, page):
    """先查资源大小，再抽取图像原色；依赖或损坏对象读取失败时放行。"""
    try:
        data = raw.FPDF_IMAGEOBJ_METADATA()
        if not raw.FPDFImageObj_GetImageMetadata(member.raw, page, ctypes.byref(data)):
            return None
        if not 0 < data.width * data.height <= 500_000:
            return None
        pixels = _bitmap_sample(raw.FPDFImageObj_GetBitmap(member.raw), "RGB", (24, 24))
        return pixels
    except Exception:
        return None


def _isolated_alpha(member, page, bbox, page_bbox):
    """仅渲染一笔图层以读取透明度，按裁剪和反射后的页面边界取样。"""
    a, _, _, d, e, f = member.matrix
    left, top = min(e, e + a) - page_bbox[0], page_bbox[3] - max(f, f + d)
    # 渲染接口以矩阵变换后尺寸为边界。切片的初始矩形比可见 clip 略大。
    part = (
        (bbox[0] - left) / abs(a),
        (bbox[1] - top) / abs(d),
        (bbox[2] - left) / abs(a),
        (bbox[3] - top) / abs(d),
    )
    try:
        alpha = _bitmap_sample(raw.FPDFImageObj_GetRenderedBitmap(page.pdf, page, member.raw), "A")
        if alpha is not None:
            x1, y1, x2, y2 = (max(0, min(_SAMPLE_EDGE, round(coord * _SAMPLE_EDGE))) for coord in part)
            if x2 - x1 >= 16 and y2 - y1 >= 16:
                return alpha[y1:y2, x1:x2]
    except Exception:
        pass
    return None


def _shadow_bbox(bbox, alpha):
    """保存可见阴影的支持范围，排除极淡长尾以便避免相邻印刷题签。"""
    ys, xs = np.nonzero(alpha >= 32)
    if not len(xs):
        return None
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    rows, columns = alpha.shape
    return (
        bbox[0] + xs.min().item() / columns * width,
        bbox[1] + ys.min().item() / rows * height,
        bbox[0] + (xs.max().item() + 1) / columns * width,
        bbox[1] + (ys.max().item() + 1) / rows * height,
    )


def _main_alpha(alpha):
    """取图层中央的半宽半高，远离矩形抗锯齿和圆角裁剪。"""
    height, width = alpha.shape
    return alpha[height // 4 : -height // 4, width // 4 : -width // 4]


def _classify_outer(member, page, bbox, page_bbox, rgb):
    """用中性色和独立图层的透明度区分透明内孔与软阴影。"""
    if rgb is None or np.mean(np.ptp(rgb, axis=2) > 24) > 0.05:
        return None
    alpha = _isolated_alpha(member, page, bbox, page_bbox)
    if alpha is None:
        return None
    center = _main_alpha(alpha)
    if member.clip is not None:
        if np.mean(center > 8) <= 0.05 and np.mean(alpha >= 64) >= 0.1:
            return "frame", None
        return None
    shade = center.mean()
    if (
        np.mean(rgb.max(axis=2) > 85) <= 0.05
        and 45 <= shade <= 200
        and np.mean(abs(center - shade) > 20) <= 0.05
        and np.mean(alpha <= 12) >= 0.05
    ):
        ink = _shadow_bbox(bbox, alpha)
        if ink is not None:
            return "shadow", ink
    return None


def _within_pixel_limit(member, page, limit):
    """在解码前验证源像素规模，读取上限失败时不推断图层关系。"""
    try:
        metadata = raw.FPDF_IMAGEOBJ_METADATA()
        return bool(raw.FPDFImageObj_GetImageMetadata(member.raw, page, ctypes.byref(metadata))) and (
            0 < metadata.width * metadata.height <= limit
        )
    except Exception:
        return False


def _opaque_foreground(member, page, bbox, page_bbox):
    """合成图必须有不透明主体；高像素或低层读取失败时保留原来的独立候选。"""
    if not _within_pixel_limit(member, page, 2_000_000):
        return False
    alpha = _isolated_alpha(member, page, bbox, page_bbox)
    return alpha is not None and np.mean(_main_alpha(alpha) >= 245) >= 0.95


def read_frame_opening(page, member, bbox: BBox, page_bbox: BBox, rotation: int) -> BBox | None:
    """仅对有界轴对齐图层解码一次，并核实缺边来自实际跨纸裁剪。"""
    a, b, c, d, _, f = member.matrix
    if (
        rotation
        or member.depth
        or abs(b) + abs(c) > 1e-6
        or min(abs(a), abs(d)) < 16
        or max(abs(a), abs(d)) > 1600
        or not _within_pixel_limit(member, page, 2_000_000)
    ):
        return None
    alpha = _isolated_alpha(member, page, bbox, page_bbox)
    if alpha is None:
        return None
    height = page_bbox[3] - page_bbox[1]
    return find_frame_opening(
        alpha,
        bbox,
        cut_top=bbox[1] <= 0.1 and max(f, f + d) > page_bbox[3] + 0.1,
        cut_bottom=bbox[3] >= height - 0.1 and min(f, f + d) < page_bbox[1] - 0.1,
    )


def _backdrop_extension(outer, inner, size):
    """提名跨越大部分栏高且前景基本沿边的延伸底图。"""
    width, height = size
    panel_width, panel_height = outer[2] - outer[0], outer[3] - outer[1]
    return (
        0.25 * width <= panel_width <= 0.58 * width
        and panel_height >= 0.88 * height
        and min(outer[0], width - outer[2]) <= 0.04 * width
        and 0.12 * panel_height <= inner[1] - outer[1] <= 0.35 * panel_height
        and abs(outer[0] - inner[0]) <= 0.03 * panel_width
        and abs(outer[2] - inner[2]) <= 0.03 * panel_width
        and abs(outer[3] - inner[3]) <= 0.03 * panel_height
    )


def _is_pale_diffuse_backdrop(member, page, page_bbox, outer, inner):
    """逐源像素验证会舍弃的边带，保住缩略图中消失的小字和标志。"""
    if not _within_pixel_limit(member, page, 3_000_000):
        return False
    try:
        handle = raw.FPDFImageObj_GetBitmap(member.raw)
        if not handle:
            return False
        bitmap = pdfium.PdfBitmap.from_raw(handle)
        try:
            with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                pixels = np.asarray(rgb, dtype=np.int16)
        finally:
            bitmap.close()
    except Exception:
        return False
    a, _, _, d, e, f = member.matrix
    left, top = min(e, e + a) - page_bbox[0], page_bbox[3] - max(f, f + d)
    rows, cols = pixels.shape[:2]
    if a < 0:
        pixels = pixels[:, ::-1]
    if d < 0:
        pixels = pixels[::-1]
    x0, y0, x1, y1 = outer
    strips = (
        (x0, y0, x1, inner[1]),
        (x0, inner[1], inner[0], inner[3]),
        (inner[2], inner[1], x1, inner[3]),
        (x0, inner[3], x1, y1),
    )
    for start_x, start_y, end_x, end_y in strips:
        if start_x >= end_x or start_y >= end_y:
            continue
        strip = pixels[
            max(0, floor((start_y - top) * rows / abs(d))) : ceil((end_y - top) * rows / abs(d)),
            max(0, floor((start_x - left) * cols / abs(a))) : ceil((end_x - left) * cols / abs(a)),
        ]
        if strip.size == 0 or strip.min() < 150:
            return False
        for stripe in (strip, strip.transpose(1, 0, 2)):
            for step, limit in ((1, 10), (3, 15)):
                if len(stripe) > step and np.max(np.abs(stripe[step:] - stripe[:-step])) > limit:
                    return False
    return True


def _masked_subject(member, page, bbox, page_bbox):
    """含开孔的前景必须有清楚不透明主体，并留下能看到底图的区域。"""
    if not _within_pixel_limit(member, page, 2_000_000):
        return False
    alpha = _isolated_alpha(member, page, bbox, page_bbox)
    return (
        alpha is not None
        and np.mean(_main_alpha(alpha) >= 245) >= 0.60
        and np.mean(alpha >= 245) >= 0.30
        and np.mean(alpha <= 8) >= 0.20
    )


def _frame_can_contain(outer, inner):
    """仅提名面积相近的完整内含关系，透明留边的宽度由真实开孔决定。"""
    width, height = outer[2] - outer[0], outer[3] - outer[1]
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2]
        and outer[1] <= inner[1] < inner[3] <= outer[3]
        and 0.3 * width * height <= (inner[2] - inner[0]) * (inner[3] - inner[1]) < width * height
    )


def confirmed_card_pairs(infos):
    """返回已证实卡面组内的位图对，防止其他图层规则重复分配同一组。"""
    return {
        frozenset(pair)
        for info in infos
        if info.composite_inner_bboxes and info.shadow_ink_bbox is None
        for pair in combinations((info.bbox, *info.composite_inner_bboxes), 2)
    }


def _attach_frame_crops(page, page_bbox, size, members, infos, eligible, multiplicity):
    """关联相邻源绘制中的开孔边框与内屏，或浅色延伸底图与真实前景。"""
    grouped_pairs = confirmed_card_pairs(infos)
    for index, (first, second) in enumerate(zip(infos, infos[1:])):
        if (
            first.composite_inner_bboxes
            or second.composite_inner_bboxes
            or first.raster_pair is not None
            or frozenset((first.bbox, second.bbox)) in grouped_pairs
            or multiplicity[first.bbox] != 1
            or multiplicity[second.bbox] != 1
        ):
            continue
        a, b = members[index : index + 2]
        if index in eligible and index + 1 in eligible and _frame_can_contain(second.bbox, first.bbox):
            if (
                (opening := read_frame_opening(page, b, second.bbox, page_bbox, 0)) is not None
                and screen_fits_opening(opening, first.bbox)
                and _opaque_foreground(a, page, first.bbox, page_bbox)
            ):
                infos[index + 1] = replace(second, raster_pair=(first.bbox, second.bbox))
        elif (
            _backdrop_extension(first.bbox, second.bbox, size)
            and all(member.depth == 0 and abs(member.matrix[1]) + abs(member.matrix[2]) <= 1e-6 for member in (a, b))
            and min(abs(a.matrix[0]), abs(a.matrix[3])) > 0
            and max(abs(b.matrix[0]), abs(b.matrix[3])) <= _MAX_RENDER_EDGE
            and _masked_subject(b, page, second.bbox, page_bbox)
            and _is_pale_diffuse_backdrop(a, page, page_bbox, first.bbox, second.bbox)
        ):
            infos[index] = replace(first, raster_pair=(second.bbox, second.bbox))


def attach_raster_composites(page, page_bbox, rotation, members, infos):
    """按同页绘制顺序关联内容层；单页工作量按位图数量线性增长。"""
    if len(infos) < 2 or rotation != 0:
        return
    size = _drawing_page_size(page_bbox, rotation)
    eligible = [
        idx
        for idx, (member, info) in enumerate(zip(members, infos, strict=True))
        if _eligible_raster(member, info.bbox, page_bbox, size, rotation)
    ]
    multiplicity = Counter(info.bbox for info in infos)
    intrinsic = lru_cache(maxsize=8)(lambda index: _intrinsic_sample(members[index], page))
    for slot, outer_idx in enumerate(eligible[:-1]):
        outer = infos[outer_idx].bbox
        # 边框扁平化常带极窄的同色拼片；只跳过这些已被几何门槛排除的小对象。
        candidates = [idx for idx in eligible[slot + 1 : slot + 5] if _candidate_margin(outer, infos[idx].bbox)]
        if not candidates or multiplicity[outer] != 1 or any(multiplicity[infos[idx].bbox] != 1 for idx in candidates):
            continue
        classified = _classify_outer(members[outer_idx], page, outer, page_bbox, intrinsic(outer_idx))
        if classified is None:
            continue
        kind, ink = classified
        if kind == "shadow":
            # 阴影只与紧邻的一笔不透明图配对；内容层同框重绘的情形继续原有流程。
            if candidates != [outer_idx + 1] or not _opaque_foreground(
                members[candidates[0]], page, infos[candidates[0]].bbox, page_bbox
            ):
                continue
        else:
            if not all(members[idx].clip is not None for idx in candidates):
                continue
            panels = [intrinsic(idx) for idx in candidates]
            if not all(panel is not None and np.max(np.ptp(panel, axis=(0, 1))) <= 16 for panel in panels):
                continue
        infos[outer_idx] = replace(
            infos[outer_idx], composite_inner_bboxes=tuple(infos[idx].bbox for idx in candidates), shadow_ink_bbox=ink
        )
    _attach_frame_crops(page, page_bbox, size, members, infos, set(eligible), multiplicity)
