"""识别带填充背景的等宽代码区域并投影其空间文本。"""

from __future__ import annotations

from .layout_evidence import build_layout_evidence
from .layout_diagnostics import retain_line_members

import math
import re
import statistics
import unicodedata
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ....document.pdf._document import PDFPathInfo
from ....schema import BBox
from .algorithm_projection import numbered_algorithm_labels
from .geometry import (
    _bbox_area,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_overlap_in_smaller,
    _bbox_union_many,
    _coerce_bbox,
)
from .models import _CodeCandidate, _LineItem, _PageSource
from .native_text import _fill_native_typography, _normalize_native_run_text, _sanitize_pdf_control_text
from .spatial_text import project_pdf_spatial_text

_MONOSPACE_FONT_HINTS = (
    "mono",
    "courier",
    "consolas",
    "menlo",
    "typewriter",
    "fixed",
    "code",
)
_EXTERNAL_CODE_MARKER = re.compile(r"\s*([0-9]{1,3}[.)])\s+(\S.*?)\s*")
_CODE_MARKER_LABEL = re.compile(r"[0-9]{1,3}[.)]")
_JOINED_CLOSING_BRACES = re.compile(r"^[ \t]*(?:}[ \t]*){2,}$", re.MULTILINE)
_PROGRAM_ROW_BOUNDARY = re.compile(r"[;{}]\s*$|^\s*(?:def|class|if|for|while)\b.*:\s*$")


def _split_background_code_markers(
    lines: list[_LineItem], paths: Iterable[PDFPathInfo], page_size: tuple[float, float]
) -> list[_LineItem]:
    """在页级样式推断前拆开灰底代码首行与框外独立编号。"""
    marked = [
        (line, match)
        for line in lines
        if line.angle == 0 and line.text[:1].isdigit() and (match := _EXTERNAL_CODE_MARKER.fullmatch(line.text))
    ]
    if not marked:
        return lines
    width, height = page_size
    panels = [
        path.bbox
        for path in paths
        if path.form_depth == 0
        and path.bbox in path.rectangle_bboxes
        and path.fill_visible
        and _path_has_visible_nonwhite_fill(path)
        and path.bbox[2] - path.bbox[0] >= 0.5 * width
        and 0.008 * height <= path.bbox[3] - path.bbox[1]
        and _bbox_area(path.bbox) < 0.8 * width * height
    ]
    if not panels:
        return lines

    splits: dict[int, tuple[_LineItem, _LineItem]] = {}
    next_source_index = max(line.source_index for line in lines) + 1
    for line, match in marked:
        em = max(1.0, line.effective_height)
        possible = [box for box in panels if line.bbox[0] < box[0] < line.bbox[2] <= box[2] and abs(line.bbox[1] - box[1]) < em]
        if len(possible) != 1:
            continue
        box = possible[0]
        parts = _split_code_marker_glyphs(line, match.groups(), box, page_size, next_source_index)
        if parts is None:
            continue
        marker, body = parts
        inside = [
            member
            for member in lines
            if member is not line and member.angle == 0 and _bbox_overlap_in_first(member.bbox, box) >= 0.8
        ]
        if (
            len(inside) < 3
            or any(member.bbox[1] < body.bbox[1] for member in inside)
            or sum(bool(_PROGRAM_ROW_BOUNDARY.search(member.text)) for member in inside) < 2
        ):
            continue
        members = [body, *inside]
        monospace, widths = _monospace_character_support(members)
        if monospace < 0.8 or not _monospace_advances_are_stable(widths):
            continue
        cell_width = statistics.median(value for group in widths.values() for value in group)
        if _code_rows_have_spatial_structure(members, box, cell_width):
            splits[line.source_index] = (marker, body)
            next_source_index += 1
    if not splits:
        return lines
    return [member for line in lines for member in splits.get(line.source_index, (line,))]


def _split_code_marker_glyphs(
    line: _LineItem,
    texts: tuple[str, str],
    panel: BBox,
    page_size: tuple[float, float],
    inner_source_index: int,
) -> tuple[_LineItem, _LineItem] | None:
    """以字形框验证序号和代码的物理分界，并为两边重建独立排版证据。"""
    left_items, right_items = [], []
    for char in line.chars:
        box = _coerce_bbox(char.get("bbox"))
        if box is None:
            if str(char.get("char") or "").strip():
                return None
            # PDFium 会在两对象之间插入零宽空格，保留两段内部的空白。
            (right_items if right_items else left_items).append((char, None))
        elif box[2] <= panel[0] and not right_items:
            left_items.append((char, box))
        elif box[0] >= panel[0] and box[2] <= panel[2]:
            right_items.append((char, box))
        else:
            return None
    output = []
    for offset, (items, text) in enumerate(zip((left_items, right_items), texts, strict=True)):
        glyphs = [char for char, _box in items]
        bounds = [box for _char, box in items if box is not None]
        if not bounds or _normalize_native_run_text("".join(str(char.get("char") or "") for char in glyphs)) != text:
            return None
        member = _LineItem(
            text=text,
            bbox=_bbox_union_many(bounds),
            angle=line.angle,
            source_index=line.source_index if offset == 0 else inner_source_index,
            chars=glyphs,
            visual_row_id=line.visual_row_id,
            run_index=line.run_index + offset,
            split_from_row=True,
            preserve_split_boundary=True,
        )
        _fill_native_typography(member, page_size)
        output.append(member)
    return output[0], output[1]


