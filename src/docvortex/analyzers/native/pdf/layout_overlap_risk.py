"""按角色冲突严重度与频次度量独立顶层框之间的空间争用。"""

from collections import defaultdict
import re

from .geometry import _bbox_area, _bbox_axis_overlap_ratio, _bbox_overlap_in_first, _bbox_overlap_in_smaller, _coerce_bbox
from .layout_risk import _block_text, _line_boxes

# 各类重叠在空间碰撞域内分别按严重度与频次分级，域内上限保持 60 分。
MAX_OVERLAP_POINTS = 60
MAX_MINOR_OVERLAP_POINTS = 9
_ROLES = {
    "text": "text",
    "paragraph_title": "title",
    "doc_title": "title",
    "image": "image",
    "chart": "image",
    "table": "table",
    "equation": "equation",
}
_WORD = re.compile(r"[^\W\d_]{2,}")
_OVERLAP_WEIGHTS = {
    "equation_text": (18, 8),
    "image_text": (10, 6),
    "title_text": (10, 6),
    "text_in_text": (8, 5),
}


def _declared_child(child, parent, parent_box):
    """原生注释或结构树已明确挂到同一主体的成员可以与该主体重叠。"""
    if (
        child.get("_native_annotation_parent_bbox") is not None
        and _coerce_bbox(child["_native_annotation_parent_bbox"]) == parent_box
    ):
        return True
    marker = child.get("_pdf_structure") or {}
    parent_id = (parent.get("_pdf_structure") or {}).get("node_id")
    return parent_id is not None and parent_id in (marker.get("visual_parent"), marker.get("inferred_visual_parent"))


def _overlap_strength(first, second):
    """按角色判断两框是否争用版面，返回类型及有效侵入比例。"""
    role, box, block = first
    other_role, other_box, other = second
    coverage = _bbox_overlap_in_smaller(box, other_box)
    if coverage < 0.20:
        return None
    if _declared_child(block, other, other_box) or _declared_child(other, block, box):
        return None
    if role == other_role == "text":
        # 两个有实际段落内容的正文框近乎包含；Tagged PDF 的整段可能只有一个逻辑行框。
        a, b = sorted((_bbox_area(box), _bbox_area(other_box)))
        if coverage >= 0.85 and a >= 0.15 * b and min(len(_block_text(block).strip()), len(_block_text(other).strip())) >= 12:
            return "text_in_text", coverage
    elif {role, other_role} == {"image", "table"}:
        # 图表都应有明显部分被侵占且各自延伸到交集外；完整位于表格单元内的图可正常交付。
        if coverage < 0.90 and min(_bbox_overlap_in_first(box, other_box), _bbox_overlap_in_first(other_box, box)) >= 0.10:
            return "image_table", coverage
    elif role == "text" or other_role == "text":
        if other_role == "text":
            box, block, other_box, other = other_box, other, box, block
            other_role = role
        if other_role not in {"title", "image", "equation"}:
            return None
        text = _block_text(block).strip()
        if not text or (other_role == "equation" and not _WORD.search(text)):
            return None
        # 独立正文可以完整排在纯图片底板上，仅凭包含关系不能认定同一文字已进入裁图。
        if other_role == "image" and _bbox_overlap_in_first(box, other_box) >= 0.95 and not _block_text(other).strip():
            return None
        # 段落外框可能包着绕排留白。缺少正文行时不把整段外框当成一行。
        axis = "x" if block.get("angle") in (90, 270) else "y"
        line_share = max(
            (
                _bbox_overlap_in_first(line, other_box)
                for line in _line_boxes(block)
                if _bbox_axis_overlap_ratio(line, other_box, axis=axis) >= 0.5
            ),
            default=0.0,
        )
        strength = min(coverage, line_share)
        if strength >= 0.20:
            return f"{other_role}_text", strength
    return None


def _kind_overlap_points(kind: str, strengths: list[float]) -> int:
    """按冲突类型、最强侵入比例与受控频次递增计算单类重叠扣分。"""
    peak = max(strengths)
    count = len(strengths)
    if kind == "image_table":
        raw = 25 if peak >= 0.45 else round(12 * peak)
    else:
        base, step = _OVERLAP_WEIGHTS[kind]
        raw = round(base * peak) + step * min(count - 1, 4)
    return min(MAX_OVERLAP_POINTS, raw)


def overlap_risk(blocks) -> tuple[int, str] | None:
    """汇总整页独立顶层块的角色重叠，跨类型采用 Noisy-OR 合成并返回归因。"""
    regions = []
    for block in blocks:
        role = _ROLES.get(block.get("type"))
        box = _coerce_bbox(block.get("bbox")) if role else None
        if box is None or (role == "image" and min(box[2] - box[0], box[3] - box[1]) >= 0.94):
            continue
        regions.append((role, box, block))
    regions.sort(key=lambda r: r[1][1])
    by_kind: dict[str, list[float]] = defaultdict(list)
    for i, first in enumerate(regions):
        left, _, right, bottom = first[1]
        for j in range(i + 1, len(regions)):
            second = regions[j]
            x, y, end, _ = second[1]
            if y >= bottom:
                break
            if x >= right or end <= left:
                continue
            if evidence := _overlap_strength(first, second):
                kind, strength = evidence
                by_kind[kind].append(strength)
    if not by_kind:
        return None
    scored: list[tuple[int, str, int, float]] = []
    survived = 1.0
    for kind, strengths in by_kind.items():
        pts = _kind_overlap_points(kind, strengths)
        if pts <= 0:
            continue
        scored.append((pts, kind, len(strengths), max(strengths)))
        survived *= 1.0 - pts / 100.0
    if not scored:
        return None
    points = max(1, min(MAX_OVERLAP_POINTS, round(100.0 * (1.0 - survived))))
    scored.sort(key=lambda item: (-item[0], item[1]))
    summary = "、".join(f"{kind}×{count}({peak:.0%})" for _, kind, count, peak in scored)
    return points, f"顶层块跨角色重叠：{summary}"


def minor_overlap_points(blocks) -> int:
    """兼容旧调用方，返回整页顶层块角色重叠扣分。"""
    risk = overlap_risk(blocks)
    return risk[0] if risk is not None else 0
