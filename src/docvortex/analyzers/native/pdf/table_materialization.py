"""PDF 表格与注释输出物化；保留原有认领顺序与判定规则。"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import replace
from typing import Any

from loguru import logger

from ....document.pdf.text._contracts import Char
from ....foundation._text import merge_text_line_contents
from ....schema import BBox
from ._table_recovery import (
    NativeTableInput,
    NativeTableRectangle,
    NativeTableResult,
    NativeTableRule,
    coerce_native_table_rectangles,
    coerce_native_table_rules,
    recover_native_pdf_table,
)
from ._table_recovery.contracts import PDFTableRecoveryError
from .algorithm_projection import project_numbered_algorithm
from .geometry import (
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_overlap_in_smaller,
    _bbox_union,
    _bbox_union_many,
    _point_in_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
)
from .line_layout import _font_signatures_share_family, _line_effective_height, _line_tight_output_bbox
from .layout_diagnostics import glyph_index_ranges, glyph_ranges, retain_glyph_members, retain_line_members
from .line_merging import _same_baseline_geometry
from .models import _LineItem, _PageSource, _TableAnnotation, _TableCandidate, _VerifiedTableCandidate
from .native_text import _normalize_native_run_text
from .text_roles import metadata_field, text_role
from .spatial_text import _select_table_chars, project_pdf_table_text
from .table_headers import _RecoveredTableHeader, recover_preceding_table_header
from .table_text_styles import render_native_table_html_with_scripts
from .table_annotations import _is_table_note_text


def _recover_native_table_html(
    source: _PageSource,
    table_bbox: BBox,
    angle: int,
    chars: tuple[Char, ...] | None = None,
    drawing_lines: tuple[NativeTableRule, ...] | None = None,
    rectangles: tuple[NativeTableRectangle, ...] | None = None,
    tight_bboxes: dict[int, BBox] | None = None,
    origins: dict[int, tuple[float, float]] | None = None,
    *,
    _owned_script_inputs: tuple[Any, dict[int, int]] | None = None,
    preserve_font_styles: bool = False,
    protected_lines: set[int] | None = None,
    obstacles: list[BBox] | None = None,
    verified_region: _VerifiedTableCandidate | None = None,
    expected_sources: frozenset[int] = frozenset(),
    layout_members: list[int] | None = None,
) -> tuple[str, _RecoveredTableHeader | None]:
    """恢复高置信表体，并在提供页内认领证据时核对遗漏的邻接表头。"""

    table_input = NativeTableInput(
        table_bbox=table_bbox,
        page_size=source.page_size,
        angle=angle,
        chars=chars if chars is not None else tuple(source.chars),
        drawing_lines=(drawing_lines if drawing_lines is not None else coerce_native_table_rules(source.drawing_lines)),
        rectangles=(rectangles if rectangles is not None else coerce_native_table_rectangles(source.path_infos)),
    )
    recovered = recover_table_result(
        table_input,
        tight_bboxes or {},
        origins or {},
        _owned_script_inputs=_owned_script_inputs,
        preserve_font_styles=preserve_font_styles,
        **({"preserve_printed_spacing": True} if verified_region and verified_region.verified_period_columns else {}),
        **(
            {"grid_tracks": verified_region.verified_grid_tracks}
            if verified_region and verified_region.verified_grid_tracks
            else {}
        ),
    )
    if recovered is None:
        return "", None
    content, body = recovered
    if layout_members is not None:
        layout_members.extend(index for cell in body.cells for index in cell.source_char_indices)
    if verified_region is not None:
        rows, cols = verified_region.verified_region_rows, verified_region.verified_region_columns
        body_band = verified_region.verified_region_body_band
        if (
            _is_verified_table_region(table_input, body, rows, cols, expected_sources, body_band)
            and (not verified_region.verified_period_columns or body.cols == verified_region.verified_period_columns)
            and not _stripe_cells_cross_gutters(table_input, body, verified_region.verified_region_gutters)
            and (body_band is None or not _stripe_currency_crosses_column_rules(table_input, body, body_band))
        ):
            return content, None
        return "", None
    if protected_lines is None or not content.startswith("<table><tbody>"):
        return content, None
    try:
        header = recover_preceding_table_header(source, table_input, body, protected_lines, obstacles or [])
        if header is not None:
            rendered = render_native_table_html_with_scripts(
                header.result,
                header.table_input,
                tight_bboxes or {},
                origins or {},
                _owned_script_inputs=_owned_script_inputs,
                preserve_font_styles=preserve_font_styles,
            )
            # 表头和表体均使用同一序列化器，原有正文格的上下标及字距字节原样保留。
            if rendered.endswith("</tbody></table>"):
                if layout_members is not None:
                    layout_members.extend(index for cell in header.result.cells for index in cell.source_char_indices)
                return rendered.removesuffix("</tbody></table>") + content.removeprefix("<table><tbody>"), header
    except Exception as exc:
        logger.warning(f"Flash native table header recovery retained body: bbox={table_bbox}, error={exc}")
    return content, None


def _is_verified_table_region(
    table_input: NativeTableInput,
    body: NativeTableResult,
    rows: int,
    cols: int,
    expected_sources: frozenset[int],
    body_band: tuple[float, float] | None = None,
) -> bool:
    """结构与实体行带一致，原生字符均能在网格中找到唯一去处。"""
    if body.rows != rows or body.cols < 2:
        return False
    if cols and (
        body.cols != cols
        or len(body.cells) != rows * cols
        or any(not any(char.isalnum() for char in cell.content) for cell in body.cells)
    ):
        return False
    assigned = [idx for cell in body.cells for idx in cell.source_char_indices]
    sources = {glyph.source_index: glyph.bbox for glyph in body.text.glyphs}
    return (
        len(assigned) == len(expected_sources)
        and set(assigned) == sources.keys() == expected_sources
        and not _verified_region_cells_cross_rails(table_input, body, sources, body_band)
    )


def _verified_region_cells_cross_rails(
    table_input: NativeTableInput,
    body: NativeTableResult,
    glyphs: dict[int, BBox],
    body_band: tuple[float, float] | None = None,
) -> bool:
    """检查贯穿正文的实体列界，接回的表头可有自己的合并格。"""
    left, top, right, bottom = table_input.table_bbox
    em = body.text.median_glyph_height
    body_top, body_bottom = body_band or (top, bottom)
    rails = [
        (rule.bbox[0] - left, rule.bbox[1] - top, rule.bbox[2] - left, rule.bbox[3] - top)
        for rule in table_input.drawing_lines
        if rule.orientation == "vertical"
        and left + 0.5 * em < rule.bbox[0] <= rule.bbox[2] < right - 0.5 * em
        and min(rule.bbox[3], body_bottom) - max(rule.bbox[1], body_top) >= 0.95 * (body_bottom - body_top)
    ]
    if not rails:
        return False
    for cell in body.cells:
        boxes = [glyphs[idx] for idx in cell.source_char_indices]
        for start, y0, end, y1 in rails:
            row = [box for box in boxes if y0 <= _bbox_center_y(box) <= y1]
            if any(box[2] <= start for box in row) and any(box[0] >= end for box in row):
                return True
    return False


def _stripe_cells_cross_gutters(
    table_input: NativeTableInput, body: NativeTableResult, gutters: tuple[tuple[float, float], ...]
) -> bool:
    """拒绝将宽列沟两侧的独立字段压到同格，保留实字跨越留白的真实合并格。"""
    if not gutters:
        return False
    left = table_input.table_bbox[0]
    intervals = [(a - left, b - left) for a, b in gutters]
    glyphs = {glyph.source_index: glyph.bbox for glyph in body.text.glyphs if any(char.isalnum() for char in glyph.text)}
    for cell in body.cells:
        boxes = sorted(glyphs[idx] for idx in cell.source_char_indices if idx in glyphs)
        if any(a[2] <= start and b[0] >= end for a, b in zip(boxes, boxes[1:]) for start, end in intervals):
            return True
    return False


def _stripe_currency_crosses_column_rules(
    table_input: NativeTableInput, body: NativeTableResult, body_band: tuple[float, float]
) -> bool:
    """用分段水平列线和原生金额右缘识别落入相邻格的独立货币字形。"""
    glyphs = body.text.glyphs
    symbols = [glyph for glyph in glyphs if len(glyph.text) == 1 and unicodedata.category(glyph.text) == "Sc"]
    if not symbols:
        return False
    left, top, _, _ = table_input.table_bbox
    em = body.text.median_glyph_height
    centers = [_bbox_center_y(row.bbox) for row in body.text.rows]
    pitches = [b - a for a, b in zip(centers, centers[1:]) if b > a]
    # 第一条记录之前可能还印有一行金额表头，邻接短轨按该表自身节距定位。
    rail_window = max(2.5 * em, 1.6 * statistics.median(pitches)) if pitches else 2.5 * em
    rules = [
        (rule.bbox[0] - left, rule.bbox[2] - left, _bbox_center_y(rule.bbox) - top)
        for rule in table_input.drawing_lines
        if rule.orientation == "horizontal"
        and rule.bbox[2] - rule.bbox[0] >= 2 * em
        and body_band[0] - rail_window <= _bbox_center_y(rule.bbox) <= body_band[1] + rail_window
    ]
    if len(rules) < 2:
        return False
    owners = {index: (cell.row, cell.col) for cell in body.cells for index in cell.source_char_indices}
    decimal_glyphs = [glyph for glyph in glyphs if glyph.text.isdecimal()]
    for symbol in symbols:
        owner = owners[symbol.source_index]
        near_rules = [rule for rule in rules if abs(_bbox_center_y(symbol.bbox) - rule[2]) <= rail_window]
        for start, end, y in near_rules:
            if not start <= _bbox_center_x(symbol.bbox) <= end:
                continue
            right_edges = {}
            next_amount_edges = []
            prior_digits = []
            for digit in decimal_glyphs:
                x = _bbox_center_x(digit.bbox)
                if start <= x <= end:
                    right_edges[digit.visual_row] = max(right_edges.get(digit.visual_row, x), digit.bbox[2])
                if digit.visual_row != symbol.visual_row:
                    continue
                digit_owner = owners[digit.source_index]
                if digit_owner == owner:
                    prior_digits.append(x)
                elif digit_owner[0] == owner[0] and start <= x <= end and digit.bbox[0] > symbol.bbox[2]:
                    next_amount_edges.append(digit.bbox[2])
            if not prior_digits or not next_amount_edges or any(start <= x <= end for x in prior_digits):
                continue
            if not any(
                edge <= start and abs(rail_y - y) <= 0.15 * em and any(begin <= x <= edge for x in prior_digits)
                for begin, edge, rail_y in near_rules
            ):
                continue
            last_digit = max(next_amount_edges)
            if sum(abs(x - last_digit) <= 0.5 * em for x in right_edges.values()) >= 3:
                return True
    return False


def _overlapping_table_character_repairs(source: _PageSource) -> dict[int, BBox]:
    """同字体汉字的实际墨迹和字宽证明宽松框异常时，按原生基线恢复聚行尺度。"""
    groups = {}
    for char in source.chars:
        font = char.get("font") or {}
        groups.setdefault((font.get("name"), font.get("weight"), font.get("flags")), []).append(char)
    repairs = {}
    for chars in groups.values():
        anchors = [
            char
            for char in chars
            if re.fullmatch(r"[\u4e00-\u9fff]", char.get("char", "")) and char.get("tight_bbox") and char.get("origin")
        ]
        if len(anchors) < 20:
            continue
        em = statistics.median(char["bbox"][2] - char["bbox"][0] for char in anchors)
        loose = statistics.median(char["bbox"][3] - char["bbox"][1] for char in anchors)
        ink = statistics.median(char["tight_bbox"][3] - char["tight_bbox"][1] for char in anchors)
        if em <= 0 or loose < 2.2 * em or not 0.5 * em <= ink <= 1.4 * em:
            continue
        for char in chars:
            box, origin = char.get("bbox"), char.get("origin")
            if box and origin and char.get("char_idx") is not None and box[3] - box[1] >= 2 * em:
                repairs[char["char_idx"]] = (box[0], origin[1] - 0.85 * em, box[2], origin[1] + 0.15 * em)
    return repairs


def _materialize_table_blocks(
    source: _PageSource,
    candidates: list[_TableCandidate],
    *,
    caption_owners: dict[int, list[tuple[BBox, bool]]] | None = None,
    tight_bboxes: dict[int, BBox] | None = None,
    origins: dict[int, tuple[float, float]] | None = None,
    calibrated_char_bboxes: dict[int, BBox] | None = None,
    _owned_script_inputs: tuple[Any, dict[int, int]] | None = None,
    independent_grid_bboxes: list[BBox] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]]:
    """原子物化表体及其独立注释，仅认领整组成功输出的文本行。"""

    table_blocks: list[dict[str, Any]] = []
    annotation_blocks: list[dict[str, Any]] = []
    accepted_candidate_bboxes: list[BBox] = []
    claimed: set[int] = set()
    if not candidates:
        return table_blocks, annotation_blocks, claimed
    # 只复制已有强证据校准的字符，保持原始来源记录及字符身份不变；未校准文档继续原路径。
    calibrated = {**_overlapping_table_character_repairs(source), **(calibrated_char_bboxes or {})}
    native_chars = (
        tuple(
            dict(char, bbox=calibrated[char["char_idx"]]) if char.get("char_idx") in calibrated else char
            for char in source.chars
        )
        if calibrated
        else tuple(source.chars)
    )
    native_rules = coerce_native_table_rules(source.drawing_lines)
    native_rectangles = coerce_native_table_rectangles(source.path_infos)
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        verified_region = candidate if isinstance(candidate, _VerifiedTableCandidate) else None
        if verified_region is not None and any(
            box not in accepted_candidate_bboxes for box in verified_region.verified_period_dependencies
        ):
            continue
        if verified_region is None:
            _separate_embedded_table_notes(source, candidate)
        # 候选去重仍使用包含注释的完整框，不能因输出表体收缩而放行重复表格。
        if any(_bbox_overlap_in_smaller(candidate.bbox, bbox) >= 0.5 for bbox in accepted_candidate_bboxes):
            continue
        output_angle = candidate.angle
        candidate_annotation_blocks, externalized_line_indices, failed_annotations = _materialize_table_annotations(
            source, candidate
        )
        projection_line_indices = (
            set(candidate.line_indices)
            if verified_region is not None
            else _candidate_projection_line_indices(source, candidate)
        )
        for annotation in candidate.annotations:
            projection_line_indices.update(annotation.line_indices)
        projection_line_indices.difference_update(externalized_line_indices)
        body_bbox = _table_body_materialization_bbox(
            candidate,
            failed_annotations,
        )
        content = ""
        recovered_content = ""
        layout_members = [] if source.collect_layout_diagnostics else None
        expected_sources = (
            frozenset(
                char["char_idx"]
                for line in source.lines
                if line.source_index in projection_line_indices
                for char in line.chars
                if char.get("char_idx") is not None and char.get("char", "").strip()
            )
            if verified_region is not None
            else frozenset()
        )
        try:
            recovered_content, header = _recover_native_table_html(
                source,
                body_bbox,
                candidate.angle,
                native_chars,
                (() if candidate.inferred_grid_authoritative else native_rules)
                + coerce_native_table_rules(candidate.inferred_grid),
                () if candidate.inferred_grid_authoritative else native_rectangles,
                tight_bboxes,
                origins,
                _owned_script_inputs=_owned_script_inputs,
                preserve_font_styles=candidate.preserve_inline_font_styles
                or any(
                    _point_in_bbox(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), body_bbox) for box in calibrated.values()
                ),
                verified_region=verified_region,
                expected_sources=expected_sources,
                **({"layout_members": layout_members} if layout_members is not None else {}),
                protected_lines=claimed
                | {
                    index
                    for other in candidates
                    for annotation in other.annotations
                    if other is not candidate or annotation.kind != "caption"
                    for index in annotation.line_indices
                },
                obstacles=[
                    *source.image_bboxes,
                    *source.form_bboxes,
                    *source.signature_bboxes,
                    *(other.core_bbox or other.bbox for other in candidates if other is not candidate),
                    # 实体横线已确认的编号题注首行沿用检测器框，含紧连中文题名。
                    *(
                        _rotate_bbox_from_upright(row, source.page_size, candidate.angle)
                        for row, _ in (caption_owners or {}).get(id(candidate), ())
                    ),
                ],
            )
            if header is not None:
                trimmed = _without_header_annotation_lines(source, candidate, header.line_indices)
                candidate_annotation_blocks, externalized_line_indices, _ = _materialize_table_annotations(source, trimmed)
                projection_line_indices.update(header.line_indices)
                body_bbox = header.bbox
            content = recovered_content
        except Exception as exc:
            logger.warning(
                f"Flash native table recovery failed and fell back to projection: bbox={candidate.bbox}, error={exc}"
            )
        if content:
            logger.debug(f"Flash native table recovery accepted: bbox={body_bbox}, angle={candidate.angle}")
        elif verified_region is not None:
            continue
        elif _restore_front_matter_text_panel(source, body_bbox):
            # 只有结构恢复失败的弱候选才撤销；已验证的 HTML 单元格表保持原判定。
            continue
        try:
            if not content:
                # 使用完整原始字符流保留 PDF 物理换行；行索引仅负责表体与注释的所有权认领。
                content = project_pdf_table_text(
                    source.chars,
                    body_bbox,
                    angle=candidate.angle,
                )
                if not (caption_owners and id(candidate) in caption_owners):
                    content = project_numbered_algorithm(source.chars, body_bbox, candidate.angle, content)
        except Exception as exc:
            # 表体投影异常时同时撤销预构造注释，保持整组不输出、不认领。
            logger.warning(f"Flash table projection failed and rolled back: bbox={candidate.bbox}, error={exc}")
            continue
        if not content or not content.strip():
            continue
        table_blocks.append(
            {
                "type": "table",
                "bbox": body_bbox,
                "angle": output_angle,
                "content": content,
            }
        )
        if layout_members is not None:
            ranges = (
                glyph_index_ranges(layout_members)
                if recovered_content
                else glyph_ranges(_select_table_chars(source.chars, body_bbox))
            )
            retain_glyph_members(table_blocks[-1], ranges)
        if caption_owners and id(candidate) in caption_owners:
            table_blocks[-1]["_confirmed_table_caption_rows"] = caption_owners[id(candidate)]
        if verified_region is not None and verified_region.verified_period_columns:
            table_blocks[-1]["_verified_period_table"] = True
        if independent_grid_bboxes is not None and verified_region is not None and verified_region.verified_region_columns:
            independent_grid_bboxes.append(body_bbox)
        annotation_blocks.extend(candidate_annotation_blocks)
        accepted_candidate_bboxes.append(candidate.bbox)
        # 表体和成功外置的注释行在同一事务中各认领一次。
        claimed.update(projection_line_indices | externalized_line_indices)
    table_blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    annotation_blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    return table_blocks, annotation_blocks, claimed


def _without_header_annotation_lines(
    source: _PageSource, candidate: _TableCandidate, header_lines: frozenset[int]
) -> _TableCandidate:
    """从题注移出已逐字核验的表头行，并按剩余来源行重建题注框。"""
    annotations = []
    for annotation in candidate.annotations:
        keep = annotation.line_indices - header_lines
        if keep == annotation.line_indices:
            annotations.append(annotation)
        elif keep:
            boxes = {
                line.source_index: annotation.line_bboxes.get(line.source_index, line.bbox)
                for line in source.lines
                if line.source_index in keep
            }
            annotations.append(
                replace(annotation, bbox=_bbox_union_many(list(boxes.values())), line_indices=keep, line_bboxes=boxes)
            )
    return replace(candidate, annotations=annotations)


def _separate_embedded_table_notes(source: _PageSource, candidate: _TableCandidate) -> None:
    """表内横线的规则单元行结束后，将外围框包住的连续表注外置。"""
    if candidate.angle or any(annotation.kind == "footnote" for annotation in candidate.annotations):
        return
    core = candidate.core_bbox or candidate.bbox
    width = core[2] - core[0]
    members = [
        line
        for line in source.lines
        if core[0] - 2 <= line.bbox[0] and line.bbox[2] <= core[2] + 2 and core[1] <= _bbox_center_y(line.bbox) <= core[3]
    ]
    rules = sorted(
        rule.bbox[1]
        for rule in source.drawing_lines
        if rule.orientation == "horizontal"
        and _bbox_axis_overlap_ratio(rule.bbox, core, axis="x") >= 0.8
        and core[1] <= rule.bbox[1] <= core[3]
    )
    for first in sorted(members, key=lambda line: line.bbox[1]):
        if not _is_table_note_text(first.text) or first.bbox[2] - first.bbox[0] < 0.6 * width:
            continue
        em = _line_effective_height(first, first.bbox)
        above = [y for y in rules if y <= first.bbox[1] + 0.1 * em]
        if len(above) < 3 or first.bbox[1] - above[-1] > 1.5 * em:
            continue
        notes = [line for line in members if line.bbox[1] >= first.bbox[1] - 0.2 * em]
        if len(notes) < 2 or any(rule < core[3] - em for rule in rules if rule > first.bbox[3]):
            continue
        # 注释为全宽说明行，不应再出现多列单元格的反复水平分隔。
        if any(abs(line.bbox[0] - first.bbox[0]) > em for line in notes):
            continue
        boxes = {line.source_index: line.bbox for line in notes}
        candidate.annotations.append(_TableAnnotation("footnote", _bbox_union_many(list(boxes.values())), set(boxes), boxes))
        candidate.core_bbox = (core[0], core[1], core[2], above[-1])
        candidate.line_indices.difference_update(boxes)
        return


def _restore_front_matter_text_panel(source: _PageSource, bbox: BBox) -> bool:
    """结构恢复失败后，以独立元数据和连续正文角色撤销首页弱表；可信网格优先。"""
    if source.page_index != 0:
        return False
    lines = [
        line
        for line in source.lines
        if line.angle == 0 and _point_in_bbox((_bbox_center_x(line.bbox), _bbox_center_y(line.bbox)), bbox)
    ]
    fields = [line for line in lines if metadata_field(line.text)]
    if len({metadata_field(line.text) for line in fields}) < 2:
        return False
    em = statistics.median(_line_effective_height(line, line.bbox) for line in lines)
    field_box = _bbox_union_many([line.bbox for line in fields])
    prose = [
        line
        for line in lines
        if line not in fields
        and text_role(line.text) is None
        and (len(line.text.split()) >= 8 or len(re.findall(r"[\u3400-\u9fff]", line.text)) >= 12)
        and (line.bbox[0] >= field_box[2] + em or line.bbox[2] <= field_box[0] - em)
    ]
    if not prose:
        return False
    prose_box = _bbox_union_many([line.bbox for line in prose])
    if not all(abs(line.bbox[0] - prose_box[0]) <= em or abs(line.bbox[2] - prose_box[2]) <= em for line in prose):
        return False
    abstracts = [
        line
        for line in lines
        if text_role(line.text) == "abstract"
        and _bbox_axis_overlap_ratio(line.bbox, prose_box, axis="x") >= 0.5
        and 0 <= prose_box[1] - line.bbox[3] <= 3 * em
    ]
    if len(prose) < 3 and not abstracts:
        return False
    if max(field_box[1], prose_box[1]) - min(field_box[3], prose_box[3]) > em:
        return False
    # 内部分格线与重复横线共同构成真网格；外边框或单独分区线不足以证明数据表。
    rules = [
        rule for rule in source.drawing_lines if _point_in_bbox((_bbox_center_x(rule.bbox), _bbox_center_y(rule.bbox)), bbox)
    ]
    vertical = [
        rule for rule in rules if rule.orientation == "vertical" and bbox[0] + em < _bbox_center_x(rule.bbox) < bbox[2] - em
    ]
    horizontal = [
        rule for rule in rules if rule.orientation == "horizontal" and bbox[1] + em < _bbox_center_y(rule.bbox) < bbox[3] - em
    ]
    if vertical and len(horizontal) >= 2:
        return False
    headings = [line for line in lines if text_role(line.text) in {"metadata", "abstract"}]
    for line in headings:
        line.semantic_type = "paragraph_title"
        line.explicit_section_title = True
    return True


def _materialize_table_annotations(
    source: _PageSource,
    candidate: _TableCandidate,
) -> tuple[list[dict[str, Any]], set[int], list[_TableAnnotation]]:
    """构造候选注释块，并返回成功外置行与需回退到表体的注释记录。"""

    blocks: list[dict[str, Any]] = []
    externalized_line_indices: set[int] = set()
    failed_annotations: list[_TableAnnotation] = []
    annotations = sorted(
        candidate.annotations,
        key=lambda annotation: (
            _rotate_bbox_to_upright(
                annotation.bbox,
                source.page_size,
                candidate.angle,
            )[1],
            _rotate_bbox_to_upright(
                annotation.bbox,
                source.page_size,
                candidate.angle,
            )[0],
            annotation.kind,
        ),
    )
    for annotation in annotations:
        annotation_blocks = _build_table_annotation_blocks(
            source,
            candidate,
            annotation,
        )
        if not annotation_blocks:
            failed_annotations.append(annotation)
            continue
        blocks.extend(annotation_blocks)
        externalized_line_indices.update(annotation.line_indices)
    return blocks, externalized_line_indices, failed_annotations


def _split_table_annotation_visual_groups(
    line_geometry: list[tuple[_LineItem, BBox]],
) -> list[list[tuple[_LineItem, BBox]]]:
    """用短尾后的字体或字号重启拆分独立表格注释段。"""

    if len(line_geometry) < 2:
        return [line_geometry] if line_geometry else []
    maximum_width = max(bbox[2] - bbox[0] for _line, bbox in line_geometry)
    group_left = min(bbox[0] for _line, bbox in line_geometry)
    groups = [[line_geometry[0]]]
    for previous, current in zip(line_geometry, line_geometry[1:]):
        previous_line, previous_bbox = previous
        current_line, current_bbox = current
        previous_width = previous_bbox[2] - previous_bbox[0]
        current_width = current_bbox[2] - current_bbox[0]
        pair_height = max(
            _line_effective_height(*previous),
            _line_effective_height(*current),
        )
        font_switch = (
            previous_line.font_signature is not None
            and current_line.font_signature is not None
            and previous_line.font_coverage >= 0.6
            and current_line.font_coverage >= 0.75
            and not _font_signatures_share_family(
                previous_line.font_signature,
                current_line.font_signature,
            )
        )
        scale_ratio = max(
            previous_line.em_height or _line_effective_height(*previous),
            current_line.em_height or _line_effective_height(*current),
        ) / max(
            0.1,
            min(
                previous_line.em_height or _line_effective_height(*previous),
                current_line.em_height or _line_effective_height(*current),
            ),
        )
        vertical_gap = current_bbox[1] - (previous_bbox[1] + _line_effective_height(*previous))
        should_split = (
            previous_width <= 0.45 * maximum_width
            and current_width >= 0.75 * maximum_width
            and current_bbox[0] - group_left <= 3.0 * pair_height
            and -0.25 * pair_height <= vertical_gap <= 0.75 * pair_height
            and (font_switch or scale_ratio >= 1.25)
        )
        if should_split:
            groups.append([current])
        else:
            groups[-1].append(current)
    return groups


def _build_table_annotation_blocks(
    source: _PageSource,
    candidate: _TableCandidate,
    annotation: _TableAnnotation,
) -> list[dict[str, Any]]:
    """按视觉重启切分原生行，并生成保留排版元数据的独立注释块。"""

    line_geometry = [
        (
            line,
            _rotate_bbox_to_upright(
                line.bbox,
                source.page_size,
                candidate.angle,
            ),
        )
        for line in source.lines
        if line.source_index in annotation.line_indices
    ]
    # 原生来源顺序可抵抗旋转文字同一基线上的字形顶边抖动。
    line_geometry.sort(key=lambda item: item[0].source_index)
    blocks = []
    for group in _split_table_annotation_visual_groups(line_geometry):
        content = _merge_table_annotation_content(
            [line.text for line, _local_bbox in group],
        )
        if not content:
            continue
        local_output_line_bboxes = []
        output_bbox_repaired = False
        for line, local_bbox in group:
            tight_candidate = _line_tight_output_bbox(
                line,
                source.page_size,
            )
            if tight_candidate is None:
                local_output_line_bboxes.append(local_bbox)
            else:
                local_output_line_bboxes.append(
                    _rotate_bbox_to_upright(
                        tight_candidate,
                        source.page_size,
                        candidate.angle,
                    )
                )
                output_bbox_repaired = True
        blocks.append(
            {
                "type": annotation.kind,
                "bbox": _bbox_union_many(
                    [
                        annotation.line_bboxes.get(
                            line.source_index,
                            line.bbox,
                        )
                        for line, _local_bbox in group
                    ]
                ),
                "angle": candidate.angle,
                "content": content,
                "_local_line_bboxes": [bbox for _line, bbox in group],
                "_local_output_line_bboxes": local_output_line_bboxes,
                "_output_bbox_repaired": output_bbox_repaired,
                "_line_heights": [_line_effective_height(line, bbox) for line, bbox in group],
                "_font_signatures": {
                    line.font_signature
                    for line, _bbox in group
                    if line.font_signature is not None and line.font_coverage >= 0.5
                },
                # 已由表格检测器给出完整边界，禁止通用表注规则继续向下扩张。
                "_table_annotation_complete": True,
            }
        )
        retain_line_members(blocks[-1], (line for line, _ in group), enabled=source.collect_layout_diagnostics)
    return blocks


def _merge_table_annotation_content(line_texts: list[str]) -> str:
    """按正文一致的语言与行末断词规则折叠表格注释原生行。"""

    normalized_lines = [normalized for text in line_texts if (normalized := _normalize_native_run_text(str(text or "")))]
    if not normalized_lines:
        return ""
    return merge_text_line_contents(normalized_lines)


def _table_body_materialization_bbox(
    candidate: _TableCandidate,
    failed_annotations: list[_TableAnnotation],
) -> BBox:
    """返回排除有效注释后的表体框，并把无效注释边界保守并回表体。"""

    if not candidate.annotations or len(failed_annotations) == len(candidate.annotations):
        return candidate.bbox
    body_bbox = candidate.core_bbox or candidate.bbox
    for annotation in failed_annotations:
        body_bbox = _bbox_union(body_bbox, annotation.bbox)
    return body_bbox


def _candidate_projection_line_indices(
    source: _PageSource,
    candidate: _TableCandidate,
) -> set[int]:
    """合并核心成员、同基线续段及非零角度表格的表头文本。"""

    line_indices = set(candidate.line_indices)
    if candidate.core_bbox is not None:
        for line in source.lines:
            if _point_in_bbox(
                (_bbox_center_x(line.bbox), _bbox_center_y(line.bbox)),
                candidate.core_bbox,
            ):
                line_indices.add(line.source_index)
    if candidate.angle == 0:
        _expand_candidate_same_baseline_members(source, candidate, line_indices)
        return line_indices
    if candidate.core_bbox is None:
        return line_indices

    candidate_local_bbox = _rotate_bbox_to_upright(
        candidate.bbox,
        source.page_size,
        candidate.angle,
    )
    core_local_bbox = _rotate_bbox_to_upright(
        candidate.core_bbox,
        source.page_size,
        candidate.angle,
    )
    for line in source.lines:
        if line.angle != candidate.angle:
            continue
        page_center = (_bbox_center_x(line.bbox), _bbox_center_y(line.bbox))
        if not _point_in_bbox(page_center, candidate.bbox):
            continue
        local_bbox = _rotate_bbox_to_upright(
            line.bbox,
            source.page_size,
            candidate.angle,
        )
        local_center_y = _bbox_center_y(local_bbox)
        if not candidate_local_bbox[1] <= local_center_y <= candidate_local_bbox[3]:
            continue
        if _bbox_axis_overlap_ratio(local_bbox, core_local_bbox, axis="x") < 0.05:
            continue
        line_indices.add(line.source_index)
    return line_indices


def _expand_candidate_same_baseline_members(
    source: _PageSource,
    candidate: _TableCandidate,
    line_indices: set[int],
) -> None:
    """迭代吸收完整候选框内与已认领成员同基线相邻的 angle=0 续段。"""

    local_bboxes = {
        line.source_index: _rotate_bbox_to_upright(
            line.bbox,
            source.page_size,
            candidate.angle,
        )
        for line in source.lines
        if line.angle == candidate.angle
    }
    changed = True
    while changed:
        changed = False
        selected_lines = [line for line in source.lines if line.angle == candidate.angle and line.source_index in line_indices]
        for line in source.lines:
            if line.angle != candidate.angle or line.source_index in line_indices:
                continue
            if not _point_in_bbox(
                (_bbox_center_x(line.bbox), _bbox_center_y(line.bbox)),
                candidate.bbox,
            ):
                continue
            line_bbox = local_bboxes[line.source_index]
            line_height = _line_effective_height(line, line_bbox)
            for selected in selected_lines:
                if (
                    line.font_signature is not None
                    and selected.font_signature is not None
                    and line.font_signature != selected.font_signature
                ):
                    continue
                selected_bbox = local_bboxes[selected.source_index]
                if not _same_baseline_geometry(
                    line_bbox,
                    line_height,
                    selected_bbox,
                    _line_effective_height(selected, selected_bbox),
                ):
                    continue
                line_indices.add(line.source_index)
                changed = True
                break


def recover_table_result(
    table_input: NativeTableInput,
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    *,
    _owned_script_inputs: tuple[Any, dict[int, int]] | None = None,
    preserve_font_styles: bool = False,
    preserve_printed_spacing: bool = False,
    grid_tracks: tuple[tuple[float, ...], tuple[float, ...]] | None = None,
) -> tuple[str, NativeTableResult] | None:
    """统一表格恢复和上下标物化，保留调用方接受或回退的决策权。"""
    try:
        result = recover_native_pdf_table(table_input)
    except Exception as error:
        raise PDFTableRecoveryError(str(error)) from error
    if result is None:
        return None
    if grid_tracks is not None:
        from ._table_recovery.candidate import GridCellSpec, build_fully_assigned_table, serialize_candidate_html

        x, y = grid_tracks
        left, top = table_input.table_bbox[:2]
        specs = [
            GridCellSpec(r, c, 1, 1, (x[c] - left, y[r] - top, x[c + 1] - left, y[r + 1] - top))
            for r in range(len(y) - 1)
            for c in range(len(x) - 1)
        ]
        grid = build_fully_assigned_table(specs, result.text, "independent_closed_grid")
        if grid is None:
            return None
        result = replace(
            result,
            rows=grid.rows,
            cols=grid.cols,
            cells=grid.cells,
            html=serialize_candidate_html(grid),
            source="vector_grid",
            confidence=grid.score,
            diagnostics=grid.issues,
        )
    content = render_native_table_html_with_scripts(
        result,
        table_input,
        tight_bboxes,
        origins,
        _owned_script_inputs=_owned_script_inputs,
        preserve_font_styles=preserve_font_styles,
        **({"preserve_printed_spacing": True} if preserve_printed_spacing else {}),
    )
    return content, result