def is_numbered_algorithm_projection(content: str) -> bool:
    """带算法题名、连续行号和多条控制语句的空间投影可直接作为代码保留。"""
    return bool(numbered_algorithm_labels(content))


def _build_code_blocks(
    source: _PageSource,
    excluded_bboxes: list[BBox],
    claimed_line_indices: set[int],
    *,
    marginal_lines: Iterable[_LineItem] = (),
) -> tuple[list[dict[str, Any]], set[int]]:
    """检测代码背景、唯一认领其文本，并输出页内 code block。"""

    candidates = _detect_code_candidates(
        source,
        excluded_bboxes,
        claimed_line_indices,
    )
    if candidates:
        members = _code_marginal_members(source, candidates, marginal_lines, claimed_line_indices)
        if members:
            source = replace(source, lines=[*source.lines, *members])
    return _materialize_code_candidates(source, candidates)


def _code_marginal_members(source, candidates, marginal_lines, claimed):
    """仅让已有内容证据的代码框取回同排版、相邻的等宽页边行。"""
    recovered = []
    by_index = {line.source_index: line for line in source.lines}
    for line in marginal_lines:
        if line.angle != 0 or line.semantic_type not in {"header", "footer"} or line.source_index in claimed:
            continue
        owners = [c for c in candidates if c.angle == 0 and _bbox_overlap_in_first(line.bbox, c.bbox) >= 0.98]
        if len(owners) != 1 or _monospace_character_support([line])[0] < 0.8:
            continue
        owner = owners[0]
        em = max(1.0, line.bbox[3] - line.bbox[1])
        if any(
            abs(other.bbox[0] - line.bbox[0]) <= 3 * em
            and 0.6 * em <= abs(_bbox_center_y(other.bbox) - _bbox_center_y(line.bbox)) <= 2.3 * em
            for other in (by_index[i] for i in owner.line_indices if i in by_index)
        ):
            owner.line_indices.add(line.source_index)
            recovered.append(line)
    return recovered


