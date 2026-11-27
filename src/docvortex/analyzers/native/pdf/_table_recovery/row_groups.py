"""用原生横竖线和独立数据行恢复纵向分组。"""

from __future__ import annotations

from bisect import bisect_right
from statistics import median

from .banded_numeric import _numeric_value
from .body_tracks import _token_slots
from .candidate import GridCellSpec, build_fully_assigned_table
from .contracts import NativeTableCandidate, NativeTableInput, NativeTableText
from .geometry import bbox_center, normalize_angle, table_local_size
from .sparse_common import _local_rules


def _body_columns(rows, em):
    """合并正文在横轴上的连续字带，每个独立列带至少见于两行。"""
    intervals = sorted((t.bbox[0], t.bbox[2], row.row_index) for row in rows for t in row.tokens)
    groups = []
    for left, right, line in intervals:
        if groups and left <= groups[-1][1] + 0.12 * em:
            groups[-1][1] = max(groups[-1][1], right)
            groups[-1][2].add(line)
        else:
            groups.append([left, right, {line}])
    if not 4 <= len(groups) <= 20 or any(len(lines) < 2 for _, _, lines in groups):
        return None
    return [(left, right) for left, right, _ in groups]


def _tracks_with_header(text, header, columns, width, em):
    """共同列缝经表头实际字隙复核，允许断开有清晰原生空格的相邻列名。"""
    glyphs = sorted((g for g in text.glyphs if g.visual_row == header.row_index), key=lambda g: g.bbox[0])
    gaps = [
        (a.bbox[2], b.bbox[0])
        for a, b in zip(glyphs, glyphs[1:])
        if b.bbox[0] - a.bbox[2] >= (0.25 if b.explicit_space_before else 0.5) * em
    ]
    tracks = [0.0]
    for (_, a), (b, _) in zip(columns, columns[1:]):
        usable = [(max(a, left), min(b, right)) for left, right in gaps if min(b, right) - max(a, left) >= 0.18 * em]
        if len(usable) != 1:
            return None
        left, right = usable[0]
        tracks.append((left + right) / 2)
    tracks.append(width)
    if len({bisect_right(tracks, (g.bbox[0] + g.bbox[2]) / 2) for g in glyphs}) != len(columns):
        return None
    return tracks


def _data_rows(slots, cols):
    """选取始终共享右侧连续列的独立行，保留左边带中的组值供单独落格。"""
    anchors = [i for i, row in enumerate(slots) if cols - 2 in row and cols - 1 in row and _numeric_value(row[cols - 1].text)]
    if len(anchors) < 6:
        return None
    common = set.intersection(*(set(slots[i]) for i in anchors))
    suffix = cols - 2
    while suffix - 1 in common:
        suffix -= 1
    anchored = set(anchors)
    if suffix == 0 or any(j >= suffix for i, row in enumerate(slots) if i not in anchored for j in row):
        return None
    return anchors, suffix


def _row_edges(data, mapped_rules, top, height):
    """每两条独立数据行用净空切分，存在印刷横线时须恰落在同一净空。"""
    edges = [top]
    index = 0
    for (_, bottom), (start, _) in zip(data, data[1:]):
        if bottom >= start:
            return None
        if index < len(mapped_rules) and mapped_rules[index][0] <= start:
            coordinate, _ = mapped_rules[index]
            if coordinate < bottom:
                return None
            edges.append(coordinate)
            index += 1
        else:
            edges.append((bottom + start) / 2)
    return [*edges, height] if index == len(mapped_rules) else None


def _span_specs(tracks, edges, slots, suffix, data, mapped_rules, em):
    """按横线横向覆盖确定每列的分组边界，每段恰有一个居中的源文字项。"""
    centers = [(a + b) / 2 for a, b in data]
    pitch = median(b - a for a, b in zip(centers, centers[1:]))
    rails = dict(mapped_rules)
    specs = []
    for col in range(suffix):
        members = [row[col] for row in slots if col in row]
        boundaries = [i for i, y in enumerate(edges[1:-1], 1) if y in rails and rails[y] <= col]
        top = 0
        consumed = 0
        for stop in (*boundaries, len(data)):
            if stop - top < 2 or consumed >= len(members):
                return None
            token = members[consumed]
            low, high = edges[top], edges[stop]
            if not low <= token.bbox[1] < token.bbox[3] <= high:
                return None
            center = (token.bbox[1] + token.bbox[3]) / 2
            target = (centers[top] + centers[stop - 1]) / 2
            tolerance = min(0.8 * em, (0.36 if stop - top == 2 else 0.55) * pitch)
            if abs(center - target) > tolerance:
                return None
            specs.append(GridCellSpec(top + 1, col, stop - top, 1, (tracks[col], low, tracks[col + 1], high)))
            top = stop
            consumed += 1
        if consumed != len(members):
            return None
    return specs


