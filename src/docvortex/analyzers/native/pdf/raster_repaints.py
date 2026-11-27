"""由纸面证据裁决与矢量容器冲突的位图局部重绘。"""

from collections.abc import Callable
from itertools import combinations
from typing import Any

from ....schema import BBox
from .geometry import (
    _bbox_area,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_intersects,
    _bbox_overlap_in_first,
    _bbox_overlap_in_smaller,
)
from .geometry import _bbox_union, _coerce_bbox, _point_in_bbox
from .graphics import _IMAGE_CONTAINER_OVERLAP_THRESHOLD, _raster_is_native_text_background
from .models import _PageSource

RasterRepaintReader = Callable[[int, BBox, BBox, BBox], bool]
_MAX_REPAINT_PROOFS_PER_PAGE = 4


def resolve_raster_insets(source, rasters, graphics, members, protected):
    """保留大幅原图的完整可见画面，同时把嵌入的小位图和带标签的矢量插图归给同一裁片。"""
    for base in rasters:
        if base not in source.inset_base_bboxes or any(_bbox_intersects(base, box) for box in protected):
            continue
        layers = {box for box in rasters if box != base and _bbox_intersects(base, box)}
        insets = [block for block in graphics if _bbox_intersects(base, block["bbox"])]
        if not layers or not insets or any(block["type"] != "image" or not members.get(id(block)) for block in insets):
            continue
        boxes = [*layers, *(block["bbox"] for block in insets)]
        if sum(_bbox_area(box) for box in boxes) > 0.25 * _bbox_area(base):
            continue
        if any(_bbox_overlap_in_first(box, base) < 0.995 for box in boxes):
            continue
        labels = set().union(*(members[id(block)] for block in insets))
        # 所有原生文字已属于局部插图时才合成；段落、题注或页码不能被背景一起裁走。
        if any(line.source_index not in labels and _bbox_intersects(base, line.ink_bbox or line.bbox) for line in source.lines):
            continue
        return layers, {id(block) for block in insets}
    return set(), set()


def pure_vector_forms(source: _PageSource, forms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按真实 Form 的 Path 成员筛选可重新归属的无文字、无位图装饰。"""
    detachable = {
        form.path_indices
        for form in source.form_infos
        if form.structure_valid
        and form.parent_id is None
        and form.path_indices
        and not form.text_indices
        and not form.image_bboxes
        and form.bbox not in source.retained_page_forms
    }
    return [block for block in forms if not block["content"] and source.form_path_sources.get(block["bbox"]) in detachable]


def _is_partial_edge_repaint(base: BBox, overlay: BBox) -> bool:
    """只提名沿两条对边铺满、从第三条边向内替换一段的重绘。"""
    for axis, cross in ((0, 1), (1, 0)):
        length = base[axis + 2] - base[axis]
        cross_length = base[cross + 2] - base[cross]
        if length <= 0 or cross_length <= 0:
            continue
        cross_tolerance = max(0.5, 0.015 * cross_length)
        if max(abs(overlay[i] - base[i]) for i in (cross, cross + 2)) > cross_tolerance:
            continue
        if not 0.15 <= (overlay[axis + 2] - overlay[axis]) / length <= 0.8:
            continue
        edge_tolerance = max(0.5, 0.02 * length)
        if min(abs(overlay[i] - base[i]) for i in (axis, axis + 2)) <= edge_tolerance:
            return True
    return False


def resolve_raster_repaints(
    source: _PageSource,
    graphics: list[dict[str, Any]],
    fixed_blocks: list[dict[str, Any]],
    fully_linked_line_indices: frozenset[int],
    verify: RasterRepaintReader,
) -> tuple[list[BBox], list[BBox], set[int]]:
    """仅在已证重绘能独占其纸面时替换候选，交还被消费矢量块的身份。"""
    boxes = source.image_bboxes
    graphic_boxes = [block["bbox"] for block in graphics]
    fixed_boxes = [box for block in fixed_blocks if (box := _coerce_bbox(block.get("bbox"))) is not None]
    fixed_boxes.extend(source.signature_bboxes)
    consumed_rasters: set[BBox] = set()
    consumed_graphics: set[int] = set()
    frames = []
    attempts = 0
    for i, graphic in enumerate(graphic_boxes):
        contenders = [box for box in boxes if _bbox_overlap_in_smaller(box, graphic) >= _IMAGE_CONTAINER_OVERLAP_THRESHOLD]
        other_graphics = [box for j, box in enumerate(graphic_boxes) if i != j]
        for first, second in combinations(contenders, 2):
            if attempts == _MAX_REPAINT_PROOFS_PER_PAGE:
                break
            if first in consumed_rasters or second in consumed_rasters:
                continue
            base, overlay = (first, second) if _is_partial_edge_repaint(first, second) else (second, first)
            if not _is_partial_edge_repaint(base, overlay):
                continue
            frame = _bbox_union(base, overlay)
            other_rasters = [box for box in boxes if box not in (base, overlay)]
            obstacles = fixed_boxes + other_graphics + other_rasters
            if any(_bbox_intersects(box, frame) or _bbox_intersects(box, graphic) for box in obstacles):
                continue
            if _raster_is_native_text_background(source, frame) or any(
                _point_in_bbox((_bbox_center_x(line.bbox), _bbox_center_y(line.bbox)), frame)
                and (
                    line.semantic_type is not None
                    or line.source_index in fully_linked_line_indices
                    or _bbox_overlap_in_first(line.ink_bbox or line.bbox, frame) < 0.98
                )
                for line in source.lines
            ):
                continue
            attempts += 1
            if verify(source.page_index, base, overlay, graphic):
                frames.append(frame)
                consumed_rasters.update((base, overlay))
                consumed_graphics.add(id(graphics[i]))
                break
        if attempts == _MAX_REPAINT_PROOFS_PER_PAGE:
            break
    return [box for box in boxes if box not in consumed_rasters], frames, consumed_graphics
