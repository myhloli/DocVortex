"""用真实横隔线和已验收的正文列轨恢复表体上缘遗漏的表头。"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace

from ....schema import BBox
from ._table_recovery.banded_numeric import _numeric_value
from ._table_recovery.candidate import serialize_native_table_html
from ._table_recovery.contracts import (
    NativeTableCell,
    NativeTableGlyph,
    NativeTableInput,
    NativeTableResult,
    NativeTableText,
    NativeTableTextRow,
)
from ._table_recovery.geometry import bbox_intersection
from ._table_recovery.text import build_cell_text, build_native_table_text
from .geometry import (
    _bbox_center_y,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
)
from .models import _LineItem, _PageSource
from .table_annotations import _is_table_note_text
from .table_constants import _TABLE_CAPTION_RE


@dataclass(frozen=True, slots=True)
class _RecoveredTableHeader:
    """保存唯一归属的表头行、单独的排版输入与扩展后的表体范围。"""

    bbox: BBox
    line_indices: frozenset[int]
    table_input: NativeTableInput
    result: NativeTableResult


def _leading_measures(body: NativeTableResult) -> dict[int, tuple[NativeTableCell, ...]]:
    """在正文首批记录中确认各数值列，已有文字表头不得充当首条记录。"""
    samples = defaultdict(list)
    for cell in body.cells:
        if cell.row >= 3 or cell.colspan != 1 or cell.rowspan != 1:
            continue
        value = cell.content.strip()
        if _numeric_value(value) and any(char.isdecimal() for char in value):
            samples[cell.col].append(cell)
    return {col: tuple(values) for col, values in samples.items() if len(values) >= 2 and any(cell.row == 0 for cell in values)}


def _column_edges(body: NativeTableResult, em: float) -> tuple[float, ...] | None:
    """按原有列和跨度取轨道交点，重叠或不一致的网格不能接上独立表头。"""
    edges: dict[int, float] = {}
    for cell in body.cells:
        for col, x in ((cell.col, cell.bbox[0]), (cell.col + cell.colspan, cell.bbox[2])):
            if abs(edges.setdefault(col, x) - x) > 0.05 * em:
                return None
    if len(edges) != body.cols + 1:
        return None
    tracks = tuple(edges[i] for i in range(body.cols + 1))
    return tracks if all(b > a for a, b in zip(tracks, tracks[1:])) else None


def _peer_label_cells(
    row: NativeTableTextRow,
    glyphs: dict[int, NativeTableGlyph],
    tracks: tuple[float, ...],
    em: float,
    minimum_gap: float,
) -> dict[int, list[NativeTableGlyph]] | None:
    """按正文轨分配整字，将挤作一行的列名仅在真实横向留白处分开。"""
    groups: dict[int, list[NativeTableGlyph]] = {}
    previous = None
    previous_col = -1
    for glyph in sorted((glyphs[i] for i in row.glyph_ids), key=lambda item: item.bbox[0]):
        left, _, right, _ = glyph.bbox
        col = bisect_right(tracks, (left + right) / 2) - 1
        if (
            glyph.writing_angle != 0
            or not 0 <= col < len(tracks) - 1
            or left < tracks[col] - 0.16 * em
            or right > tracks[col + 1] + 0.16 * em
            or (previous is not None and col != previous_col and left - previous.bbox[2] < minimum_gap)
        ):
            return None
        groups.setdefault(col, []).append(glyph)
        previous, previous_col = glyph, col
    return groups


def _partially_ruled_label(
    upper: NativeTableTextRow,
    lower: NativeTableTextRow,
    tracks: tuple[float, ...],
    rails: list[BBox],
    offset: tuple[float, float],
    em: float,
) -> tuple[int, int, float] | None:
    """用只覆盖部分列的印刷横线确认上层标签的跨列范围。"""
    if upper.bbox[3] >= lower.bbox[1] or (lower.bbox[1] - upper.bbox[3]) > 0.8 * em or len(upper.tokens) != 1:
        return None
    x_offset, y_offset = offset
    candidates = set()
    for box in rails:
        y = _bbox_center_y(box) - y_offset
        if not upper.bbox[3] <= y <= lower.bbox[1]:
            continue
        left, right = box[0] - x_offset, box[2] - x_offset
        start = min(range(len(tracks)), key=lambda col: abs(tracks[col] - left))
        end = min(range(len(tracks)), key=lambda col: abs(tracks[col] - right))
        if (
            2 <= end - start < len(tracks) - 1
            and abs(tracks[start] - left) <= em
            and abs(tracks[end] - right) <= em
            and left <= upper.bbox[0]
            and upper.bbox[2] <= right
            and abs((upper.bbox[0] + upper.bbox[2] - left - right) / 2) <= 0.5 * em
        ):
            candidates.add((start, end, y))
    return candidates.pop() if len(candidates) == 1 else None


def _label_aligns_with_measure(
    group: list[NativeTableGlyph],
    track: tuple[float, float],
    cells: tuple[NativeTableCell, ...],
    body_glyphs: dict[int, NativeTableGlyph],
    em: float,
) -> bool:
    """列名可居中于列轨，也可沿重复正文值的字形左缘、中轴或右缘对齐。"""
    left = min(glyph.bbox[0] for glyph in group)
    right = max(glyph.bbox[2] for glyph in group)
    if abs((left + right - track[0] - track[1]) / 2) <= 0.7 * em:
        return True
    anchors = []
    for cell in cells:
        glyphs = [body_glyphs[i] for i in cell.source_char_indices if i in body_glyphs]
        if not glyphs:
            return False
        x0 = min(glyph.bbox[0] for glyph in glyphs)
        x1 = max(glyph.bbox[2] for glyph in glyphs)
        anchors.append((x0, (x0 + x1) / 2, x1))
    return any(
        all(abs(label - x) <= 0.4 * em for x in positions)
        for label, positions in zip((left, (left + right) / 2, right), zip(*anchors), strict=True)
    )


def _header_line_owners(
    lines: list[_LineItem],
    glyphs: tuple[NativeTableGlyph, ...],
    protected: set[int],
) -> frozenset[int] | None:
    """只移交完整原生行，拒绝部分字形、显式题注、表注及重复来源。"""
    ids = {glyph.source_index for glyph in glyphs}
    owners = set()
    covered: set[int] = set()
    for line in lines:
        chars = {char.get("char_idx") for char in line.chars if str(char.get("char") or "").strip()}
        if not chars & ids:
            continue
        if (
            line.source_index in protected
            or chars - ids
            or chars & covered
            or _TABLE_CAPTION_RE.match(line.text)
            or _is_table_note_text(line.text)
        ):
            return None
        covered.update(chars)
        owners.add(line.source_index)
    return frozenset(owners) if covered == ids and len(ids) == len(glyphs) else None


def _make_label_cell(row: int, col: int, span: int, bbox: BBox, glyphs: list[NativeTableGlyph], em: float) -> NativeTableCell:
    """用既有单元格读序和原始来源编号物化一个已分配表头格。"""
    return NativeTableCell(
        row,
        col,
        1,
        span,
        bbox,
        build_cell_text(glyphs, em),
        tuple(glyph.source_index for glyph in glyphs),
    )


def recover_preceding_table_header(
    source: _PageSource,
    table_input: NativeTableInput,
    body: NativeTableResult,
    protected_lines: set[int],
    obstacles: list[BBox],
) -> _RecoveredTableHeader | None:
    """保持正文网格固定，核验横隔线上紧邻的列名及可选的部分跨列标题。"""
    if body.rows < 3 or body.cols < 3:
        return None
    measures = _leading_measures(body)
    if len(measures) < 2:
        return None
    angle, page_size = table_input.angle, table_input.page_size
    upright = _rotate_bbox_to_upright(table_input.table_bbox, page_size, angle)
    left, top, right, bottom = upright
    em = body.text.median_glyph_height
    width = right - left
    top_limit = max(0.0, top - 3.2 * em)
    if top_limit >= top:
        return None

    orientation = "vertical" if angle in (90, 270) else "horizontal"
    rails = []
    for rule in source.drawing_lines:
        if rule.orientation != orientation:
            continue
        box = _rotate_bbox_to_upright(rule.bbox, page_size, angle)
        if top_limit <= _bbox_center_y(box) <= top + 0.2 * em and min(right, box[2]) > max(left, box[0]):
            rails.append(box)
    if not any(
        abs(_bbox_center_y(box) - top) <= 0.2 * em and min(right, box[2]) - max(left, box[0]) >= 0.95 * width for box in rails
    ):
        return None

    tracks = _column_edges(body, em)
    if tracks is None:
        return None
    search = _rotate_bbox_from_upright((left, top_limit, right, top), page_size, angle)
    lines = [line for line in source.lines if line.angle == angle and bbox_intersection(line.bbox, search)]
    if not lines:
        return None
    # 原生文字预处理同时保留旋转、字距和 source_idx，列界只决定已可独立读出的字形归属。
    header_input = replace(table_input, table_bbox=search)
    native = build_native_table_text(header_input)
    if native is None:
        return None
    row = native.rows[-1]
    height = top - top_limit
    if row.bbox[3] < height - 0.45 * em or not 0.65 * em <= row.bbox[3] - row.bbox[1] <= 1.35 * em:
        return None
    glyphs = {glyph.glyph_id: glyph for glyph in native.glyphs}
    groups = _peer_label_cells(row, glyphs, tracks, em, 0.3 * em)
    if groups is None or len(groups) < max(3, 0.65 * body.cols) or not measures.keys() <= groups.keys():
        return None
    content = {col: build_cell_text(group, native.median_glyph_height) for col, group in groups.items()}
    if not any(any(char.isalpha() for char in value) for value in content.values()):
        return None
    body_glyphs = {glyph.source_index: glyph for glyph in body.text.glyphs}
    if not all(
        _label_aligns_with_measure(groups[col], (tracks[col], tracks[col + 1]), cells, body_glyphs, em)
        for col, cells in measures.items()
    ):
        return None

    upper = native.rows[-2] if len(native.rows) > 1 else None
    partial = _partially_ruled_label(upper, row, tracks, rails, (left, top_limit), em) if upper else None
    if partial is not None:
        start, end, y = partial
        upper_glyphs = [glyphs[i] for i in upper.glyph_ids]
        if (
            not all(col in groups for col in range(start, end))
            or any(glyph.writing_angle != 0 for glyph in upper_glyphs)
            or not any(char.isalpha() for char in upper.tokens[0].text)
        ):
            return None
        header_top = upper.bbox[1]
        cells = [_make_label_cell(0, start, end - start, (tracks[start], header_top, tracks[end], y), upper_glyphs, em)]
        cells.extend(
            _make_label_cell(0, col, 1, (tracks[col], header_top, tracks[col + 1], y), [], em)
            for col in range(body.cols)
            if not start <= col < end
        )
        peer_row, peer_top, rows = 1, y, (upper, row)
    else:
        # 形似正文数据的行需要不同的度量表示，如文字列名或列名整数与小数数据。
        changes = sum(
            not _numeric_value(content[col]) or (content[col].isdecimal() and all("." in cell.content for cell in values))
            for col, values in measures.items()
        )
        if changes <= len(measures) / 2:
            return None
        header_top = row.bbox[1]
        cells = []
        peer_row, peer_top, rows = 0, header_top, (row,)
    cells.extend(
        _make_label_cell(peer_row, col, 1, (a, peer_top, b, height), groups.get(col, []), em)
        for col, (a, b) in enumerate(zip(tracks, tracks[1:]))
    )
    selected = tuple(glyphs[i] for band in rows for i in band.glyph_ids)
    line_indices = _header_line_owners(lines, selected, protected_lines)
    if line_indices is None:
        return None
    header_box = _rotate_bbox_from_upright((left, top_limit + header_top, right, top), page_size, angle)
    if any(bbox_intersection(header_box, box) for box in obstacles):
        return None
    selected_ids = {glyph.source_index for glyph in selected}
    if any(glyph.source_index in selected_ids for glyph in body.text.glyphs):
        return None
    cells = tuple(cells)
    result = replace(
        body,
        rows=peer_row + 1,
        cells=cells,
        html=serialize_native_table_html(peer_row + 1, cells),
        text=NativeTableText(selected, rows, native.median_glyph_width, native.median_glyph_height),
    )
    bbox = _rotate_bbox_from_upright((left, top_limit + header_top, right, bottom), page_size, angle)
    return _RecoveredTableHeader(bbox, line_indices, header_input, result)
