"""从独立年份表头和合计线定位稳定右对齐的少线金额表。"""

from __future__ import annotations

import re
import statistics
import unicodedata

from ....schema import BBox
from .geometry import _bbox_center_y, _bbox_overlap_in_first, _bbox_overlap_in_smaller, _bbox_union_many
from .models import _AxisLine, _Fragment, _PageSource, _TableCandidate, _VerifiedTableCandidate, _VisualRow
from .table_constants import _ACCOUNTING_AMOUNT_RE
from .table_rules import _build_fragments, _cluster_fragment_rows, _full_height_vertical_rule_positions

_YEAR = re.compile(r"(?:18|19|20|21)\d{2}")


def _period_years(row: _VisualRow, em: float) -> list[_Fragment]:
    """识别相互分开的连续年份列，金额或正文中的年份不能单独启动候选。"""
    years = [part for part in row.fragments if _YEAR.fullmatch("".join(part.text.split()))]
    if not 2 <= len(years) <= 3 or any(b.local_bbox[0] - a.local_bbox[2] < 2 * em for a, b in zip(years, years[1:])):
        return []
    values = [int("".join(part.text.split())) for part in years]
    deltas = [b - a for a, b in zip(values, values[1:])]
    if not (all(1 <= delta <= 10 for delta in deltas) or all(-10 <= delta <= -1 for delta in deltas)):
        return []
    if any(part not in years and part.local_bbox[2] > years[0].local_bbox[0] - em for part in row.fragments):
        return []
    return years


def _period_amount_row(row, years, em):
    """验证每个年份槽中独立出现的金额，独立货币符号继续保留在其金额槽内。"""
    left = years[0].local_bbox[0] - 4.5 * em
    labels = []
    symbols = []
    slots = [[] for _ in years]
    left_edges = [part.local_bbox[0] for part in years]
    for part in row.fragments:
        text = "".join(part.text.split())
        box = part.local_bbox
        ends_currency = bool(text) and unicodedata.category(text[-1]) == "Sc"
        is_currency = len(text) == 1 and ends_currency
        is_amount = _ACCOUNTING_AMOUNT_RE.fullmatch(text)
        if box[2] <= left:
            # 左侧若已有独立数字列，当前年份没有资格承接相邻栏的另一张表。
            if is_amount or ends_currency:
                return None
            labels.append(part)
        elif is_currency:
            symbols.append(box)
        elif is_amount:
            column = sum(box[2] > start - 0.25 * em for start in left_edges[1:])
            slots[column].append(box)
        elif box[2] < left_edges[0] - em and not ends_currency:
            labels.append(part)
        else:
            return None
    if not labels or not any(char.isalpha() for part in labels for char in part.text):
        return None
    if any(len(slot) != 1 for slot in slots):
        return None
    values = [slot[0] for slot in slots]
    symbol_columns = set()
    for symbol in symbols:
        column = next((i for i, box in enumerate(values) if box[0] >= symbol[2]), None)
        if column is None or column in symbol_columns or values[column][0] - symbol[2] > 4.5 * em:
            return None
        values[column] = _bbox_union_many([symbol, values[column]])
        symbol_columns.add(column)
    offsets = [box[2] - year.local_bbox[2] for box, year in zip(values, years)]
    if min(offsets) < -3 * em or max(offsets) > 4.5 * em or max(offsets) - min(offsets) > 0.8 * em:
        return None
    if max(part.local_bbox[2] for part in labels) + em >= values[0][0]:
        return None
    return values


def _period_total_rules(row, previous, values, rules, em):
    """确认合计行上下都有实体线，多段同高金额线可共同覆盖所有槽。"""
    center = row.center_y
    local = [rule for rule in rules if previous.center_y < _bbox_center_y(rule.bbox) <= center + 1.35 * em]
    supporting = [
        rule
        for rule in local
        if all(
            any(
                abs(_bbox_center_y(other.bbox) - _bbox_center_y(rule.bbox)) <= 0.15 * em
                and other.bbox[0] <= box[0] + 0.35 * em
                and other.bbox[2] >= box[2] - 0.35 * em
                for other in local
            )
            for box in values
        )
    ]
    if not any(_bbox_center_y(rule.bbox) <= center - 0.25 * em for rule in supporting):
        return []
    return [rule for rule in supporting if _bbox_center_y(rule.bbox) >= center + 0.15 * em]


def _period_header_end(rows, start, years, em):
    """金额列下邻的单位行可属于同一表头，保留普通小节行的正文归属。"""
    if start + 1 >= len(rows):
        return start
    units = rows[start + 1]
    positions = [fragment for fragment in units.fragments if fragment.local_bbox[2] >= years[0].local_bbox[0]]
    if rows[start].bbox[3] + 0.8 * em < units.bbox[1] or len(positions) != len(years):
        return start
    if all(
        abs(pos.local_bbox[2] - year.local_bbox[2]) <= 0.45 * em
        and any(unicodedata.category(char) == "Sc" for char in pos.text)
        and any(char.isalpha() for char in pos.text)
        for pos, year in zip(positions, years)
    ):
        return start + 1
    return start


