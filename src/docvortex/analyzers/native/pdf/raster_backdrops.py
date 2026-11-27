"""将无独立墨迹的横向衬底从全宽图像候选移回印刷背景。"""

from ....document.pdf.native_contracts import PDFImageInfo
from .geometry import _bbox_overlap_in_first
from .models import _PageSource


def filter_panel_backdrops(source: _PageSource, images: list[PDFImageInfo], placements) -> list[PDFImageInfo]:
    """只在前景卡片和独立正文同时存在时取消衬底的成品图归属。"""
    backdrops = [
        info for info in images if info.quiet_band_backdrop and info.bbox not in placements and not info.composite_inner_bboxes
    ]
    if not backdrops or source.signature_bboxes or source.trusted_text_bboxes:
        return images
    retained = []
    lines = [line.ink_bbox or line.bbox for line in source.lines if line.angle == 0 and line.text.strip()]
    for backdrop in backdrops:
        box = backdrop.bbox
        panels = [
            info.bbox
            for info in images
            if info.composite_inner_bboxes
            and _bbox_overlap_in_first(info.bbox, box) >= 0.99
            and any(_bbox_overlap_in_first(line, info.bbox) >= 0.95 for line in lines)
        ]
        if len(panels) < 3:
            retained.append(backdrop)
            continue
        left, top, right, bottom = (
            min(b[0] for b in panels),
            min(b[1] for b in panels),
            max(b[2] for b in panels),
            max(b[3] for b in panels),
        )
        words_left = words_right = 0
        for line in lines:
            if (
                _bbox_overlap_in_first(line, box) < 0.95
                or line[2] - line[0] < 0.22 * source.page_size[0]
                or not top < (line[1] + line[3]) / 2 < bottom
            ):
                continue
            words_left += line[2] < left
            words_right += line[0] > right
        if max(words_left, words_right) < 4:
            retained.append(backdrop)
    return [info for info in images if info not in backdrops or info in retained]