def _build_rule_delimited_code_blocks(
    source: _PageSource,
    excluded_bboxes: list[BBox],
    claimed_line_indices: set[int] | None = None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """在表格认领前物化外框或上下横线限定的代码清单。"""

    candidates = _detect_rule_delimited_code_candidates(
        source,
        excluded_bboxes,
        claimed_line_indices or set(),
    )
    candidates += _detect_caption_algorithm_candidates(
        source, excluded_bboxes + [candidate.bbox for candidate in candidates], claimed_line_indices or set()
    )
    return _materialize_code_candidates(source, candidates)


def _detect_caption_algorithm_candidates(
    source: _PageSource,
    excluded_bboxes: list[BBox],
    claimed: set[int],
) -> list[_CodeCandidate]:
    """按算法标题及重复控制行恢复清单，无横线时以正文或下一节边界停止。"""
    width, _height = source.page_size
    headings = [
        line
        for line in source.lines
        if line.angle == 0 and re.match(r"^\s*(?:algorithm|算法)\s*\d+[.:\s]", line.text, re.IGNORECASE)
    ]
    control = re.compile(
        r"^\s*(?:\d+\s*[:.]\s*)?(?:if|else|end|for|while|input|output|return|repeat|until|require|ensure)\b", re.IGNORECASE
    )
    layout = build_layout_evidence(source.lines, source.page_size, barriers=excluded_bboxes)
    output = []
    for heading in headings:
        hb = heading.bbox
        corridor = layout.corridor(hb)
        if corridor is None:
            continue
        left, right = corridor
        em = max(1.0, heading.effective_height)
        available = sorted(
            [
                line
                for line in source.lines
                if line.angle == 0
                and line.source_index not in claimed
                and left <= (line.bbox[0] + line.bbox[2]) / 2 < right
                and line.bbox[1] >= hb[1]
                and line is not heading
            ],
            key=lambda line: (line.bbox[1], line.bbox[0]),
        )
        starts = [i for i, line in enumerate(available) if control.match(line.text) and line.bbox[1] - hb[3] < 5 * em]
        if not starts:
            continue
        members = []
        bottom = available[starts[0]].bbox[3]
        for line in available[starts[0] :]:
            if line.bbox[1] - bottom > 1.5 * em:
                break
            if re.match(r"^\s*\d+(?:\.\d+)*[.]?\s+[A-Z]", line.text) and not control.match(line.text):
                break
            if any(_bbox_overlap_in_smaller(line.bbox, bbox) >= 0.5 for bbox in excluded_bboxes):
                break
            members.append(line)
            bottom = max(bottom, line.bbox[3])
        if sum(bool(control.match(line.text)) for line in members) < 3:
            continue
        bbox = _bbox_union_many([line.bbox for line in members])
        if bbox[3] - bbox[1] < 5 * em:
            continue
        rules = [
            rule.bbox
            for rule in source.drawing_lines
            if rule.orientation == "horizontal"
            and abs(rule.bbox[0] - hb[0]) < em
            and rule.bbox[2] >= bbox[2] - em
            and rule.bbox[2] <= right + em
        ]
        borders = [
            rule
            for rule in rules
            if hb[3] <= rule[1] <= bbox[1] and bbox[1] - rule[3] <= 1.5 * em or bbox[3] <= rule[1] <= bbox[3] + 1.5 * em
        ]
        if borders:
            bbox = _bbox_union_many([bbox, *borders])
        output.append(_CodeCandidate(bbox=bbox, angle=0, line_indices={line.source_index for line in members}))
    return output


def _materialize_code_candidates(
    source: _PageSource,
    candidates: list[_CodeCandidate],
) -> tuple[list[dict[str, Any]], set[int]]:
    """统一投影代码候选，确保来源行只被一个 code block 认领。"""

    if not candidates:
        return [], set()
    lines_by_index = {line.source_index: line for line in source.lines}
    blocks: list[dict[str, Any]] = []
    claimed: set[int] = set()
    for candidate in candidates:
        members = [
            lines_by_index[source_index] for source_index in sorted(candidate.line_indices) if source_index in lines_by_index
        ]
        if not members:
            continue
        separated = _project_numbered_panel_rows(members, source.lines, candidate.bbox)
        if separated is not None:
            content, marker_index = separated
        else:
            marker_index = None
            content = project_pdf_spatial_text(
                _code_member_chars(members),
                candidate.bbox,
                candidate.angle,
                preserve_blank_rows=True,
            )
            if candidate.angle == 0 and _JOINED_CLOSING_BRACES.search(content):
                content = _restore_split_closing_braces(content, members, candidate.bbox)
        if not content:
            content = _fallback_code_content(members)
        if not content:
            continue
        blocks.append(
            {
                "type": "code",
                "bbox": candidate.bbox,
                "angle": candidate.angle,
                "content": content,
            }
        )
        if marker_index is not None:
            blocks[-1]["_code_external_marker"] = marker_index
        retain_line_members(blocks[-1], members, enabled=source.collect_layout_diagnostics)
        claimed.update(candidate.line_indices)
    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    return blocks, claimed


def _project_numbered_panel_rows(members: list[_LineItem], page_lines: list[_LineItem], bbox: BBox) -> tuple[str, int] | None:
    """在已按框拆开的代码中逐视觉行恢复缩进与空行，避免紧邻花括号被叠成一行。"""
    if not any(line.preserve_split_boundary for line in members):
        return None
    ordered = sorted(members, key=lambda line: (line.bbox[1], line.bbox[0]))
    first = ordered[0]
    if not first.preserve_split_boundary or first.visual_row_id is None:
        return None
    markers = [
        line
        for line in page_lines
        if line.visual_row_id == first.visual_row_id
        and line.preserve_split_boundary
        and line.bbox[2] <= bbox[0]
        and _CODE_MARKER_LABEL.fullmatch(line.text)
    ]
    if len(markers) != 1:
        return None
    content = _project_physical_code_rows(ordered)
    return (content, markers[0].source_index) if content is not None else None


def _restore_split_closing_braces(content: str, members: list[_LineItem], bbox: BBox) -> str:
    """只有投影把多个实际独立闭括号叠到同行，且其他非空行逐字相同时，采用源物理行。"""
    monospace, widths = _monospace_character_support(members)
    if monospace < 0.8 or not _monospace_advances_are_stable(widths):
        return content
    cell_width = statistics.median(value for values in widths.values() for value in values)
    if not _code_rows_have_spatial_structure(members, bbox, cell_width):
        return content
    physical = _project_physical_code_rows(sorted(members, key=lambda line: (line.bbox[1], line.bbox[0])))
    if physical is None:
        return content
    physical_lines = [line for line in physical.splitlines() if line.strip()]
    position = 0
    corrected = False
    for line in content.splitlines():
        if not line.strip():
            continue
        if position < len(physical_lines) and line == physical_lines[position]:
            position += 1
            continue
        if not _JOINED_CLOSING_BRACES.fullmatch(line):
            return content
        count = line.count("}")
        if [item.strip() for item in physical_lines[position : position + count]] != ["}"] * count:
            return content
        position += count
        corrected = True
    return physical if corrected and position == len(physical_lines) else content


def _project_physical_code_rows(ordered: list[_LineItem]) -> str | None:
    """复用 PDF 视觉行的来源身份，按实际节距还原空行和缩进。"""
    grouped: dict[int, list[_LineItem]] = {}
    for line in ordered:
        if line.visual_row_id is None:
            return None
        grouped.setdefault(line.visual_row_id, []).append(line)
    visual_rows = [(members, _bbox_union_many([member.bbox for member in members])) for members in grouped.values()]
    deltas = [b[1] - a[1] for (_members, a), (_next_members, b) in zip(visual_rows, visual_rows[1:])]
    widths = _monospace_character_support(ordered)[1]["narrow"]
    if not deltas or min(deltas) <= 0 or not widths:
        return None
    row_pitch = _code_row_pitch(deltas)
    cell_width = statistics.median(widths)
    left = min(box[0] for _items, box in visual_rows)
    rows = []
    for index, (items, box) in enumerate(visual_rows):
        text = project_pdf_spatial_text(_code_member_chars(items), box, 0)
        if not text or "\n" in text:
            return None
        if index:
            rows.extend([""] * max(0, round(deltas[index - 1] / row_pitch) - 1))
        rows.append(" " * round((box[0] - left) / cell_width) + text)
    return "\n".join(rows)


def _order_code_marker_companions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """让同一首行的框外编号紧接在其代码框之前，其余块保持原相对顺序。"""
    wanted = {block["_code_external_marker"] for block in blocks if "_code_external_marker" in block}
    if not wanted:
        return blocks
    markers = {}
    for block in blocks:
        lines = block.get("_text_lines", ())
        if len(lines) == 1 and lines[0].source_index in wanted and _CODE_MARKER_LABEL.fullmatch(block["content"]):
            markers[lines[0].source_index] = block
    marker_ids = {id(block) for block in markers.values()}
    ordered = []
    for block in blocks:
        if id(block) in marker_ids:
            continue
        if (marker := markers.pop(block.get("_code_external_marker"), None)) is not None:
            ordered.append(marker)
        ordered.append(block)
    return ordered


def _vertical_rule_candidate_height_coverage(
    rule_bbox: BBox,
    candidate_bbox: BBox,
) -> float:
    """计算竖轨在候选高度方向上的实际覆盖比例，长轨超出候选时仍按交集计量。"""

    candidate_height = max(0.0, candidate_bbox[3] - candidate_bbox[1])
    if candidate_height <= 0:
        return 0.0
    overlap = max(
        0.0,
        min(rule_bbox[3], candidate_bbox[3]) - max(rule_bbox[1], candidate_bbox[1]),
    )
    return overlap / candidate_height


def _detect_rule_delimited_code_candidates(source, excluded_bboxes, claimed_line_indices):
    """几何窗口整批计算，结构、认领及稳定排序继续逐项使用原有规则。"""
    from ...._compute_backend import get_native

    native = get_native()
    page_width, page_height = source.page_size
    if page_width <= 0 or page_height <= 0:
        return []
    available = [line for line in source.lines if line.source_index not in claimed_line_indices and line.angle == 0]
    if len(available) < 5:
        return []
    median_height = statistics.median(max(0.1, line.effective_height or line.bbox[3] - line.bbox[1]) for line in available)
    horizontal = sorted(
        [
            line
            for line in source.drawing_lines
            if line.orientation == "horizontal" and line.bbox[2] - line.bbox[0] >= 0.22 * page_width
        ],
        key=lambda line: (line.bbox[1], line.bbox[0]),
    )
    vertical = [line for line in source.drawing_lines if line.orientation == "vertical"]
    boxes = [line.bbox for line in available]
    hb, vb = [rule.bbox for rule in horizontal], [rule.bbox for rule in vertical]
    windows = native.code_rule_windows(hb, vb, boxes, excluded_bboxes, median_height, page_height)
    if windows is None:
        raise ValueError("native code window detection rejected normalized geometry")
    raw_candidates = []
    protect_wrapped_prose = bool(getattr(source, "discarded_raster_forms", ()))
    evidence = {"protect_wrapped_prose": True} if protect_wrapped_prose else {}
    for top, bottom, indices in windows:
        bbox = _bbox_union_many([horizontal[top].bbox, horizontal[bottom].bbox])
        members = [available[index] for index in indices]
        if _rule_delimited_code_members_are_structured(members, bbox, median_height, **evidence):
            raw_candidates.append(_CodeCandidate(bbox, 0, {line.source_index for line in members}))
        elif protect_wrapped_prose:
            _protect_bordered_list_label(source, members, bbox, median_height)
    accepted = []
    for candidate in sorted(raw_candidates, key=lambda item: _bbox_area(item.bbox)):
        if any(_bbox_overlap_in_smaller(candidate.bbox, existing.bbox) >= 0.85 for existing in accepted):
            continue
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: (item.bbox[1], item.bbox[0]))