def _period_body_rows(rows, start, years, em):
    """从表头读取金额记录，跨列表述立刻终止，暂留的左列小节只能在后续金额轨证实后加入。"""
    data = []
    previous = rows[start]
    empty = 0
    for index in range(start + 1, len(rows)):
        row = rows[index]
        if row.bbox[1] - previous.bbox[3] > 2.15 * em or _period_years(row, em):
            break
        values = _period_amount_row(row, years, em)
        if values is None:
            if row.bbox[2] >= years[0].local_bbox[0] - 0.5 * em or empty >= 3:
                break
            empty += 1
        else:
            if data and any(abs(a[2] - b[2]) > 0.65 * em for a, b in zip(values, data[0][1])):
                break
            data.append((index, values))
            empty = 0
        previous = row
    return data


def _period_logical_rows(rows, body_start, data_indices, em):
    """合并有缩进或未闭合括号证实的左列续行，其他没有金额的小节保持独立行。"""
    logical = []
    prior = None
    for index, row in enumerate(rows):
        continuation = False
        if prior is not None and index > body_start and row.bbox[1] - prior.bbox[3] <= 0.4 * em:
            a, b = prior.fragments[0], row.fragments[0]
            continued_parentheses = a.text.count("(") > a.text.count(")") and ")" in b.text
            indented_label = (
                index - 1 not in data_indices
                and len(prior.fragments) == 1
                and not a.text.rstrip().endswith((":", "："))
                and b.local_bbox[0] - a.local_bbox[0] >= 0.5 * em
                and b.local_bbox[2] < a.local_bbox[2]
                and row.center_y - prior.center_y <= 1.35 * em
            )
            continuation = continued_parentheses or indented_label
        if continuation:
            logical[-1].append(row)
        else:
            logical.append([row])
        prior = row
    return logical


def _period_candidate(rows, start, years, rules, em, trusted_text_bboxes):
    """用净空求列界并按确证的合计边缘构建验证候选，字符最终由原生求解器分配。"""
    header_end = _period_header_end(rows, start, years, em)
    data = _period_body_rows(rows, header_end, years, em)
    if len(data) < 4:
        return None
    last, final_values = data[-1]
    footer = _period_total_rules(rows[last], rows[last - 1], final_values, rules, em)
    if not footer:
        return None
    first_amount_left = min(boxes[0][0] for _, boxes in data)
    label_right = max(
        part.local_bbox[2] for row in rows[start : last + 1] for part in row.fragments if part.local_bbox[2] < first_amount_left
    )
    columns = [(label_right + first_amount_left) / 2]
    for i, (a, b) in enumerate(zip(years, years[1:])):
        left_edge = max(a.local_bbox[2], *(boxes[i][2] for _, boxes in data))
        right_edge = min(b.local_bbox[0], *(boxes[i + 1][0] for _, boxes in data))
        if right_edge - left_edge < 0.4 * em:
            return None
        columns.append((left_edge + right_edge) / 2)

    top = start
    span_header = False
    if start > 0:
        prior = rows[start - 1]
        if (
            len(years) == 2
            and len(prior.fragments) == 2
            and rows[start].bbox[1] - prior.bbox[3] <= 0.65 * em
            and prior.fragments[0].local_bbox[2] < columns[0]
            and columns[0] < prior.fragments[1].local_bbox[0] < columns[1] < prior.bbox[2]
            and abs(prior.fragments[1].local_bbox[0] - years[0].local_bbox[0]) < em
        ):
            top -= 1
            span_header = True

    # 物理横线与字带短距相贴时采用其外缘，子列小计线不放大整个左列。
    body = rows[top : last + 1]
    left, top_y, right, bottom = _bbox_union_many([row.bbox for row in body])
    table_top = top_y - 0.15 * em
    preceding = (*trusted_text_bboxes, *((rows[top - 1].bbox,) if top else ()))
    # 左面的独立题名可能与年份字框虚边重叠，据其下缘保留排序间隙。
    table_top = max(
        [
            table_top,
            *(
                box[3] + 0.03 * em
                for box in preceding
                if box[0] <= left + em
                and left < box[2] <= columns[0]
                and box[1] < top_y
                and table_top < box[3] + 0.03 * em < rows[top].center_y
            ),
        ]
    )
    borders = [
        rule
        for rule in [*footer, *rules]
        if rule.bbox[0] <= left + 0.6 * em
        and rule.bbox[2] >= right - 0.4 * em
        and top_y - 0.6 * em <= _bbox_center_y(rule.bbox) <= rows[header_end].bbox[3] + 0.6 * em
    ] + [rule for rule in footer if rule.bbox[0] <= left + 0.6 * em and rule.bbox[2] >= right - 0.4 * em]
    bounds = (
        min(left - 0.15 * em, *(rule.bbox[0] for rule in borders)) if borders else left - 0.15 * em,
        table_top,
        max(right + 0.15 * em, *(rule.bbox[2] for rule in borders)) if borders else right + 0.15 * em,
        max(bottom + 0.15 * em, *(rule.bbox[3] for rule in footer)),
    )
    logical = _period_logical_rows(body, header_end - top, {idx - top for idx, _ in data}, em)
    vertical = [bounds[0], *columns, bounds[2]]
    horizontal = [
        bounds[1],
        *((a[-1].center_y + b[0].center_y) / 2 for a, b in zip(logical, logical[1:])),
        bounds[3],
    ]
    grid = [
        _AxisLine((x - 0.05, horizontal[1] if span_header and i == 2 else bounds[1], x + 0.05, bounds[3]), 0.1, "vertical")
        for i, x in enumerate(vertical)
    ]
    grid.extend(_AxisLine((bounds[0], y - 0.05, bounds[2], y + 0.05), 0.1, "horizontal") for y in horizontal)
    return _VerifiedTableCandidate(
        bbox=bounds,
        local_bbox=bounds,
        angle=0,
        score=125,
        core_bbox=bounds,
        line_indices={fragment.line_index for row in body for fragment in row.fragments},
        inferred_grid=grid,
        inferred_grid_authoritative=True,
        preserve_inline_font_styles=True,
        verified_region_rows=len(logical),
        verified_period_columns=len(vertical) - 1,
    )


