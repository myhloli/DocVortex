"""以真实开边竖轨和重复字形净空恢复表内较密的列。"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from statistics import median

from ....schema import BBox
from ._table_recovery import NativeTableInput, coerce_native_table_rules, recover_native_pdf_table
from ._table_recovery.contracts import NativeTableGlyph, NativeTableTextRow
from ._table_recovery.text import build_native_table_text
from .geometry import _bbox_center_x
from .models import _AxisLine, _PageSource

_VALUE = re.compile(r"(?:[+−-]?\d+(?:[.,]\d+)*(?:[%‰a-zA-Z]{1,3})?[∗*†‡]?|[-–—])")


def _common_gutters(rows: list[list[NativeTableGlyph]], width: float, minimum: float) -> list[tuple[float, float]]:
    """求全部正文行共有的连续字形净空，剔除仅在个别行出现的词间距。"""
    common = [(0.0, width)]
    for glyphs in rows:
        intervals = []
        edge = glyphs[0].bbox[2]
        for glyph in glyphs[1:]:
            left, _, right, _ = glyph.bbox
            if left - edge >= minimum:
                intervals.append((edge, left))
            edge = max(edge, right)
        common = [
            (max(left, start), min(right, stop))
            for left, right in common
            for start, stop in intervals
            if min(right, stop) - max(left, start) >= minimum
        ]
    return common


def _cells(glyphs: Sequence[NativeTableGlyph], tracks: list[float]) -> list[str] | None:
    """按已证明的竖轨读取每个格的字形，拒绝缺格和切开字形的分界。"""
    contents = [[] for _ in tracks[1:]]
    for glyph in glyphs:
        left, _, right, _ = glyph.bbox
        column = bisect_right(tracks, (left + right) / 2) - 1
        if not 0 <= column < len(contents) or left < tracks[column] - 0.25 or right > tracks[column + 1] + 0.25:
            return None
        contents[column].append(glyph.text)
    return ["".join(parts) for parts in contents] if all(contents) else None


def _header_groups(
    upper: NativeTableTextRow,
    lower: list[NativeTableGlyph],
    tracks: list[float],
    printed: list[float],
    em: float,
) -> tuple[list[tuple[float, float]], float] | None:
    """确认跨列表题由两侧实体长轨限定，并和下一行格名之间保有实际间距。"""
    groups = []
    cut_top = []
    cut_bottom = []
    boundaries = [0.0, *printed, tracks[-1]]
    for label in upper.tokens:
        left, _, right, bottom = label.bbox
        if bisect_right(tracks, left) == bisect_right(tracks, right):
            continue
        enclosing = next(((a, b) for a, b in zip(boundaries, boundaries[1:]) if a <= left and right <= b), None)
        if enclosing is None or enclosing in groups:
            return None
        start, stop = enclosing
        if abs((left + right - start - stop) / 2) > em:
            return None
        leaf = [glyph for glyph in lower if start <= glyph.bbox[0] and glyph.bbox[2] <= stop]
        if not leaf or min(glyph.bbox[1] for glyph in leaf) <= bottom:
            return None
        groups.append(enclosing)
        cut_top.append(bottom)
        cut_bottom.append(min(glyph.bbox[1] for glyph in leaf))
    if not groups or max(cut_top) >= min(cut_bottom):
        return None
    return groups, (max(cut_top) + min(cut_bottom)) / 2


def infer_captioned_open_grid(source: _PageSource, bounds: BBox, divider_y: float, rails: list[BBox]) -> list[_AxisLine]:
    """从开边表的实际字形推断完整细网格，经既有物化器校验后才允许取代源行。"""
    x0, y0, x1, y1 = bounds
    width = x1 - x0
    divider = divider_y - y0
    table_input = NativeTableInput(bounds, source.page_size, 0, tuple(source.chars))
    text = build_native_table_text(table_input)
    if text is None:
        return []
    em = text.median_glyph_height
    by_id = {glyph.glyph_id: glyph for glyph in text.glyphs}
    glyph_rows = [[by_id[index] for index in row.glyph_ids] for row in text.rows]
    centers = [median((g.bbox[1] + g.bbox[3]) / 2 for g in row) for row in glyph_rows]
    levels = bisect_right(centers, divider)
    data = glyph_rows[levels:]
    if levels not in (1, 2) or len(data) < 4:
        return []
    distances = [b - a for a, b in zip(centers[levels:], centers[levels + 1 :])]
    pitch = median(distances)
    if not 0.9 * em <= pitch <= 2 * em or any(abs(distance - pitch) > 0.35 * em for distance in distances):
        return []
    gutters = _common_gutters([*data, glyph_rows[levels - 1]], width, 0.55 * em)
    if not 3 <= len(gutters) <= 24:
        return []
    printed = sorted(_bbox_center_x(rail) - x0 for rail in rails)
    if not all(any(left <= x <= right for left, right in gutters) for x in printed):
        return []
    tracks = [
        0.0,
        *(next((x for x in printed if left <= x <= right), (left + right) / 2) for left, right in gutters),
        width,
    ]
    if not _cells(glyph_rows[levels - 1], tracks):
        return []
    for row in data:
        cells = _cells(row, tracks)
        if cells is None or _VALUE.fullmatch(cells[0]) or sum(_VALUE.fullmatch(cell) is not None for cell in cells[1:]) < 2:
            return []
    group_ranges: list[tuple[float, float]] = []
    top_split = 0.0
    if levels == 2:
        heading = _header_groups(text.rows[0], glyph_rows[1], tracks, printed, em)
        if heading is None:
            return []
        group_ranges, top_split = heading
    y_edges = [0.0, divider, *((a + b) / 2 for a, b in zip(centers[levels:], centers[levels + 1 :])), y1 - y0]
    inferred = [
        _AxisLine(
            (x0 + x - 0.05, y0 + (top_split if any(a < x < b for a, b in group_ranges) else 0), x0 + x + 0.05, y1),
            0.1,
            "vertical",
        )
        for x in tracks
    ]
    inferred.extend(_AxisLine((x0, y0 + y - 0.05, x1, y0 + y + 0.05), 0.1, "horizontal") for y in y_edges)
    inferred.extend(
        _AxisLine((x0 + a, y0 + top_split - 0.05, x0 + b, y0 + top_split + 0.05), 0.1, "horizontal") for a, b in group_ranges
    )
    result = recover_native_pdf_table(replace(table_input, drawing_lines=coerce_native_table_rules(inferred)))
    if result is None or result.rows != len(glyph_rows) or result.cols != len(tracks) - 1:
        return []
    assigned = [index for cell in result.cells for index in cell.source_char_indices]
    if len(assigned) != len(text.glyphs) or set(assigned) != {glyph.source_index for glyph in text.glyphs}:
        return []
    return inferred