def _rule_delimited_code_members_are_structured(
    members: list[_LineItem],
    candidate_bbox: BBox,
    median_height: float,
    *,
    protect_wrapped_prose: bool = False,
) -> bool:
    """验证候选具有稳定基线、代码缩进或窄行号槽，并排除规则多列表格。"""

    if len(members) < 5:
        return False
    rows: dict[int, list[_LineItem]] = {}
    fallback_row = 1_000_000
    for line in members:
        row_key = line.visual_row_id
        if row_key is None:
            row_key = fallback_row
            fallback_row += 1
        rows.setdefault(row_key, []).append(line)
    if len(rows) < 5:
        return False
    ordered_rows = sorted(
        rows.values(),
        key=lambda row: min(_bbox_center_y(line.bbox) for line in row),
    )
    row_centers = [statistics.median(_bbox_center_y(line.bbox) for line in row) for row in ordered_rows]
    row_gaps = [current - previous for previous, current in zip(row_centers, row_centers[1:]) if current > previous]
    if not row_gaps:
        return False
    base_pitch = statistics.median(row_gaps)
    if base_pitch <= 0 or sum(0.65 * base_pitch <= gap <= 1.8 * base_pitch for gap in row_gaps) / len(row_gaps) < 0.75:
        return False
    if sum(len(row) >= 3 for row in ordered_rows) / len(ordered_rows) >= 0.25:
        return False

    left_positions = sorted(line.bbox[0] for line in members)
    indent_clusters: list[list[float]] = []
    for position in left_positions:
        if not indent_clusters or position - statistics.median(indent_clusters[-1]) > 0.75 * median_height:
            indent_clusters.append([position])
        else:
            indent_clusters[-1].append(position)
    narrow_gutter_rows = 0
    for row in ordered_rows:
        ordered = sorted(row, key=lambda line: line.bbox[0])
        if (
            len(ordered) >= 2
            and ordered[0].bbox[2] - ordered[0].bbox[0] <= 2.0 * median_height
            and ordered[1].bbox[0] - ordered[0].bbox[2] >= 0.5 * median_height
        ):
            narrow_gutter_rows += 1
    has_line_number_gutter = narrow_gutter_rows / len(ordered_rows) >= 0.35
    has_indent_hierarchy = len(indent_clusters) >= 3 and sum(len(cluster) >= 2 for cluster in indent_clusters) >= 2
    if not has_line_number_gutter and not has_indent_hierarchy:
        return False
    # 稳定行号槽属于强代码证据，允许右侧长语句或注释自然触及清单边界。
    if has_line_number_gutter:
        return True

    # 缩进和等行距也常见于规范正文；无行号清单还须有程序语法或等宽拉丁排版证据。
    syntax_rows = sum(
        bool(
            re.search(
                r"(?:\b(?:if|else|while|for|return|def|function|begin|end)\b|:=|->|[{}]|\w+\s*\([^。]+\)\s*[;:])",
                line.text,
                re.I,
            )
        )
        for line in members
    )
    monospace_ratio, advances = _monospace_character_support(members)
    if protect_wrapped_prose and monospace_ratio < 0.8 and _rule_window_wraps_numbered_prose(ordered_rows, median_height):
        return False
    if syntax_rows < 2 and not (monospace_ratio >= 0.8 and _monospace_advances_are_stable(advances)):
        return False

    occupied_width = max(line.bbox[2] for line in members) - min(line.bbox[0] for line in members)
    return occupied_width <= 0.95 * max(
        0.1,
        candidate_bbox[2] - candidate_bbox[0],
    )


