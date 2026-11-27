"""合并已由源位图证实的阴影或卡面，同时保留相邻印刷文字。"""

from collections import Counter

from .geometry import _bbox_area, _bbox_axis_overlap_ratio, _bbox_overlap_in_first, _bbox_union_many


def _clip_shadow_around_print(outer, inner, ink, foreign, paths):
    """让只覆盖外缘的印刷标签保持在图外，原图及其余阴影范围原样保留。"""
    crop = list(outer)
    for line in foreign:
        decorations = [
            info.bbox
            for info in paths
            if info.fill_visible
            and info.fill_rgba is not None
            and info.fill_rgba[3] >= 192
            and _bbox_area(info.bbox) <= min(8 * _bbox_area(line), 0.18 * _bbox_area(outer))
            and _bbox_overlap_in_first(line, info.bbox) >= 0.9
            and _bbox_overlap_in_first(info.bbox, inner) <= 0.01
        ]
        obstacle = max(decorations, key=_bbox_area, default=line)
        if _bbox_overlap_in_first(obstacle, outer) <= 0:
            continue
        sides = (
            (obstacle[2] - crop[0], 0, obstacle[2], obstacle[2] <= inner[0]),
            (obstacle[3] - crop[1], 1, obstacle[3], obstacle[3] <= inner[1]),
            (crop[2] - obstacle[0], 2, obstacle[0], obstacle[0] >= inner[2]),
            (crop[3] - obstacle[1], 3, obstacle[1], obstacle[1] >= inner[3]),
        )
        choices = [(loss, side, point) for loss, side, point, separate in sides if separate and loss >= 0]
        if not choices:
            return None
        _, side, point = min(choices)
        crop[side] = point
    candidate = tuple(crop)
    return candidate if _bbox_overlap_in_first(ink, candidate) >= 0.96 else None


def merge_raster_composites(source, raster_bboxes):
    """用已确认的源图层生成一幅裁片，保持文字归属及未受影响的位图放置。"""
    current = set(raster_bboxes)
    consumed = set()
    replacement = {}
    card_labels = {}
    lines = [line.ink_bbox or line.bbox for line in source.lines if line.text.strip()]
    for info in source.raster_composite_infos:
        outer = info.bbox
        if info.raster_pair is not None:
            inner, crop = info.raster_pair
            if crop != outer:
                continue
            foreign = any(
                _bbox_overlap_in_first(box, crop) >= 0.05 and _bbox_overlap_in_first(box, inner) < 0.6 for box in lines
            )
            if (
                outer in current
                and inner in current
                and outer not in consumed
                and inner not in consumed
                and inner not in replacement
                and not foreign
            ):
                replacement[outer] = crop
                consumed.add(inner)
            continue
        members = tuple(box for box in info.composite_inner_bboxes if box in current and box not in consumed)
        if outer in consumed or outer not in current or not (members or info.shadow_ink_bbox is None):
            continue
        shadows = info.shadow_ink_bbox is not None
        inside = members if shadows else info.composite_inner_bboxes
        vicinity = outer
        if shadows:
            # 题签的独立不透明底板可能先接触阴影边缘，字形则留在外框以外。
            margin = 0.05 * min(outer[2] - outer[0], outer[3] - outer[1])
            vicinity = (outer[0] - margin, outer[1] - margin, outer[2] + margin, outer[3] + margin)
        foreign = [
            line
            for line in lines
            if _bbox_overlap_in_first(line, vicinity) >= 0.05
            and not any(_bbox_overlap_in_first(line, box) >= 0.6 for box in inside)
        ]
        crop = outer
        if not shadows:
            # 同一排的多行标签垂直居中，使用完整字框以免首行偏移拆散卡片。
            labels = [line for line in lines if any(_bbox_overlap_in_first(line, box) >= 0.9 for box in inside)]
            if foreign or not labels:
                continue
            card_labels[outer] = _bbox_union_many(labels)
        elif foreign:
            crop = _clip_shadow_around_print(outer, members[0], info.shadow_ink_bbox, foreign, source.path_infos)
            if crop is None:
                continue
        replacement[outer] = crop
        consumed.update(members)
    return [replacement.get(box, box) for box in raster_bboxes if box not in consumed], card_labels


