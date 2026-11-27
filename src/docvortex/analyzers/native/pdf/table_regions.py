"""由实体填充行带和原生文字共同提出全页表区。"""

from __future__ import annotations

import statistics
import re
from dataclasses import dataclass

from ....schema import BBox
from ._table_recovery.banded_numeric import _numeric_value
from .geometry import (
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_overlap_in_smaller,
    _bbox_union_many,
    _point_in_bbox,
)
from .models import _LineItem, _PageSource, _TableCandidate, _VerifiedTableCandidate
from .table_constants import _TABLE_CAPTION_RE


@dataclass(slots=True)
class _TextBand:
    """保存表区横向范围内处于同一文字基线的原生行。"""

    bbox: BBox
    lines: list[_LineItem]

    @property
    def center(self) -> float:
        """用墨迹中心在局部行带内定位文字。"""
        return _bbox_center_y(self.bbox)


def _visible_row_fills(source: _PageSource) -> list[BBox]:
    """合并同一水平条带上相接的可见色块，重叠的内侧填充只贡献一次。"""
    width, height = source.page_size
    rectangles = sorted(
        (
            box
            for path in source.path_infos
            if path.form_depth == 0
            and path.fill_visible
            and path.fill_rgba
            and path.fill_rgba[3] > 0
            and min(path.fill_rgba[:3]) < 250
            for box in path.rectangle_bboxes
            if 0.002 * height <= box[3] - box[1] <= 0.09 * height
        ),
        key=lambda box: (box[1], box[0]),
    )
    # 同一排矩形的上下沿可有半条描边的偏移，按局部行高归组。
    tiers: list[list[BBox]] = []
    for box in rectangles:
        if tiers:
            reference = tiers[-1][0]
            tolerance = 0.1 * (reference[3] - reference[1])
            if abs(box[1] - reference[1]) <= tolerance and abs(box[3] - reference[3]) <= tolerance:
                tiers[-1].append(box)
                continue
        tiers.append([box])

    bands = []
    for tier in tiers:
        combined = []
        for box in sorted(tier):
            if combined and box[0] <= combined[-1][2] + 0.12 * (box[3] - box[1]):
                combined[-1] = _bbox_union_many([combined[-1], box])
            else:
                combined.append(box)
        bands.extend(box for box in combined if box[2] - box[0] >= 0.15 * width)
    return bands


def _repeated_fill_regions(bands: list[BBox]) -> list[list[BBox]]:
    """用一致的横向端点、行高和重复节距分离相邻的底纹表。"""
    groups: list[list[BBox]] = []
    for box in bands:
        for group in reversed(groups):
            first, last = group[0], group[-1]
            height = first[3] - first[1]
            pitch = box[1] - last[1]
            if (
                max(abs(first[0] - box[0]), abs(first[2] - box[2])) <= 0.2 * height
                and abs((box[3] - box[1]) - height) <= 0.18 * height
                and 0.85 * height <= pitch <= 2.15 * height
                and (len(group) < 2 or abs(pitch - (group[1][1] - first[1])) <= 0.2 * height)
            ):
                group.append(box)
                break
        else:
            groups.append([box])
    return [group for group in groups if len(group) >= 3]


def _text_bands(lines: list[_LineItem], bounds: BBox, tolerance: float, y_padding: float) -> list[_TextBand]:
    """用原生墨迹分行，保留穿越左右边界的文字供表区反证。"""
    groups: list[_TextBand] = []
    visible = [
        (line.ink_bbox or line.bbox, line)
        for line in lines
        if not line.angle and not line.formula_candidate_only and line.text.strip()
    ]
    for box, line in sorted(visible, key=lambda member: _bbox_center_y(member[0])):
        center = _bbox_center_y(box)
        if not bounds[1] - y_padding <= center <= bounds[3] + y_padding or box[2] < bounds[0] or box[0] > bounds[2]:
            continue
        if groups and abs(center - groups[-1].center) <= tolerance:
            groups[-1].bbox = _bbox_union_many([groups[-1].bbox, box])
            groups[-1].lines.append(line)
        else:
            groups.append(_TextBand(box, [line]))
    return groups


