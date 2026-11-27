"""用可核对的来源冲突和页内几何证据校正 Tagged PDF 的阅读位置。"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from typing import TYPE_CHECKING

from ....content.spans import inline_span_plain_text
from .._shared.xycut import sort_entries
from .geometry import _bbox_axis_overlap_ratio, _bbox_union_many
from .tagged_captions import bind_reordered_figure_captions, numbered_figure_prefix

if TYPE_CHECKING:
    from ....document.pdf._document import PDFDocument
    from ....document.pdf.marked_content import MarkedPage


_VISUAL_TYPES = frozenset({"image", "table", "chart"})


def _marker(block: dict) -> dict:
    """取得可核对父子关系及来源身份的 Tagged 标记。"""
    return block.get("_pdf_structure", {})


def _visual_parent(block: dict) -> str | None:
    """读取已有作者或推断图文关系，以免排序覆盖已经成组的图。"""
    marker = _marker(block)
    return marker.get("visual_parent") or marker.get("inferred_visual_parent")


def _block_text(block: dict) -> str:
    """读取原生或结构阶段的可见文本，兼容未物化的文本块。"""
    content = block["content"]
    return content if isinstance(content, str) else inline_span_plain_text(content)


def _has_numbered_caption(block: dict, roles: dict[str, str]) -> bool:
    """要求独立单页角色及以分隔符结束的印刷编号，排除正文图号引用。"""
    return (
        block["type"] == "text"
        and not block.get("angle")
        and _marker(block).get("node_id") in roles
        and not _visual_parent(block)
        and numbered_figure_prefix(_block_text(block)) is not None
    )


def _has_inversion(blocks: list[dict], cap: int, fig: int) -> bool:
    """核实图在树中越过了物理位置位于其题注下方的同栏正文。"""
    image, caption = blocks[fig]["bbox"], blocks[cap]["bbox"]
    height = caption[3] - caption[1]
    return (
        0 <= caption[1] - image[3] <= 2.2 * height
        and _bbox_axis_overlap_ratio(image, caption, axis="x") >= 0.85
        and any(
            # 一条单独的小节标题不足以否定作者有意的图文顺序。
            b["type"] in {"text", "list"}
            and b["bbox"][1] >= caption[3]
            and _bbox_axis_overlap_ratio(image, b["bbox"], axis="x") >= 0.35
            for b in blocks[cap + 1 : fig]
        )
        and not any(
            j not in (cap, fig)
            and _bbox_axis_overlap_ratio(b["bbox"], caption, axis="x") >= 0.25
            and b["bbox"][1] < caption[1]
            and b["bbox"][3] > image[3]
            for j, b in enumerate(blocks)
        )
        and not any(
            j != fig
            and b["type"] in _VISUAL_TYPES
            and _bbox_axis_overlap_ratio(b["bbox"], caption, axis="x") >= 0.25
            and b["bbox"][1] < caption[3]
            and b["bbox"][3] >= image[3] - height
            for j, b in enumerate(blocks)
        )
    )


def _figure_inversions(blocks: list[dict], roles: dict[str, str]) -> set[tuple[int, int]]:
    """在运行几何排序前查找低成本、无父级竞争的编号图文逆序证据。"""
    if len(blocks) < 3 or not any(b["type"] == "image" and _marker(b) for b in blocks):
        return set()
    if any(not block.get("bbox") for block in blocks):
        return set()
    counts = Counter(_marker(b).get("node_id") for b in blocks)
    owned = {_visual_parent(b) for b in blocks}
    captions = []
    pairs = set()
    for i, block in enumerate(blocks):
        node = _marker(block).get("node_id")
        if counts[node] != 1:
            continue
        if _has_numbered_caption(block, roles):
            captions.append(i)
        elif block["type"] == "image" and node and not block.get("angle") and node not in owned:
            pairs.update((i, j) for j in captions if _has_inversion(blocks, j, i))
    by_caption = Counter(caption for _, caption in pairs)
    by_image = Counter(image for image, _ in pairs)
    return {(image, caption) for image, caption in pairs if by_caption[caption] == by_image[image] == 1}


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


def _sort_page(blocks: list[dict], size: tuple[float, float]) -> list[int]:
    """把归一化模型块恢复到 PDF 点坐标，再调用生产 XY-Cut++。"""
    return [
        record["index"]
        for record in sort_entries(
            [
                {"bbox": [value * size[axis % 2] for axis, value in enumerate(block["bbox"])], "index": i}
                for i, block in enumerate(blocks)
            ]
        )
    ]


def _adjacent_folios(pages: list[list[dict]], positions: dict[int, int], document: PDFDocument, physical: int):
    """尽量复用已经解析的邻页，选页才从原件读取缺少的页码证据。"""
    return [
        _folio_candidates(pages[position])
        if (position := positions.get(index)) is not None
        else _source_folios(document, index)
        for index in (physical - 1, physical + 1)
    ]


def prefer_geometric_tagged_order(
    pages: list[list[dict]], marked_pages: list[MarkedPage], roles: dict[str, str], document: PDFDocument
) -> None:
    """只在可证的图文倒置或前置页码阻塞合并时采用几何顺序。"""
    physical_positions = {page.page_index: i for i, page in enumerate(marked_pages)}
    for i, (blocks, page) in enumerate(zip(pages, marked_pages, strict=True)):
        possibilities = _figure_inversions(blocks, roles) if roles else set()
        if not possibilities:
            if (conflict := _folio_blocked_body(blocks)) is not None:
                folio = conflict[0]
                neighbors = _adjacent_folios(pages, physical_positions, document, page.page_index)
                if folio in _confirmed_folios(blocks, neighbors):
                    geometry = _sort_page(blocks, page.size)
                    if _body_geometry_agrees(blocks, geometry, conflict):
                        blocks[folio]["type"] = "page_number"
                        pages[i] = [blocks[j] for j in geometry]
            continue
        geometry = _sort_page(blocks, page.size)
        pairs = {caption: image for image, caption in zip(geometry, geometry[1:]) if (image, caption) in possibilities}
        if not pairs:
            continue
        moved_images = set(pairs.values())
        local = []
        for j in range(len(blocks)):
            if j in moved_images:
                continue
            if j in pairs:
                local.append(pairs[j])
            local.append(j)
        folios = set()
        if local != geometry:
            neighbors = _adjacent_folios(pages, physical_positions, document, page.page_index)
            folios = _confirmed_folios(blocks, neighbors)
            if [j for j in local if j not in folios] != [j for j in geometry if j not in folios]:
                continue
        for j in folios:
            blocks[j]["type"] = "page_number"
        ordered = [blocks[j] for j in geometry]
        bind_reordered_figure_captions(ordered, [(blocks[image], blocks[caption]) for caption, image in pairs.items()], roles)
        pages[i] = ordered
