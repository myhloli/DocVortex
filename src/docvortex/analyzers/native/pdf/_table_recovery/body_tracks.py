"""用完整表头横隔线与重复正文列的基线恢复少线表的逻辑行。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from math import ceil
from statistics import median

from .candidate import GridCellSpec, build_candidate
from .contracts import NativeTableCandidate, NativeTableInput, NativeTableText
from .geometry import normalize_angle, table_local_size
from .sparse_common import _local_rules, cluster_members
from .sparse_multiline import _build_column_hypothesis, _local_rectangles, _separator_coverage


def _token_slots(row, tracks):
    """要求正文中每个原生片段唯一落在一列，不能将重叠文本硬切成数值。"""
    slots = {}
    for token in row.tokens:
        left, _, right, _ = token.bbox
        col = bisect_right(tracks, (left + right) / 2) - 1
        if not 0 <= col < len(tracks) - 1 or left < tracks[col] - 0.25 or right > tracks[col + 1] + 0.25 or col in slots:
            return None
        slots[col] = token
    return slots


def _parallel_wrapped_prose(slots, cols, em):
    """多栏长文字各自左齐、跨多行仅末行结句时，同步文字基线不足以确认数据行。"""
    if any(len(row) != cols for row in slots):
        return False
    stops = (".", "!", "?", "。", "！", "？")
    for col in range(cols):
        tokens = [row[col] for row in slots]
        if (
            not tokens[-1].text.endswith(stops)
            or any(t.text.endswith(stops) for t in tokens[:-1])
            or any(t.bbox[2] - t.bbox[0] < 3 * em or not any(c.isalpha() for c in t.text) for t in tokens)
            or max(t.bbox[0] for t in tokens) - min(t.bbox[0] for t in tokens) > 0.35 * em
        ):
            return False
    return True


def _body_boundaries(rows, tracks, rails, em):
    """以多列同步出现的正文基线作行锚，单列折行只在近邻与物理屏障均支持时归属。"""
    slots = [_token_slots(row, tracks) for row in rows]
    if any(s is None for s in slots):
        return None
    cols = len(tracks) - 1
    if not rails and _parallel_wrapped_prose(slots, cols, em):
        return None
    anchors = [i for i, s in enumerate(slots) if len(s) >= max(2, ceil(0.65 * cols))]
    if len(anchors) < 3 or sum(len(slots[i]) == cols for i in anchors) < 2:
        return None
    # 每条列轨都须由多行独立确认；空格不可以直接用周围的词填上。
    for col in range(cols):
        values = [slots[i][col].bbox for i in anchors if col in slots[i]]
        if len(values) < max(2, ceil(0.45 * len(anchors))):
            return None
        alignments = [[box[0] for box in values], [box[2] for box in values], [(box[0] + box[2]) / 2 for box in values]]
        centers = [median(axis) for axis in alignments]
        if all(
            sum(abs(x - center) <= 0.35 * em for x in axis) < 0.75 * len(values) for axis, center in zip(alignments, centers)
        ):
            return None
    centers = [(row.bbox[1] + row.bbox[3]) / 2 for row in rows]
    bounds = [[rows[i].bbox[1], rows[i].bbox[3]] for i in anchors]
    anchored = set(anchors)
    for i, s in enumerate(slots):
        if i in anchored:
            continue
        if len(s) != 1:
            return None
        insertion = bisect_left(anchors, i)
        # 基线按纵坐标有序，单栏续行只需比较左右最近的两个行锚及其后继。
        nearby = range(max(0, insertion - 2), min(len(anchors), insertion + 2))
        distances = sorted((abs(centers[i] - centers[anchors[k]]), k) for k in nearby)
        distance, k = distances[0]
        if distance > 1.2 * em or distance >= 0.85 * distances[1][0]:
            return None
        lo, hi = sorted((centers[i], centers[anchors[k]]))
        if any(lo < y < hi for y in rails):
            return None
        bounds[k][0] = min(bounds[k][0], rows[i].bbox[1])
        bounds[k][1] = max(bounds[k][1], rows[i].bbox[3])
    edges = []
    for (_, bottom), (top, _) in zip(bounds, bounds[1:]):
        if top < bottom - 0.25:
            return None
        dividers = [y for y in rails if bottom - 0.25 <= y <= top + 0.25]
        if len(dividers) > 1:
            return None
        edges.append(dividers[0] if dividers else (top + bottom) / 2)
    if any(y not in edges for y in rails):
        return None
    return edges


def _grouped_header_cells(header, tracks, rules, bottom, em):
    """用横隔线上方的实体纵轨定义分组，要求组标题与各子列名都对应真实文字。"""
    if len(header) < 2 or header[0].bbox[3] >= header[-1].bbox[1]:
        return None
    cols = len(tracks) - 1
    cuts = {0, cols}
    for rule in rules:
        if rule.orientation != "vertical" or rule.start > header[0].bbox[1] or rule.end < header[-1].bbox[3]:
            continue
        idx = min(range(len(tracks)), key=lambda j: abs(tracks[j] - rule.coordinate))
        if abs(tracks[idx] - rule.coordinate) > 0.4 * em:
            return None
        cuts.add(idx)
    if not 2 < len(cuts) < len(tracks):
        return None
    cuts = sorted(cuts)
    y = (header[0].bbox[3] + header[-1].bbox[1]) / 2
    # 若两个文字带之间印有横线，沿用已有横线覆盖计算；被横线绕开的单列是纵向整格。
    partial_rules = [
        r
        for r in rules
        if r.orientation == "horizontal"
        and header[0].bbox[3] <= r.coordinate <= header[-1].bbox[1]
        and r.end - r.start >= 0.2 * tracks[-1]
    ]
    printed = max(partial_rules, key=lambda r: r.end - r.start, default=None)
    if printed is not None:
        y = printed.coordinate
    tokens = [token for row in header for token in row.tokens]
    specs = []
    for left, right in zip(cuts, cuts[1:]):
        a, b = tracks[left], tracks[right]
        group = [t for t in tokens if a <= (t.bbox[0] + t.bbox[2]) / 2 <= b]
        if not group or any(t.bbox[0] < a - 0.25 or t.bbox[2] > b + 0.25 for t in group):
            return None
        if right == left + 1 and (
            printed is None
            and len(group) == 1
            or printed is not None
            and _separator_coverage(rules, y, a, b, 0.25 * em) <= 0.20
        ):
            specs.append(GridCellSpec(0, left, 2, 1, (a, 0.0, b, bottom)))
            continue
        upper = [t for t in group if t.bbox[3] <= y]
        lower = [t for t in group if t.bbox[1] >= y]
        if len(upper) != 1 or len(lower) != right - left or len(group) != len(upper) + len(lower):
            return None
        specs.append(GridCellSpec(0, left, 1, right - left, (a, 0.0, b, y)))
        for col, token in zip(range(left, right), sorted(lower, key=lambda t: t.bbox[0]), strict=True):
            if token.bbox[0] < tracks[col] - 0.25 or token.bbox[2] > tracks[col + 1] + 0.25:
                return None
            specs.append(GridCellSpec(1, col, 1, 1, (tracks[col], y, tracks[col + 1], bottom)))
    return specs


def build_body_track_candidate(table_input: NativeTableInput, text: NativeTableText) -> NativeTableCandidate | None:
    """仅为已定位且含完整表头隔线的表构造可核验的正文网格，保留不确定布局的旧结果。"""
    if len(text.rows) < 4 or len(table_input.drawing_lines) < 3:
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    rules = _local_rules(table_input, width, height)
    em = text.median_glyph_height
    tolerance = max(1.0, 0.15 * em)
    rails = sorted({r.coordinate for r in rules if r.orientation == "horizontal" and r.end - r.start >= 0.95 * width})
    if len(rails) < 3 or rails[0] > tolerance or height - rails[-1] > tolerance:
        return None
    top = rails[1]
    header = [r for r in text.rows if r.bbox[3] <= top + 0.25]
    body = [r for r in text.rows if r.bbox[1] >= top - 0.25]
    if not 1 <= len(header) <= 3 or len(header) + len(body) != len(text.rows):
        return None
    hypothesis = _build_column_hypothesis(text, width, rules, _local_rectangles(table_input, width, height), None)
    if hypothesis is None:
        return None
    tracks = hypothesis.x_tracks
    cols = len(tracks) - 1
    # 若每个内列都有贯穿表头和正文的实线，文字对齐仅证明列数，物理分界优先。
    full_verticals = [
        r.coordinate
        for r in rules
        if r.orientation == "vertical"
        and r.start <= tolerance
        and r.end >= height - tolerance
        and tolerance < r.coordinate < width - tolerance
    ]
    physical = [x for x, _ in cluster_members(full_verticals, tolerance)]
    if len(physical) == cols - 1:
        tracks = (0.0, *physical, width)
    edges = _body_boundaries(body, tracks, rails[2:-1], em)
    if edges is None:
        return None
    verticals = [
        r.coordinate for r in rules if r.orientation == "vertical" and r.start <= tolerance and r.end >= top - tolerance
    ]
    merged_title = (
        len(header) == 1
        and len(header[0].tokens) == 1
        and abs((header[0].bbox[0] + header[0].bbox[2] - width) / 2) < em
        and not any(tolerance < x < width - tolerance for x in verticals)
    )
    grouped = _grouped_header_cells(header, tracks, rules, top, em)
    header_rows = 2 if grouped else 1
    if grouped:
        specs = grouped
    elif merged_title:
        specs = [GridCellSpec(0, 0, 1, cols, (0.0, 0.0, width, top))]
    else:
        occupied = [_token_slots(row, tracks) for row in header]
        if any(s is None for s in occupied) or len(set().union(*occupied)) != cols:
            return None
        if len(occupied) > 1 and any(len(s) != cols for s in occupied):
            if any(not any(abs(x - v) <= 0.4 * em for v in verticals) for x in tracks[1:-1]):
                return None
        specs = [GridCellSpec(0, j, 1, 1, (a, 0.0, b, top)) for j, (a, b) in enumerate(zip(tracks, tracks[1:]))]
    ys = [top, *edges, height]
    specs.extend(
        GridCellSpec(i, j, 1, 1, (a, y, b, end))
        for i, (y, end) in enumerate(zip(ys, ys[1:]), header_rows)
        for j, (a, b) in enumerate(zip(tracks, tracks[1:]))
    )
    metrics = {}
    candidate = build_candidate(
        source="sparse_multiline",
        rows=header_rows + len(ys) - 1,
        cols=cols,
        specs=tuple(specs),
        text=text,
        structure_support=1.0,
        row_stability=1.0,
        column_stability=1.0,
        issues=("evidence=ruled_header_body_tracks",),
        require_atomic_tokens=True,
        use_grid_index=True,
        diagnostics=metrics,
    )
    if (
        candidate is None
        or candidate.text_capture < 1.0
        or candidate.order_consistency < 1.0
        or candidate.score < 0.98
        or metrics.get("ambiguous_glyph_ratio")
    ):
        return None
    return candidate
