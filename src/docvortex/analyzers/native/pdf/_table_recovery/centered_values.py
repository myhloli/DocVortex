"""用独立列锚点与跨中缝的值确认正文中的共享参数格。"""

from __future__ import annotations

import re
from statistics import median

from .banded_numeric import _numeric_value
from .candidate import GridCellSpec, build_fully_assigned_table
from .contracts import NativeTableCandidate, NativeTableInput, NativeTableText
from .geometry import bbox_center, normalize_angle, table_local_size
from .sparse_common import _local_rules

_EXPONENT_NUMBER = re.compile(r"[+−-]?\d+(?:\.\d+)?[Ee][+−-]?\d+")


def build_centered_value_candidate(table_input: NativeTableInput, text: NativeTableText) -> NativeTableCandidate | None:
    """恢复横隔封口、竖线分隔标签且有重复独立数据的双值列。"""
    if len(text.rows) < 6 or len(table_input.drawing_lines) < 4:
        return None
    width, height = table_local_size(table_input.table_bbox, normalize_angle(table_input.angle))
    em = text.median_glyph_height
    rules = _local_rules(table_input, width, height)
    horizontal = sorted((r for r in rules if r.orientation == "horizontal"), key=lambda r: r.coordinate)
    if (
        len(horizontal) != 3
        or horizontal[0].coordinate > 0.4 * em
        or height - horizontal[-1].coordinate > 0.4 * em
        or any(rule.end - rule.start < 0.93 * width for rule in horizontal)
    ):
        return None
    head, *body = text.rows
    boundary = horizontal[1].coordinate
    if (
        len(head.tokens) != 3
        or head.bbox[3] > boundary
        or boundary > body[0].bbox[1]
        or body[-1].bbox[3] > horizontal[-1].coordinate
        or any(len(row.tokens) not in (2, 3) for row in body)
    ):
        return None

    # 完整独立数值行重复确认两个条件列，避免用表头宽词或稀疏行猜测列位。
    full = [row.tokens for row in body if len(row.tokens) == 3]
    if len(full) < 3 or len(body) - len(full) < 2:
        return None
    full_slots = [row[1:] for row in full]
    if any(not (_numeric_value(t.text) or _EXPONENT_NUMBER.fullmatch(t.text)) for pair in full_slots for t in pair):
        return None
    left, right = (median(bbox_center(t.bbox)[0] for t in group) for group in zip(*full_slots, strict=True))
    tolerance = 0.18 * em
    split = (left + right) / 2
    if (
        right - left < 2 * em
        or any(
            abs(bbox_center(a.bbox)[0] - left) > tolerance or abs(bbox_center(b.bbox)[0] - right) > tolerance
            for a, b in full_slots
        )
        or abs(bbox_center(head.tokens[1].bbox)[0] - left) > tolerance
        or abs(bbox_center(head.tokens[2].bbox)[0] - right) > tolerance
    ):
        return None

    # 标签竖线必须位于整列共同的净空里，并覆盖绝大多数正文。
    label_right = max(row.tokens[0].bbox[2] for row in text.rows)
    value_left = min(row.tokens[1].bbox[0] for row in text.rows)
    start, end = body[0].bbox[1], body[-1].bbox[3]
    vertical = [r for r in rules if r.orientation == "vertical"]
    stubs = [
        r.coordinate
        for r in vertical
        if label_right < r.coordinate < value_left and min(end, r.end) - max(start, r.start) >= 0.8 * (end - start)
    ]
    if len(stubs) != 1:
        return None
    stub = stubs[0]
    cross_count = 0
    shared_count = 0
    slots = [[(0, 1), (1, 2), (2, 3)]]
    for row in body:
        if len(row.tokens) == 3:
            slots.append(slots[0])
            continue
        token = row.tokens[1]
        cx = bbox_center(token.bbox)[0]
        if abs(cx - split) <= tolerance:
            top, bottom = row.bbox[1], row.bbox[3]
            if any(left < r.coordinate < right and r.start < bottom and top < r.end for r in vertical):
                return None
            slots.append([(0, 1), (1, 3)])
            shared_count += 1
            cross_count += token.bbox[0] < split - em and split + em < token.bbox[2]
        elif (abs(cx - left) <= tolerance and token.bbox[2] <= split) or (
            abs(cx - right) <= tolerance and token.bbox[0] >= split
        ):
            slots.append(slots[0])
        else:
            return None
    if shared_count < 2 or cross_count == 0:
        return None

    edges = [0.0, boundary]
    for before, after in zip(body, body[1:]):
        if before.bbox[3] > after.bbox[1]:
            return None
        edges.append((before.bbox[3] + after.bbox[1]) / 2)
    edges.append(height)
    tracks = (0.0, stub, split, width)
    specs = [
        GridCellSpec(i, a, 1, b - a, (tracks[a], edges[i], tracks[b], edges[i + 1]))
        for i, row in enumerate(slots)
        for a, b in row
    ]
    return build_fully_assigned_table(specs, text, "centered_body_values")