def _row_gutters(band: _TextBand, gap: float) -> list[tuple[float, float]]:
    """在有真实字词的分段之间寻找距离足够大的列沟。"""
    boxes = sorted(line.ink_bbox or line.bbox for line in band.lines if any(char.isalnum() for char in line.text))
    return [(a[2], b[0]) for a, b in zip(boxes, boxes[1:]) if b[0] - a[2] >= gap]


def _persistent_row_gutters(
    gutters: list[list[tuple[float, float]]], minimum_overlap: float
) -> tuple[tuple[float, float], ...]:
    """保留全部重复正文行共同留白的宽列沟，忽略单行偶发空隙。"""
    common = gutters[0]
    for row in gutters[1:]:
        common = [(max(a, c), min(b, d)) for a, b in common for c, d in row if min(b, d) - max(a, c) >= minimum_overlap]
        if not common:
            break
    return tuple(common)


def _repeated_numeric_gutters(rows: list[_TextBand], pitch: float) -> list[tuple[float, float]]:
    """用多数正文行的重复金额右缘确认短列沟，漏掉金额符号的行仍可作证。"""
    tracks: list[list[tuple[int, BBox, BBox]]] = []
    tolerance = 0.25 * pitch
    for index, row in enumerate(rows):
        boxes = sorted(
            line.ink_bbox or line.bbox
            for line in row.lines
            if _numeric_value(line.text) and any(char.isdecimal() for char in line.text)
        )
        for left, right in zip(boxes, boxes[1:]):
            if right[0] - left[2] < 0.5 * pitch:
                continue
            for track in tracks:
                first_left, first_right = track[0][1:]
                if abs(left[2] - first_left[2]) <= tolerance and abs(right[2] - first_right[2]) <= tolerance:
                    track.append((index, left, right))
                    break
            else:
                tracks.append([(index, left, right)])
    confirmed = []
    for track in tracks:
        if len({index for index, _, _ in track}) < max(3, 0.6 * len(rows)):
            continue
        left = max(a[2] for _, a, _ in track)
        right = min(b[0] for _, _, b in track)
        if right - left >= 0.5 * pitch:
            confirmed.append((left, right))
    return confirmed


def _stripe_horizontal_edges(source: _PageSource, left: float, right: float, pitch: float) -> list[float]:
    """长横线或两端齐且合计覆盖绝大部分表宽的分段横线均可证明边沿。"""
    rules = sorted(
        (
            box
            for rule in source.drawing_lines
            if rule.orientation == "horizontal" and left - 0.2 * pitch <= (box := rule.bbox)[0] < box[2] <= right + 0.2 * pitch
        ),
        key=_bbox_center_y,
    )
    groups: list[list[BBox]] = []
    for box in rules:
        if groups and abs(_bbox_center_y(box) - _bbox_center_y(groups[-1][0])) <= 0.09 * pitch:
            groups[-1].append(box)
        else:
            groups.append([box])
    edges = []
    for group in groups:
        intervals = sorted((box[0], box[2]) for box in group)
        start, cursor = intervals[0]
        union_width = cursor - start
        for a, b in intervals[1:]:
            union_width += max(0, b - max(cursor, a))
            cursor = max(cursor, b)
        if abs(start - left) <= 0.2 * pitch and abs(cursor - right) <= 0.2 * pitch and union_width >= 0.85 * (right - left):
            edges.append(statistics.median(_bbox_center_y(box) for box in group))
    return edges


def _follows_stripe_gutters(band: _TextBand, gutters: tuple[tuple[float, float], ...], minimum_gap: float) -> bool:
    """核对相邻无底纹行在正文全部可靠列沟两侧都有独立文字。"""
    gaps = _row_gutters(band, minimum_gap)
    return bool(gutters) and all(any(a <= (left + right) / 2 <= b for a, b in gaps) for left, right in gutters)