def prepare_backdrop_titles(source, images):
    """在仍保存源行的阶段给延伸底图记下能完整恢复的栏头。"""
    from dataclasses import replace
    from statistics import median

    from .graphics import _image_members_to_content
    from .text_assembly.assembly import _build_text_blocks

    originals = {tuple(img["bbox"]): img for img in images}
    for info in source.raster_composite_infos:
        if info.raster_pair is None or info.raster_pair[1] == info.bbox:
            continue
        inner, crop = info.raster_pair
        if inner not in originals or info.bbox not in originals:
            continue
        outer = originals[info.bbox]
        labels = [
            line
            for line in source.lines
            if line.angle == 0
            and line.text.strip()
            and (box := line.ink_bbox or line.bbox)[3] < crop[1]
            and _bbox_overlap_in_first(box, info.bbox) >= 0.95
        ]
        if not labels or " ".join(_image_members_to_content(labels, source.page_size).split()) != " ".join(
            outer["content"].split()
        ):
            continue
        heights = [
            line.effective_height
            for line in source.lines
            if line.effective_height > 0 and _bbox_overlap_in_first(line.bbox, info.bbox) <= 0.05
        ]
        body_height = median(heights) if heights else 0
        rows = [
            replace(
                line,
                semantic_type="paragraph_title"
                if body_height and line.effective_height >= 1.4 * body_height and (line.dominant_font_weight or 400) >= 600
                else "text",
            )
            for line in labels
        ]
        title_blocks = _build_text_blocks(rows, [], source.page_size)
        if " ".join(" ".join(block["content"].split()) for block in title_blocks) == " ".join(outer["content"].split()):
            outer["_deferred_backdrop_titles"] = (inner, outer["content"], title_blocks)


def materialize_backdrop_titles(blocks):
    """原容器和正文都排序完后，用已证实的独立栏头投影替换延伸底图。"""
    image_bboxes = {tuple(block["bbox"]) for block in blocks if block["type"] == "image"}
    owners = {tuple(box) for block in blocks if (box := block.get("_native_annotation_parent_bbox")) is not None}
    projected = []
    for block in blocks:
        proposal = block.get("_deferred_backdrop_titles")
        if proposal is not None:
            inner, old_content, labels = proposal
            if inner in image_bboxes and block["content"] == old_content and tuple(block["bbox"]) not in owners:
                projected.extend(labels)
                continue
        projected.append(block)
    return projected


def card_row_groups(blocks, excluded_indices):
    """只用证实的卡面原生标签对齐同行，按从左到右块序加入既有阅读排序组。"""
    cards = [
        (index, block)
        for index, block in enumerate(blocks)
        if index not in excluded_indices and block.get("_raster_card_label_bbox") is not None
    ]
    rows = []
    for card in sorted(cards, key=lambda item: item[1]["_raster_card_label_bbox"][1]):
        label = card[1]["_raster_card_label_bbox"]
        if not rows or _bbox_axis_overlap_ratio(label, rows[-1][0][1]["_raster_card_label_bbox"], axis="y") < 0.5:
            rows.append([])
        rows[-1].append(card)
    groups = []
    for row in rows:
        peers = []
        for card in sorted(row, key=lambda item: item[1]["bbox"][0]):
            if peers:
                prior = peers[-1][1]["bbox"]
                current = card[1]["bbox"]
                width = min(prior[2] - prior[0], current[2] - current[0])
                gap = current[0] - prior[2]
                if not -0.05 * width <= gap <= 0.5 * width:
                    if len(peers) >= 2:
                        groups.append(peers)
                    peers = []
            peers.append(card)
        if len(peers) >= 2:
            groups.append(peers)
    return groups


def occluded_raster_boxes(source, plain):
    """求已通过绘制核验且无独立原生文字归属的被遮层。"""
    removed = set()
    for info in sorted(source.raster_composite_infos, key=lambda info: -_bbox_area(info.bbox)):
        if not info.occluded_inner_bboxes or info.bbox not in plain or info.bbox in removed:
            continue
        for inner in info.occluded_inner_bboxes:
            if inner in plain and not any(
                _bbox_overlap_in_first(line.ink_bbox or line.bbox, inner) > 0 for line in source.lines if line.text.strip()
            ):
                removed.add(inner)
    return removed


def remove_occluded_rasters(source, candidate_specs):
    """两层均准入为独立裁片时，仅移除无独立原生文字归属的被遮层。"""
    plain = {bbox for bbox, row_id in candidate_specs if row_id is None}
    removed = occluded_raster_boxes(source, plain)
    available = plain - removed - {bbox for bbox, row in candidate_specs if row is not None}
    repaints = [
        (inner, info.bbox)
        for info in source.raster_composite_infos
        for inner in info.repainted_inner_bboxes
        if inner in available and info.bbox in available
    ]
    if not repaints:
        return [(bbox, row) for bbox, row in candidate_specs if bbox not in removed or row is not None]
    occurrences = Counter(box for pair in repaints for box in pair)
    replacement = {}
    for inner, outer in repaints:
        if occurrences[inner] != 1 or occurrences[outer] != 1:
            continue
        union = _bbox_union_many((inner, outer))
        if any(_bbox_overlap_in_first(line.ink_bbox or line.bbox, union) > 0 for line in source.lines if line.text.strip()):
            continue
        replacement[outer] = union
        removed.add(inner)
    return [
        (replacement.get(bbox, bbox) if row is None else bbox, row)
        for bbox, row in candidate_specs
        if bbox not in removed or row is not None
    ]
