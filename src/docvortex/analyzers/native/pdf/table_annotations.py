"""PDF 表题和表注认领；保留原有认领顺序与判定规则。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
import statistics
import math
import unicodedata
from itertools import islice
from typing import Any, Literal
from ....schema import BBox
from .models import _LineItem, _TableAnnotation, _TableCandidate, _VisualRow
from .geometry import (
    _bbox_axis_overlap_ratio,
    _bbox_center_y,
    _bbox_union,
    _bbox_union_many,
    _coerce_bbox,
    _rotate_bbox_to_upright,
)
from .line_layout import _line_effective_height

from .table_constants import _AUXILIARY_TABLE_NOTE_RE, _TABLE_CAPTION_RE, _TABLE_NOTE_RE, _TABLE_SPLIT_NUMBER_RE
from .table_rows import _clip_visual_row_to_corridor


@dataclass(slots=True)
class _PreparedTableNoteRow:
    """保存表注走廊中与候选上下边界无关的预计算行信息。"""

    row: _VisualRow
    line_indices: frozenset[int]
    row_height: float
    row_fonts: frozenset[tuple[str, int]]
    explicit_note: bool
    auxiliary_marker: str | None


@dataclass(slots=True)
class _PreparedTableNoteBodyMetrics:
    """保存同一文本方向的正文高度样本，供候选按纵向排除区间精确筛选。"""

    items: tuple[tuple[int, float, float], ...]
    centers: tuple[float, ...]
    native: Any = None


@dataclass(slots=True)
class _PreparedTableCoreRows:
    """保存一个走廊的原始行引用和来源倒排索引，不跨候选构建缓存。"""

    rows: list[_VisualRow]
    positions: dict[int, int]
    source_rows: dict[int, list[int]]
    native: Any
    marker_positions: dict[str, list[int]] = field(default_factory=dict)
    marker_safe: bool = False
    geometry: Any = None

    def bbox(self, start, end):
        """按极值来源复用原始坐标对象，避免新建大量 Python 浮点值。"""
        if self.geometry is None:
            return None
        indices = self.geometry.union_indices(start, end)
        return tuple(self.rows[index].bbox[axis] for axis, index in enumerate(indices)) if indices is not None else None

    def interval(self, rows: list[_VisualRow]) -> tuple[int, int] | None:
        """只有完全相同、连续且顺序一致的行引用才能使用区间描述。"""
        if not rows:
            return None
        start = self.positions.get(id(rows[0]))
        if start is None or start + len(rows) > len(self.rows):
            return None
        if any(row is not self.rows[start + offset] for offset, row in enumerate(rows)):
            return None
        return start, start + len(rows)

    def references(self, marker, start, end, lines, page_size, angle, marker_cache):
        """Python 语义只算一次，再用来源对应行的有序位置查询精确候选范围。"""
        positions = self.marker_positions.get(marker)
        if positions is None:
            matches = set()
            for line in lines:
                rows = self.source_rows.get(line.source_index)
                if rows and _table_core_references_marker(marker, [line], page_size, angle, marker_cache):
                    matches.update(rows)
            positions = sorted(matches)
            self.marker_positions[marker] = positions
        offset = bisect_left(positions, start)
        return offset < len(positions) and positions[offset] < end


def _prepare_table_core_rows(rows, lines, metrics):
    """一次校验并打包走廊成员；特殊来源和对象仍交给原集合路径。"""
    if metrics.native is None or len({id(row) for row in rows}) != len(rows):
        return None
    members = []
    source_rows = {}
    for position, row in enumerate(rows):
        indices = []
        for fragment in row.fragments:
            index = fragment.line_index
            if type(index) is not int or not -(2**63) <= index < 2**63:
                return None
            indices.append(index)
            source_rows.setdefault(index, []).append(position)
        members.append(indices)
    from ....document.pdf.text import Bbox

    marker_safe = all(
        type(line) is _LineItem
        and type(line.source_index) is int
        and type(line.text) is str
        and type(line.chars) is list
        and all(
            type(char) is dict
            and (char.get("char") is None or type(char.get("char")) is str)
            and (
                char.get("bbox") is None
                or (
                    type(char.get("bbox")) in (tuple, list, Bbox)
                    and len(char["bbox"].bbox if type(char["bbox"]) is Bbox else char["bbox"]) == 4
                    and all(
                        (type(value) is float and math.isfinite(value)) or (type(value) is int and -(2**53) <= value <= 2**53)
                        for value in char["bbox"]
                    )
                )
            )
            for char in line.chars
        )
        for line in lines
    )
    from ...._compute_backend import get_native

    geometry = None
    if all(
        type(row.bbox) in (tuple, list)
        and len(row.bbox) == 4
        and all(type(value) is float and math.isfinite(value) for value in row.bbox)
        for row in rows
    ):
        geometry = get_native().TableRowGeometry([row.bbox for row in rows])
    return _PreparedTableCoreRows(
        rows,
        {id(row): index for index, row in enumerate(rows)},
        source_rows,
        metrics.native.prepare_rows(members),
        marker_safe=marker_safe,
        geometry=geometry,
    )


class _PreparedTableNoteRows(list):
    """保留列表协议并附加本次表注走廊的保序下边界索引。"""

    def __init__(self, rows):
        """只为普通有限行框建立索引，特殊几何保持原遍历行为。"""
        super().__init__(rows)
        from ...._compute_backend import get_native

        native = get_native()
        self.geometry = None
        if native is not None and all(
            type(row.row.bbox) in (tuple, list)
            and len(row.row.bbox) == 4
            and all(type(value) is float and math.isfinite(value) for value in row.row.bbox)
            for row in rows
        ):
            self.geometry = native.TableRowGeometry([row.row.bbox for row in rows])

    def first_after(self, bottom):
        """查找可以跳过的前缀，非有限表底仍从原始首行开始。"""
        if self.geometry is None or type(bottom) is not float:
            return 0
        result = self.geometry.first_after(bottom)
        return 0 if result is None else result


def _table_caption_candidates(
    lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
) -> list[tuple[_LineItem, BBox]]:
    """预先筛出当前文本方向可能成为表题的行，保留原有文本判定顺序。"""
    candidates: list[tuple[_LineItem, BBox]] = []
    for line in lines:
        text = line.text.strip()
        caption_match = _TABLE_CAPTION_RE.match(text)
        is_split_label = text.lower().rstrip(".") in {"table", "tab", "表", "表格"}
        if caption_match is None and not is_split_label:
            continue
        if caption_match is not None:
            suffix = caption_match.group("suffix").strip(" .:–—-")
            if suffix and suffix[0].islower():
                continue
        if is_split_label and not _find_caption_number_peers(line, lines, page_size, angle, median_height):
            continue
        candidates.append((line, _rotate_bbox_to_upright(line.bbox, page_size, angle)))
    return candidates


def _build_table_annotation(
    kind: Literal["caption", "footnote"],
    rows: list[_VisualRow],
    *,
    excluded_line_indices: set[int] | None = None,
    excluded_local_bbox: BBox | None = None,
) -> _TableAnnotation | None:
    """把已确认视觉行压缩成一个带精确来源行集合的表格注释记录。"""

    excluded_line_indices = excluded_line_indices or set()
    fragments = [
        fragment
        for row in rows
        for fragment in row.fragments
        if fragment.line_index not in excluded_line_indices
        and not (
            excluded_local_bbox is not None
            and fragment.local_bbox[3] > excluded_local_bbox[1]
            and _bbox_axis_overlap_ratio(
                fragment.local_bbox,
                excluded_local_bbox,
                axis="x",
            )
            >= 0.05
        )
    ]
    if not fragments:
        return None
    line_bboxes: dict[int, BBox] = {}
    for fragment in fragments:
        existing_bbox = line_bboxes.get(fragment.line_index)
        line_bboxes[fragment.line_index] = fragment.bbox if existing_bbox is None else _bbox_union(existing_bbox, fragment.bbox)
    return _TableAnnotation(
        kind=kind,
        bbox=_bbox_union_many(list(line_bboxes.values())),
        line_indices=set(line_bboxes),
        line_bboxes=line_bboxes,
    )


def _collect_caption_rows(
    rows: list[_VisualRow],
    caption_line: _LineItem | None,
    rule_bbox: BBox,
    median_height: float,
) -> list[_VisualRow]:
    """收集显式标题所在行及其到表格上边界之间的连续换行。"""

    if caption_line is None:
        return []
    caption_row_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(fragment.line_index == caption_line.source_index for fragment in row.fragments)
        ),
        None,
    )
    if caption_row_index is None:
        return []

    output: list[_VisualRow] = []
    previous_bbox: BBox | None = None
    margin = 2.0 * median_height
    for index, row in enumerate(rows[caption_row_index:], start=caption_row_index):
        clipped_row = _clip_visual_row_to_corridor(row, rule_bbox, margin=margin)
        if clipped_row is None:
            continue
        if index > caption_row_index and clipped_row.center_y >= rule_bbox[1]:
            break
        if previous_bbox is not None and max(0.0, clipped_row.bbox[1] - previous_bbox[3]) > 2.0 * median_height:
            break
        output.append(clipped_row)
        previous_bbox = clipped_row.bbox

    if not output or rule_bbox[1] - output[-1].bbox[3] > 2.0 * median_height:
        return []
    return output


def _prepare_table_note_rows(
    rows: list[_VisualRow],
    lines: list[_LineItem],
    rule_bbox: BBox,
    median_height: float,
    page_size: tuple[float, float],
    angle: int,
) -> list[_PreparedTableNoteRow]:
    """按精确横向走廊预计算表注行的候选无关属性，供多个表格候选复用。"""

    margin = 2.0 * median_height
    line_by_index = {line.source_index: line for line in lines}
    output: list[_PreparedTableNoteRow] = []
    for row in rows:
        clipped_row = _clip_visual_row_to_corridor(row, rule_bbox, margin=margin)
        if clipped_row is None:
            continue
        line_indices = frozenset(fragment.line_index for fragment in clipped_row.fragments)
        row_lines = [line_by_index[line_index] for line_index in line_indices if line_index in line_by_index]
        row_heights = [
            _line_effective_height(
                line,
                _rotate_bbox_to_upright(line.bbox, page_size, angle),
            )
            for line in row_lines
        ]
        row_height = statistics.median(row_heights) if row_heights else clipped_row.bbox[3] - clipped_row.bbox[1]
        row_fonts = frozenset(
            line.font_signature for line in row_lines if line.font_signature is not None and line.font_coverage >= 0.75
        )
        row_text = _visual_row_text(clipped_row)
        output.append(
            _PreparedTableNoteRow(
                row=clipped_row,
                line_indices=line_indices,
                row_height=row_height,
                row_fonts=row_fonts,
                explicit_note=_is_table_note_text(row_text),
                auxiliary_marker=_extract_auxiliary_table_note_marker(row_text),
            )
        )
    return _PreparedTableNoteRows(output)


def _prepare_table_note_body_metrics(
    lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
) -> _PreparedTableNoteBodyMetrics:
    """预先计算同方向文本行的局部中心和有效高度，避免候选重复旋转与测量。"""

    items: list[tuple[int, float, float]] = []
    for line in lines:
        if line.angle != angle:
            continue
        local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, angle)
        items.append(
            (
                line.source_index,
                _bbox_center_y(local_bbox),
                _line_effective_height(line, local_bbox),
            )
        )
    items.sort(key=lambda item: item[1])
    frozen_items = tuple(items)
    from ...._compute_backend import get_native

    native = get_native()
    prepared_native = None
    if native is not None and all(
        type(index) is int
        and -(2**63) <= index < 2**63
        and type(center) is float
        and type(height) is float
        and math.isfinite(center)
        and math.isfinite(height)
        for index, center, height in frozen_items
    ):
        prepared_native = native.TableNoteMetrics(frozen_items)
    return _PreparedTableNoteBodyMetrics(
        items=frozen_items,
        centers=tuple(item[1] for item in frozen_items),
        native=prepared_native,
    )


def _collect_footnote_rows(
    rows: list[_VisualRow],
    lines: list[_LineItem],
    rule_bbox: BBox,
    median_height: float,
    core_line_indices: set[int],
    page_size: tuple[float, float],
    angle: int,
    marker_cache: dict[tuple[int, str], bool] | None = None,
    prepared_rows: list[_PreparedTableNoteRow] | None = None,
    prepared_body_metrics: _PreparedTableNoteBodyMetrics | None = None,
    prepared_core: tuple[_PreparedTableCoreRows, int, int] | None = None,
) -> list[_VisualRow]:
    """从表格下边界吸收具有表内引用和版面证据的表注连续行。"""

    output: list[_VisualRow] = []
    bottom = rule_bbox[3]
    note_chain_started = False
    selected_line_indices = set(core_line_indices)
    core_lines = (
        None
        if prepared_core is not None and prepared_core[0].marker_safe
        else [line for line in lines if line.source_index in core_line_indices]
    )
    body_reference_height: float | None = None
    note_left: float | None = None
    note_height: float | None = None
    note_fonts: set[tuple[str, int]] | frozenset[tuple[str, int]] = set()
    prepared = prepared_rows
    if prepared is None:
        prepared = _prepare_table_note_rows(
            rows,
            lines,
            rule_bbox,
            median_height,
            page_size,
            angle,
        )
    start = prepared.first_after(bottom) if isinstance(prepared, _PreparedTableNoteRows) else 0
    for prepared_row in islice(prepared, start, None):
        clipped_row = prepared_row.row
        if clipped_row.bbox[3] <= bottom:
            continue
        line_indices = prepared_row.line_indices
        if line_indices.issubset(selected_line_indices):
            bottom = max(bottom, clipped_row.bbox[3])
            continue
        row_gap = max(0.0, clipped_row.bbox[1] - bottom)
        row_height = prepared_row.row_height
        row_fonts = prepared_row.row_fonts
        if note_chain_started and clipped_row.bbox[3] - rule_bbox[3] > 10.0 * median_height:
            break
        explicit_note = prepared_row.explicit_note
        auxiliary_marker = prepared_row.auxiliary_marker
        auxiliary_note = False
        if auxiliary_marker is not None:
            if prepared_core is not None and prepared_core[0].marker_safe:
                context, start, end = prepared_core
                auxiliary_note = context.references(auxiliary_marker, start, end, lines, page_size, angle, marker_cache)
            else:
                if core_lines is None:
                    core_lines = [line for line in lines if line.source_index in core_line_indices]
                auxiliary_note = _table_core_references_marker(auxiliary_marker, core_lines, page_size, angle, marker_cache)
        first_gap_limit = 0.75 if auxiliary_note and not explicit_note else 1.25
        if row_gap > (first_gap_limit if not note_chain_started else 1.0) * median_height:
            break
        if not note_chain_started:
            if not explicit_note and not auxiliary_note:
                break
            if explicit_note:
                spatially_compatible = (
                    _bbox_axis_overlap_ratio(clipped_row.bbox, rule_bbox, axis="x") >= 0.35
                    and abs(clipped_row.bbox[0] - rule_bbox[0]) <= 2.0 * median_height
                    and row_height <= 1.15 * median_height
                )
            else:
                if body_reference_height is None:
                    # 仅辅助标记表注需要正文高度参照；显式 Note/Source 不再提前扫描整页。
                    body_reference_height = _table_note_body_reference_height(
                        lines,
                        rule_bbox,
                        median_height,
                        core_line_indices,
                        page_size,
                        angle,
                        prepared_body_metrics,
                        prepared_core,
                    )
                spatially_compatible = (
                    _bbox_axis_overlap_ratio(clipped_row.bbox, rule_bbox, axis="x") >= 0.50
                    and abs(clipped_row.bbox[0] - rule_bbox[0]) <= 3.0 * median_height
                    and row_height <= 1.05 * median_height
                    and row_height <= 0.90 * body_reference_height
                )
            if not spatially_compatible:
                break
            note_left = clipped_row.bbox[0]
            note_height = max(0.1, row_height)
            note_fonts = row_fonts
        elif (
            note_left is None
            or note_height is None
            or abs(clipped_row.bbox[0] - note_left) > 1.5 * median_height
            or not 0.75 <= row_height / note_height <= 1.25
            or (note_fonts and row_fonts and note_fonts.isdisjoint(row_fonts))
            or _bbox_axis_overlap_ratio(clipped_row.bbox, rule_bbox, axis="x") < 0.35
        ):
            # 字号、字体或缩进突变表明已进入标题/正文，表注链必须立即终止。
            break
        output.append(clipped_row)
        selected_line_indices.update(line_indices)
        bottom = max(bottom, clipped_row.bbox[3])
        note_chain_started = True
    return output


def _extract_auxiliary_table_note_marker(text: str) -> str | None:
    """提取行首一至三个通用 Unicode 标记，不解释任何具体标记含义。"""

    match = _AUXILIARY_TABLE_NOTE_RE.match(str(text or ""))
    if match is None:
        return None
    marker = unicodedata.normalize("NFKC", match.group("marker")).casefold()
    if not marker or not all(unicodedata.category(char)[0] in {"L", "N", "S"} for char in marker):
        return None
    return marker


def _table_core_references_marker(
    marker: str,
    core_lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    marker_cache: dict[tuple[int, str], bool] | None = None,
) -> bool:
    """要求通用短标记在表格核心中具有上标或紧凑单元格引用。"""

    for line in core_lines:
        key = (line.source_index, marker)
        if marker_cache is not None and key in marker_cache:
            if marker_cache[key]:
                return True
            continue
        result = _line_has_superscript_marker(line, marker, page_size, angle) or _line_has_compact_marker_token(
            line.text,
            marker,
        )
        if marker_cache is not None:
            marker_cache[key] = result
        if result:
            return True
    return False


def _line_has_compact_marker_token(text: str, marker: str) -> bool:
    """仅在短小单元格文本中确认独立标记 token，避免普通句子偶然命中。"""

    normalized_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    if sum(not char.isspace() for char in normalized_text) > 12:
        return False
    tokens: list[str] = []
    current: list[str] = []
    for char in normalized_text:
        if unicodedata.category(char)[0] in {"L", "N", "S"}:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return len(tokens) <= 4 and marker in tokens


def _line_has_superscript_marker(
    line: _LineItem,
    marker: str,
    page_size: tuple[float, float],
    angle: int,
) -> bool:
    """在正向局部坐标中检查标记字形是否同时更小并明显上移。"""

    glyphs: list[tuple[str, BBox]] = []
    for char in line.chars:
        raw_char = str(char.get("char") or "")
        if not raw_char.isprintable() or raw_char.isspace():
            continue
        bbox = _coerce_bbox(char.get("bbox"))
        if bbox is None:
            continue
        local_bbox = _rotate_bbox_to_upright(bbox, page_size, angle)
        glyphs.append((unicodedata.normalize("NFKC", raw_char).casefold(), local_bbox))
    if len(glyphs) < 2:
        return False

    for start_index in range(len(glyphs)):
        combined = ""
        for end_index in range(start_index, len(glyphs)):
            combined += glyphs[end_index][0]
            if not marker.startswith(combined):
                break
            if combined != marker:
                continue
            marker_indices = set(range(start_index, end_index + 1))
            ordinary_bboxes = [bbox for index, (_char, bbox) in enumerate(glyphs) if index not in marker_indices]
            if not ordinary_bboxes:
                continue
            normal_height = statistics.median(bbox[3] - bbox[1] for bbox in ordinary_bboxes)
            if normal_height <= 0:
                continue
            baseline_bboxes = [bbox for bbox in ordinary_bboxes if bbox[3] - bbox[1] >= 0.90 * normal_height]
            marker_bboxes = [glyphs[index][1] for index in marker_indices]
            marker_height = statistics.median(bbox[3] - bbox[1] for bbox in marker_bboxes)
            marker_center = statistics.median(_bbox_center_y(bbox) for bbox in marker_bboxes)
            normal_center = statistics.median(_bbox_center_y(bbox) for bbox in baseline_bboxes)
            if marker_height <= 0.85 * normal_height and normal_center - marker_center >= 0.12 * normal_height:
                return True
    return False


def _table_note_body_reference_height(
    lines: list[_LineItem],
    rule_bbox: BBox,
    median_height: float,
    core_line_indices: set[int],
    page_size: tuple[float, float],
    angle: int,
    prepared_metrics: _PreparedTableNoteBodyMetrics | None = None,
    prepared_core: tuple[_PreparedTableCoreRows, int, int] | None = None,
) -> float:
    """以同方向非表格行的最高四分位估计正文高度，样本不足时稳健回退。"""

    exclusion_top = rule_bbox[1] - 3.0 * median_height
    exclusion_bottom = rule_bbox[3] + 10.0 * median_height
    if prepared_core is not None and prepared_metrics is not None and prepared_metrics.native is not None:
        context, start, end = prepared_core
        result = prepared_metrics.native.height_for_rows(
            context.native,
            start,
            end,
            exclusion_top,
            exclusion_bottom,
            1.25 * median_height,
        )
        if result is not None:
            return result
    if (
        prepared_metrics is not None
        and prepared_metrics.native is not None
        and all(type(index) is int and -(2**63) <= index < 2**63 for index in core_line_indices)
    ):
        result = prepared_metrics.native.height(exclusion_top, exclusion_bottom, list(core_line_indices), 1.25 * median_height)
        if result is not None:
            return result
    heights: list[float] = []
    if prepared_metrics is None:
        for line in lines:
            if line.angle != angle or line.source_index in core_line_indices:
                continue
            local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, angle)
            if exclusion_top <= _bbox_center_y(local_bbox) <= exclusion_bottom:
                continue
            heights.append(_line_effective_height(line, local_bbox))
    else:
        left_end = bisect_left(prepared_metrics.centers, exclusion_top)
        right_start = bisect_right(prepared_metrics.centers, exclusion_bottom)
        for item_index in range(left_end):
            source_index, _center_y, height = prepared_metrics.items[item_index]
            if source_index not in core_line_indices:
                heights.append(height)
        for item_index in range(right_start, len(prepared_metrics.items)):
            source_index, _center_y, height = prepared_metrics.items[item_index]
            if source_index not in core_line_indices:
                heights.append(height)
    if len(heights) < 4:
        return 1.25 * median_height
    heights.sort()
    upper_quartile_count = max(1, (len(heights) + 3) // 4)
    return statistics.median(heights[-upper_quartile_count:])


def _visual_row_text(row: _VisualRow) -> str:
    """按局部 x 顺序拼接视觉行文本，供拆分脚注标记判断。"""

    return " ".join(fragment.text.strip() for fragment in row.fragments if fragment.text.strip())


def _has_possible_table_note_rows(rows: list[_VisualRow]) -> bool:
    """判断给定视觉行集合中是否存在显式或辅助表注起始行。"""

    return any(
        _is_table_note_text(text := _visual_row_text(row)) or _extract_auxiliary_table_note_marker(text) is not None
        for row in rows
    )


def _has_possible_table_note_rows_in_corridor(
    rows: list[_VisualRow],
    rule_bbox: BBox,
    median_height: float,
) -> bool:
    """按与真实表注收集一致的横向走廊裁剪后，再执行安全的负向预筛。"""

    margin = 2.0 * median_height
    clipped_rows = [
        clipped_row for row in rows if (clipped_row := _clip_visual_row_to_corridor(row, rule_bbox, margin=margin)) is not None
    ]
    return _has_possible_table_note_rows(clipped_rows)


def _is_table_note_text(text: str) -> bool:
    """判断表后首行是否具有明确的注释、来源或脚注标记。"""

    return bool(_TABLE_NOTE_RE.match(str(text or "").strip()))


def _find_table_caption(
    lines: list[_LineItem],
    core_bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
    caption_candidates: list[tuple[_LineItem, BBox]] | None = None,
) -> _LineItem | None:
    """在核心表格上方最多十二倍行高内查找显式 Table/表标题。"""

    candidates: list[tuple[float, _LineItem]] = []
    candidate_rows = caption_candidates
    if candidate_rows is None:
        candidate_rows = _table_caption_candidates(lines, page_size, angle, median_height)
    for line, local_bbox in candidate_rows:
        if _bbox_axis_overlap_ratio(local_bbox, core_bbox, axis="x") < 0.05:
            continue
        gap = core_bbox[1] - local_bbox[3]
        if -median_height <= gap <= 12.0 * median_height:
            candidates.append((abs(gap), line))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _find_caption_number_peers(
    caption_line: _LineItem,
    lines: list[_LineItem],
    page_size: tuple[float, float],
    angle: int,
    median_height: float,
) -> list[_LineItem]:
    """查找与拆分 Table/表 标签同一视觉行的编号文本。"""

    caption_local_bbox = _rotate_bbox_to_upright(caption_line.bbox, page_size, angle)
    peers: list[_LineItem] = []
    for peer in lines:
        if peer.source_index == caption_line.source_index:
            continue
        if not _TABLE_SPLIT_NUMBER_RE.match(peer.text.strip()):
            continue
        peer_local_bbox = _rotate_bbox_to_upright(peer.bbox, page_size, angle)
        gap = peer_local_bbox[0] - caption_local_bbox[2]
        if _bbox_axis_overlap_ratio(caption_local_bbox, peer_local_bbox, axis="y") >= 0.5 and 0.0 <= gap <= 4.0 * median_height:
            peers.append(peer)
    return sorted(
        peers,
        key=lambda peer: _rotate_bbox_to_upright(peer.bbox, page_size, angle)[0],
    )


def _merge_table_candidate_annotations(
    target: _TableCandidate,
    candidate: _TableCandidate,
) -> None:
    """按类型合并重复候选注释，并以表体优先消解来源行角色冲突。"""

    for annotation in candidate.annotations:
        existing = next(
            (item for item in target.annotations if item.kind == annotation.kind),
            None,
        )
        if existing is None:
            target.annotations.append(
                _TableAnnotation(
                    kind=annotation.kind,
                    bbox=annotation.bbox,
                    line_indices=set(annotation.line_indices),
                    line_bboxes=dict(annotation.line_bboxes),
                )
            )
            continue
        existing.bbox = _bbox_union(existing.bbox, annotation.bbox)
        existing.line_indices.update(annotation.line_indices)
        for line_index, bbox in annotation.line_bboxes.items():
            existing_bbox = existing.line_bboxes.get(line_index)
            existing.line_bboxes[line_index] = bbox if existing_bbox is None else _bbox_union(existing_bbox, bbox)

    # 重复候选发生角色冲突时以任一候选确认的表体成员为准，避免表头被并入 caption。
    retained_annotations: list[_TableAnnotation] = []
    for annotation in target.annotations:
        # 注释成员通常很少，逐个查询大型共享表体集合比反向遍历表体成员更省时。
        annotation.line_indices = {
            line_index for line_index in annotation.line_indices if line_index not in target.line_indices
        }
        annotation.line_bboxes = {
            line_index: bbox for line_index, bbox in annotation.line_bboxes.items() if line_index in annotation.line_indices
        }
        if not annotation.line_indices:
            continue
        if annotation.line_bboxes:
            annotation.bbox = _bbox_union_many(
                list(annotation.line_bboxes.values()),
            )
        retained_annotations.append(annotation)
    target.annotations = retained_annotations