def _rule_window_wraps_numbered_prose(rows: list[list[_LineItem]], em: float) -> bool:
    """多项连续自然句的同宽悬挂续行是正文段落，框线和散落的编程关键词不能认作代码。"""
    ordered = [sorted(row, key=lambda line: line.bbox[0]) for row in rows]
    markers = []
    for index, row in enumerate(ordered):
        first = row[0]
        match = re.match(r"^\s*(\d{1,3})[.)]\s+([A-Za-z].{24,})", first.text)
        if match and len(re.findall(r"[A-Za-z]{2,}", match[2])) >= 6:
            markers.append((index, int(match[1]), first.bbox[0]))
    wrapped = 0
    for (start, number, left), (end, next_number, next_left) in zip(markers, markers[1:]):
        if next_number != number + 1 or abs(next_left - left) > 0.35 * em or end < start + 2:
            continue
        continuation = ordered[start + 1][0]
        tail = ordered[end - 1][-1].text.rstrip()
        if 0.45 * em <= continuation.bbox[0] - left <= 2.5 * em and re.search(r"[A-Za-z][.!?]$", tail):
            wrapped += 1
    return wrapped >= 3


def _protect_bordered_list_label(source, members, bounds, em):
    """用真实方框和框内悬挂段落保留局部标签，避免去水印后将标签提升为全文标题。"""
    if not members or not any(
        max(abs(a - b) for a, b in zip(rect, bounds, strict=True)) <= 0.35 * em
        for path in source.path_infos
        for rect in path.rectangle_bboxes
    ):
        return
    by_row = {}
    for line in members:
        key = (line.visual_row_id is None, line.visual_row_id if line.visual_row_id is not None else line.source_index)
        by_row.setdefault(key, []).append(line)
    rows = sorted(by_row.values(), key=lambda row: min(_bbox_center_y(line.bbox) for line in row))
    label = rows[0][0]
    if (
        len(rows[0]) == 1
        and label.semantic_type is None
        and 2 <= len(label.text.split()) <= 6
        and label.text.isupper()
        and (label.dominant_font_weight or 0) >= 600
        and 0 <= label.bbox[0] - bounds[0] <= 2.0 * em
        and 0 <= label.bbox[1] - bounds[1] <= 2.5 * em
        and _rule_window_wraps_numbered_prose(rows, em)
    ):
        label.semantic_type = "text"
        label.title_suppressed = True