def _row_fill_candidate(source: _PageSource, fills: list[BBox]) -> _TableCandidate | None:
    """用重复底纹固定表身节距，接回由稳定列沟和边沿证明的首尾行。"""
    left, top, right, bottom = _bbox_union_many(fills)
    height = statistics.median(band[3] - band[1] for band in fills)
    alternating = fills[1][1] - fills[0][1] > 1.5 * height
    jumps = 2 if alternating else 1
    pitch = statistics.median(b[1] - a[1] for a, b in zip(fills, fills[1:])) / jumps
    groups = _text_bands(source.lines, (left, top, right, bottom), 0.32 * pitch, 1.5 * pitch)
    inside = [row for row in groups if top <= row.center <= bottom]
    if len(inside) != (len(fills) - 1) * jumps + 1:
        return None
    if any(abs(row.center - inside[0].center - index * pitch) > 0.26 * pitch for index, row in enumerate(inside)):
        return None
    if any(not band[1] <= inside[n * jumps].center <= band[3] for n, band in enumerate(fills)):
        return None

    gap = max(1.4 * height, 0.05 * (right - left))
    gutters = [_row_gutters(row, gap) for row in inside]
    # 首个填色行可为原生文本合成的一整行列名；后续各行必须共享可见列沟。
    if not all(gutters[1:]):
        return None
    common = next(
        (
            x
            for a, b in gutters[1]
            for x in ((a + b) / 2,)
            if all(any(start <= x <= end for start, end in row) for row in gutters[2:])
        ),
        None,
    )
    if common is None:
        return None
    persistent = _persistent_row_gutters(gutters[1:], 0.5 * gap)
    numeric_gutters = _repeated_numeric_gutters(inside, pitch)
    body_top = top

    horizontal_edges = _stripe_horizontal_edges(source, left, right, pitch)
    preceding = next((row for row in reversed(groups) if row.center < top), None)
    if preceding and (
        abs(inside[0].center - preceding.center - pitch) <= 0.26 * pitch
        and any(a <= common <= b for a, b in _row_gutters(preceding, gap))
        and (
            any(preceding.bbox[1] - 0.6 * pitch <= y <= preceding.bbox[1] for y in horizontal_edges)
            or any(any(char.isalpha() for char in line.text) for line in preceding.lines)
            and all(not any(char.isalpha() for char in line.text) for line in inside[0].lines)
            or (
                any(preceding.bbox[3] - 0.1 * pitch <= y <= top + 0.15 * pitch for y in horizontal_edges)
                and _follows_stripe_gutters(preceding, persistent, gap)
                and sum(any(char.isalpha() for char in line.text) for line in preceding.lines) >= 2
                and not any(_TABLE_CAPTION_RE.match(line.text) for line in preceding.lines)
            )
        )
    ):
        inside.insert(0, preceding)
        top = min(
            [preceding.bbox[1], *(y for y in horizontal_edges if preceding.bbox[1] - 0.6 * pitch <= y <= preceding.bbox[1])]
        )
    elif not gutters[0]:
        return None

    following = next((row for row in groups if row.center > bottom), None)
    if (
        alternating
        and following
        and abs(following.center - inside[-1].center - pitch) <= 0.26 * pitch
        and _follows_stripe_gutters(following, persistent, gap)
    ):
        # 与表身同节距同列的未着色尾行必须一并认领。底边无法证明闭合时保留原文本所有权。
        closing_edges = [
            y for y in horizontal_edges if following.bbox[3] - 0.08 * pitch <= y <= following.bbox[3] + 0.45 * pitch
        ]
        if not closing_edges:
            return None
        inside.append(following)
        bottom = min(closing_edges)

    if any(row.bbox[0] < left - 0.2 * pitch or row.bbox[2] > right + 0.2 * pitch for row in inside):
        return None
    bottom = max([bottom, *(y for y in horizontal_edges if bottom <= y <= bottom + 0.25 * pitch)])
    padding = 0.12 * pitch
    # 原生求解器同时读取矩形边和侧边短轨，留出细描边和靠边字形的完整范围。
    horizontal_padding = 0.2 * pitch
    bounds = (
        max(0, min(left, *(row.bbox[0] for row in inside)) - horizontal_padding),
        max(0, top - padding),
        min(source.page_size[0], max(right, *(row.bbox[2] for row in inside)) + horizontal_padding),
        min(source.page_size[1], bottom + padding),
    )
    return _VerifiedTableCandidate(
        bounds,
        bounds,
        0,
        80.0,
        core_bbox=bounds,
        line_indices={line.source_index for row in inside for line in row.lines},
        verified_region_rows=len(inside),
        verified_region_gutters=tuple(sorted(set(persistent).union(numeric_gutters))),
        verified_region_body_band=(body_top, bottom),
    )