def _paired_header_specs(header, tracks, vertical, bottom, em):
    """无短横线时仅接纳各自居中且不与相邻候选重叠的双列上级标题。"""
    upper, lower = header
    leaves = _token_slots(lower, tracks)
    cols = len(tracks) - 1
    if leaves is None or len(leaves) != cols or not 2 <= len(upper.tokens) <= cols // 2:
        return None
    if upper.bbox[3] >= lower.bbox[1]:
        return None
    division = (upper.bbox[3] + lower.bbox[1]) / 2
    pairs = []
    owned = set()
    centers = [bbox_center(leaves[i].bbox)[0] for i in range(cols)]
    for parent in upper.tokens:
        center = bbox_center(parent.bbox)[0]
        choices = [
            i
            for i, (a, b) in enumerate(zip(centers, centers[1:]))
            if b - a >= 1.5 * em
            and abs(center - (a + b) / 2) <= 0.22 * em
            and tracks[i] <= parent.bbox[0] < parent.bbox[2] <= tracks[i + 2]
            and not any(a < r.coordinate < b and r.start < upper.bbox[3] and upper.bbox[1] < r.end for r in vertical)
        ]
        if len(choices) != 1:
            return None
        i = choices[0]
        if i == 0 or {i, i + 1} & owned:
            return None
        owned.update((i, i + 1))
        pairs.append(i)
    specs = [GridCellSpec(0, i, 1, 2, (tracks[i], 0.0, tracks[i + 2], division)) for i in pairs]
    specs.extend(
        GridCellSpec(1, i, 1, 1, (a, division, b, bottom)) if i in owned else GridCellSpec(0, i, 2, 1, (a, 0.0, b, bottom))
        for i, (a, b) in enumerate(zip(tracks, tracks[1:]))
    )
    return specs


def _physical_stub_band(slots, cols, top, bottom, rail_x, vertical, em):
    """实际竖线贯穿每个组的数值记录，组名独立于任一正常数据基线。"""
    anchored = []
    labels = []
    for row in slots:
        if 0 in row:
            labels.append(row[0])
        values = [t for j, t in row.items() if j]
        if not values:
            continue
        if len(values) != cols - 1 or not all(_numeric_value(t.text) for t in values):
            return None
        lo, hi = min(t.bbox[1] for t in values), max(t.bbox[3] for t in values)
        if not top < lo < hi < bottom:
            return None
        anchored.append((lo, hi))
    if len(labels) != 1 or len(anchored) < 3:
        return None
    label = labels[0]
    if not any(c.isalpha() for c in label.text) or not top < label.bbox[1] < label.bbox[3] < bottom:
        return None
    centers = [(lo + hi) / 2 for lo, hi in anchored]
    steps = [b - a for a, b in zip(centers, centers[1:])]
    step = median(steps)
    cy = (label.bbox[1] + label.bbox[3]) / 2
    extent = centers[-1] - centers[0]
    if (
        step < em
        or max(abs(s - step) for s in steps) > 0.18 * em
        or not centers[0] + 0.20 * extent <= cy <= centers[-1] - 0.20 * extent
        or any(abs(cy - y) < 0.3 * em for y in centers)
        or not any(
            abs(r.coordinate - rail_x) <= 0.25 * em and r.start <= anchored[0][0] and r.end >= anchored[-1][1] for r in vertical
        )
    ):
        return None
    return anchored


def _ordinary_specs(tracks, edges, *, first_row=0, first_col=0):
    """为独立数据行或普通表头填充全部未合并单元。"""
    return [
        GridCellSpec(i, j, 1, 1, (tracks[j], a, tracks[j + 1], b))
        for i, (a, b) in enumerate(zip(edges, edges[1:]), first_row)
        for j in range(first_col, len(tracks) - 1)
    ]


def _stepped_specs(rails, full, slots, columns, tracks, height, em):
    """短横线只分割其右侧列，逐段检验未切开组名和独立数据。"""
    indices = _data_rows(slots, len(columns))
    if indices is None:
        return None
    anchors, suffix = indices
    mapped = []
    for i, rule in enumerate(rails[2:-1], 2):
        if i in full:
            mapped.append((rule.coordinate, 0))
            continue
        # 横线起点须在真实列缝；右端须覆盖最后一列已经独立确认的数值带。
        a = next((j for j in range(1, suffix + 1) if columns[j - 1][1] < rule.start < columns[j][0]), None)
        if a is None or rule.end < columns[-1][1] - 0.25 * em:
            return None
        mapped.append((rule.coordinate, a))
    if len({a for _, a in mapped if a}) != 1:
        return None

    top = rails[1].coordinate
    data = [
        (
            min(slots[i][j].bbox[1] for j in range(suffix, len(columns))),
            max(slots[i][j].bbox[3] for j in range(suffix, len(columns))),
        )
        for i in anchors
    ]
    if data[-1][1] > rails[-1].coordinate:
        return None
    edges = _row_edges(data, mapped, top, height)
    if edges is None:
        return None
    spans = _span_specs(tracks, edges, slots, suffix, data, mapped, em)
    if spans is None:
        return None
    return [
        *_ordinary_specs(tracks, (0.0, top)),
        *spans,
        *_ordinary_specs(tracks, edges, first_row=1, first_col=suffix),
    ]