def _code_member_chars(lines: list[_LineItem]) -> list[dict[str, Any]]:
    """按 char_idx 去重代码成员字符，避免区域内斜向水印混入空间投影。"""

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line in lines:
        for fallback_index, char in enumerate(line.chars):
            if not isinstance(char, dict):
                continue
            try:
                identity = ("source", int(char.get("char_idx")))
            except (TypeError, ValueError):
                identity = ("fallback", fallback_index + line.source_index * 1_000_000)
            if identity in seen:
                continue
            seen.add(identity)
            output.append(char)
    return output


def _framed_template_candidates(source: _PageSource, excluded_bboxes: list[BBox], claimed: set[int]) -> list[_CodeCandidate]:
    """在真实填充框内核对成对的模板指令与插值行，保留框上的题签和完整缩进。"""
    if not any("{%" in line.text and "end" in line.text for line in source.lines):
        return []
    candidates = []
    for path in source.path_infos:
        box = path.bbox
        if (
            not path.fill_visible
            or not _path_has_visible_nonwhite_fill(path)
            or any(_bbox_overlap_in_smaller(box, other) >= 0.5 for other in excluded_bboxes)
        ):
            continue
        members = sorted(
            (line for line in source.lines if _bbox_overlap_in_first(line.bbox, box) > 0), key=lambda line: line.bbox[1]
        )
        if not members or any(
            line.angle or line.source_index in claimed or line.bbox[0] < box[0] or line.bbox[2] > box[2] for line in members
        ):
            continue
        em = statistics.median(max(1.0, line.effective_height) for line in members)
        if (
            abs(members[0].bbox[1] - box[1]) > em
            or abs(members[-1].bbox[3] - box[3]) > em
            or any(b.bbox[1] - a.bbox[3] > em for a, b in zip(members, members[1:]))
        ):
            continue
        nesting = []
        closed = expressions = 0
        for line in members:
            tag = re.fullmatch(r"\s*\{%-?\s*(\w+)\b.*?-?%\}\s*", line.text)
            if tag:
                word = tag[1]
                if word.startswith("end"):
                    if not nesting or nesting.pop() != word[3:]:
                        break
                    closed += 1
                else:
                    nesting.append(word)
            elif re.search(r"\{\{[^{}]+\}\}", line.text):
                expressions += 1
            elif nesting or closed:
                break
        else:
            if closed and not nesting and expressions and 2 * closed + expressions > len(members) / 2:
                bounds = _bbox_union_many([box, *(line.bbox for line in members)])
                candidates.append(_CodeCandidate(bounds, 0, {line.source_index for line in members}))
    return candidates