def _detect_repeated_fill_table_regions(
    source: _PageSource, excluded: list[BBox], vector_graphics: list[BBox]
) -> list[_TableCandidate]:
    """在已认领区域外补提名重复底纹表，保护真实图像和超出表体的矢量图。"""
    regions = []
    for fills in _repeated_fill_regions(_visible_row_fills(source)):
        bounds = _bbox_union_many(fills)
        if any(_bbox_overlap_in_smaller(bounds, box) >= 0.15 for box in excluded):
            continue
        candidate = _row_fill_candidate(source, fills)
        if candidate is None:
            continue
        box = candidate.bbox
        if any(_bbox_overlap_in_smaller(box, block) >= 0.15 for block in excluded):
            continue
        if any(
            _bbox_overlap_in_smaller(box, block) >= 0.25 and _bbox_overlap_in_first(block, box) < 0.9
            for block in vector_graphics
        ):
            continue
        regions.append(candidate)
    return regions


def _label_value_row(row: _TextBand, em: float) -> str:
    """识别由宽列沟分隔的短标签和重复格式的多段数字值。"""
    lines = sorted(row.lines, key=lambda line: (line.ink_bbox or line.bbox)[0])
    if len(lines) < 2:
        return ""
    *labels, value = lines
    label_box = _bbox_union_many([line.ink_bbox or line.bbox for line in labels])
    right_box = value.ink_bbox or value.bbox
    if (
        right_box[0] - label_box[2] < 3.5 * em
        or right_box[2] - right_box[0] > 9 * em
        or not labels[0].text.lstrip()[:1].isalpha()
        or not any(sum(char.isalpha() for char in line.text) >= 3 for line in labels)
        or len(re.findall(r"\d+", value.text)) < 2
    ):
        return ""
    # 日期、版本与范围等印刷格式可以比较连续字母段和数字段，词义及所用字体无需相同。
    return re.sub(r"[^\W\d_]+", "A", re.sub(r"\d+", "0", " ".join(value.text.split())))


def _detect_unruled_label_value_regions(source: _PageSource, excluded: list[BBox]) -> list[_TableCandidate]:
    """在与上下正文分隔的位置查找等距双列标签及右对齐的短数据。"""
    if len(source.lines) < 8:
        return []
    heights = [
        line.effective_height or line.bbox[3] - line.bbox[1]
        for line in source.lines
        if not line.angle and any(char.isdigit() for char in line.text)
    ]
    if len(heights) < 4:
        return []
    em = statistics.median(heights)
    if em <= 0:
        return []
    width, height = source.page_size
    bands = _text_bands(source.lines, (0, 0, width, height), 0.34 * em, 0)
    values = [_label_value_row(row, em) for row in bands]
    candidates = []
    cursor = 0
    for start, shape in enumerate(values):
        if start < cursor or not shape or start + 3 >= len(bands):
            continue
        first = bands[start]
        pitch = bands[start + 1].center - first.center
        if not 1.65 * em <= pitch <= 3 * em:
            continue
        end = start + 1
        for index in range(start + 1, len(bands)):
            row = bands[index]
            if (
                values[index] != shape
                or abs(row.center - first.center - (index - start) * pitch) > 0.17 * pitch
                or abs(row.bbox[0] - first.bbox[0]) > 0.24 * em
                or abs(row.bbox[2] - first.bbox[2]) > 0.24 * em
            ):
                break
            end = index + 1
        if end - start < 4:
            continue
        cursor = end
        last = bands[end - 1]
        if (start and first.center - bands[start - 1].center < 1.35 * pitch) or (
            end < len(bands) and bands[end].center - last.center < 1.35 * pitch
        ):
            continue
        rows = bands[start:end]
        left, top, right, bottom = _bbox_union_many([row.bbox for row in rows])
        pad = 0.12 * em
        box = (left - pad, top - pad, right + pad, bottom + pad)
        if any(_bbox_overlap_in_smaller(box, block) >= 0.15 for block in excluded) or any(
            _bbox_overlap_in_first(rule.bbox, box) >= 0.5 for rule in source.drawing_lines
        ):
            continue
        candidates.append(
            _VerifiedTableCandidate(
                box,
                box,
                0,
                80.0,
                core_bbox=box,
                line_indices={line.source_index for row in rows for line in row.lines},
                verified_region_rows=len(rows),
            )
        )
    return candidates