def _period_candidate_recovers_area(candidate, existing, siblings, rows, source, em):
    """允许完整表替代旧裁片，仅有越过新框的正文才能证明旧大框吞入了外部内容。"""
    bounds = candidate.bbox
    dependencies = set()
    for prior in existing:
        old = prior.core_bbox or prior.bbox
        if _bbox_overlap_in_smaller(bounds, old) < 0.5:
            continue
        if (
            not prior.annotations
            and _bbox_overlap_in_first(old, bounds) >= 0.95
            and bounds[3] - bounds[1] > old[3] - old[1] + 1.5 * em
        ):
            continue
        if (
            not prior.annotations
            and _bbox_overlap_in_first(bounds, old) >= 0.75
            and any(
                line.source_index in prior.line_indices
                and not bounds[1] <= _bbox_center_y(line.bbox) <= bounds[3]
                and line.bbox[0] < bounds[0] + em
                and line.bbox[2] > bounds[2] - 2 * em
                and len(line.text.split()) >= 10
                for line in source.lines
            )
        ):
            outside = set(prior.line_indices) - candidate.line_indices
            amounts = [
                {
                    part.line_index
                    for part in row.fragments
                    if part.line_index in outside and _ACCOUNTING_AMOUNT_RE.fullmatch("".join(part.text.split()))
                }
                for row in rows
            ]
            structured = set().union(*(indices for indices in amounts if len(indices) >= 2))
            # 旧框的其他金额行必须由独立兄弟表接住，并等其成功物化才允许此表替代。
            supporting = [item for item in siblings if structured.intersection(item.line_indices)]
            if structured.issubset(set().union(*(item.line_indices for item in supporting))):
                dependencies.update(item.bbox for item in supporting)
                continue
        return False
    candidate.verified_period_dependencies = tuple(sorted(dependencies))
    return True


def _detect_period_amount_tables(source: _PageSource, excluded: list[BBox], existing: list[_TableCandidate]):
    """提名具备年份、金额、横线和排除区证据的少线表区，保留可用的既有候选。"""
    lines = [
        line
        for line in source.lines
        if line.angle == 0 and line.semantic_type is None and line.effective_height > 0 and not line.formula_candidate_only
    ]
    if sum(bool(_YEAR.fullmatch("".join(line.text.split()))) for line in lines) < 2:
        return []
    em = statistics.median(line.effective_height for line in lines)
    rules = [
        rule for rule in source.drawing_lines if rule.orientation == "horizontal" and rule.bbox[2] - rule.bbox[0] >= 2 * em
    ]
    if len(rules) < 2:
        return []
    rows = _cluster_fragment_rows(_build_fragments(lines, source.page_size), em)
    vertical_rules = [rule for rule in source.drawing_lines if rule.orientation == "vertical"]
    result = []
    for start, row in enumerate(rows):
        years = _period_years(row, em)
        if not years:
            continue
        candidate = _period_candidate(rows, start, years, rules, em, source.trusted_text_bboxes)
        if (
            candidate is not None
            and not any(
                candidate.bbox[0] + 0.5 * em < x < candidate.bbox[2] - 0.5 * em
                for x in _full_height_vertical_rule_positions(vertical_rules, candidate.bbox, em)
            )
            and not any(_bbox_overlap_in_smaller(candidate.bbox, box) >= 0.5 for box in excluded)
            and _period_candidate_recovers_area(candidate, [*existing, *result], result, rows, source, em)
        ):
            result.append(candidate)
    return result