def _detect_code_candidates(
    source: _PageSource,
    excluded_bboxes: list[BBox],
    claimed_line_indices: set[int],
) -> list[_CodeCandidate]:
    """以非白填充矩形、等宽字体和规则空间栅格筛选代码候选。"""

    page_width, page_height = source.page_size
    page_area = max(0.0, page_width) * max(0.0, page_height)
    if page_area <= 0 or not source.path_infos:
        return []
    raw_candidates = _framed_template_candidates(source, excluded_bboxes, claimed_line_indices)
    for path_info in source.path_infos:
        bbox = path_info.bbox
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if (
            path_info.form_depth != 0
            or not path_info.fill_visible
            or path_info.segment_count < 4
            or not _path_has_visible_nonwhite_fill(path_info)
            or width < 0.5 * page_width
            or height < 0.008 * page_height
            or _bbox_area(bbox) >= 0.8 * page_area
            or any(_bbox_overlap_in_smaller(bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes)
        ):
            continue
        members = [
            line
            for line in source.lines
            if line.source_index not in claimed_line_indices and _bbox_overlap_in_first(line.bbox, bbox) >= 0.8
        ]
        if not members:
            continue
        dominant_angle = _dominant_code_angle(members)
        angle_members = [line for line in members if line.angle == dominant_angle]
        total_support = sum(_estimated_line_character_count(line) for line in members)
        angle_support = sum(_estimated_line_character_count(line) for line in angle_members)
        if total_support <= 0 or angle_support / total_support < 0.8:
            continue
        monospace_ratio, cell_widths = _monospace_character_support(angle_members)
        if monospace_ratio < 0.8 and not _has_monospaced_program_with_comments(angle_members):
            continue
        if not _monospace_advances_are_stable(cell_widths):
            continue
        median_cell_width = statistics.median(width_value for values in cell_widths.values() for width_value in values)
        if not _code_rows_have_spatial_structure(
            angle_members,
            bbox,
            median_cell_width,
        ):
            continue
        raw_candidates.append(
            _CodeCandidate(
                bbox=bbox,
                angle=dominant_angle,
                line_indices={line.source_index for line in angle_members},
            )
        )

    accepted: list[_CodeCandidate] = []
    for candidate in sorted(raw_candidates, key=lambda item: _bbox_area(item.bbox)):
        if any(_bbox_overlap_in_smaller(candidate.bbox, existing.bbox) >= 0.9 for existing in accepted):
            continue
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: (item.bbox[1], item.bbox[0]))


def _has_monospaced_program_with_comments(lines: list[_LineItem]) -> bool:
    """注释使用正文或 CJK 字体时，独立用有缩进和重复语法的程序行验证等宽支持。"""
    body = [line for line in lines if not line.text.lstrip().startswith(("//", "/*", "* ", "*/", "# "))]
    if len(body) == len(lines) or len(body) < max(3, len(lines) / 3):
        return False
    statements = [
        line
        for line in body
        if re.match(r"^\s*(?:(?:if|while|for)\s*\(|return\b|\w[\w\s*\[\].,]*\([^)]*\)\s*[{;]|[\w.\[\]]+\s*=(?!=))", line.text)
    ]
    if len(statements) < 2:
        return False
    em = statistics.median(max(1.0, line.bbox[3] - line.bbox[1]) for line in body)
    if max(line.bbox[0] for line in body) - min(line.bbox[0] for line in body) < 0.5 * em:
        return False
    return _monospace_character_support(body)[0] >= 0.8


def _path_has_visible_nonwhite_fill(path_info: PDFPathInfo) -> bool:
    """检查填充色是否可见且与白色背景存在最小颜色差。"""

    if path_info.fill_rgba is None:
        return False
    red, green, blue, alpha = path_info.fill_rgba
    return alpha > 0 and max(255 - red, 255 - green, 255 - blue) >= 5


def _dominant_code_angle(lines: list[_LineItem]) -> int:
    """按估算字符数选择代码区域的主文本方向。"""

    support: dict[int, float] = {}
    for line in lines:
        support[line.angle] = support.get(line.angle, 0.0) + _estimated_line_character_count(line)
    return max(sorted(support), key=lambda angle: support[angle])


def _estimated_line_character_count(line: _LineItem) -> float:
    """优先按字符对象计数，缺失时用行宽和缓存字宽估算字符支持。"""

    valid_chars = [char for char in line.chars if isinstance(char, dict) and str(char.get("char") or "").strip()]
    if valid_chars:
        return float(len(valid_chars))
    line_width = max(0.1, line.bbox[2] - line.bbox[0])
    glyph_width = max(0.1, line.median_glyph_width or line_width)
    return max(1.0, line_width / glyph_width)


def _font_name_looks_monospaced(name: str | None) -> bool:
    """按字体元数据中的通用等宽提示判断字体族，不匹配文档内容。"""

    normalized = (name or "").replace("-", "").replace("_", "").casefold()
    return any(hint in normalized for hint in _MONOSPACE_FONT_HINTS)