def _detect_independent_closed_grid_regions(
    source: _PageSource, excluded: list[BBox], vector_graphics: list[BBox]
) -> list[_TableCandidate]:
    """从疑似图形分量中找出单独绘制、各格有原生文字的完整小网格。"""
    if not vector_graphics or len(source.drawing_lines) < 7:
        return []
    result = []
    for path in source.path_infos:
        box = path.bbox
        if (
            path.form_depth
            or path.fill_visible
            or not path.stroke_visible
            or (path.segment_count < 11 and path.segment_count != 5)
        ):
            continue
        if not any(_grid_can_split_vector_region(box, graphic) for graphic in vector_graphics):
            continue
        if any(_bbox_overlap_in_smaller(box, other) >= 0.15 for other in excluded):
            continue
        left, top, right, bottom = box
        tolerance = 0.025 * min(right - left, bottom - top)
        if tolerance <= 0:
            continue
        horizontals = sorted(
            {
                _bbox_center_y(rule.bbox)
                for rule in source.drawing_lines
                if rule.orientation == "horizontal"
                and top <= _bbox_center_y(rule.bbox) <= bottom
                and max(abs(rule.bbox[0] - left), abs(rule.bbox[2] - right)) <= tolerance
            }
        )
        verticals = sorted(
            {
                _bbox_center_x(rule.bbox)
                for rule in source.drawing_lines
                if rule.orientation == "vertical"
                and left <= _bbox_center_x(rule.bbox) <= right
                and max(abs(rule.bbox[1] - top), abs(rule.bbox[3] - bottom)) <= tolerance
            }
        )
        if (
            len(horizontals) < 3
            or len(verticals) < 4
            or max(horizontals[0] - top, bottom - horizontals[-1], verticals[0] - left, right - verticals[-1]) > tolerance
        ):
            continue
        # 一条闭合外缘与每道内部直轨须解释全部笔画，允许 PDF 把外框和网线分开绘制。
        strokes = path.segment_count
        if strokes == 5:
            strokes += sum(
                other.segment_count
                for other in source.path_infos
                if other is not path
                and not other.form_depth
                and other.stroke_visible
                and not other.fill_visible
                and other.segment_count % 2 == 0
                and other.bbox[0] >= left - tolerance
                and other.bbox[1] >= top - tolerance
                and other.bbox[2] <= right + tolerance
                and other.bbox[3] <= bottom + tolerance
                and (
                    max(abs(other.bbox[0] - left), abs(other.bbox[2] - right)) <= tolerance
                    or max(abs(other.bbox[1] - top), abs(other.bbox[3] - bottom)) <= tolerance
                )
            )
        if strokes != 2 * (len(horizontals) + len(verticals)) - 3:
            continue
        lines = [
            line
            for line in source.lines
            if not line.angle
            and _point_in_bbox((_bbox_center_x(line.ink_bbox or line.bbox), _bbox_center_y(line.ink_bbox or line.bbox)), box)
        ]
        if not lines or any(_bbox_overlap_in_first(line.ink_bbox or line.bbox, box) < 0.98 for line in lines):
            continue
        result.append(
            _VerifiedTableCandidate(
                box,
                box,
                0,
                80.0,
                core_bbox=box,
                line_indices={line.source_index for line in lines},
                verified_region_rows=len(horizontals) - 1,
                verified_region_columns=len(verticals) - 1,
                verified_grid_tracks=(tuple(verticals), tuple(horizontals)) if path.segment_count == 5 else None,
            )
        )
    return result


def _grid_can_split_vector_region(table: BBox, graphic: BBox) -> bool:
    """只有横向贯穿且完全位于矢量分量内的表才允许将其上下分离。"""
    if table[1] < graphic[1] or table[3] > graphic[3]:
        return False
    width = table[2] - table[0]
    return width > 0 and max(abs(table[0] - graphic[0]), abs(table[2] - graphic[2])) <= 0.025 * width


def _split_graphic_at_independent_grids(graphics: list[BBox], tables: list[BBox]) -> list[BBox]:
    """移出已物化的独立网格，保留原矢量分量上下仍需图形判定的区域。"""
    for table in sorted(tables):
        remaining = []
        for box in graphics:
            if not _grid_can_split_vector_region(table, box):
                remaining.append(box)
                continue
            left, top, right, bottom = box
            remaining.extend((left, y0, right, y1) for y0, y1 in ((top, table[1]), (table[3], bottom)) if y1 > y0)
        graphics = remaining
    return graphics
