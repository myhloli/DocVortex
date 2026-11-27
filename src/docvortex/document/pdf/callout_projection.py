"""按真实绘制贡献输出说明框以外仍有独立意义的可见底图。"""

from math import ceil
import numpy as np
from PIL import Image
import pypdfium2.raw as raw

from ...foundation.image_encoding import image_to_b64str
from .drawing_visibility import DrawingIsolationError, hide_drawings, render_source_patch
from .marked_visibility import _pixel_crop
from .native_objects import _clipped_objects_of_type, _image_bbox_from_matrix, _clip_object_visual_bbox
from .raster import DEFAULT_MAX_RENDER_EDGE, DEFAULT_PDF_IMAGE_DPI


def visible_callout_underlay(page, base, note, normalized):
    """只在独立注框内确实看不见原位图时回绘，留白和其他对象不会进入底图实物。"""
    if page.get_rotation():
        return None
    size = page.get_size()
    scale = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(size))
    crop = _pixel_crop(normalized, size, scale)
    if crop is None:
        return None
    found = []
    pb = page.get_bbox()
    for m in _clipped_objects_of_type(page, raw.FPDF_PAGEOBJ_IMAGE):
        b = _image_bbox_from_matrix(m.matrix, pb, 0)
        if b and _clip_object_visual_bbox(b, m.clip, pb, 0) == base:
            if m.depth or raw.FPDFPageObj_GetMarkedContentID(m.raw) >= 0:
                return None
            found.append(m.raw)
    if len(found) != 1:
        return None
    try:
        with render_source_patch(page, scale, crop) as original, original.convert("RGB") as rgb:
            paper = np.array(rgb)
        with hide_drawings(found), render_source_patch(page, scale, crop) as disabled, disabled.convert("RGB") as rgb:
            delta = np.max(np.abs(paper.astype(np.int16) - np.asarray(rgb)), axis=2)
    except (OSError, ValueError, DrawingIsolationError):
        return None
    width, height = ceil(size[0] * scale), ceil(size[1] * scale)
    xs = (ceil(crop[0] * scale) + np.arange(paper.shape[1]) + 0.5) * size[0] / width
    ys = (ceil(crop[3] * scale) + np.arange(paper.shape[0]) + 0.5) * size[1] / height
    note_pixels = ((xs >= note[0]) & (xs < note[2]))[None, :] & ((ys >= note[1]) & (ys < note[3]))[:, None]
    visible = delta > 0
    # 这里是位置重叠但独立上层可完全承接的说明框，真实开孔及半透明覆盖继续保持原归属。
    if np.any(delta[note_pixels] > 4) or np.count_nonzero((delta > 16) & ~note_pixels) < max(32, 0.035 * delta.size):
        return None
    visible &= ~note_pixels
    # 普通 PDF 裁片以白纸为底。源 JPEG 的近白噪点不能变成透明轮廓的毛边。
    finished = np.where(visible[:, :, None], paper, np.uint8(255))
    with Image.fromarray(finished) as out:
        return image_to_b64str(out, "PNG")
