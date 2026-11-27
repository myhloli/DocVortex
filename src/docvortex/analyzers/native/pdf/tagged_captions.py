"""在原结构没有声明归属时，为有唯一近邻图体的印刷图题补充投影关系。"""

from __future__ import annotations

from collections import Counter, defaultdict
import re

from ....content.spans import inline_span_plain_text
from .annotation_text import _is_strong_caption_text
from .geometry import _bbox_axis_overlap_ratio


_NUMBERED_FIGURE_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure)?\.?\s*|图\s*)\d+(?:(?:\s*\.\s*|[-–])\d+)*(?:[.．:：]\s+|\s*[:：]\s*)", re.I
)
_SUBFIGURE_LABEL = re.compile(r"\(([a-z])\.?\)", re.I)


def numbered_figure_prefix(text: str) -> re.Match[str] | None:
    """返回以印刷分隔符结束的独立图号，供读序证据和同图续注共用。"""
    return _NUMBERED_FIGURE_CAPTION.match(text)


def _caption_neighbour(caption, role, figures, blocks, position):
    """在题注的垂直投影内选择图体，并用中间实物及同高并排图否决歧义。"""
    q = caption["bbox"]
    height = min(q[3] - q[1], 0.018)
    options = []
    for figure in figures:
        p = figure["bbox"]
        gap = max(q[1] - p[3], p[1] - q[3])
        if not 0 <= gap <= 2 * height or _bbox_axis_overlap_ratio(q, p, axis="x") < 0.70:
            continue
        # 题注覆盖两个并排子图时，两者都进入竞争，不能只检索最居中的一个。
        options.append((gap, figure))
    if not options:
        return None
    options.sort(key=lambda x: x[0])
    gap, figure = options[0]
    if abs(position[id(figure)] - position[id(caption)]) != 1:
        # 将题注嵌入沉底的结构图会使其越过小节或正文；保留源序直到整组位置可证。
        return None
    p = figure["bbox"]
    if len(options) > 1 and options[1][0] - gap <= 0.5 * height:
        return None
    if abs((q[0] + q[2]) - (p[0] + p[2])) > 0.36 * max(q[2] - q[0], p[2] - p[0]):
        return None
    below = q[1] >= p[3]
    if role == "P" and not below:
        return None
    top, bottom = (p[3], q[1]) if below else (q[3], p[1])
    left, right = max(q[0], p[0]), min(q[2], p[2])
    for block in blocks:
        if block is caption or block is figure or not block.get("bbox"):
            continue
        x, y, u, v = block["bbox"]
        if block["type"] == "image":
            # 多子图的短共题可能完全在中间图投影内，近邻等高图也是父对象竞争证据。
            panel_gap = max(x - p[2], p[0] - u)
            if 0 <= panel_gap <= 0.2 * min(u - x, p[2] - p[0]) and max(abs(y - p[1]), abs(v - p[3])) <= 0.15 * (p[3] - p[1]):
                return None
        if min(right, u) > max(left, x) and min(bottom, v) > max(top, y):
            return None
        if (
            block.get("type") in {"image", "table", "equation", "code"}
            and _bbox_axis_overlap_ratio(p, block["bbox"], axis="y") >= 0.5
            and min(q[2], u) - max(q[0], x) > 0.20 * (q[2] - q[0])
        ):
            return None
    return figure


def _bind_caption(block: dict, parent_id: str) -> None:
    """将图题原块挂到图体，保留字形、样式、链接与真实源节点。"""
    block["type"] = "image_caption"
    block["_pdf_structure"]["inferred_visual_parent"] = parent_id


def _next_subfigure_index(text: str, expected: int | None = None) -> int | None:
    """识别同一字母编号序列，允许一行写多个连续子图标签。"""
    text = text.lstrip()
    if _SUBFIGURE_LABEL.match(text) is None:
        return None
    labels = [ord(label.lower()) for label in _SUBFIGURE_LABEL.findall(text)]
    first = labels[0]
    if (expected is not None and first != expected) or labels != list(range(first, first + len(labels))):
        return None
    return labels[-1] + 1


def _subfigure_line_follows(previous: dict, candidate: dict, roles: dict[str, str]) -> bool:
    """检查紧邻行的垂直距离、大小、中心和原有父子关系。"""
    marker = candidate.get("_pdf_structure", {})
    if (
        candidate["type"] != "text"
        or candidate.get("angle")
        or marker.get("node_id") not in roles
        or marker.get("visual_parent")
        or marker.get("inferred_visual_parent")
        or not (previous_lines := previous.get("lines"))
        or not (candidate_lines := candidate.get("lines"))
    ):
        return False
    previous_box, candidate_box = previous_lines[-1]["bbox"], candidate_lines[0]["bbox"]
    x0, y0, x1, y1 = previous_box
    a0, b0, a1, b1 = candidate_box
    height = y1 - y0
    return (
        height > 0
        and 0 <= b0 - y1 <= 0.55 * height
        and abs(b1 - b0 - height) <= 0.12 * height
        and abs(x0 + x1 - a0 - a1) <= 0.10 * (x1 - x0)
        and _bbox_axis_overlap_ratio(previous_box, candidate_box, axis="x") >= 0.90
    )


def bind_reordered_figure_captions(blocks: list[dict], pairs: list[tuple[dict, dict]], roles: dict[str, str]) -> None:
    """绑定已验逆序的图题，并保留属于同一子图编号序列的连续源行。"""
    positions = {id(block): i for i, block in enumerate(blocks)}
    for figure, lead in pairs:
        parent_id = figure["_pdf_structure"]["node_id"]
        _bind_caption(lead, parent_id)
        lead_text = inline_span_plain_text(lead["content"])
        prefix = numbered_figure_prefix(lead_text)
        expected = _next_subfigure_index(lead_text[prefix.end() :]) if prefix is not None else None
        previous = lead
        for index in range(positions[id(lead)] + 1, len(blocks)):
            candidate = blocks[index]
            if expected is None or not _subfigure_line_follows(previous, candidate, roles):
                break
            expected = _next_subfigure_index(inline_span_plain_text(candidate["content"]), expected)
            if expected is None:
                break
            _bind_caption(candidate, parent_id)
            previous = candidate


def bind_orphan_figure_captions(blocks, roles):
    """保留显式父级和跨页节点，仅将明确的单页编号题注投影到唯一且未占用的结构图。"""
    figures = [b for b in blocks if b["type"] == "image" and b.get("bbox") and "_pdf_structure" in b]
    if not figures:
        return
    markers = [b["_pdf_structure"] for b in blocks if "_pdf_structure" in b]
    existing = {m.get("visual_parent") or m.get("inferred_visual_parent") for m in markers}
    node_counts = Counter(m["node_id"] for m in markers)
    position = {id(block): i for i, block in enumerate(blocks)}
    candidates = defaultdict(list)
    for block in blocks:
        marker = block.get("_pdf_structure", {})
        node = marker.get("node_id")
        role = roles.get(node)
        if not role or node_counts[node] != 1 or block["type"] != "text" or not block.get("bbox"):
            continue
        text = inline_span_plain_text(block["content"])
        if not re.match(r"^\s*(?:fig(?:ure)?[.\s\d]|图\s*\d)", text, re.I) or not _is_strong_caption_text(text):
            continue
        figure = _caption_neighbour(block, role, figures, blocks, position)
        if figure is not None:
            candidates[figure["_pdf_structure"]["node_id"]].append(block)
    for parent, children in candidates.items():
        if len(children) == 1 and parent not in existing and node_counts[parent] == 1:
            _bind_caption(children[0], parent)
