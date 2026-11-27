"""从同栏文字块的基线、间距及跨行切点计算版面碎裂的几何扣分。"""

from bisect import bisect_left
from dataclasses import dataclass
from statistics import median
from typing import NamedTuple

from ....content.spans import inline_span_plain_text
from .geometry import _bbox_overlap_in_first, _bbox_union, _coerce_bbox


class _TextPiece(NamedTuple):
    """一条物理基线上的候选文字块，横纵尺度均与原生框的归一化坐标一致。"""

    center_y: float
    height: float
    box: tuple[float, float, float, float]
    advance: float
    text: str


@dataclass(frozen=True, slots=True)
class _SplitLine:
    """保存一条物理行中连续碎块的外框、切点和估算字宽。"""

    box: tuple[float, float, float, float]
    cuts: tuple[float, ...]
    advance: float


def _split_lines(blocks):
    """按基线和词间距连接相邻文字框；已有表图容器内部的碎块不参与。"""
    owners = [
        b
        for block in blocks
        if block.get("type") in {"table", "image", "chart", "code", "algorithm"} and (b := _coerce_bbox(block.get("bbox")))
    ]
    cells = []
    for block in blocks:
        if block.get("type") not in {"text", "paragraph_title"} or block.get("angle"):
            continue
        box = _coerce_bbox(block.get("bbox"))
        if not box:
            continue
        x, top, end, bottom = box
        h = bottom - top
        if not 0 < h <= 0.06 or any(_bbox_overlap_in_first(box, owner) >= 0.8 for owner in owners):
            continue
        lines = block.get("lines") or ()
        if len(lines) > 1:
            ys = [(b[1] + b[3]) / 2 for line in lines if (b := _coerce_bbox(line.get("bbox")))]
            if ys and max(ys) - min(ys) > 0.5 * h:
                continue
        value = block.get("content")
        text = (inline_span_plain_text(value) if isinstance(value, list) else value or "").strip()
        if end > x and any(c.isalpha() for c in text):
            cells.append(_TextPiece((top + bottom) / 2, h, box, (end - x) / len(text), text))
    bands = []
    for cell in sorted(cells):
        if not bands or cell.center_y - bands[-1][0].center_y > 0.48 * max(cell.height, bands[-1][0].height):
            bands.append([])
        bands[-1].append(cell)
    result = []
    for band in bands:
        chain = []
        for cell in sorted(band, key=lambda piece: piece.box[0]):
            if chain:
                previous = chain[-1]
                gap = cell.box[0] - previous.box[2]
                advance = min(cell.advance, previous.advance)
                similar_height = min(cell.height, previous.height) >= 0.48 * max(cell.height, previous.height)
                if not similar_height or not -0.35 * advance <= gap <= 1.5 * advance:
                    _append_split_line(result, chain)
                    chain = []
            chain.append(cell)
        _append_split_line(result, chain)
    return sorted(result, key=lambda line: line.box[1])


def _append_split_line(result, chain):
    """一行至少三块且以字词为主体时记录切点，忽略数字、符号和离散标签。"""
    if len(chain) < 3:
        return
    text = "".join(piece.text for piece in chain)
    letters = sum(c.isalpha() for c in text)
    if letters < 16 or letters < 0.65 * len(text):
        return
    box = chain[0].box
    for piece in chain[1:]:
        box = _bbox_union(box, piece.box)
    result.append(_SplitLine(box, tuple(piece.box[0] for piece in chain[1:]), median(piece.advance for piece in chain)))


def _aligned_cut(cuts, x, tolerance):
    """检查另一行在同一横坐标附近是否反复出现列缝。"""
    i = bisect_left(cuts, x - tolerance)
    return i < len(cuts) and cuts[i] <= x + tolerance


def spatial_fragmentation(blocks):
    """给同一局部区域重复出现的行内断点计分，用最严重区域代表该页。"""
    if len(blocks) < 6:
        return None
    lines = _split_lines(blocks)
    best = 0
    detail = ""
    for i, first in enumerate(lines):
        left, top, right, bottom = first.box
        h, width = bottom - top, right - left
        group = [first]
        for j in range(i + 1, len(lines)):
            line = lines[j]
            x, y, end, base = line.box
            if y - top > 10 * h:
                break
            if y - group[-1].box[1] < 0.6 * h or not 0.65 * h <= base - y <= 1.55 * h:
                continue
            if min(width, end - x) >= 0.4 * max(width, end - x) and min(right, end) - max(left, x) >= 0.7 * min(width, end - x):
                group.append(line)
        if len(group) < 2:
            continue
        cuts, rows, region = 0, 0, None
        for line in group:
            # 反复出现在相同列位的是稳定列缝；每个基线最多贡献四个非固定切点。
            count = 0
            for x in line.cuts:
                aligned = sum(
                    _aligned_cut(other.cuts, x, min(line.advance, other.advance)) for other in group if other is not line
                )
                if 2 * aligned <= len(group) - 1:
                    count += 1
            if count:
                rows += 1
                cuts += min(count, 4)
                region = line.box if region is None else _bbox_union(region, line.box)
        points = min(60, 3 * cuts) if rows >= 2 else 0
        if points > best:
            best = points
            detail = f"同栏 {rows} 行文字在词间距内被反复切开，计入 {cuts} 个非固定列位断点，区域 {region}"
    return (best, detail) if best else None
