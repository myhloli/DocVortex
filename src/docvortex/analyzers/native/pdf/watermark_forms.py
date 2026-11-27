"""阻止已排除的装饰位图借顶层 Form 重新成为内容。"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from itertools import pairwise
import re
from statistics import median

from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_union_many,
    _clip_bbox,
    _transform_axis_lines,
)


def discarded_raster_forms(source, image_infos, smooth_ids, repeat_ids):
    """用同页图像放置编号确认近整页 Form 的可见成员全部是已排除位图。"""
    discarded = smooth_ids | repeat_ids
    area = source.page_size[0] * source.page_size[1]
    placements = Counter(image.form_raster_id for image in image_infos if image.form_raster_id is not None)
    roots = [form for form in source.form_infos if form.parent_id is None]
    boxes = Counter(form.bbox for form in roots)
    result = set()
    for form in roots:
        owned = set(form.image_source_ids)
        if (
            not form.structure_valid
            or form.text_indices
            or form.path_indices
            or form.has_untracked_content
            or boxes[form.bbox] != 1
            or _bbox_area(form.bbox) < 0.8 * area
            or not owned
            or len(owned) != len(form.image_source_ids)
            or not owned <= discarded
            or any(placements[item] != 1 for item in owned)
        ):
            continue
        # 未声明时还需跨页重复与整页平滑底板同时佐证，防止仅凭重复吞掉实体扫描。
        if form.declared_watermark or (owned & repeat_ids and owned & smooth_ids):
            result.add(form.instance_id)
    return frozenset(result)


def outlined_grid_crops(source, covered):
    """为没有可复制文字的封闭网格保留整块图面，避免只交付分散的轮廓公式。"""
    from .table_rules import _connected_rule_grid_components

    if not source.discarded_raster_forms or len(source.path_infos) < 20:
        return []
    heights = [line.effective_height for line in source.lines if line.effective_height > 0]
    if not heights:
        return []
    em = median(heights)
    axis_lines = _transform_axis_lines(source.drawing_lines, source.page_size, 0)
    vertical = [line for line in axis_lines if line.orientation == "vertical"]
    tolerance = max(1.5, 0.3 * em)
    results = []
    for rules in _connected_rule_grid_components(axis_lines, em):
        if len(rules) < 4:
            continue
        box = _bbox_union_many([rule.bbox for rule in rules])
        if any(_bbox_overlap_in_first(box, bounds) >= 0.9 for bounds in covered):
            continue
        if any(_bbox_overlap_in_first(line.bbox, box) >= 0.8 for line in source.lines if line.text.strip()):
            continue
        rails = sorted(
            (
                rule
                for rule in vertical
                if rule.bbox[1] <= box[1] + tolerance
                and rule.bbox[3] >= box[3] - tolerance
                and box[0] - tolerance <= _bbox_center_x(rule.bbox) <= box[2] + tolerance
            ),
            key=lambda rule: _bbox_center_x(rule.bbox),
        )
        xs = [_bbox_center_x(rule.bbox) for rule in rails]
        if (
            len(xs) < 4
            or abs(xs[0] - box[0]) > tolerance
            or abs(xs[-1] - box[2]) > tolerance
            or min(b - a for a, b in zip(xs, xs[1:])) < 2 * em
        ):
            continue
        ys = [_bbox_center_y(rule.bbox) for rule in rules]
        marks = Counter(
            (bisect_right(xs, _bbox_center_x(path.bbox)), bisect_right(ys, _bbox_center_y(path.bbox)))
            for path in source.path_infos
            if path.fill_visible
            and path.segment_count >= 4
            and 0.1 <= path.bbox[2] - path.bbox[0] <= 2.5 * em
            and 0.1 <= path.bbox[3] - path.bbox[1] <= 2.5 * em
            and _bbox_overlap_in_first(path.bbox, box) >= 0.95
        )
        used = {(x, y) for (x, y), count in marks.items() if 0 < x < len(xs) and 0 < y < len(ys) and count >= 2}
        if len({x for x, _ in used}) < 3 or len({y for _, y in used}) < 3:
            continue
        crop = _clip_bbox(_bbox_union_many([box, *(rail.bbox for rail in rails)]), source.page_size)
        if crop is not None:
            results.append({"type": "image", "bbox": crop, "angle": 0, "content": ""})
    return results


def carries_filled_prose_panel(source, candidate, lines, em):
    """用同一填色矩形内三行齐边自然句判定普通文字面板，独立矢量成分继续保留。"""
    for path in source.path_infos:
        if not path.fill_visible or path.form_depth:
            continue
        for box in path.rectangle_bboxes:
            width = box[2] - box[0]
            if (
                width < 13 * em
                or box[3] - box[1] < 3.5 * em
                or abs(box[0] - candidate[0]) > em
                or abs(box[2] - candidate[2]) > em
                or _bbox_overlap_in_first(box, candidate) < 0.9
            ):
                continue
            if any(
                not mark.rectangle_bboxes
                and mark.segment_count >= 2
                and min(mark.bbox[2] - mark.bbox[0], mark.bbox[3] - mark.bbox[1]) >= 2 * em
                and _bbox_overlap_in_first(mark.bbox, box) >= 0.8
                # 独立描边可能用四段线画同一底板的外框，边缘吻合时不当成内部矢量。
                and not (
                    mark.stroke_visible
                    and not mark.fill_visible
                    and mark.segment_count == 5
                    and max(abs(a - b) for a, b in zip(mark.bbox, box, strict=True)) <= 0.2 * em
                )
                for mark in source.path_infos
            ):
                continue
            paragraphs = sorted(
                (
                    line
                    for line in lines
                    if line.angle == 0
                    and line.font_signature is not None
                    and line.bbox[2] - line.bbox[0] >= 0.72 * width
                    and _bbox_overlap_in_first(line.bbox, box) >= 0.95
                    and len(re.findall(r"[A-Za-z]{2,}", line.text)) >= 5
                ),
                key=lambda line: line.bbox[1],
            )
            consecutive = 0
            for previous, following in pairwise(paragraphs):
                if (
                    previous.font_signature == following.font_signature
                    and abs(previous.bbox[0] - following.bbox[0]) <= 0.3 * em
                    and abs(previous.bbox[2] - following.bbox[2]) <= 0.55 * em
                    and 0.85 * em <= following.bbox[1] - previous.bbox[1] <= 1.65 * em
                ):
                    consecutive += 1
                    if consecutive >= 2:
                        return True
                else:
                    consecutive = 0
    return False


_NUMBERED_TEXT = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s*([A-Za-z].+)?\s*$")
_NUMBERED_BODY = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-Za-z].+)\s*$")
_RELATION_SIGNS = frozenset("=≈≃≅≠≤≥<>")
_OPERATORS = _RELATION_SIGNS | frozenset("+−–±×÷∓√∑∫")
_MATH_WORDS = {"sin", "cos", "tan", "log", "exp", "lim", "max", "min"}


def _mark_non_title(line):
    """禁止再晋升真实题项、目录或局部数学符号；正文与公式后续仍按现有流程装配。"""
    line.title_suppressed = True
    if line.semantic_type == "paragraph_title":
        line.semantic_type = None
        line.structural_title = line.explicit_section_title = False
        line.title_band_id = None


def _numbered_stems(lines):
    """收集可见编号和同视觉行的正文，兼容 PDF 拆开的编号与首行。"""
    stems = []
    for line in lines:
        full = _NUMBERED_BODY.match(line.text)
        if full:
            number, text, body = full[1], full[2], line
        else:
            marker = _NUMBERED_TEXT.fullmatch(line.text)
            if marker is None or marker[2] is not None or not line.text.strip().endswith("."):
                continue
            same_row = [
                other
                for other in lines
                if other is not line
                and line.visual_row_id is not None
                and other.visual_row_id == line.visual_row_id
                and 0 <= other.bbox[0] - line.bbox[2] <= 3 * line.effective_height
                and _bbox_axis_overlap_ratio(line.bbox, other.bbox, axis="y") >= 0.8
                and len(re.findall(r"[A-Za-z]{2,}", other.text)) >= 4
            ]
            if not same_row:
                continue
            number, body = marker[1], min(same_row, key=lambda n: n.bbox[0])
            text = body.text
        if text.isupper():
            continue
        parts = number.split(".")
        stems.append((parts[:-1], int(parts[-1]), line, body, text))
    return stems


def protect_native_non_titles(source):
    """用明确栏框、序号链及悬挂续行将正文题项与真正章节分开。"""
    lines = [line for line in source.lines if line.angle == 0 and line.effective_height > 0 and line.text.strip()]
    if not lines:
        return
    em = median(line.effective_height for line in lines)
    width, height = source.page_size
    narrow_panels = [
        box
        for path in source.path_infos
        if path.fill_visible and not path.form_depth
        for box in path.rectangle_bboxes
        if box[2] - box[0] <= 0.38 * width and box[3] - box[1] >= 0.2 * height
    ]
    stems = _numbered_stems(lines)
    chains = []
    for item in sorted(stems, key=lambda stem: (stem[2].bbox[1], stem[2].bbox[0])):
        prefix, number, line, body, _ = item
        match = next(
            (
                chain
                for chain in reversed(chains)
                if chain[-1][0] == prefix
                and chain[-1][1] + 1 == number
                and abs(chain[-1][2].bbox[0] - line.bbox[0]) <= 0.55 * em
                and abs(chain[-1][3].effective_height - body.effective_height) <= 0.2 * em
            ),
            None,
        )
        if match is None:
            chains.append([item])
        else:
            match.append(item)
    for chain in chains:
        if len(chain) < 3:
            continue
        bounds = [body.bbox for _, _, _, body, _ in chain]
        contents = (
            len(chain) >= 5
            and any(all(_bbox_overlap_in_first(bound, panel) >= 0.95 for bound in bounds) for panel in narrow_panels)
            and all(0.9 * em <= after[1] - before[1] <= 3.6 * em for before, after in pairwise(bounds))
        )
        if not contents:
            prose_count = sum(len(re.findall(r"[A-Za-z]{2,}", text)) >= 6 for _, _, _, _, text in chain)
            if prose_count < max(2, len(chain) / 2):
                continue
            hanging = sum(
                any(
                    candidate is not body
                    and candidate.font_signature == body.font_signature
                    and 0.6 * em <= candidate.bbox[0] - line.bbox[0] <= 3.6 * em
                    and -0.18 * em <= candidate.bbox[1] - body.bbox[3] <= 0.95 * em
                    and candidate.bbox[1] - body.bbox[1] >= 0.55 * em
                    and len(re.findall(r"[A-Za-z]{2,}", candidate.text)) >= 4
                    for candidate in lines
                )
                for _, _, line, body, _ in chain
            )
            if hanging < 2:
                continue
        for _, _, line, body, _ in chain:
            _mark_non_title(line)
            _mark_non_title(body)
    protect_equation_fragments(lines)


def protect_equation_fragments(lines, equations=(), *, source_lines=None):
    """利用相邻运算符及已定公式框保护短符号，以免原生公式碎片变成章节。"""
    anchors = [
        part
        for part in (lines if source_lines is None else source_lines)
        if part.angle == 0 and any(sign in part.text for sign in _OPERATORS)
    ]
    for line in lines:
        text = line.text.strip()
        em = line.effective_height
        if (
            line.angle
            or em <= 0
            or line.semantic_type not in {None, "paragraph_title"}
            or not text
            or len(text) > 36
            or any(len(word) > 2 and word.lower() not in _MATH_WORDS for word in re.findall(r"[A-Za-z]+", text))
            or re.search(r"[\u3400-\u9fff]", text)
        ):
            continue
        box = line.bbox
        if (
            any(sign in text for sign in _RELATION_SIGNS)
            or any(_bbox_overlap_in_first(box, region) >= 0.6 for region in equations)
            or any(
                part is not line
                and abs(_bbox_center_y(box) - _bbox_center_y(part.bbox)) <= 1.45 * em
                and max(part.bbox[0] - box[2], box[0] - part.bbox[2], 0) <= 1.4 * em
                for part in anchors
            )
        ):
            _mark_non_title(line)