def _ruled_stub_track(header, body, columns, vertical, top, em):
    """确定稳定首列实竖轨，拒绝与正文字形带或表头相冲突的线段。"""
    if any(top < r.end and r.start < body[-1].bbox[3] and any(a < r.coordinate < b for a, b in columns) for r in vertical):
        return None
    first_gap = columns[0][1], columns[1][0]
    rails = [r.coordinate for r in vertical if first_gap[0] < r.coordinate < first_gap[1]]
    if not rails or max(rails) - min(rails) > 0.25 * em:
        return None
    rail_x = median(rails)
    leaves = header[-1].tokens
    return rail_x if len(leaves) >= 2 and leaves[0].bbox[2] < rail_x < leaves[1].bbox[0] else None


def _ruled_specs(rails, vertical, header, body, slots, tracks, height, em):
    """全宽横线界定各组，组内通过真实首列竖轨和非同基线标签证明跨行。"""
    top = rails[1].coordinate
    if len(header) == 1:
        leaves = _token_slots(header[0], tracks)
        if leaves is None or len(leaves) != len(tracks) - 1:
            return None
        specs = _ordinary_specs(tracks, (0.0, top))
    else:
        specs = _paired_header_specs(header, tracks, vertical, top, em)
        if specs is None:
            return None
    bands = [[] for _ in range(len(rails) - 2)]
    band = 0
    for row, members in zip(body, slots, strict=True):
        while band + 2 < len(rails) - 1 and row.bbox[1] > rails[band + 2].coordinate:
            band += 1
        if row.bbox[3] >= rails[band + 2].coordinate:
            return None
        bands[band].append(members)
    start = len(header)
    rail_x = tracks[1]
    for i, members in enumerate(bands):
        upper, lower = rails[i + 1].coordinate, rails[i + 2].coordinate
        anchored = _physical_stub_band(members, len(tracks) - 1, upper, lower, rail_x, vertical, em)
        if anchored is None:
            return None
        finish = height if i == len(bands) - 1 else lower
        edges = _row_edges(anchored, [], upper, finish)
        if edges is None:
            return None
        specs.append(GridCellSpec(start, 0, len(anchored), 1, (0.0, upper, rail_x, finish)))
        specs.extend(_ordinary_specs(tracks, edges, first_row=start, first_col=1))
        start += len(anchored)
    return specs


def build_row_group_candidate(table_input: NativeTableInput, text: NativeTableText) -> NativeTableCandidate | None:
    """从重复物理组界和独立数据行证明跨行归属；没证实的空单元沿用旧行为。"""
    if len(text.rows) < 7 or len(table_input.drawing_lines) < 5:
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    em = text.median_glyph_height
    rules = _local_rules(table_input, width, height)
    rails = sorted((r for r in rules if r.orientation == "horizontal"), key=lambda r: r.coordinate)
    full = {i for i, r in enumerate(rails) if r.start <= em and r.end >= width - em}
    if (
        len(full) < 4
        or not {0, 1, len(rails) - 1}.issubset(full)
        or rails[0].coordinate > 0.4 * em
        or height - rails[-1].coordinate > 0.4 * em
    ):
        return None
    vertical = [r for r in rules if r.orientation == "vertical" and 0.5 * em < r.coordinate < width - 0.5 * em]
    stepped = len(full) < len(rails)
    if (stepped and (len(rails) - len(full) < 2 or vertical)) or (not stepped and not vertical):
        return None

    top = rails[1].coordinate
    header_count = sum(row.bbox[3] <= top for row in text.rows[:3])
    if not 1 <= header_count <= (1 if stepped else 2):
        return None
    header, body = text.rows[:header_count], text.rows[header_count:]
    if top > body[0].bbox[1]:
        return None
    columns = _body_columns(body, em)
    if columns is None:
        return None
    tracks = _tracks_with_header(text, header[-1], columns, width, em)
    if tracks is None:
        return None
    if not stepped:
        rail_x = _ruled_stub_track(header, body, columns, vertical, top, em)
        if rail_x is None:
            return None
        tracks[1] = rail_x
    slots = [_token_slots(row, tracks) for row in body]
    if any(row is None for row in slots):
        return None

    if stepped:
        specs = _stepped_specs(rails, full, slots, columns, tracks, height, em)
        evidence = "stepped_body_row_groups"
    else:
        specs = _ruled_specs(rails, vertical, header, body, slots, tracks, height, em)
        evidence = "ruled_body_row_groups"
    return build_fully_assigned_table(specs, text, evidence, allow_header_token_split=stepped) if specs else None