def _monospace_character_support(
    lines: list[_LineItem],
) -> tuple[float, dict[str, list[float]]]:
    """统计等宽字体字符占比，并按东西文宽度组收集字符 advance。"""

    supported = 0.0
    total = 0.0
    widths: dict[str, list[float]] = {"narrow": [], "wide": []}
    for line in lines:
        fallback_monospace = _font_name_looks_monospaced(line.font_signature[0] if line.font_signature is not None else None)
        for char in line.chars:
            if not isinstance(char, dict):
                continue
            value = str(char.get("char") or "")
            if not value.strip():
                continue
            total += 1.0
            font = char.get("font")
            font_name = font.get("name") if isinstance(font, dict) else None
            if _font_name_looks_monospaced(font_name) or fallback_monospace:
                supported += 1.0
            try:
                x0, _y0, x1, _y1 = [float(item) for item in char.get("bbox", [])]
            except (TypeError, ValueError):
                continue
            width = x1 - x0
            if not math.isfinite(width) or width <= 0.1:
                continue
            width_group = "wide" if unicodedata.east_asian_width(value[0]) in {"W", "F"} else "narrow"
            widths[width_group].append(width)

    if total <= 0:
        fallback_support = sum(_estimated_line_character_count(line) for line in lines)
        if fallback_support <= 0:
            return 0.0, widths
        supported_support = sum(
            _estimated_line_character_count(line)
            for line in lines
            if _font_name_looks_monospaced(line.font_signature[0] if line.font_signature is not None else None)
        )
        fallback_widths = [
            line.median_glyph_width for line in lines if line.median_glyph_width is not None and line.median_glyph_width > 0
        ]
        widths["narrow"].extend(fallback_widths)
        return supported_support / fallback_support, widths
    return supported / total, widths


def _monospace_advances_are_stable(
    widths: dict[str, list[float]],
) -> bool:
    """验证各字符宽度组的中位绝对偏差足够小，并校验中西文宽度关系。"""

    populated = [values for values in widths.values() if values]
    if not populated or sum(len(values) for values in populated) < 3:
        return False
    medians: dict[str, float] = {}
    for group_name, values in widths.items():
        if not values:
            continue
        median_width = statistics.median(values)
        mad = statistics.median(abs(value - median_width) for value in values)
        if mad / max(0.1, median_width) > 0.2:
            return False
        medians[group_name] = median_width
    if "narrow" in medians and "wide" in medians:
        ratio = medians["wide"] / medians["narrow"]
        if not 1.25 <= ratio <= 2.25:
            return False
    return True


def _code_rows_have_spatial_structure(
    lines: list[_LineItem],
    candidate_bbox: BBox,
    median_cell_width: float,
) -> bool:
    """验证代码行具有规则基线，并且左缘落在一致的等宽字符槽。"""

    rows: list[list[_LineItem]] = []
    for line in sorted(lines, key=lambda item: (_bbox_center_y(item.bbox), item.bbox[0])):
        target = next(
            (
                row
                for row in rows
                if any(
                    min(member.bbox[3], line.bbox[3]) - max(member.bbox[1], line.bbox[1])
                    >= 0.5
                    * min(
                        member.bbox[3] - member.bbox[1],
                        line.bbox[3] - line.bbox[1],
                    )
                    for member in row
                )
            ),
            None,
        )
        if target is None:
            rows.append([line])
        else:
            target.append(line)
    if not rows:
        return False

    residuals = []
    for line in lines:
        slot = (line.bbox[0] - candidate_bbox[0]) / max(0.1, median_cell_width)
        residuals.append(abs(slot - round(slot)))
    if sum(residual <= 0.35 for residual in residuals) / len(residuals) < 0.8:
        return False
    if len(rows) == 1:
        return True

    row_tops = sorted(min(line.bbox[1] for line in row) for row in rows)
    deltas = [current - previous for previous, current in zip(row_tops, row_tops[1:]) if current > previous]
    if not deltas:
        return False
    base_pitch = _code_row_pitch(deltas)
    if base_pitch <= 0:
        return False
    return all(abs(delta / base_pitch - round(delta / base_pitch)) <= 0.35 for delta in deltas)


def _code_row_pitch(deltas: list[float]) -> float:
    """从较短的行距求基本节距，避免空行抬高单行估计。"""
    return statistics.median(sorted(deltas)[: math.ceil(0.6 * len(deltas))])


def _fallback_code_content(lines: list[_LineItem]) -> str:
    """空间投影失败时按视觉行和水平位置保留代码文本的最小结构。"""

    ordered = sorted(
        lines,
        key=lambda line: (
            round(_bbox_center_y(line.bbox), 1),
            _bbox_center_x(line.bbox),
            line.source_index,
        ),
    )
    content = "\n".join(line.text for line in ordered if line.text)
    return _sanitize_pdf_control_text(content, preserve_newlines=True).strip()
