"""为有原生文字说明覆盖的地图和照片恢复仍能独立引用的底图。"""

from collections.abc import Callable
from .geometry import _bbox_area, _bbox_overlap_in_first, _bbox_overlap_in_smaller, _normalize_bbox_to_unit
from ....schema import BBox

VisibleRasterReader = Callable[[int, BBox, BBox, BBox], str | None]


def attach_callout_underlays(source, forms, other_blocks, read: VisibleRasterReader):
    """只有一个独立原位图和一个局部文字说明竞争时，延迟交付原图有可见墨迹的部分。"""
    area = source.page_size[0] * source.page_size[1]
    if not area:
        return
    attempts = 0
    for form in forms:
        note = form["bbox"]
        candidates = [b for b in source.image_bboxes if _bbox_overlap_in_smaller(b, note) >= 0.5]
        if len(candidates) != 1:
            continue
        base = candidates[0]
        if not 0.01 <= _bbox_area(base) / area <= 0.55 or _bbox_overlap_in_first(note, base) < 0.995:
            continue
        if _bbox_area(note) > 0.4 * _bbox_area(base):
            continue
        if any(b != base and _bbox_overlap_in_smaller(base, b) >= 0.10 for b in source.image_bboxes):
            continue
        if any(_bbox_overlap_in_smaller(base, b["bbox"]) >= 0.1 for b in (*forms, *other_blocks) if b is not form):
            continue
        # 文字说明本来独立存在。排除穿过正文、整版底纹和需要另外认领图外标签的情况。
        local = [
            line.ink_bbox or line.bbox
            for line in source.lines
            if line.text.strip() and _bbox_overlap_in_first(line.ink_bbox or line.bbox, base) >= 0.5
        ]
        if not local or any(_bbox_overlap_in_first(b, note) < 0.95 for b in local):
            continue
        attempts += 1
        normalized = tuple(_normalize_bbox_to_unit(base, source.page_size))
        if payload := read(source.page_index, base, note, normalized):
            form["_callout_underlay"] = {
                "type": "image",
                "bbox": base,
                "angle": 0,
                "content": "",
                "_visible_raster_payload": payload,
            }
        if attempts == 4:
            break
