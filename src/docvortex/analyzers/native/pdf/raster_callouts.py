"""用原生文字、框与引线识别被重复水印规则误删的插图。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .annotation_text import _is_strong_caption_text

from ....document.pdf._document import PDFImageInfo
from ....schema import BBox
from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_union_many,
    _point_in_bbox,
)
from .models import _PageSource


@dataclass(frozen=True, slots=True)
class _PlacedRaster:
    """保留同一栅格素材在独立正文槽位中的一次出现。"""

    page: int
    bbox: BBox
    center: tuple[float, float]
    callout: BBox | None


def _placed_raster_em(source: _PageSource, box: BBox) -> float | None:
    """确认点阵图占用独立正文槽位，并返回本页文字尺度。"""
    lines = [line for line in source.lines if line.text.strip()]
    horizontal = [line for line in lines if line.angle == 0 and line.effective_height > 0]
    if not horizontal or any(_bbox_overlap_in_first(line.ink_bbox or line.bbox, box) > 0.1 for line in lines):
        return None
    em = median(line.effective_height for line in horizontal)
    for line in horizontal:
        ink = line.ink_bbox or line.bbox
        gap = max(box[1] - ink[3], ink[1] - box[3])
        x_overlap = min(ink[2], box[2]) - max(ink[0], box[0])
        if 0 <= gap <= 3 * em and x_overlap >= 0.55 * (box[2] - box[0]):
            return em
    return None


def _leader_reaches_label(
    endpoint: tuple[float, float],
    panel: BBox,
    verticals: list[BBox],
    tolerance: float,
    max_bend: float,
) -> bool:
    """用横线端点直连侧框，或在一次直角折线后贴到侧框上下缘。"""
    x, y = endpoint
    if _point_in_bbox(endpoint, (panel[0] - tolerance, panel[1] - tolerance, panel[2] + tolerance, panel[3] + tolerance)):
        return True
    for segment in verticals:
        sx = _bbox_center_x(segment)
        if not panel[0] <= sx <= panel[2] or abs(sx - x) > tolerance or segment[3] - segment[1] > max_bend:
            continue
        if (
            abs(segment[1] - y) <= tolerance
            and abs(segment[3] - panel[1]) <= tolerance
            or abs(segment[3] - y) <= tolerance
            and abs(segment[1] - panel[3]) <= tolerance
        ):
            return True
    return False


def _find_raster_callout(source: _PageSource, image: PDFImageInfo, page_images: list[PDFImageInfo], em: float) -> BBox | None:
    """只合并有明确引线的图内高亮和图外短标签框，并保留矩形角落的正文。"""
    box = image.bbox
    width, height = box[2] - box[0], box[3] - box[1]
    tolerance = 0.22 * em
    horizontals = [line.bbox for line in source.drawing_lines if line.orientation == "horizontal"]
    verticals = [line.bbox for line in source.drawing_lines if line.orientation == "vertical"]
    strokes = [path.bbox for path in source.path_infos if path.stroke_visible]
    frames = [
        frame
        for frame in strokes
        if _bbox_overlap_in_first(frame, box) >= 0.8
        and 0.4 * width <= frame[2] - frame[0] <= 1.15 * width
        and 0.035 * height <= frame[3] - frame[1] <= 0.45 * height
        and all(
            any(
                abs(_bbox_center_y(rule) - edge) <= tolerance
                and min(rule[2], frame[2]) - max(rule[0], frame[0]) >= 0.85 * (frame[2] - frame[0])
                for rule in horizontals
            )
            for edge in (frame[1], frame[3])
        )
    ]
    if not frames:
        return None

    components = []
    owned_lines = set()
    for path in source.path_infos:
        panel = path.bbox
        if (
            not path.fill_visible
            or not 2 * em <= panel[2] - panel[0] <= 0.45 * width
            or not 2 * em <= panel[3] - panel[1] <= 0.45 * height
            or not box[1] - em <= panel[1] <= panel[3] <= box[3] + em
        ):
            continue
        left = panel[2] <= box[0] + tolerance
        if not left and panel[0] < box[2] - tolerance:
            continue
        # 保留描边的完整可见范围。填充与描边可能由不同的 PDF Path 重叠绘制。
        matching_strokes = [stroke for stroke in strokes if max(abs(a - b) for a, b in zip(panel, stroke)) <= tolerance]
        if not matching_strokes:
            continue
        panel = _bbox_union_many([panel, *matching_strokes])
        members = [
            line
            for line in source.lines
            if line.angle == 0 and line.text.strip() and _bbox_overlap_in_first(line.ink_bbox or line.bbox, panel) >= 0.95
        ]
        if (
            len(members) < 2
            or max(_bbox_center_y(line.bbox) for line in members) - min(_bbox_center_y(line.bbox) for line in members)
            < 0.75 * em
        ):
            continue
        for frame in frames:
            frame_edge = frame[0] if left else frame[2]
            for leader in horizontals:
                inner, outer = (leader[2], leader[0]) if left else (leader[0], leader[2])
                y = _bbox_center_y(leader)
                if (
                    not 0.7 * em <= leader[2] - leader[0] <= 0.55 * width
                    or abs(inner - frame_edge) > tolerance
                    or not frame[1] + tolerance < y < frame[3] - tolerance
                    or not _leader_reaches_label((outer, y), panel, verticals, tolerance, 0.25 * height)
                ):
                    continue
                components.extend((panel, leader, frame))
                owned_lines.update(line.source_index for line in members)

    if not components:
        return None
    combined = _bbox_union_many([box, *components])
    if any(
        line.source_index not in owned_lines
        and line.text.strip()
        and _bbox_overlap_in_first(line.ink_bbox or line.bbox, combined) > 0.1
        for line in source.lines
    ) or any(
        other is not image
        and _bbox_area(other.bbox) >= 0.02 * _bbox_area(box)
        and _bbox_overlap_in_first(other.bbox, combined) >= 0.1
        for other in page_images
    ):
        return None
    return combined


def _captioned_rasters(
    source: _PageSource, images: list[PDFImageInfo], candidates: list[PDFImageInfo]
) -> dict[BBox, tuple[BBox, str]]:
    """用独立编号图题证明一处重复位图是正文插图，并排除重叠正文及竞争图片。"""
    rows: dict[BBox, list[tuple[BBox, str]]] = defaultdict(list)
    for line in source.lines:
        if line.angle != 0 or not line.text.lstrip().casefold().startswith(("fig", "图", "chart", "diagram", "scheme")):
            continue
        if not _is_strong_caption_text(line.text):
            continue
        em = max(1.0, line.bbox[3] - line.bbox[1])
        near = [
            image
            for image in images
            if 0 <= line.bbox[1] - image.bbox[3] <= 2 * em and _bbox_axis_overlap_ratio(image.bbox, line.bbox, axis="x") >= 0.75
        ]
        if len(near) != 1 or near[0] not in candidates:
            continue
        box = near[0].bbox
        if any(text.text.strip() and _bbox_overlap_in_first(text.ink_bbox or text.bbox, box) > 0.1 for text in source.lines):
            continue
        if any(image is not near[0] and _bbox_overlap_in_first(image.bbox, box) > 0.1 for image in images):
            continue
        rows[box].append((line.bbox, line.text))
    return {box: values[0] for box, values in rows.items() if len(values) == 1}


def _recover_placed_repeated_rasters(
    sources: list[_PageSource],
    page_images: list[list[PDFImageInfo]],
    repeated: set[str],
    min_page_area_ratio: float,
    min_distinct_pages: int,
) -> list[dict[BBox, BBox]]:
    """以跨页位置变化、独立正文槽位及本地引线共同豁免真实重复插图。"""
    results: list[dict[BBox, BBox]] = [{} for _ in sources]
    if not repeated:
        return results

    placements: dict[str, list[_PlacedRaster]] = defaultdict(list)
    for page, (source, images) in enumerate(zip(sources, page_images, strict=True)):
        width, height = source.page_size
        candidates = [
            image
            for image in images
            if image.fingerprint in repeated
            and not image.smooth_background
            and _bbox_area(image.bbox) >= min_page_area_ratio * width * height
        ]
        if not candidates:
            continue
        source.raster_caption_rows = _captioned_rasters(source, images, candidates)
        results[page].update((box, box) for box in source.raster_caption_rows)
        for image in candidates:
            em = _placed_raster_em(source, image.bbox)
            if em is None:
                continue
            center = (_bbox_center_x(image.bbox) / width, _bbox_center_y(image.bbox) / height)
            placements[image.fingerprint].append(
                _PlacedRaster(page, image.bbox, center, _find_raster_callout(source, image, images, em))
            )

    for group in placements.values():
        # 位置不动的版心底图始终走原水印路径。在至少一页读到实际引线后，才比较同素材的其它用法。
        if not any(item.callout is not None for item in group):
            continue
        for item in group:
            if (
                item.callout is None
                and len(
                    {
                        other.page
                        for other in group
                        if max(abs(item.center[0] - other.center[0]), abs(item.center[1] - other.center[1])) <= 0.02
                    }
                )
                >= min_distinct_pages
            ):
                continue
            if any(
                item.page != other.page
                and max(abs(item.center[0] - other.center[0]), abs(item.center[1] - other.center[1])) >= 0.07
                and (item.callout is not None or other.callout is not None)
                for other in group
            ):
                results[item.page][item.bbox] = item.callout or item.bbox
    return results
