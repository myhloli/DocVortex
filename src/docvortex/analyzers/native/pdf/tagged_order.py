"""用可核对的来源冲突和页内几何证据校正 Tagged PDF 的阅读位置。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
import re
from statistics import median
from typing import TYPE_CHECKING

from ....content.spans import inline_span_plain_text
from .._shared.xycut import sort_entries
from .geometry import _bbox_axis_overlap_ratio, _bbox_overlap_in_smaller, _bbox_union_many
from .tagged_captions import bind_reordered_figure_captions, numbered_figure_prefix

if TYPE_CHECKING:
    from ....document.pdf._document import PDFDocument
    from ....document.pdf.marked_content import MarkedPage


_VISUAL_TYPES = frozenset({"image", "table", "chart"})
_CAPTION_TYPES = frozenset({"text", "caption", "image_caption"})
_TABLE_CAPTION_TYPES = frozenset({"text", "caption", "table_caption"})
_BODY_WIDTH_TYPES = frozenset({"text", "list"})
_SEVERE_ORDER_PROSE_TYPES = frozenset({"text", "paragraph_title", "doc_title", "list"})
_NUMBERED_TABLE_CAPTION = re.compile(
    r"^\s*(?:tab(?:le)?\.?\s*|表\s*)\d+(?:(?:\s*\.\s*|[-–])\d+)*(?:[.．:：]\s+|\s*[:：]\s*)",
    re.I,
)
_BOTTOM_FOOTNOTE_PREFIX = re.compile(r"^\s*(?:\d{1,3}|[*†‡§])[\s.)]\s*\S")


def _marker(block: dict) -> dict:
    """取得可核对父子关系及来源身份的 Tagged 标记。"""
    return block.get("_pdf_structure", {})


def _visual_parent(block: dict) -> str | None:
    """读取已有作者或推断图文关系，以免排序覆盖已经成组的图。"""
    marker = _marker(block)
    return marker.get("visual_parent") or marker.get("inferred_visual_parent")


def _block_text(block: dict) -> str:
    """读取原生或结构阶段的可见文本，兼容未物化的文本块。"""
    content = block.get("content", "")
    return content if isinstance(content, str) else inline_span_plain_text(content)


def _has_numbered_caption(block: dict, roles: dict[str, str]) -> bool:
    """要求独立单页角色及以分隔符结束的印刷编号，排除正文图号引用。"""
    return (
        block["type"] in _CAPTION_TYPES
        and not block.get("angle")
        and _marker(block).get("node_id") in roles
        and not _visual_parent(block)
        and numbered_figure_prefix(_block_text(block)) is not None
    )


def _has_inversion(blocks: list[dict], cap: int, fig: int) -> str | None:
    """核实图在树中越过了位于其下方的同栏正文，支持图在题注上方或下方两种印刷位置。"""
    image, caption = blocks[fig]["bbox"], blocks[cap]["bbox"]
    height = caption[3] - caption[1]
    if _bbox_axis_overlap_ratio(image, caption, axis="x") < 0.85:
        return None
    if 0 <= caption[1] - image[3] <= 2.2 * height:
        # 图在题注正上方，正文位于题注下方。
        direction = "image_above"
        gap = caption[1] - image[3]
        gap_top, gap_bottom = image[3], caption[1]
        prose_floor = caption[3]
        visual_top, visual_bottom = image[3] - height, caption[3]
    elif 0 <= image[1] - caption[3] <= 2.2 * height:
        # 题注在图正上方，正文位于图下方或题注下方。
        direction = "caption_above"
        gap = image[1] - caption[3]
        gap_top, gap_bottom = caption[3], image[1]
        prose_floor = image[3]
        visual_top, visual_bottom = caption[1], image[1] + height
    else:
        return None
    has_intervening_prose = any(
        # 一条单独的小节标题不足以否定作者有意的图文顺序。
        b["type"] in {"text", "list"}
        and b["bbox"][1] >= prose_floor
        and _bbox_axis_overlap_ratio(image, b["bbox"], axis="x") >= 0.35
        for b in blocks[cap + 1 : fig]
    )
    if not has_intervening_prose:
        return None
    if any(
        j not in (cap, fig)
        and _bbox_axis_overlap_ratio(b["bbox"], caption, axis="x") >= 0.25
        and b["bbox"][1] < gap_bottom
        and b["bbox"][3] > gap_top
        for j, b in enumerate(blocks)
    ):
        return None
    for j, b in enumerate(blocks):
        if j == fig or b["type"] not in _VISUAL_TYPES or _bbox_axis_overlap_ratio(b["bbox"], caption, axis="x") < 0.25:
            continue
        if b["bbox"][1] < visual_bottom and b["bbox"][3] >= visual_top:
            return None
        # 若题注另一侧存在更近或距离相近的竞争视觉块，说明归属有歧义，放弃该方向候选。
        opp_gap = (b["bbox"][1] - caption[3]) if direction == "image_above" else (caption[1] - b["bbox"][3])
        if 0 <= opp_gap <= gap * 1.5:
            return None
    return direction


def _figure_inversions(blocks: list[dict], roles: dict[str, str]) -> dict[tuple[int, int], str]:
    """在运行几何排序前查找低成本、无父级竞争的编号图文逆序证据。"""
    if len(blocks) < 3 or not any(b["type"] == "image" and _marker(b) for b in blocks):
        return {}
    if any(not block.get("bbox") for block in blocks):
        return {}
    counts = Counter(_marker(b).get("node_id") for b in blocks)
    owned = {_visual_parent(b) for b in blocks}
    captions = []
    candidates: dict[tuple[int, int], str] = {}
    for i, block in enumerate(blocks):
        node = _marker(block).get("node_id")
        if counts[node] != 1:
            continue
        if _has_numbered_caption(block, roles):
            captions.append(i)
        elif block["type"] == "image" and node and not block.get("angle") and node not in owned:
            for j in captions:
                if (direction := _has_inversion(blocks, j, i)) is not None:
                    candidates[(i, j)] = direction
    by_caption = Counter(caption for _, caption in candidates)
    by_image = Counter(image for image, _ in candidates)
    return {
        pair: direction
        for pair, direction in candidates.items()
        if by_caption[pair[1]] == by_image[pair[0]] == 1
    }


def _reverse_adjacent_above_captions(blocks: list[dict], geometry: list[int]) -> list[tuple[int, int]]:
    """识别物理位于图/表正上方却在相邻结构输出中排在图/表之后的编号题注对。"""
    if len(blocks) < 2 or any(not block.get("bbox") for block in blocks):
        return []
    adjacent_geo = set(zip(geometry, geometry[1:]))
    counts = Counter(_marker(b).get("node_id") for b in blocks if _marker(b).get("node_id"))
    swaps: list[tuple[int, int]] = []
    for vis in range(len(blocks) - 1):
        cap = vis + 1
        visual, caption = blocks[vis], blocks[cap]
        if visual["type"] not in _VISUAL_TYPES or visual.get("angle") or caption.get("angle"):
            continue
        vis_node = _marker(visual).get("node_id")
        cap_node = _marker(caption).get("node_id")
        if not vis_node or not cap_node or counts[vis_node] != 1 or counts[cap_node] != 1:
            continue
        cap_parent = _visual_parent(caption)
        if cap_parent is not None and cap_parent != vis_node:
            continue
        if visual["type"] == "table":
            if caption["type"] not in _TABLE_CAPTION_TYPES or _NUMBERED_TABLE_CAPTION.match(_block_text(caption)) is None:
                continue
        else:
            if caption["type"] not in _CAPTION_TYPES or numbered_figure_prefix(_block_text(caption)) is None:
                continue
        v_box, c_box = visual["bbox"], caption["bbox"]
        c_height = c_box[3] - c_box[1]
        gap = v_box[1] - c_box[3]
        if not (0 <= gap <= max(2.2 * c_height, 0.03)):
            continue
        if _bbox_axis_overlap_ratio(v_box, c_box, axis="x") < 0.70:
            continue
        if (cap, vis) not in adjacent_geo and not (cap_parent == vis_node and (vis, cap) in adjacent_geo):
            continue
        # 两者间隙内不得存在阻隔块；未显式绑定父节点时，题注正上方也不得存在更近或距离相近的竞争图/表。
        if any(
            j not in (vis, cap)
            and _bbox_axis_overlap_ratio(b["bbox"], c_box, axis="x") >= 0.25
            and b["bbox"][1] < v_box[1]
            and b["bbox"][3] > c_box[3]
            for j, b in enumerate(blocks)
        ):
            continue
        if cap_parent != vis_node and any(
            j != vis
            and b["type"] in _VISUAL_TYPES
            and _bbox_axis_overlap_ratio(b["bbox"], c_box, axis="x") >= 0.25
            and 0 <= c_box[1] - b["bbox"][3] <= max(gap * 1.5, 0.005)
            for j, b in enumerate(blocks)
        ):
            continue
        swaps.append((vis, cap))
    return swaps


def _edge_number(text: str, box: list[float]) -> int | None:
    """仅接受位于页边的小块阿拉伯整数。"""
    value = text.strip()
    if 1 <= len(value) <= 5 and value.isascii() and value.isdigit() and (box[1] > 0.90 or box[3] < 0.10):
        return int(value)
    return None


def _folio_candidates(blocks: list[dict]) -> list[tuple[int, int, list[float]]]:
    """提取非旋转页边的孤立阿拉伯数字，等待相邻页的递增证据。"""
    result = []
    for i, block in enumerate(blocks):
        box = block.get("bbox")
        if block["type"] not in {"text", "page_number"} or block.get("angle") or _visual_parent(block) or not box:
            continue
        if (number := _edge_number(_block_text(block), box)) is not None:
            result.append((i, number, box))
    return result


def _source_folios(document: PDFDocument, physical: int) -> list[tuple[int, int, list[float]]]:
    """选页场景只为候选页读取相邻物理页的可见原生边缘数字。"""
    if not 0 <= physical < document.page_count:
        return []
    try:
        width, height = document.page_size(physical)
        lines = document.get_page_lines(physical, visible_only=True)
    except (ValueError, RuntimeError):
        return []
    if min(width, height) <= 0:
        return []
    result = []
    for index, line in enumerate(lines):
        x0, y0, x1, y1 = line["bbox"]
        box = [x0 / width, y0 / height, x1 / width, y1 / height]
        if line["rotation"] or not (box[1] > 0.90 or box[3] < 0.10):
            continue
        text = "".join(span["text"] for span in line["spans"])
        if (number := _edge_number(text, box)) is not None:
            ink = [
                bounds
                for span in line["spans"]
                for char in span["chars"]
                if char["char"].strip() and (bounds := char.get("tight_bbox"))
            ]
            if ink:
                x0, y0, x1, y1 = _bbox_union_many(ink)
                box = [x0 / width, y0 / height, x1 / width, y1 / height]
            result.append((index, number, box))
    return result


def _confirmed_folios(blocks: list[dict], neighbors: list[list[tuple[int, int, list[float]]]]) -> set[int]:
    """要求同一边缘位置有相邻三页连续且无歧义的印刷页码。"""
    return {
        index
        for index, value, box in _folio_candidates(blocks)
        if all(
            sum(
                number == value + offset
                and abs(bounds[0] - box[0]) <= 0.025
                and max(abs(bounds[j] - box[j]) for j in (1, 3)) <= 0.005
                for _, number, bounds in candidates
            )
            == 1
            for offset, candidates in zip((-1, 1), neighbors, strict=True)
        )
    }


def _folio_blocked_body(blocks: list[dict]) -> tuple[int, list[int], list[int]] | None:
    """发现前置作者页码把同栏的下方残余段挤到小节标题前的情况。"""
    if len(blocks) < 5 or blocks[0]["type"] != "text" or _marker(blocks[0]) or len(blocks[0].get("lines") or ()) < 2:
        return None
    if any(not b.get("bbox") or b.get("angle") for b in blocks):
        return None
    authored = [i for i, block in enumerate(blocks) if _marker(block)]
    if len(authored) < 4 or blocks[authored[1]]["type"] != "paragraph_title":
        return None
    folio = authored[0]
    if blocks[folio]["bbox"][1] <= 0.90 or not _folio_candidates([blocks[folio]]):
        return None
    notes = [i for i, block in enumerate(blocks) if not _marker(block) and block["type"] == "page_footnote"]
    if len(authored) + len(notes) + 1 != len(blocks):
        return None
    left, top, right, bottom = blocks[0]["bbox"]
    if bottom - top < 0.035 or sum(blocks[i]["bbox"][3] < top - 0.007 for i in authored[1:]) < 3:
        return None
    # 正文必须沿同一左缘和宽栏连续排版；独立编号、并排栏和窄文字列不能作证。
    body = [0, *authored[1:]]
    for i in body:
        block = blocks[i]
        x0, _, x1, y1 = block["bbox"]
        if block["type"] not in {"text", "paragraph_title"} or abs(x0 - left) > 0.035 or y1 >= blocks[folio]["bbox"][1]:
            return None
        if block["type"] == "text" and min(x1, right) - max(x0, left) < 0.40:
            return None
    return folio, body, notes


def _body_geometry_agrees(blocks: list[dict], geometry: list[int], conflict: tuple[int, list[int], list[int]]) -> bool:
    """只允许两股来源交织，作者段落、残余脚注及所有纵向边界各自稳定。"""
    folio, body, notes = conflict
    body_indices = set(body)
    ordered_body = [i for i in geometry if i in body_indices]
    return (
        [i for i in ordered_body if i != 0] == body[1:]
        and geometry == [*ordered_body, *notes, folio]
        and all(blocks[a]["bbox"][3] <= blocks[b]["bbox"][1] for a, b in pairwise(geometry))
    )


def _point_bbox(box: Sequence[float], size: tuple[float, float]) -> tuple[float, float, float, float]:
    """将归一化坐标转换到 PDF point 绝对坐标。"""
    return (box[0] * size[0], box[1] * size[1], box[2] * size[0], box[3] * size[1])


def sort_tagged_blocks(blocks: Sequence[dict], size: tuple[float, float]) -> list[int]:
    """对 Tagged/混合页块执行受保护的 XYCut++：边栏外置、视觉父子原子化、正文宽中位数保护。"""
    headers: list[dict] = []
    footers: list[dict] = []
    body_indices: list[int] = []
    invalid: list[int] = []

    for i, block in enumerate(blocks):
        box = block.get("bbox")
        if not box:
            invalid.append(i)
            continue
        pbox = _point_bbox(box, size)
        kind = block.get("type")
        if not _visual_parent(block):
            if kind == "header" or (kind == "page_number" and box[3] < 0.12):
                headers.append({"bbox": pbox, "members": [i]})
                continue
            if kind == "footer" or (kind == "page_number" and box[3] >= 0.12):
                footers.append({"bbox": pbox, "members": [i]})
                continue
        body_indices.append(i)

    # 识别正文中唯一可归属的视觉父子组，以并集外框整体参与切分，防止拆散已绑定的图与题注。
    node_counts = Counter(
        node
        for i in body_indices
        if (node := _marker(blocks[i]).get("node_id")) is not None
    )
    owner_by_node = {
        node: i
        for i in body_indices
        if (node := _marker(blocks[i]).get("node_id")) is not None and node_counts[node] == 1
    }
    group_members: dict[int, list[int]] = {}
    grouped_children: set[int] = set()
    for i in body_indices:
        parent_id = _visual_parent(blocks[i])
        if parent_id is not None and (owner_idx := owner_by_node.get(parent_id)) is not None and owner_idx != i:
            group_members.setdefault(owner_idx, [owner_idx]).append(i)
            grouped_children.add(i)

    body_entries: list[dict] = []
    all_body_widths: list[float] = []
    body_text_widths: list[float] = []
    for i in body_indices:
        if i in grouped_children:
            continue
        members = sorted(group_members[i]) if i in group_members else [i]
        pboxes = [_point_bbox(blocks[m]["bbox"], size) for m in members]
        union = pboxes[0] if len(pboxes) == 1 else _bbox_union_many(pboxes)
        body_entries.append({"bbox": union, "members": members})
        width = union[2] - union[0]
        if width > 0:
            all_body_widths.append(width)
            if len(members) == 1:
                block = blocks[i]
                if block.get("type") in _BODY_WIDTH_TYPES and not block.get("angle"):
                    body_text_widths.append(width)

    # 仅用正文宽中位数抵御窄小非正文块下拉；当整页块中位宽本身处于单栏尺度时，不得被短单行居中标注反而拉低。
    root_median_width: float | None = None
    if len(body_text_widths) >= 3:
        text_median = median(body_text_widths)
        all_median = median(all_body_widths) if all_body_widths else None
        if all_median is not None and all_median < 0.5 * size[0]:
            root_median_width = max(text_median, all_median)
        else:
            root_median_width = text_median

    result: list[int] = []
    for entry in sort_entries(headers):
        result.extend(entry["members"])
    for entry in sort_entries(body_entries, root_median_width=root_median_width, min_vertical_gap=1.0):
        result.extend(entry["members"])
    for entry in sort_entries(footers):
        result.extend(entry["members"])
    result.extend(invalid)
    return result


def _sort_page(blocks: list[dict], size: tuple[float, float]) -> list[int]:
    """把归一化模型块恢复到 PDF 点坐标，调用受保护的 Tagged XY-Cut++。"""
    return sort_tagged_blocks(blocks, size)


def _adjacent_folios(pages: list[list[dict]], positions: dict[int, int], document: PDFDocument, physical: int):
    """尽量复用已经解析的邻页，选页才从原件读取缺少的页码证据。"""
    return [
        _folio_candidates(pages[position])
        if (position := positions.get(index)) is not None
        else _source_folios(document, index)
        for index in (physical - 1, physical + 1)
    ]


def _normalize_confirmed_edge_folio(
    blocks: list[dict],
    pages: list[list[dict]],
    positions: dict[int, int],
    document: PDFDocument,
    physical: int,
) -> tuple[list[dict], str] | None:
    """确认页首/页尾的边缘数字为跨三页递增页码时，执行页首底边页码归位或原位标注 page_number。"""
    if not blocks:
        return None
    front = blocks[0]
    if (
        len(blocks) >= 2
        and front.get("bbox")
        and not front.get("angle")
        and _marker(front)
        and front["bbox"][1] > 0.90
        and _folio_candidates([front])
        and all(b.get("bbox") for b in blocks[1:])
    ):
        folio_top = front["bbox"][1]
        if all(b["bbox"][3] <= folio_top or b["bbox"][1] >= 0.90 for b in blocks[1:]):
            neighbors = _adjacent_folios(pages, positions, document, physical)
            if 0 in _confirmed_folios(blocks, neighbors):
                front["type"] = "page_number"
                return [*blocks[1:], front], "front_folio_relocate"

    edge_slots: list[int] = []
    if (
        front.get("type") == "text"
        and front.get("bbox")
        and not front.get("angle")
        and _marker(front)
        and front["bbox"][3] < 0.10
        and _folio_candidates([front])
    ):
        edge_slots.append(0)
    tail_idx = len(blocks) - 1
    tail = blocks[tail_idx]
    if (
        tail.get("type") == "text"
        and tail.get("bbox")
        and not tail.get("angle")
        and _marker(tail)
        and tail["bbox"][1] > 0.90
        and _folio_candidates([tail])
    ):
        edge_slots.append(tail_idx)
    if not edge_slots:
        return None
    neighbors = _adjacent_folios(pages, positions, document, physical)
    confirmed = _confirmed_folios(blocks, neighbors)
    promoted = False
    for slot in edge_slots:
        if slot in confirmed:
            blocks[slot]["type"] = "page_number"
            promoted = True
    return (blocks, "edge_folio_type") if promoted else None


def _is_bottom_footnote(block: dict) -> bool:
    """识别位于页底的编号脚注，避免将引用点内嵌脚注误判为页面级顺序错乱。"""
    box = block.get("bbox")
    if not box or box[1] < 0.78:
        return False
    if block.get("type") == "page_footnote":
        return True
    return block.get("type") == "text" and _BOTTOM_FOOTNOTE_PREFIX.match(_block_text(block)) is not None


def _has_overlapping_trusted_prose(blocks: list[dict], *, threshold: float = 0.05) -> bool:
    """存在跨栏续段或拆段重叠的已标正文时，禁止整页回退几何排序以免破坏跨栏块。"""
    boxes = [
        b["bbox"]
        for b in blocks
        if _marker(b) and b.get("type") in _SEVERE_ORDER_PROSE_TYPES and b.get("bbox") and not b.get("angle")
    ]
    for i in range(len(boxes)):
        a = boxes[i]
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                if _bbox_overlap_in_smaller(a, b) >= threshold:
                    return True
    return False


def _body_prose_indices(blocks: list[dict], *, trusted_only: bool) -> list[int]:
    """提取非边栏、非脚注、非大图内嵌标注且含有效字符的正文/列表下标。"""
    large_images = [
        b["bbox"]
        for b in blocks
        if b.get("type") == "image"
        and b.get("bbox")
        and (b["bbox"][2] - b["bbox"][0]) >= 0.15
        and (b["bbox"][3] - b["bbox"][1]) >= 0.10
        and (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1]) < 0.80
    ]
    indices: list[int] = []
    for i, block in enumerate(blocks):
        if trusted_only and not _marker(block):
            continue
        box = block.get("bbox")
        if (
            block.get("type") not in _SEVERE_ORDER_PROSE_TYPES
            or not box
            or block.get("angle")
            or box[1] <= 0.05
            or box[3] <= 0.10
            or box[3] >= 0.92
            or _is_bottom_footnote(block)
        ):
            continue
        if block.get("type") != "list" and not any(ch.isalnum() for ch in _block_text(block)):
            continue
        if block.get("type") == "text" and any(_bbox_overlap_in_smaller(box, img) >= 0.50 for img in large_images):
            continue
        indices.append(i)
    return indices


def _has_severe_vertical_backtrack(blocks: list[dict], *, trusted_only: bool) -> bool:
    """检查同栏正文是否存在后块底边高于前块顶边至少 15% 页高的显著回跳。"""
    indices = _body_prose_indices(blocks, trusted_only=trusted_only)
    for pos_a in range(len(indices)):
        ba = blocks[indices[pos_a]]["bbox"]
        for pos_b in range(pos_a + 1, len(indices)):
            bb = blocks[indices[pos_b]]["bbox"]
            if ba[1] - bb[3] >= 0.15 and _bbox_axis_overlap_ratio(ba, bb, axis="x") >= 0.70:
                return True
    return False


def _has_same_band_column_reentry(
    blocks: list[dict], *, trusted_only: bool, require_same_column_for_lrl: bool = False
) -> bool:
    """以 O(N²) 枢轴扫描识别同一纵带内 L→R→L 或 R→L→R 的水平栏折返，排除上下分带双栏页。"""
    indices = _body_prose_indices(blocks, trusted_only=trusted_only)
    if len(indices) < 3:
        return False
    boxes = [blocks[i]["bbox"] for i in indices]
    for pivot in range(1, len(boxes) - 1):
        mid = boxes[pivot]
        left_prev: list[list[float]] = []
        right_before = False
        for i in range(pivot):
            prev = boxes[i]
            if _bbox_axis_overlap_ratio(prev, mid, axis="y") < 0.30:
                continue
            if prev[2] + 0.02 < mid[0]:
                left_prev.append(prev)
            elif mid[2] + 0.02 < prev[0]:
                right_before = True
        if not (left_prev or right_before):
            continue
        for j in range(pivot + 1, len(boxes)):
            nxt = boxes[j]
            if _bbox_axis_overlap_ratio(mid, nxt, axis="y") < 0.30:
                continue
            if left_prev and nxt[2] + 0.02 < mid[0]:
                if not require_same_column_for_lrl or any(
                    _bbox_axis_overlap_ratio(prev, nxt, axis="x") >= 0.50 for prev in left_prev
                ):
                    return True
            if right_before and mid[2] + 0.02 < nxt[0]:
                return True
    return False


def _fallback_severe_non_ua_order(blocks: list[dict], size: tuple[float, float]) -> tuple[list[dict], str] | None:
    """非 PDF/UA 单页出现确定性严重错序且无重叠正文时，经自校验回退至受保护 XYCut++，保留全部结构语义块。"""
    trusted_count = sum(1 for b in blocks if _marker(b) and b.get("bbox") and not b.get("angle"))
    if trusted_count < 3 or _has_overlapping_trusted_prose(blocks):
        return None
    has_vertical = _has_severe_vertical_backtrack(blocks, trusted_only=True)
    has_reentry = (
        False
        if has_vertical
        else _has_same_band_column_reentry(blocks, trusted_only=True, require_same_column_for_lrl=True)
    )
    if not (has_vertical or has_reentry):
        return None
    order = _sort_page(blocks, size)
    if order == list(range(len(blocks))):
        return None
    candidate = [blocks[i] for i in order]
    if _has_severe_vertical_backtrack(candidate, trusted_only=False):
        return None
    if _has_same_band_column_reentry(candidate, trusted_only=False):
        return None
    action = "severe_non_ua_vertical_fallback" if has_vertical else "severe_non_ua_reentry_fallback"
    return candidate, action


def prefer_geometric_tagged_order(
    pages: list[list[dict]],
    marked_pages: list[MarkedPage],
    roles: dict[str, str],
    document: PDFDocument,
    *,
    declared_ua: str | None = None,
    repairs: list[dict] | None = None,
) -> None:
    """执行局部图文/表题逆序归位、确认边缘页码规范化与非 PDF/UA 单页确定性严重错序自校验回退。"""

    def record(page_idx: int, action: str) -> None:
        if repairs is not None:
            repairs.append({"page_idx": page_idx, "action": action})

    physical_positions = {page.page_index: i for i, page in enumerate(marked_pages)}
    for i, (blocks, page) in enumerate(zip(pages, marked_pages, strict=True)):
        possibilities = _figure_inversions(blocks, roles) if roles else {}
        if possibilities:
            geometry = _sort_page(blocks, page.size)
            pos_in_geo = {idx: pos for pos, idx in enumerate(geometry)}
            matched: dict[int, tuple[int, str]] = {}
            for (fig, cap), direction in possibilities.items():
                p_fig, p_cap = pos_in_geo[fig], pos_in_geo[cap]
                if direction == "image_above":
                    in_order = p_fig < p_cap and all(
                        blocks[k]["type"] == "image" for k in geometry[p_fig + 1 : p_cap]
                    )
                else:
                    in_order = p_cap < p_fig and all(
                        blocks[k]["type"] == "image" for k in geometry[p_cap + 1 : p_fig]
                    )
                if in_order:
                    matched[cap] = (fig, direction)
            if matched:
                moved_images = {fig for fig, _ in matched.values()}
                local: list[int] = []
                for j in range(len(blocks)):
                    if j in moved_images:
                        continue
                    if j in matched:
                        fig, direction = matched[j]
                        if direction == "image_above":
                            local.extend((fig, j))
                        else:
                            local.extend((j, fig))
                    else:
                        local.append(j)
                ordered = [blocks[j] for j in local]
                bind_reordered_figure_captions(
                    ordered,
                    [(blocks[fig], blocks[cap]) for cap, (fig, _) in matched.items()],
                    roles,
                )
                record(page.page_index, "figure_inversion")
                if (folio_res := _normalize_confirmed_edge_folio(ordered, pages, physical_positions, document, page.page_index)) is not None:
                    ordered, folio_action = folio_res
                    record(page.page_index, folio_action)
                pages[i] = ordered
                continue

        if (conflict := _folio_blocked_body(blocks)) is not None:
            folio = conflict[0]
            neighbors = _adjacent_folios(pages, physical_positions, document, page.page_index)
            if folio in _confirmed_folios(blocks, neighbors):
                geometry = _sort_page(blocks, page.size)
                if _body_geometry_agrees(blocks, geometry, conflict):
                    blocks[folio]["type"] = "page_number"
                    pages[i] = [blocks[j] for j in geometry]
                    record(page.page_index, "folio_blocked_body")
                    continue

        # 确认页首错置的页底页码归位，或将已在首/尾的跨三页确认边缘页码标注为 page_number。
        if (folio_res := _normalize_confirmed_edge_folio(blocks, pages, physical_positions, document, page.page_index)) is not None:
            blocks, folio_action = folio_res
            pages[i] = blocks
            record(page.page_index, folio_action)

        # 非 PDF/UA 页面若仍存在确定性严重同栏回跳或同带水平栏折返，经自校验回退至受保护 XYCut++。
        if declared_ua is None and (fallback_res := _fallback_severe_non_ua_order(blocks, page.size)) is not None:
            blocks, fallback_action = fallback_res
            pages[i] = blocks
            record(page.page_index, fallback_action)

        # 对物理位于图/表正上方但相邻输出倒置的编号题注执行严格局部交换。
        if len(blocks) >= 2 and any(b["type"] in _VISUAL_TYPES and _marker(b) for b in blocks):
            geometry = _sort_page(blocks, page.size)
            swaps = _reverse_adjacent_above_captions(blocks, geometry)
            if swaps:
                updated = list(blocks)
                for vis, cap in swaps:
                    updated[vis], updated[cap] = updated[cap], updated[vis]
                pages[i] = updated
                record(page.page_index, "above_caption_swap")
