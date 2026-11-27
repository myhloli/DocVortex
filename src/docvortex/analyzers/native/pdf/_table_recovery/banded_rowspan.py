"""以重复横线分组和独立数值行证明居中首列的 rowspan。"""

from __future__ import annotations

import statistics
from collections import defaultdict
from itertools import groupby

from .banded_numeric import _numeric_value
from .candidate import GridCellSpec, build_candidate
from .contracts import NativeTableCandidate, NativeTableCell, NativeTableGlyph, NativeTableInput, NativeTableText
from .geometry import normalize_angle, table_local_size
from .sparse_common import _local_rules, cluster_members
from .sparse_multiline import _build_column_hypothesis, _local_rectangles
from .text import build_cell_text


def build_banded_rowspan_candidate(
    table_input: NativeTableInput,
    original: NativeTableText,
    refined: NativeTableText,
) -> NativeTableCandidate | None:
    """仅修复至少三组重复的两行数值记录；证据不足时保留原有候选路径。"""

    # 正常视觉行不受影响；居中文字桥接会在独立中心组行后多出行。
    if len(refined.rows) <= len(original.rows):
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    rules = _local_rules(table_input, width, height)
    tolerance = max(0.6, 0.12 * refined.median_glyph_height)
    boundaries = sorted({r.coordinate for r in rules if r.orientation == "horizontal" and r.end - r.start >= 0.9 * width})
    if (
        len(boundaries) < 5
        or boundaries[0] > tolerance
        or height - boundaries[-1] > tolerance
        or any(r.orientation == "vertical" and tolerance < r.coordinate < width - tolerance for r in rules)
    ):
        return None
    # 局部横线可能表示更复杂的单元格拓扑，不能用跨行推测覆盖它。
    if any(r.orientation == "horizontal" and min(abs(r.coordinate - y) for y in boundaries) > tolerance for r in rules):
        return None
    hypothesis = _build_column_hypothesis(original, width, rules, _local_rectangles(table_input, width, height), None)
    if hypothesis is None or len(hypothesis.x_tracks) < 4:
        return None
    tracks = hypothesis.x_tracks
    cols = len(tracks) - 1
    header = [r for r in refined.rows if r.bbox[3] <= boundaries[1]]
    if len(header) != 1 or header[0].bbox[1] < boundaries[0]:
        return None
    # 不允许字形跨列；字符来源最终还需全量、唯一落格。
    if any(not any(tracks[c] <= g.bbox[0] and g.bbox[2] <= tracks[c + 1] for c in range(cols)) for g in refined.glyphs):
        return None
    specs = [GridCellSpec(0, c, 1, 1, (tracks[c], 0.0, tracks[c + 1], boundaries[1])) for c in range(cols)]
    logical_row = 1
    for top, bottom in zip(boundaries[1:-1], boundaries[2:]):
        members = [g for g in refined.glyphs if top < (g.bbox[1] + g.bbox[3]) / 2 < bottom]
        labels = [g for g in members if g.bbox[2] <= tracks[1]]
        data = [g for g in members if g.bbox[0] >= tracks[1]]
        indices = sorted({g.visual_row for g in data})
        if not labels or len(indices) != 2 or len({g.visual_row for g in labels}) != 1:
            return None
        rows = [[g for g in data if g.visual_row == i] for i in indices]
        centers = [statistics.median((g.bbox[1] + g.bbox[3]) / 2 for g in row) for row in rows]
        label_center = statistics.median((g.bbox[1] + g.bbox[3]) / 2 for g in labels)
        if (
            not centers[0] < label_center < centers[1]
            or abs(label_center - sum(centers) / 2) > tolerance
            or any(g.bbox[1] < top or g.bbox[3] > bottom for g in members)
        ):
            return None
        # 每个右侧列都必须独立支持两条数值/状态记录，不拆普通段落换行。
        for row in rows:
            for col in range(1, cols):
                owned = sorted(
                    [g for g in row if tracks[col] <= g.bbox[0] and g.bbox[2] <= tracks[col + 1]],
                    key=lambda g: g.bbox[0],
                )
                if not owned or not _numeric_value("".join(g.text for g in owned)):
                    return None
        upper_bottom = max(g.bbox[3] for g in rows[0])
        lower_top = min(g.bbox[1] for g in rows[1])
        if upper_bottom >= lower_top:
            return None
        cut = (upper_bottom + lower_top) / 2
        specs.append(GridCellSpec(logical_row, 0, 2, 1, (tracks[0], top, tracks[1], bottom)))
        for offset, (y0, y1) in enumerate(((top, cut), (cut, bottom))):
            for col in range(1, cols):
                specs.append(GridCellSpec(logical_row + offset, col, 1, 1, (tracks[col], y0, tracks[col + 1], y1)))
        logical_row += 2
    diagnostics: dict[str, object] = {}
    candidate = build_candidate(
        source="sparse_hybrid",
        rows=logical_row,
        cols=cols,
        specs=tuple(specs),
        text=refined,
        structure_support=1.0,
        row_stability=1.0,
        column_stability=1.0,
        issues=("evidence=banded_rowspan",),
        use_grid_index=True,
        diagnostics=diagnostics,
    )
    if (
        candidate is None
        or candidate.text_capture < 1.0
        or candidate.order_consistency < 1.0
        or candidate.score < 0.98
        or diagnostics.get("ambiguous_glyph_ratio", 1.0) > 0.0
    ):
        return None
    sources = [index for cell in candidate.cells for index in cell.source_char_indices]
    if len(sources) != len(set(sources)) or len(sources) != len(refined.glyphs):
        return None
    return candidate


