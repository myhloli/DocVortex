"""恢复由相接填色轮廓和内嵌短标签组成的矢量图体。"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import replace

from ...._compute_backend import get_native
from ....document.pdf._document import PDFPathInfo
from ....schema import BBox
from .annotation_text import _is_strong_caption_text
from .geometry import _bbox_area, _bbox_axis_overlap_ratio, _bbox_overlap_in_first, _bbox_overlap_in_smaller, _bbox_union_many
from .line_layout import _line_effective_height
from .models import _DeferredFilledRegion, _LineItem, _PageSource
from .table_annotations import _table_caption_candidates


def _filled_faces(source: _PageSource, em: float) -> list[BBox]:
    """合并同轮廓的填充和描边，兼容一次绘制及分开绘制的 PDF 对象。"""
    outlines: dict[int, list[BBox]] = defaultdict(list)
    fills: list[PDFPathInfo] = []
    for path in source.path_infos:
        box = path.bbox
        if path.form_depth or path.rectangle_bboxes or path.segment_count < 6 or min(box[2] - box[0], box[3] - box[1]) < 2 * em:
            continue
        if path.fill_visible:
            fills.append(path)
        elif path.stroke_visible:
            outlines[path.segment_count].append(box)
    if len(fills) < 4:
        return []
    for boxes in outlines.values():
        boxes.sort()

    faces = []
    tolerance = 0.15 * em
    for fill in fills:
        box = fill.bbox
        if fill.stroke_visible:
            faces.append(box)
            continue
        possible = outlines.get(fill.segment_count, [])
        start = bisect_left(possible, (box[0] - tolerance,))
        for offset in range(start, len(possible)):
            stroke = possible[offset]
            if stroke[0] > box[0] + tolerance:
                break
            if max(abs(a - b) for a, b in zip(box, stroke)) <= tolerance:
                faces.append(_bbox_union_many([box, stroke]))
                break
    return faces


def _accepted_face_labels(box: BBox, faces: list[BBox], lines: list[_LineItem]) -> tuple[int, list[_LineItem]]:
    """统计独立标签，并保留完整位于某一填色面的源行证据。"""
    labeled = set()
    members = []
    for line in lines:
        if _bbox_overlap_in_first(line.bbox, box) <= 0.08:
            continue
        if (
            line.angle != 0
            or line.bbox[2] - line.bbox[0] > 7 * _line_effective_height(line, line.bbox)
            or _is_strong_caption_text(line.text)
        ):
            return 0, []
        owners = [index for index, face in enumerate(faces) if _bbox_overlap_in_first(line.bbox, face) >= 0.98]
        if not owners:
            return 0, []
        if len(owners) == 1 and any(character.isalpha() for character in line.text):
            labeled.add(owners[0])
        members.append(line)
    return len(labeled), members


def _has_competing_table_caption(box: BBox, captions: list[BBox], occupied: list[BBox], em: float) -> bool:
    """只用同栏紧邻、间隔内无其他容器的明确表题阻止分面图认领。"""
    for caption in captions:
        if not 0 <= box[1] - caption[3] <= 3 * em or _bbox_axis_overlap_ratio(box, caption, axis="x") < 0.65:
            continue
        gap = (max(box[0], caption[0]), caption[3], min(box[2], caption[2]), box[1])
        if not any(_bbox_overlap_in_first(gap, barrier) >= 0.3 for barrier in occupied):
            return True
    return False


def detect_labeled_filled_regions(
    source: _PageSource,
    lines: list[_LineItem],
    occupied: list[BBox],
    em: float,
    *,
    all_lines: list[_LineItem] | None = None,
    already_claimed: set[int] | frozenset[int] = frozenset(),
) -> list[_DeferredFilledRegion]:
    """用多个相接轮廓及分散标签提出独立图核，保留已确认图表和框外正文。"""
    if len(source.path_infos) < 4 or len(lines) < 4:
        return []
    # 连通计算复用原生路径分组。大面之间仅接受轮廓量级的缝隙，不跨字级间距串接图体。
    faces = _filled_faces(source, em)
    if len(faces) < 4:
        return []
    # 分组只读取 bbox，复用原生距离计算，无需扩大原始 Path 的几何合同。
    groups = get_native().isolated_path_groups(faces, em / 2)
    if groups is None:
        return []
    output = []
    page_area = source.page_size[0] * source.page_size[1]
    occupied = occupied + source.image_bboxes + source.signature_bboxes
    captions = [
        bbox
        for line, bbox in _table_caption_candidates(source.lines, source.page_size, 0, em)
        if line.angle == 0 and _is_strong_caption_text(line.text)
    ]
    for group in groups:
        if len(group) < 4:
            continue
        component = [faces[index] for index in group]
        box = _bbox_union_many(component)
        if (
            min(box[2] - box[0], box[3] - box[1]) < 5 * em
            or _bbox_area(box) > 0.28 * page_area
            or any(_bbox_overlap_in_smaller(box, other) >= 0.25 for other in occupied)
            or any(
                _bbox_overlap_in_smaller(face, other) > 0.65
                for index, face in enumerate(component)
                for other in component[index + 1 :]
            )
            or _has_competing_table_caption(box, captions, occupied, em)
        ):
            continue
        count, _ = _accepted_face_labels(box, component, lines)
        if count < max(4, len(group) / 2):
            continue
        count, members = _accepted_face_labels(box, component, all_lines if all_lines is not None else lines)
        if count < max(4, len(group) / 2) or any(
            line.source_index in already_claimed or line.semantic_type not in {None, "header", "footer"} for line in members
        ):
            continue
        output.append(_DeferredFilledRegion(box, tuple(replace(line, chars=[]) for line in members)))
    return output
