"""从重复完整数据行确定叶列，按局部横线与子列标题的中心恢复上层列组。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from statistics import median

from .body_tracks import _parallel_wrapped_prose, _token_slots
from .candidate import GridCellSpec, build_candidate
from .contracts import NativeTableCandidate, NativeTableInput, NativeTableText
from .geometry import normalize_angle, table_local_size
from .sparse_common import _local_rules


@dataclass(frozen=True, slots=True)
class _Heading:
    """记录一个有文字的列标题及其连续叶列，层级自下而上计数。"""

    start: int
    stop: int
    level: int
    center: float


def _group_children(parents, children, tracks, rails, em):
    """局部横线限定组的子列，缺横线时要求有序子标题的中心共同指向唯一上级。"""
    owned = [[] for _ in parents]
    centers = [(p.bbox[0] + p.bbox[2]) / 2 for p in parents]
    physical = []
    for center in centers:
        lines = [r for r in rails if r.start < center < r.end]
        if len(lines) > 1:
            return None
        physical.append(lines[0] if lines else None)
    if rails and (None in physical or len(set(physical)) != len(rails)):
        return None
    for child in children:
        if rails:
            choices = [i for i, r in enumerate(physical) if r.start - 0.25 <= child.center <= r.end + 0.25]
            if not choices:
                continue
            if len(choices) != 1:
                return None
            owner = choices[0]
        else:
            owner = min(range(len(parents)), key=lambda i: abs(centers[i] - child.center))
        owned[owner].append(child)
    for p, center, kids in zip(parents, centers, owned, strict=True):
        if len(kids) < 2 or any(a.stop != b.start for a, b in zip(kids, kids[1:])):
            return None
        midpoint = (kids[0].center + kids[-1].center) / 2
        pitch = min(b.center - a.center for a, b in zip(kids, kids[1:]))
        if abs(center - midpoint) > min(1.25 * em, 0.22 * pitch):
            return None
        # 没有下划线的短标题若同时正对某个子列，其实也可能只是一格里的两行文字。
        if not rails and any(
            abs(center - child.center) < 0.45 * em and tracks[child.start] <= p.bbox[0] and p.bbox[2] <= tracks[child.stop]
            for child in kids
        ):
            return None
        if p.bbox[0] < tracks[kids[0].start] - 0.25 or p.bbox[2] > tracks[kids[-1].stop] + 0.25:
            return None
    return owned


def _header_specs(header, tracks, rules, bottom, em):
    """自下而上构造列标题树，合并同列折行，将独立行标题纵向贯通。"""
    leaves = _token_slots(header[-1], tracks)
    if leaves is None or len(leaves) < 2:
        return None
    cols = len(tracks) - 1
    active = [_Heading(i, i + 1, 0, (t.bbox[0] + t.bbox[2]) / 2) for i, t in sorted(leaves.items())]
    nodes = list(active)
    parent_level = {}
    stubs = set(range(cols)) - leaves.keys()
    stub_text = set()
    separators = [bottom]
    ruled_groups = set()
    layer_top = header[-1].bbox[1]
    for row in reversed(header[:-1]):
        parents = []
        for t in row.tokens:
            center = (t.bbox[0] + t.bbox[2]) / 2
            col = bisect_right(tracks, center) - 1
            if col in stubs and tracks[col] - 0.25 <= t.bbox[0] and t.bbox[2] <= tracks[col + 1] + 0.25:
                if col in stub_text:
                    return None
                stub_text.add(col)
            else:
                parents.append(t)
        if not parents:
            continue
        # 叶标题可有第二行单位或样本量。至少两列各自在叶标题中心对齐才允许合并。
        if len(separators) == 1:
            occupied = _token_slots(row, tracks)
            if (
                occupied
                and len(parents) >= 2
                and all(
                    col in stubs
                    or (col in leaves and abs(t.bbox[0] + t.bbox[2] - leaves[col].bbox[0] - leaves[col].bbox[2]) <= 0.9 * em)
                    for col, t in occupied.items()
                )
            ):
                layer_top = row.bbox[1]
                continue
        parent_bottom = max(t.bbox[3] for t in parents)
        if parent_bottom >= layer_top:
            return None
        rails = [
            r
            for r in rules
            if r.orientation == "horizontal" and parent_bottom <= r.coordinate <= layer_top and r.end - r.start > em
        ]
        if rails and max(r.coordinate for r in rails) - min(r.coordinate for r in rails) > 0.15 * em:
            return None
        owned = _group_children(parents, active, tracks, rails, em)
        if owned is None:
            return None
        level = len(separators)
        separators.append(median(r.coordinate for r in rails) if rails else (parent_bottom + layer_top) / 2)
        next_active = []
        for token, children in zip(parents, owned, strict=True):
            parent_level.update((child, level) for child in children)
            new = _Heading(children[0].start, children[-1].stop, level, (token.bbox[0] + token.bbox[2]) / 2)
            if rails:
                ruled_groups.add((new.start, new.stop))
            nodes.append(new)
            next_active.append(new)
        active = next_active
        layer_top = min(p.bbox[1] for p in parents)
    levels = len(separators)
    if levels < 2:
        return None
    ys = [0.0, *reversed(separators)]
    specs = []
    for node in nodes:
        top = levels - parent_level.get(node, levels)
        end = levels - node.level
        specs.append(
            GridCellSpec(
                top, node.start, end - top, node.stop - node.start, (tracks[node.start], ys[top], tracks[node.stop], ys[end])
            )
        )
    specs.extend(GridCellSpec(0, col, levels, 1, (tracks[col], 0, tracks[col + 1], bottom)) for col in stubs)
    return levels, specs, ruled_groups


def _data_tracks(rows, width, em):
    """各完整数据行都须支持相同的净空列缝，不将表头或分节文字计入列数。"""
    counts = Counter(len(row.tokens) for row in rows)
    cols, n = counts.most_common(1)[0]
    if not 3 <= cols <= 20 or n < max(3, 0.65 * len(rows)) or set(counts) - {1, cols}:
        return None
    full = [row for row in rows if len(row.tokens) == cols]
    tracks = [0.0]
    for col in range(cols - 1):
        left = max(r.tokens[col].bbox[2] for r in full)
        right = min(r.tokens[col + 1].bbox[0] for r in full)
        if right - left < 0.35 * em:
            return None
        tracks.append((left + right) / 2)
    tracks.append(width)
    return tracks


def _section_has_distinct_widths(rows, index, edges, barriers, cols, em):
    """在同一分节的连续完整子项中验证左对齐，遇到横线、下一分节或缩进变化即停止。"""
    first, second = (r.tokens[0].bbox for r in rows[index + 1 : index + 3])
    min_right, max_right = sorted((first[2], second[2]))
    if max_right - min_right >= 0.6 * em:
        return True
    if edges[index + 1] in barriers or edges[index + 2] in barriers:
        return False
    min_left, max_left = sorted((first[0], second[0]))
    indent = rows[index].bbox[0] + 0.5 * em
    # 只有无横隔的单项标题会发起后续扫描，下一条单项标题截断它，各分节的扫描区间互不重叠。
    for j in range(index + 3, len(rows)):
        if edges[j] in barriers or len(rows[j].tokens) != cols:
            break
        left, _, right, _ = rows[j].tokens[0].bbox
        min_left, max_left = min(min_left, left), max(max_left, left)
        if min_left < indent or max_left - min_left > 0.25 * em:
            break
        min_right, max_right = min(min_right, right), max(max_right, right)
        if max_right - min_right >= 0.6 * em:
            return True
    return False


def _body_specs(rows, rails, tracks, height, em, header_count):
    """用横隔或有独立缩进的子项验证分节行，保留已证明的同步折行长段落。"""
    cols = len(tracks) - 1
    if len(rails) == 1 and _parallel_wrapped_prose([dict(enumerate(r.tokens)) for r in rows], cols, em):
        return None
    # 横线或相邻完整行间的净空区分逻辑记录，不在正文字形交叠处造行。
    edges = [rails[0]]
    for prev, row in zip(rows, rows[1:]):
        if prev.bbox[3] > row.bbox[1] + 0.25:
            return None
        start = bisect_left(rails, prev.bbox[3] - 0.25, lo=1)
        stop = bisect_right(rails, row.bbox[1] + 0.25, lo=start)
        if stop > start + 1:
            return None
        edges.append(rails[start] if stop > start else (prev.bbox[3] + row.bbox[1]) / 2)
    edges.append(height)
    barriers = set(rails)
    if not barriers.issubset(edges):
        return None
    body = []
    for i, row in enumerate(rows):
        y, end = edges[i : i + 2]
        if len(row.tokens) == 1:
            if i + 2 >= len(rows) or any(len(r.tokens) != cols for r in rows[i + 1 : i + 3]):
                return None
            if row.bbox[0] >= tracks[1] or end - y > 3 * em:
                return None
            if y in barriers:
                body.append(GridCellSpec(i + header_count, 0, 1, cols, (0, y, tracks[-1], end)))
                continue
            children = [r.tokens[0].bbox for r in rows[i + 1 : i + 3]]
            # 前两条数据须先给出清楚的共同缩进，沿同一节的左缘检查是否确有不同宽度。
            if (
                row.bbox[2] >= tracks[1]
                or min(c[0] for c in children) < row.bbox[0] + 0.5 * em
                or abs(children[0][0] - children[1][0]) > 0.25 * em
                or not _section_has_distinct_widths(rows, i, edges, barriers, cols, em)
            ):
                return None
        body.extend(
            GridCellSpec(i + header_count, c, 1, 1, (left, y, right, end))
            for c, (left, right) in enumerate(zip(tracks, tracks[1:]))
        )
    return body


def build_column_group_candidate(
    table_input: NativeTableInput,
    text: NativeTableText,
    previous: NativeTableCandidate | None = None,
) -> NativeTableCandidate | None:
    """只在横隔明确、正文列稳定且表头确有跨列层级时，用整表原生字形验证新拓扑。"""
    if len(text.rows) < 5 or len(table_input.drawing_lines) < 3:
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    rules = _local_rules(table_input, width, height)
    em = text.median_glyph_height
    if any(r.orientation == "vertical" and em < r.coordinate < width - em for r in rules):
        return None
    full = sorted({r.coordinate for r in rules if r.orientation == "horizontal" and r.end - r.start >= 0.95 * width})
    if len(full) < 3 or full[0] > 0.4 * em or height - full[-1] > 0.4 * em:
        return None
    # 外边框可由两笔紧邻横线构成。只有两笔完全位于所有字形行之外，才取其内侧为边框。
    # 表身内相邻的两道横线仍同时交给正文分界检查，不自动抹去夹在其中的空行。
    interior = full[1:-1]
    if interior[0] - full[0] <= 0.45 * em and interior[0] < min(row.bbox[1] for row in text.rows):
        interior = interior[1:]
    if interior and full[-1] - interior[-1] <= 0.45 * em and interior[-1] > max(row.bbox[3] for row in text.rows):
        interior = interior[:-1]
    if not interior:
        return None
    cut = interior[0]
    header = [r for r in text.rows[:5] if r.bbox[3] <= cut + 0.25]
    if not 2 <= len(header) <= 4 or text.rows[len(header)].bbox[1] < cut - 0.25:
        return None
    body = text.rows[len(header) :]
    tracks = _data_tracks(body, width, em)
    if tracks is None:
        return None
    heading = _header_specs(header, tracks, rules, cut, em)
    if heading is None:
        return None
    levels, specs, ruled_groups = heading
    rows = levels + len(body)
    cols = len(tracks) - 1
    if previous is not None and (previous.cols != cols or previous.rows > rows or previous.rows == rows and not ruled_groups):
        return None
    data_specs = _body_specs(body, interior, tracks, height, em, levels)
    if data_specs is None:
        return None
    specs.extend(data_specs)
    metrics = {}
    candidate = build_candidate(
        source="sparse_multiline",
        rows=rows,
        cols=cols,
        specs=tuple(specs),
        text=text,
        structure_support=1.0,
        row_stability=1.0,
        column_stability=1.0,
        issues=("evidence=column_group_hierarchy",),
        require_atomic_tokens=True,
        align_rowspans=True,
        use_grid_index=True,
        diagnostics=metrics,
    )
    if (
        candidate is None
        or candidate.text_capture < 1
        or candidate.order_consistency < 1
        or metrics.get("ambiguous_glyph_ratio")
    ):
        return None
    if previous is not None and not _preserves_existing_grid(candidate, previous, levels, ruled_groups):
        return None
    return candidate


def _preserves_existing_grid(candidate, previous, levels, ruled_groups):
    """补表头时保持原有正文的逐格归属；同尺寸仅允许有独立横轨佐证的跨度扩张。"""
    offset = candidate.rows - previous.rows
    by_source = {c.source_char_indices: c for c in candidate.cells if c.source_char_indices}
    expanded = False
    for old in previous.cells:
        if not old.source_char_indices or (offset and old.row + offset < levels):
            continue
        new = by_source.get(old.source_char_indices)
        if new is None or new.content != old.content or new.row != old.row + offset or new.rowspan != old.rowspan:
            return False
        if (new.col, new.colspan) == (old.col, old.colspan):
            continue
        # 分节行可从首列加多个空格变成整行标题，已有数据的其他横向位置不可移动。
        if offset and new.row >= levels and new.colspan == candidate.cols and old.col == new.col == 0:
            continue
        if (
            new.row >= levels
            or (new.col, new.col + new.colspan) not in ruled_groups
            or not new.col <= old.col < old.col + old.colspan <= new.col + new.colspan
        ):
            return False
        expanded = True
    return bool(offset or expanded)
