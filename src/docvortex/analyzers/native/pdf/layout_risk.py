"""从最终排序的归一化块中提取可离线复查的版面风险。"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from itertools import combinations, pairwise
from statistics import median

from ....schema import BBox
from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_union,
    _coerce_bbox,
    _horizontal_bbox_gap,
)
from .layout_paragraph_risk import paragraph_boundary_risk
from .layout_table_risk import table_structure_risk
from .table_constants import _TABLE_CAPTION_RE

_MARGINS = frozenset({"header", "footer", "page_number", "page_footnote"})
_PROSE = frozenset({"text", "list"})
_CROPS = frozenset({"image", "chart", "image_block"})
_IMAGE_LABELS = frozenset({"caption", "image_caption", "chart_caption", "doc_title", "paragraph_title"})
_IMAGE_SEAM_GAP = 0.008
_BAND_DIVIDERS = _CROPS | {"text", "list", "code", "algorithm", "doc_title", "paragraph_title", "table", "equation"}
_NUMERIC_CELL = re.compile(r"[+\-−]?\d[\d.,%‰+\-−/\s]*")
_MODERATE = 30
_STRONG = 60

_Risk = tuple[int, str]
_LocatedBlock = tuple[int, str, BBox, dict]


def _height(box: BBox) -> float:
    """返回块或行占页面纵轴的高度。"""
    return box[3] - box[1]


def _width(box: BBox) -> float:
    """返回块或行占页面横轴的宽度。"""
    return box[2] - box[0]


def _region(box: BBox) -> str:
    """用与模型坐标精度一致的区域说明触发证据。"""
    return "[" + ", ".join(f"{coord:.3f}" for coord in box) + "]"


def _block_text(block: dict) -> str:
    """兼容原生纯文本和写出的 ModelJson 行内成员。"""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [child.get("content") for child in content if isinstance(child, dict)]
        return "".join(part for part in parts if isinstance(part, str))
    return ""


def _line_boxes(block: dict) -> list[BBox]:
    """只读取模型中有效的行框；跨栏和相交判断优先用真实行框。"""
    return [box for line in block.get("lines") or () if isinstance(line, dict) and (box := _coerce_bbox(line.get("bbox")))]


def _marginal_risk(blocks: list[_LocatedBlock], line_heights: list[float]) -> _Risk | None:
    """识别与内容区冲突的页边块，并把单独的大号页眉作为弱证据。"""
    body = [box for _, kind, box, _ in blocks if kind in _PROSE]
    # 局部无行框时用 1.4% 页高近似一行；限制散落的大框拉宽邻接距离。
    line_height = min(0.03, median(line_heights)) if line_heights else 0.014
    margins = [(kind, box, block) for _, kind, box, block in blocks if kind in _MARGINS]
    fixed = [(kind, box) for _, kind, box, _ in blocks if kind in {"code", "algorithm", "equation", "table"}]
    best = None
    for kind, box, block in margins:
        content = _block_text(block).strip()
        if kind != "page_number" and not content:
            continue
        for fixed_kind, fixed_box in fixed:
            if _bbox_overlap_in_first(box, fixed_box) >= 0.50 and kind in {"header", "footer", "page_number"}:
                return _STRONG, f"{kind} 与 {fixed_kind} 的排版区域重叠，页边框 {_region(box)}"

        same_column = [bounds for bounds in body if _bbox_axis_overlap_ratio(box, bounds, axis="x") >= 0.55]
        if kind in {"header", "footer"} and re.fullmatch(r"(?:}\s*){1,3};?", content):
            code_rows = [
                (bounds, _block_text(item))
                for _, role, bounds, item in blocks
                if role in {"text", "paragraph_title", "doc_title"}
                and not item.get("angle")
                and 0 <= box[1] - bounds[3] <= 0.14
            ]
            opening = [
                bounds
                for bounds, text in code_rows
                if abs(bounds[0] - box[0]) <= 0.035 and re.match(r"^\s*[\w*&:<>,\s]+\([^{}]*\)\s*\{", text.split("//", 1)[0])
            ]
            if opening and any(
                bounds[1] >= min(head[1] for head in opening)
                and box[1] - bounds[3] <= 0.025
                and -0.01 <= bounds[0] - box[0] <= 0.08
                and re.search(r"(?:\breturn\b|\b[\w.\[\]>-]+\s*=(?!=))[^;]{0,100};", text)
                for bounds, text in code_rows
            ):
                return _MODERATE, f"程序开括号和末行语句对齐，但闭花括号落入页边，区域 {_region(box)}"
        if (
            kind == "footer"
            and isinstance(block.get("content"), list)
            and any(
                style in ("subscript", "superscript")
                for span in block["content"]
                if isinstance(span, dict)
                for style in (span.get("styles") or ())
            )
            and re.search(r"[=±×÷/∑√]", content)
        ):
            for _, role, bounds, paragraph in blocks:
                if role != "text" or not 0 <= box[1] - bounds[3] <= 1.5 * line_height:
                    continue
                if _bbox_axis_overlap_ratio(box, bounds, axis="x") < 0.7:
                    continue
                lead = _block_text(paragraph).rstrip()
                if lead.endswith((":", "：")) and re.search(
                    r"\b(?:equation|formula|expression)\b|公式|方程|表达式", lead[-100:], re.I
                ):
                    return _STRONG, f"公式引导句紧接含上下标及运算符的页脚，区域 {_region(box)}"
        if kind in {"header", "footer"}:
            y = _bbox_center_y(box)
            above = any(bounds[1] < y and box[1] - bounds[3] <= line_height for bounds in same_column)
            below = any(bounds[3] > y and bounds[1] - box[3] <= line_height for bounds in same_column)
            if above and below:
                return _STRONG, f"{kind} 插在同栏连续正文之间，页边框 {_region(box)}"
            if (
                kind == "footer"
                and below
                and any(
                    fixed_kind == "table"
                    and 0 <= box[1] - bounds[3] <= 1.3 * line_height
                    and _bbox_axis_overlap_ratio(box, bounds, axis="x") >= 0.75
                    for fixed_kind, bounds in fixed
                )
            ):
                return _STRONG, f"页脚夹在表体与其后紧邻正文之间，页边框 {_region(box)}"

        # 数个页边块必须紧挨主内容且挤在同一物理行，正常分散的刊名、网址和页码不计。
        if kind in {"header", "footer"}:
            y = _bbox_center_y(box)
            peers = [
                other
                for peer_kind, other, _ in margins
                if peer_kind in {kind, "page_number"} and other[1] - 0.002 <= y <= other[3] + 0.002
            ]
            if len(peers) >= 4 and any(
                bounds[1] - 1.5 * line_height <= box[3] and box[1] <= bounds[3] + 1.5 * line_height for bounds in same_column
            ):
                return _STRONG, f"同一行的 {len(peers)} 个页边碎块紧邻正文，页边框 {_region(box)}"

        caption = _TABLE_CAPTION_RE.fullmatch(content) if kind == "header" else None
        if caption and not caption["suffix"].strip(".:： "):
            if any(
                fixed_kind == "table"
                and 0 <= bounds[1] - box[3] <= 2.5 * line_height
                and _bbox_axis_overlap_ratio(box, bounds, axis="x") >= 0.75
                for fixed_kind, bounds in fixed
            ):
                return _STRONG, f"页眉中的表编号与下方表格对齐，页边框 {_region(box)}"

        if (
            kind == "header"
            and _height(box) >= max(0.032, 1.45 * line_height)
            and _width(box) >= 0.45
            and any(0 <= bounds[1] - box[3] <= 0.08 for bounds in same_column)
        ):
            best = _MODERATE, f"正文紧邻宽幅大字页眉，页眉高占页面 {_height(box):.1%}，区域 {_region(box)}"
    return best


def _column_risk(blocks: list[_LocatedBlock]) -> _Risk | None:
    """寻找同一正文带中留有窄栏沟、且切到邻栏后又返回的多行段。"""
    body = [
        (index, box)
        for index, kind, box, block in blocks
        if kind in _PROSE and _width(box) >= 0.12 and _height(box) >= 0.022 and len(_line_boxes(block)) >= 2
    ]
    moderate = None
    for (first_index, first), (_, middle), (last_index, last) in zip(body, body[1:], body[2:]):
        # 同栏的两段须相互对齐，隔栏须有可见横向净空且物理纵向区间重合。
        if abs(first[0] - last[0]) > 0.05 or _bbox_axis_overlap_ratio(first, last, axis="x") < 0.75:
            continue
        side = _bbox_union(first, last)
        gutter = _horizontal_bbox_gap(side, middle)
        # 宽度差显著的栏常是正文与独立窄侧栏。
        if min(_width(side), _width(middle)) < 0.60 * max(_width(side), _width(middle)):
            continue
        if not 0.012 <= gutter <= 0.15 or _bbox_axis_overlap_ratio(side, middle, axis="y") < 0.4:
            continue
        if last[1] - first[3] > 0.09 or _bbox_axis_overlap_ratio(last, middle, axis="y") < 0.2:
            continue
        region = _bbox_union(side, middle)
        # 上下独立的正文带可以重复先左后右；横跨两栏的标题和图表把正文分带。
        if any(
            kind in _BAND_DIVIDERS
            and first_index < index < last_index
            and region[1] < _bbox_center_y(box) < region[3]
            and _bbox_axis_overlap_ratio(box, side, axis="x") >= 0.50
            and _bbox_axis_overlap_ratio(box, middle, axis="x") >= 0.50
            for index, kind, box, _ in blocks
        ):
            continue
        direction = "左→右→左" if side[2] < middle[0] else "右→左→右"
        reason = f"同一正文带的多行段按 {direction} 排列，栏沟宽 {gutter:.1%}，区域 {_region(region)}"
        if _bbox_axis_overlap_ratio(last, middle, axis="y") >= 0.55 and min(_height(last), _height(middle)) >= 0.045:
            return _STRONG, reason
        moderate = _MODERATE, reason
    return moderate


def _image_seam(first: BBox, second: BBox, axis: int, dividers: list[BBox]) -> float | None:
    """度量有共同接边且中间没有正文或题注的两个图块。"""
    cross = 1 - axis
    gap = second[axis] - first[axis + 2]
    shorter = min(first[axis + 2] - first[axis], second[axis + 2] - second[axis])
    if not -0.10 * shorter <= gap <= min(_IMAGE_SEAM_GAP, 0.25 * shorter):
        return None
    start, stop = max(first[cross], second[cross]), min(first[cross + 2], second[cross + 2])
    widths = first[cross + 2] - first[cross], second[cross + 2] - second[cross]
    if stop - start < max(0.75 * min(widths), 0.45 * max(widths)):
        return None
    edge = (second[axis] + first[axis + 2]) / 2
    for box in dividers:
        center = (box[axis] + box[axis + 2]) / 2
        overlap = min(box[cross + 2], stop) - max(box[cross], start)
        if abs(center - edge) <= max(gap / 2, 0.005) and overlap >= 0.55 * min(stop - start, box[cross + 2] - box[cross]):
            return None
    return gap


def _image_join_risk(connections: list[tuple[BBox, BBox, float]], axis: int) -> _Risk | None:
    """三块连拼给出弱证据，至少四块窄图在同一方向紧贴给出强证据。"""
    runs: dict[BBox, tuple[int, BBox]] = {}
    thin_runs: dict[BBox, tuple[int, BBox]] = {}
    best = None
    cross = 1 - axis
    for first, second, gap in sorted(connections, key=lambda pair: pair[0][axis]):
        count, region = runs.get(first, (1, first))
        if count + 1 > runs.get(second, (0, second))[0]:
            region = _bbox_union(region, second)
            runs[second] = count + 1, region
            if count >= 2:
                direction = "横向" if axis == 0 else "纵向"
                best = _MODERATE, f"{count + 1} 个图块沿{direction}连续拼接且无独立分隔，区域 {_region(region)}"

        # 在每个轴内比较归一化跨度，避免用两轴相除推断图块或 PDF 的物理长宽比。
        if abs(gap) <= 0.004 and all(
            box[axis + 2] - box[axis] <= 0.022 and box[cross + 2] - box[cross] >= 0.25 for box in (first, second)
        ):
            thin_count, thin_region = thin_runs.get(first, (1, first))
            if thin_count + 1 > thin_runs.get(second, (0, second))[0]:
                thin_region = _bbox_union(thin_region, second)
                thin_runs[second] = thin_count + 1, thin_region
                if thin_count >= 3:
                    return _STRONG, f"{thin_count + 1} 个窄图块连续紧贴，区域 {_region(thin_region)}"
    return best


def _image_composition_risk(images: list[BBox], blocks: list[_LocatedBlock]) -> _Risk | None:
    """识别缺少独立标注的可见图块重叠与连续拼接。"""
    # 近整页图可能是背景，其特殊重复情形由上层单独判断。
    candidates = sorted(
        (box for box in images if _bbox_area(box) >= 0.003 and min(_width(box), _height(box)) < 0.94),
        key=lambda box: box[1],
    )
    if len(candidates) < 2:
        return None

    labeled: set[BBox] = set()
    dividers: list[BBox] = []
    for _, kind, box, block in blocks:
        if kind in _IMAGE_LABELS:
            if owner := _coerce_bbox(block.get("_native_annotation_parent_bbox")):
                labeled.add(owner)
            else:
                nearby = [
                    image
                    for image in candidates
                    if _bbox_axis_overlap_ratio(box, image, axis="x") >= 0.60
                    and 0 <= max(box[1] - image[3], image[1] - box[3]) <= 0.025
                ]
                if len(nearby) == 1:
                    labeled.add(nearby[0])
            dividers.append(box)
        elif kind in _PROSE:
            dividers.extend(_line_boxes(block) or [box])

    connections: tuple[list[tuple[BBox, BBox, float]], list[tuple[BBox, BBox, float]]] = ([], [])
    tops = [box[1] for box in candidates]
    best = None
    for index, first in enumerate(candidates):
        end = bisect_right(tops, first[3] + _IMAGE_SEAM_GAP, lo=index + 1)
        for pos in range(index + 1, end):
            second = candidates[pos]
            if first in labeled and second in labeled:
                continue
            smaller, larger = (first, second) if _bbox_area(first) <= _bbox_area(second) else (second, first)
            overlap = _bbox_overlap_in_first(smaller, larger)
            if overlap >= 0.50 and _bbox_area(smaller) >= 0.15 * _bbox_area(larger) and smaller not in labeled:
                bounds = _region(_bbox_union(first, second))
                best = _MODERATE, f"无独立题注的图块有 {overlap:.0%} 面积被另一图块覆盖，区域 {bounds}"

            for axis, edges in enumerate(connections):
                before, after = (first, second) if first[axis] <= second[axis] else (second, first)
                gap = _image_seam(before, after, axis, dividers)
                if gap is not None:
                    edges.append((before, after, gap))

    for axis, edges in enumerate(connections):
        if risk := _image_join_risk(edges, axis):
            if risk[0] == _STRONG:
                return risk
            best = risk
    return best


def _embedded_formula_risk(blocks: list[_LocatedBlock], equations: list[BBox], equation_tops: list[float]) -> _Risk | None:
    """检测正文与多个展示式裁片对同一区域的实际争抢。"""
    best = None
    max_equation_height = max(map(_height, equations), default=0)
    for _, kind, body, block in blocks:
        if kind != "text" or len(equations) < 3:
            continue
        lines = _line_boxes(block)
        if len(lines) < 3:
            continue
        # 重叠七成的公式最多只能有三成高度高于正文块。
        start = bisect_left(equation_tops, body[1] - 0.30 * max_equation_height)
        end = bisect_left(equation_tops, body[3], lo=start)
        embedded = [eq for eq in equations[start:end] if _bbox_overlap_in_first(eq, body) >= 0.70]
        if len(embedded) < 3:
            continue
        competing_lines = [[line for line in lines if _bbox_axis_overlap_ratio(eq, line, axis="y") >= 0.60] for eq in embedded]
        if (
            sum(
                any(_bbox_axis_overlap_ratio(eq, line, axis="x") >= 0.50 for line in aligned)
                for eq, aligned in zip(embedded, competing_lines)
            )
            >= 2
        ):
            return _STRONG, f"{len(embedded)} 个公式裁片落进同一正文块并与正文行交叠，正文框 {_region(body)}"
        if (
            sum(
                any(_horizontal_bbox_gap(eq, line) <= 0.055 for line in aligned)
                for eq, aligned in zip(embedded, competing_lines)
            )
            >= 3
        ):
            best = _MODERATE, f"{len(embedded)} 个公式裁片插在同一正文块的多个物理行中，正文框 {_region(body)}"

    return best


def _overlapping_equation_risk(equations: list[BBox]) -> _Risk | None:
    """识别两个独立展示公式框按较小框面积计算交叠达到一半以上的严重冲突。"""
    equation_tops = [box[1] for box in equations]
    for index, first in enumerate(equations):
        end = bisect_left(equation_tops, first[3], lo=index + 1)
        for pos in range(index + 1, end):
            second = equations[pos]
            smaller, larger = (first, second) if _bbox_area(first) <= _bbox_area(second) else (second, first)
            overlap = _bbox_overlap_in_first(smaller, larger)
            if overlap >= 0.50:
                bounds = _region(_bbox_union(first, second))
                return _STRONG, f"两个独立公式框交叠 {overlap:.0%}（按较小框面积），区域 {bounds}"
    return None


def _asset_risk(blocks: list[_LocatedBlock]) -> _Risk | None:
    """识别互相重叠的独立公式、图块组成冲突和整页图的重复裁片。"""
    equations = sorted((box for _, kind, box, _ in blocks if kind == "equation"), key=lambda box: box[1])
    if overlap := _overlapping_equation_risk(equations):
        return overlap
    equation_tops = [box[1] for box in equations]

    images = [box for _, kind, box, _ in blocks if kind in _CROPS]
    # 有独立正文的整页背景可以包含已单独导出的真实插图。
    if not any(kind in _PROSE for _, kind, _, _ in blocks):
        for outer in images:
            if _width(outer) < 0.94 or _height(outer) < 0.94:
                continue
            for inner in images:
                if inner is not outer and _bbox_area(inner) >= 0.08 and _bbox_overlap_in_first(inner, outer) >= 0.95:
                    return _STRONG, f"无独立正文的近整页图面又导出内部大裁片，内部框 {_region(inner)}"
    best = _image_composition_risk(images, blocks)
    if best and best[0] == _STRONG:
        return best

    if conflict := _embedded_formula_risk(blocks, equations, equation_tops):
        return conflict

    # 页边起始且横向强对齐的大量公式是弱线索，正文/数学混排可有宽公式。
    wide = [
        box
        for box in equations
        if _width(box) >= 0.75 and min(box[0], 1 - box[2]) <= 0.035 and abs(box[0] - (1 - box[2])) >= 0.08
    ]
    if len(wide) >= 4 and len(wide) * 2 >= len(blocks):
        first = wide[0]
        if sum(abs(box[0] - first[0]) <= 0.012 and abs(box[2] - first[2]) <= 0.012 for box in wide) >= 4:
            return _MODERATE, f"{len(wide)} 个相似的通宽公式裁片对齐到页边，首个框 {_region(first)}"
    return best


def _grid_risk(blocks: list[_LocatedBlock]) -> _Risk | None:
    """查找落在现有图表外、重复排成多行多列的窄数值正文块。"""
    assets = [box for _, kind, box, _ in blocks if kind in _CROPS or kind == "table"]
    cells = [
        box
        for _, kind, box, block in blocks
        if kind == "text"
        and 0.005 <= _height(box) <= 0.055
        and _width(box) <= 0.12
        and len(block.get("lines") or ()) <= 1
        and _NUMERIC_CELL.fullmatch(_block_text(block).strip())
        and not any(_bbox_overlap_in_first(box, asset) >= 0.50 for asset in assets)
    ]
    # 四行四轨用品质稳定的数值提供证据，两个并排栏目的页码或列表编号不满足。
    if len(cells) < 16:
        return None
    height = median(map(_height, cells))
    row_tolerance = min(0.008, 0.4 * height)
    rows: list[list[BBox]] = []
    for cell in sorted(cells, key=_bbox_center_y):
        if not rows or _bbox_center_y(cell) - _bbox_center_y(rows[-1][0]) > row_tolerance:
            rows.append([])
        rows[-1].append(cell)

    tracks: list[tuple[float, list[float]]] = []
    for row in rows:
        right_edges: list[float] = []
        for cell in sorted(row, key=lambda box: box[2]):
            if not right_edges or (cell[0] - right_edges[-1] >= 0.004 and cell[2] - right_edges[-1] > 0.024):
                right_edges.append(cell[2])
        if len(right_edges) >= 4:
            tracks.append((_bbox_center_y(row[0]), right_edges))

    for offset in range(len(tracks) - 3):
        window = tracks[offset : offset + 4]
        if any(lower[0] - upper[0] > 2.5 * height for upper, lower in pairwise(window)):
            continue
        columns = window[0][1]
        # 数值右对齐时位数变化不移动列轨。同一轨允许不到页面 1.2% 的偏差。
        for _, edges in window[1:]:
            columns = [anchor for anchor in columns if any(abs(anchor - edge) <= 0.012 for edge in edges)]
        if len(columns) >= 4 and columns[-1] - columns[0] >= 0.20:
            return _STRONG, f"4 行独立数值正文重复对齐到 {len(columns)} 个列轨，首行位于页高 {window[0][0]:.1%}"
    return None


def _image_text_order_risk(blocks: list[_LocatedBlock]) -> _Risk | None:
    """并列图各有紧贴的独立说明时，查找输出另一幅图后才返回前图说明的折返。"""
    images = [(idx, box) for idx, kind, box, _ in blocks if kind in _CROPS and _bbox_area(box) >= 0.012]
    if len(images) < 2:
        return None
    labels = sorted(
        (box[1], idx, box)
        for idx, kind, box, block in blocks
        if kind in {"paragraph_title", "text"} and sum(c.isalnum() for c in _block_text(block)) >= 5
    )
    tops = [top for top, _, _ in labels]
    pairs = []
    for idx, box in images:
        # 只取紧贴图底且几乎完全在图宽内的第一条独立文字，共用的通栏题注不能归给某一图。
        start = bisect_left(tops, box[3] - 0.002)
        stop = bisect_right(tops, box[3] + 0.025, lo=start)
        for _, label_idx, label in labels[start:stop]:
            overlap = min(box[2], label[2]) - max(box[0], label[0])
            if overlap >= 0.9 * _width(label) and _width(label) >= 0.2 * _width(box):
                pairs.append((idx, label_idx, box))
                break
    for (first, caption, left), (other, other_caption, right) in combinations(pairs, 2):
        if (
            first < other < caption
            and other < other_caption
            and _horizontal_bbox_gap(left, right) > 0
            and _bbox_axis_overlap_ratio(left, right, axis="y") >= 0.75
        ):
            return (
                _MODERATE,
                f"并列图文顺序折返，图块 {first} 的独立说明 {caption} 位于另一图块 {other} 后，区域 {_region(_bbox_union(left, right))}",
            )
    return None


def _locate_upright_blocks(blocks: list[dict]) -> list[_LocatedBlock]:
    """抽取未旋转且带有效归一化框的顶层块，避免调用方重复解析。"""
    located: list[_LocatedBlock] = []
    for index, block in enumerate(blocks):
        kind = block.get("type")
        if isinstance(kind, str) and not block.get("angle") and (box := _coerce_bbox(block.get("bbox"))):
            located.append((index, kind, box, block))
    return located


def _formula_ownership_risk(located: list[_LocatedBlock]) -> _Risk | None:
    """汇总独立公式框重度交叠与正文行争抢公式裁片的归属风险。"""
    equations = sorted((box for _, kind, box, _ in located if kind == "equation"), key=lambda box: box[1])
    if len(equations) < 2:
        return None
    if overlap := _overlapping_equation_risk(equations):
        return overlap
    if len(equations) >= 3:
        return _embedded_formula_risk(located, equations, [box[1] for box in equations])
    return None


def extract_scored_layout_risk(blocks: list[dict]) -> dict[str, tuple[int, str]]:
    """仅提取进入页级伪置信度计分的活跃风险族，跳过已停用的弱诊断族。"""
    located = _locate_upright_blocks(blocks)
    return {
        family: risk
        for family, risk in (
            ("formula_ownership", _formula_ownership_risk(located)),
            ("grid_fragmentation", _grid_risk(located)),
            ("table_structure", table_structure_risk(blocks)),
        )
        if risk is not None
    }


def extract_layout_risk(blocks: list[dict], *, separate_severe: bool = False) -> dict[str, tuple[int, str]]:
    """读取一页归一化块并按风险族返回最高可信度与所在区域，不修改输入。"""
    located = _locate_upright_blocks(blocks)
    line_heights = [_height(line) for _, kind, _, block in located if kind in _PROSE for line in _line_boxes(block)]
    factors = {
        family: risk
        for family, risk in (
            ("marginal_role", _marginal_risk(located, line_heights)),
            ("column_reentry", _column_risk(located) or _image_text_order_risk(located)),
            ("asset_fragmentation", _asset_risk(located)),
            ("grid_fragmentation", _grid_risk(located)),
            ("table_structure", table_structure_risk(blocks)),
            ("paragraph_boundary", paragraph_boundary_risk(blocks)),
        )
        if risk is not None
    }
    if separate_severe and "asset_fragmentation" in factors:
        if risk := _formula_ownership_risk(located):
            factors["formula_ownership"] = risk
    return factors