def _repeat_band_rows(
    cells: tuple[NativeTableCell, ...],
    glyphs: dict[int, NativeTableGlyph],
    em: float,
) -> tuple[tuple[str, ...], list[float]] | None:
    """核对同一实体带内的居中组名、完整数据基线和可落格的净空。"""
    top, bottom = cells[0].bbox[1], cells[0].bbox[3]
    if bottom - top < 2.3 * em:
        return None
    by_col: list[dict[int, list[NativeTableGlyph]]] = []
    for cell in cells:
        rows: dict[int, list[NativeTableGlyph]] = defaultdict(list)
        for source in cell.source_char_indices:
            glyph = glyphs[source]
            if glyph.writing_angle != 0:
                return None
            rows[glyph.visual_row].append(glyph)
        by_col.append(rows)
    stub, first_data, *remaining_data = by_col
    if len(stub) != 1 or len(first_data) < 2 or any(rows.keys() != first_data.keys() for rows in remaining_data):
        return None
    indices = sorted(first_data)
    keys = tuple(build_cell_text(first_data[index], em).strip() for index in indices)
    if len(set(keys)) != len(keys) or any(not key for key in keys):
        return None
    if len(remaining_data) < 2 and not all(_numeric_value(key) for key in keys):
        return None
    if any(not _numeric_value(build_cell_text(rows[index], em)) for rows in remaining_data for index in indices):
        return None

    data = [[glyph for rows in by_col[1:] for glyph in rows[index]] for index in indices]
    bounds = [(min(g.bbox[1] for g in row), max(g.bbox[3] for g in row)) for row in data]
    centers = [(a + b) / 2 for a, b in bounds]
    stub_center = statistics.median((g.bbox[1] + g.bbox[3]) / 2 for group in stub.values() for g in group)
    tolerance = max(0.6, 0.15 * em)
    if (
        not centers[0] < stub_center < centers[-1]
        or abs(stub_center - (centers[0] + centers[-1]) / 2) > tolerance
        or abs(stub_center - (top + bottom) / 2) > tolerance
        or any(previous[1] >= current[0] for previous, current in zip(bounds, bounds[1:]))
    ):
        return None
    return keys, [top, *((previous[1] + current[0]) / 2 for previous, current in zip(bounds, bounds[1:])), bottom]


def _restore_physical_bands(
    coarse: NativeTableCandidate,
    text: NativeTableText,
    lines: set[float],
    height: float,
    tolerance: float,
) -> NativeTableCandidate | None:
    """少线网格保留原列，在实体横轨形成的行带重新核对字形归属。"""
    edges = [center for center, _ in cluster_members([line for line in lines if 0 <= line <= height], tolerance)]
    if len(edges) < 4 or edges[0] > tolerance or height - edges[-1] > tolerance:
        return None
    edges[0], edges[-1] = 0.0, height
    first = coarse.cells[: coarse.cols]
    x_tracks = [cell.bbox[0] for cell in first] + [first[-1].bbox[2]]
    if any(cell.bbox[0] != x_tracks[cell.col] or cell.bbox[2] != x_tracks[cell.col + 1] for cell in coarse.cells):
        return None
    specs = tuple(
        GridCellSpec(row, col, 1, 1, (left, top, right, bottom))
        for row, (top, bottom) in enumerate(zip(edges, edges[1:]))
        for col, (left, right) in enumerate(zip(x_tracks, x_tracks[1:]))
    )
    stats: dict[str, object] = {}
    grouped = build_candidate(
        source=coarse.source,
        rows=len(edges) - 1,
        cols=coarse.cols,
        specs=specs,
        text=text,
        structure_support=coarse.structure_support,
        row_stability=coarse.row_stability,
        column_stability=coarse.column_stability,
        issues=coarse.issues,
        use_grid_index=True,
        diagnostics=stats,
    )
    if grouped is None or grouped.text_capture < 1.0 or stats.get("ambiguous_glyph_ratio") != 0.0:
        return None
    old_columns = {source: cell.col for cell in coarse.cells for source in cell.source_char_indices}
    new_columns = {source: cell.col for cell in grouped.cells for source in cell.source_char_indices}
    return grouped if old_columns == new_columns else None


def refine_repeated_body_bands(
    table_input: NativeTableInput,
    text: NativeTableText,
    coarse: NativeTableCandidate,
) -> NativeTableCandidate | None:
    """用重复记录细分已验证的横线带，只在原首列格内建立纵向跨度。"""
    if (
        coarse.rows < 4
        or coarse.cols < 3
        or len(text.rows) < coarse.rows + 3
        or coarse.text_capture < 1.0
        or any(cell.rowspan != 1 or cell.colspan != 1 for cell in coarse.cells)
    ):
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    tolerance = max(0.6, 0.12 * text.median_glyph_height)
    full_lines = {
        rule.coordinate
        for rule in _local_rules(table_input, width, height)
        if rule.orientation == "horizontal" and rule.start <= 0.05 * width and rule.end >= 0.95 * width
    }
    if len(full_lines) < 4:
        return None
    original = coarse
    if "evidence=rule_band" in coarse.issues:
        grouped = _restore_physical_bands(coarse, text, full_lines, height, tolerance)
        if grouped is None:
            return None
        coarse = grouped
    glyphs = {glyph.source_index: glyph for glyph in text.glyphs}
    bands = [tuple(cells) for _, cells in groupby(coarse.cells, key=lambda cell: cell.row)]
    splits = {}
    for row, cells in enumerate(bands):
        edges = (cells[0].bbox[1], cells[0].bbox[3])
        if not all(any(abs(edge - line) <= tolerance for line in full_lines) for edge in edges):
            continue
        if split := _repeat_band_rows(cells, glyphs, text.median_glyph_height):
            splits[row] = split
    # 三个连续实体带能同时证实组名和独立记录；偶然出现的两行文字仍保留原格。
    if len(splits) < 3 or max(splits) - min(splits) + 1 != len(splits) or len({keys for keys, _ in splits.values()}) != 1:
        return None
    if coarse is not original:
        top, bottom = splits[min(splits)][1][0], splits[max(splits)][1][-1]
        previous = {edge for cell in original.cells for edge in (cell.bbox[1], cell.bbox[3])}
        actual = {edge for cell in coarse.cells for edge in (cell.bbox[1], cell.bbox[3])}
        # 只在已证实的重复体内替换行界，表头和其他部分仍按既有边界。
        if any(
            (edge < top - tolerance or edge > bottom + tolerance)
            and not any(abs(edge - other) <= tolerance for other in target)
            for source, target in ((previous, actual), (actual, previous))
            for edge in source
        ):
            return None

    specs = []
    old_rows = []
    for row, cells in enumerate(bands):
        index = len(old_rows)
        edges = splits[row][1] if row in splits else [cells[0].bbox[1], cells[0].bbox[3]]
        old_rows.extend([row] * (len(edges) - 1))
        stub = cells[0]
        specs.append(GridCellSpec(index, 0, len(edges) - 1, 1, stub.bbox))
        for offset, (top, bottom) in enumerate(zip(edges, edges[1:])):
            for cell in cells[1:]:
                x0, _, x1, _ = cell.bbox
                specs.append(GridCellSpec(index + offset, cell.col, 1, 1, (x0, top, x1, bottom)))
    diagnostics: dict[str, object] = {}
    refined = build_candidate(
        source=coarse.source,
        rows=len(old_rows),
        cols=coarse.cols,
        specs=tuple(specs),
        text=text,
        structure_support=coarse.structure_support,
        row_stability=coarse.row_stability,
        column_stability=coarse.column_stability,
        issues=(*coarse.issues, "evidence=repeated_body_bands"),
        align_rowspans=True,
        use_grid_index=True,
        diagnostics=diagnostics,
    )
    if (
        refined is None
        or refined.text_capture < 1.0
        or refined.order_consistency < 1.0
        or diagnostics.get("ambiguous_glyph_ratio") != 0.0
    ):
        return None
    # 允许原物理竖线已证实的分词，逐旧格核对归属防止任何列或实体行带间的迁移。
    previous_owners = {source: (cell.row, cell.col) for cell in coarse.cells for source in cell.source_char_indices}
    new_owners = {source: (old_rows[cell.row], cell.col) for cell in refined.cells for source in cell.source_char_indices}
    return refined if previous_owners == new_owners else None
