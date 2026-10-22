"""将 PDF 原生字符重建为带排版信息的视觉文本行。"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ....document.pdf._document import PDFDocument, PDFDrawingLine
from ....document.pdf.text._contracts import Char
from ....schema import BBox
from .geometry import (
    _bbox_center_y,
    _bbox_union,
    _bbox_union_many,
    _clip_validated_bbox,
    _coerce_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
)
from .models import _AxisLine, _LineItem
from .typography import _normalized_font_family
from ....document.pdf.text._contracts import Bbox as _TypographyBbox

_PDF_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PDF_LINE_END_SOFT_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])[\x02\u00ad](?=[\t ]*(?:\n|$))")
_INLINE_REFERENCE_MARKER_RE = re.compile(
    r"^[\[（(［]\s*\d{1,4}\s*[\]）)］]$",
)
# Unicode Zs 空格在 model_list 中只承担分词作用，统一成可互操作的 ASCII 空格。
_PDF_SEPARATOR_SPACE_CHARS = "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_PDF_UNICODE_TEXT_TRANSLATION = str.maketrans(
    {
        **dict.fromkeys(_PDF_SEPARATOR_SPACE_CHARS, " "),
        "\u0085": "\n",
        "\u2028": "\n",
        "\u2029": "\n",
        "\u200b": None,
        "\u2060": None,
        "\ufeff": None,
    }
)
_PDFTEXT_ROTATION_SPLIT_THRESHOLD_DEGREES = 44.9
_PDFTEXT_LINE_ANGLE_TOLERANCE_DEGREES = 0.1
_SUPPORTED_PDFTEXT_LINE_ANGLES = (0.0, 90.0, 270.0)
_PDFTEXT_SHEARED_HORIZONTAL_MAX_ANGLE_DEGREES = 30.0
_PDFTEXT_HORIZONTAL_BASELINE_MAX_ANGLE_DEGREES = 2.0
_PDFTEXT_HORIZONTAL_BASELINE_MAX_DISPERSION_RATIO = 0.75
_PDFTEXT_FORMULA_OPERATOR_CHARS = frozenset("=∑∫√±×÷")


@dataclass(frozen=True, slots=True)
class _NativeVisualResplit:
    """保存一个粗行及其按 canonical 字符框重切后的成员。"""

    source: _LineItem
    members: tuple[_LineItem, ...]


def _native_diagonal_glyph(char: dict[str, Any], page_rotation: int, *, _allow_space: bool = False) -> tuple | None:
    """只取可见原字的有限原点、墨迹和非标准方向，字面及字体名称不参与空间判定。"""
    text = str(char.get("char") or "")
    if (
        not text
        or (not _allow_space and not text.strip())
        or char.get("is_generated")
        or char.get("text_render_mode") in (3, 7)
    ):
        return None
    rotation = char.get("rotation")
    if type(rotation) not in (int, float) or not math.isfinite(rotation):
        return None
    angle = (_pdftext_angle_degrees(rotation) + page_rotation) % 180.0
    if min(angle, abs(angle - 90.0), 180.0 - angle) <= _PDFTEXT_HORIZONTAL_BASELINE_MAX_ANGLE_DEGREES:
        return None
    origin = char.get("origin")
    box = _coerce_bbox(char.get("tight_bbox"))
    if (
        not isinstance(origin, (tuple, list))
        or len(origin) != 2
        or not all(type(value) in (int, float) and math.isfinite(value) for value in origin)
        or box is None
        or not all(math.isfinite(value) for value in box)
        or box[2] <= box[0]
        or box[3] <= box[1]
    ):
        return None
    return char, origin, box, angle, min(box[2] - box[0], box[3] - box[1])


def _native_diagonal_line_is_proven(glyphs: list[tuple]) -> bool:
    """用整行原点的主轴、跨度、残差及墨迹包络确认真实斜排，不把局部字形倾斜当行方向。"""
    if len(glyphs) < 4 or len({tuple(item[1]) for item in glyphs}) < 4:
        return False
    scale = statistics.median(item[4] for item in glyphs)
    cx = math.fsum(item[1][0] for item in glyphs) / len(glyphs)
    cy = math.fsum(item[1][1] for item in glyphs) / len(glyphs)
    points = [(item[1][0] - cx, item[1][1] - cy) for item in glyphs]
    xx = math.fsum(x * x for x, _y in points)
    yy = math.fsum(y * y for _x, y in points)
    xy = math.fsum(x * y for x, y in points)
    if not all(math.isfinite(value) for value in (xx, yy, xy)):
        return False
    theta = 0.5 * math.atan2(2 * xy, xx - yy)
    axis = math.degrees(theta) % 180.0
    if min(axis, abs(axis - 90.0), 180.0 - axis) <= _PDFTEXT_HORIZONTAL_BASELINE_MAX_ANGLE_DEGREES:
        return False
    if any(min(abs(item[3] - axis), 180.0 - abs(item[3] - axis)) > 2.0 for item in glyphs):
        return False
    cosine, sine = math.cos(theta), math.sin(theta)
    along = [x * cosine + y * sine for x, y in points]
    across = [-x * sine + y * cosine for x, y in points]
    span = max(along) - min(along)
    if span < 3.0 * scale or max(abs(value) for value in across) > 0.25 * scale:
        return False
    box = _bbox_union_many([item[2] for item in glyphs])
    # 整行外框应随斜向主轴同时向两轴展开；异常巨框或错位墨迹不能只凭原点直线被删除。
    if box[2] - box[0] < span * abs(cosine) - 0.5 * scale or box[3] - box[1] < span * abs(sine) - 0.5 * scale:
        return False
    ink_across = [
        -(x - cx) * sine + (y - cy) * cosine
        for item in glyphs
        for x in (item[2][0], item[2][2])
        for y in (item[2][1], item[2][3])
    ]
    typical_band = statistics.median(
        abs(sine) * (item[2][2] - item[2][0]) + abs(cosine) * (item[2][3] - item[2][1]) for item in glyphs
    )
    return max(ink_across) - min(ink_across) <= typical_band + 0.5 * scale


def _native_diagonal_space_is_owned(space: tuple, glyphs: list[tuple]) -> bool:
    """空格不提供斜行证明；只有原方向、原点和行内位置吻合时才随已证物理行移除。"""
    angle = glyphs[0][3]
    difference = abs(space[3] - angle)
    if min(difference, 180.0 - difference) > 2.0:
        return False
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    cx, cy = glyphs[0][1]
    scale = statistics.median(item[4] for item in glyphs)
    along = [(item[1][0] - cx) * cosine + (item[1][1] - cy) * sine for item in glyphs]
    dx, dy = space[1][0] - cx, space[1][1] - cy
    return (
        min(along) - 6.0 * scale <= dx * cosine + dy * sine <= max(along) + 6.0 * scale
        and abs(-dx * sine + dy * cosine) <= 0.25 * scale
    )


def _filter_native_direction_chars(chars: list[Char], *, page_rotation: int = 0) -> list[Char]:
    """在视觉分段前按空间连续性恢复候选物理行，只移除完整斜行证据认领的原字。"""
    rejected: set[int] = set()
    group: list[tuple] = []
    spaces: list[tuple] = []
    cardinal_page = page_rotation % 90 == 0

    def finish() -> None:
        """局部行完整通过几何门后才登记成员，短片段和证据不足的输入原样保留。"""
        if _native_diagonal_line_is_proven(group):
            rejected.update(id(item[0]) for item in group)
            rejected.update(id(item[0]) for item in spaces if _native_diagonal_space_is_owned(item, group))
        group.clear()
        spaces.clear()

    for char in chars:
        # 最常见的标准原字且没有待判斜行时直接通过，避免常规页逐字重复角度和墨迹检查。
        if cardinal_page and not group and not spaces and char.get("rotation") == 0.0:
            continue
        text = str(char.get("char") or "")
        if text in {"\n", "\r", "\x02"}:
            finish()
            continue
        if not text.strip():
            space = _native_diagonal_glyph(char, page_rotation, _allow_space=True)
            if space is not None:
                spaces.append(space)
            continue
        if char.get("is_generated"):
            continue
        item = _native_diagonal_glyph(char, page_rotation)
        if item is None:
            finish()
            continue
        if group:
            previous = group[-1]
            difference = abs(item[3] - group[0][3])
            angle = math.radians(group[0][3])
            dx, dy = item[1][0] - previous[1][0], item[1][1] - previous[1][1]
            along = dx * math.cos(angle) + dy * math.sin(angle)
            across = -dx * math.sin(angle) + dy * math.cos(angle)
            scale = max(item[4], previous[4])
            if min(difference, 180.0 - difference) > 2.0 or abs(along) > 6.0 * scale or abs(across) > 0.5 * scale:
                finish()
        group.append(item)
    finish()
    return [char for char in chars if id(char) not in rejected] if rejected else chars


def _filter_native_direction_pdf_lines(pdf_lines: Sequence[dict[str, Any]], page_rotation: int) -> list[dict[str, Any]]:
    """粗行中正文与斜水印分别按实际字形分布裁决，重建受影响片段的内容与包络。"""
    output = []
    for line in pdf_lines:
        original = [char for span in line.get("spans") or [] for char in span.get("chars") or []]
        retained = _filter_native_direction_chars(original, page_rotation=page_rotation)
        if retained is original:
            output.append(line)
            continue
        identities = {id(char) for char in retained}
        spans = []
        for span in line.get("spans") or []:
            chars = [char for char in span.get("chars") or [] if id(char) in identities]
            if not chars:
                continue
            boxes = [box for char in chars if (box := _coerce_bbox(char.get("bbox"))) is not None]
            spans.append(
                {
                    **span,
                    "chars": chars,
                    "text": "".join(str(char.get("char") or "") for char in chars),
                    "bbox": _bbox_union_many(boxes) if boxes else span["bbox"],
                }
            )
        if spans:
            output.append(_build_pdftext_child_line(line, spans, _pdftext_angle_degrees(spans[0].get("rotation"))))
    return output


def _build_native_line_items_from_chars(
    chars: list[Char],
    page_size: tuple[float, float],
    *,
    page_rotation: int = 0,
    supported_angles: Sequence[float] = _SUPPORTED_PDFTEXT_LINE_ANGLES,
) -> list[_LineItem]:
    """连续消费字符到视觉 run，省去基础片段和粗行的 Python 中间物化。"""
    from ...._compute_backend import get_native

    chars = _filter_native_direction_chars(chars, page_rotation=page_rotation)
    native = get_native()
    records = native.prepare_visual_lines(chars, page_size, page_rotation, supported_angles)
    if records is None:
        raise ValueError("native visual line preparation rejected normalized characters")
    return _build_native_line_items_from_records(records, page_size)


def _build_native_line_items_from_records(records, page_size):
    """把已完成原生计算的视觉记录物化为原有行对象，供字符列表和自有快照共用。"""
    normal_items: list[_LineItem] = []
    formula_items: list[_LineItem] = []
    for text, bbox, angle, members, row_id, run_index, split, formula, coarse, metrics, terminal in records:
        item = _LineItem(
            text=text,
            bbox=bbox,
            angle=angle,
            source_index=-1,
            chars=members,
            visual_row_id=row_id,
            run_index=run_index,
            split_from_row=split,
            formula_candidate_only=formula,
        )
        if coarse:
            local = _rotate_bbox_to_upright(bbox, page_size, angle)
            item.effective_height = max(0.1, local[3] - local[1])
        elif metrics is not None:
            height, width, signature, coverage, weight, emphasis, typography, scale = metrics
            item.effective_height = item.em_height = height
            item.native_typographic_scale = scale
            item.median_glyph_width = width
            item.font_signature = signature
            item.font_coverage = coverage
            item.dominant_font_weight = weight
            item.leading_emphasis_width = emphasis
            item.leading_typography_width = typography
            item.paragraph_terminal = terminal
        else:
            _fill_native_typography(item, page_size)
        (formula_items if formula else normal_items).append(item)
    stable_items = _merge_native_inline_scripts(normal_items, page_size)
    for source_index, item in enumerate(stable_items):
        item.source_index = source_index
    formula_items = _merge_native_inline_scripts(formula_items, page_size)
    for source_index, item in enumerate(formula_items, start=len(stable_items)):
        item.source_index = source_index
    output = [*stable_items, *formula_items]
    output.sort(key=lambda item: (item.visual_row_id if item.visual_row_id is not None else math.inf, item.run_index))
    return output


def _build_native_line_items(
    pdf_lines: Sequence[dict[str, Any]],
    page_size: tuple[float, float],
    *,
    page_rotation: int = 0,
    supported_angles: Sequence[float] = _SUPPORTED_PDFTEXT_LINE_ANGLES,
) -> list[_LineItem]:
    """按指定视觉方向将 pdftext 粗行精修成字符间隙分隔的视觉 run。"""

    normal_items: list[_LineItem] = []
    formula_items: list[_LineItem] = []
    supported_lines: list[tuple[dict[str, Any], int, bool]] = []
    for pdf_line in _filter_native_direction_pdf_lines(pdf_lines, page_rotation):
        for child_line in _split_pdftext_line_by_rotation(pdf_line):
            visual_angle = _resolve_pdftext_line_angle(
                child_line,
                page_rotation=page_rotation,
                supported_angles=supported_angles,
            )
            formula_candidate_only = False
            if visual_angle is None:
                visual_angle = _resolve_pdftext_formula_candidate_angle(
                    child_line,
                    page_rotation=page_rotation,
                    supported_angles=supported_angles,
                )
                formula_candidate_only = visual_angle is not None
            if visual_angle is not None:
                supported_lines.append((child_line, visual_angle, formula_candidate_only))
    for visual_row_id, (pdf_line, visual_angle, formula_candidate_only) in enumerate(supported_lines):
        bbox = _clip_validated_bbox(_coerce_bbox(pdf_line.get("bbox")), page_size)
        if bbox is None:
            continue
        spans = pdf_line.get("spans") or []
        chars = [char for span in spans for char in (span.get("chars") or []) if isinstance(char, dict)]
        coarse_item = _LineItem(
            text="".join(str(span.get("text") or "") for span in spans),
            bbox=bbox,
            angle=visual_angle,
            source_index=-1,
            chars=chars,
            visual_row_id=visual_row_id,
            formula_candidate_only=formula_candidate_only,
        )
        target = formula_items if formula_candidate_only else normal_items
        target.extend(_split_native_visual_runs(coarse_item, page_size))

    stable_items = _merge_native_inline_scripts(normal_items, page_size)
    for source_index, item in enumerate(stable_items):
        # source_index 必须在页内唯一，表格投影和失败回滚都依赖该精确成员标识。
        item.source_index = source_index
    formula_items = _merge_native_inline_scripts(formula_items, page_size)
    next_source_index = len(stable_items)
    for item in formula_items:
        item.source_index = next_source_index
        next_source_index += 1
    output = [*stable_items, *formula_items]
    output.sort(key=lambda item: (item.visual_row_id if item.visual_row_id is not None else math.inf, item.run_index))
    return output


def _extract_decorative_text_rules(
    lines: list[_LineItem],
    page_size: tuple[float, float],
) -> tuple[list[_LineItem], list[_AxisLine]]:
    """把宽幅私用区重复字形转为装饰分隔线，避免其进入文本块。"""

    retained: list[_LineItem] = []
    decorative_rules: list[_AxisLine] = []
    for line in lines:
        local_bbox = _rotate_bbox_to_upright(
            line.bbox,
            page_size,
            line.angle,
        )
        local_page_width = page_size[1] if line.angle in {90, 270} else page_size[0]
        local_page_height = page_size[0] if line.angle in {90, 270} else page_size[1]
        compact_text = "".join(
            char for char in line.text if not char.isspace() and (char.isprintable() or unicodedata.category(char) == "Co")
        )
        private_use_count = sum(unicodedata.category(char) == "Co" for char in compact_text)
        repeated_count = max(
            Counter(compact_text).values(),
            default=0,
        )
        local_width = max(0.0, local_bbox[2] - local_bbox[0])
        local_height = max(0.1, local_bbox[3] - local_bbox[1])
        is_decorative_rule = (
            len(compact_text) >= 8
            and private_use_count / len(compact_text) >= 0.8
            and repeated_count / len(compact_text) >= 0.8
            and local_width >= 0.4 * local_page_width
            and local_width / local_height >= 20.0
            and _bbox_center_y(local_bbox) <= 0.2 * local_page_height
        )
        if not is_decorative_rule:
            retained.append(line)
            continue
        decorative_rules.append(
            _AxisLine(
                bbox=line.bbox,
                width=min(1.0, local_height),
                orientation=("vertical" if line.angle in {90, 270} else "horizontal"),
            )
        )
    return retained, decorative_rules


def _pdftext_angle_degrees(value: Any) -> float:
    """把 pdftext 弧度方向转换为 0 到 360 度，非法值按 0 度处理。"""

    try:
        angle_radians = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(angle_radians):
        return 0.0
    return math.degrees(angle_radians) % 360.0


def _circular_angle_distance(first: float, second: float) -> float:
    """返回两个方向之间不超过 180 度的最短圆周角差。"""

    return abs((first - second + 180.0) % 360.0 - 180.0)


def _span_has_visible_text(span: dict[str, Any]) -> bool:
    """判断 span 是否包含可见的非空白字符，换行与占位空格不参与方向拆分。"""

    return any(char.isprintable() and not char.isspace() for char in str(span.get("text") or ""))


def _build_pdftext_child_line(
    pdf_line: dict[str, Any],
    spans: list[dict[str, Any]],
    angle_degrees: float,
) -> dict[str, Any]:
    """使用同方向 span 重建子行，并收缩原粗行被异向内容扩大的 bbox。"""

    span_bboxes = [bbox for span in spans if (bbox := _coerce_bbox(span.get("bbox"))) is not None]
    child_line = dict(pdf_line)
    child_line["spans"] = spans
    child_line["bbox"] = _bbox_union_many(span_bboxes) if span_bboxes else pdf_line.get("bbox")
    child_line["rotation"] = math.radians(angle_degrees)
    return child_line


def _split_pdftext_line_by_rotation(pdf_line: dict[str, Any]) -> list[dict[str, Any]]:
    """按 45 度边界拆开 pdftext 误合并的异向 span，并保留小角度仿斜体。"""

    spans = [span for span in (pdf_line.get("spans") or []) if isinstance(span, dict)]
    if not spans:
        return [pdf_line]

    output: list[dict[str, Any]] = []
    current_spans: list[dict[str, Any]] = []
    current_angle = _pdftext_angle_degrees(pdf_line.get("rotation"))
    for span in spans:
        span_angle = _pdftext_angle_degrees(span.get("rotation"))
        if (
            current_spans
            and _span_has_visible_text(span)
            and _circular_angle_distance(span_angle, current_angle) >= _PDFTEXT_ROTATION_SPLIT_THRESHOLD_DEGREES
        ):
            output.append(_build_pdftext_child_line(pdf_line, current_spans, current_angle))
            current_spans = []
            current_angle = span_angle
        current_spans.append(span)

    if current_spans:
        output.append(_build_pdftext_child_line(pdf_line, current_spans, current_angle))
    return output


def _is_supported_pdftext_line_rotation(
    value: Any,
    *,
    page_rotation: int,
    supported_angles: Sequence[float] = _SUPPORTED_PDFTEXT_LINE_ANGLES,
) -> bool:
    """应用页面旋转后按调用方白名单筛选视觉文字方向。"""

    visual_angle = (_pdftext_angle_degrees(value) + int(page_rotation or 0)) % 360.0
    return any(
        _circular_angle_distance(visual_angle, supported_angle) <= _PDFTEXT_LINE_ANGLE_TOLERANCE_DEGREES
        for supported_angle in supported_angles
    )


def _resolve_pdftext_line_angle(
    pdf_line: dict[str, Any],
    *,
    page_rotation: int,
    supported_angles: Sequence[float] = _SUPPORTED_PDFTEXT_LINE_ANGLES,
) -> int | None:
    """解析视觉文字方向，并用字符基线纠正字体 shear 造成的伪斜向行。"""

    visual_angle = (_pdftext_angle_degrees(pdf_line.get("rotation")) + int(page_rotation or 0)) % 360.0
    visual_angle = _correct_negative_scale_angle(pdf_line, visual_angle)
    for supported_angle in supported_angles:
        if _circular_angle_distance(visual_angle, supported_angle) <= _PDFTEXT_LINE_ANGLE_TOLERANCE_DEGREES:
            return int(supported_angle)
    supports_horizontal = any(
        _circular_angle_distance(0.0, supported_angle) <= _PDFTEXT_LINE_ANGLE_TOLERANCE_DEGREES
        for supported_angle in supported_angles
    )
    if (
        supports_horizontal
        and _circular_angle_distance(visual_angle, 0.0) <= _PDFTEXT_SHEARED_HORIZONTAL_MAX_ANGLE_DEGREES
        and _pdftext_line_has_horizontal_char_baseline(pdf_line)
    ):
        return 0
    return None


def _correct_negative_scale_angle(pdf_line: dict[str, Any], visual_angle: float) -> float:
    """负字号和同源字母数字基线共同证实半周翻转才纠正，单字及描粗副层不作新证据。"""
    candidate = (visual_angle + 180.0) % 360.0
    cardinal = min((0, 90, 180, 270), key=lambda angle: _circular_angle_distance(candidate, angle))
    if _circular_angle_distance(candidate, cardinal) > _PDFTEXT_LINE_ANGLE_TOLERANCE_DEGREES:
        return visual_angle
    axis_x, axis_y = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[cardinal]
    positions: list[tuple[float, float]] = []
    heights: list[float] = []
    seen: dict[tuple[str, str, int, float], list[BBox]] = {}
    for span in pdf_line.get("spans") or []:
        for char in span.get("chars") or []:
            text = str(char.get("char") or "")
            bbox = _coerce_bbox(char.get("bbox"))
            if not text.isalnum() or bbox is None:
                continue
            font = char.get("font") or {}
            size = font.get("size")
            if not isinstance(size, (int, float)) or not math.isfinite(size) or size >= 0:
                return visual_angle
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            name = str(font.get("name") or "")
            try:
                flags = int(font.get("flags") or 0) if name else 0
            except (TypeError, ValueError):
                flags = 0
            identity = (text, name, flags, float(size))
            repeated = False
            for previous in seen.get(identity, []):
                intersection = max(0, min(bbox[2], previous[2]) - max(bbox[0], previous[0])) * max(
                    0, min(bbox[3], previous[3]) - max(bbox[1], previous[1])
                )
                union = area + (previous[2] - previous[0]) * (previous[3] - previous[1]) - intersection
                if intersection >= 0.5 * union:
                    repeated = True
                    break
            if repeated:
                continue
            # 描粗副层只从方向证据去重，不删除或重排原始字符成员。
            seen.setdefault(identity, []).append(bbox)
            x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            positions.append((x * axis_x + y * axis_y, -x * axis_y + y * axis_x))
            heights.append(bbox[3] - bbox[1] if axis_x else bbox[2] - bbox[0])
    if len(positions) < 2:
        return visual_angle
    median_height = statistics.median(heights)
    forward = positions[-1][0] - positions[0][0]
    sideways = positions[-1][1] - positions[0][1]
    if (
        forward < 0.5 * median_height
        or abs(math.degrees(math.atan2(sideways, forward))) > _PDFTEXT_HORIZONTAL_BASELINE_MAX_ANGLE_DEGREES
        or max(position[1] for position in positions) - min(position[1] for position in positions)
        > _PDFTEXT_HORIZONTAL_BASELINE_MAX_DISPERSION_RATIO * median_height
        or any(after[0] < before[0] - 0.1 * median_height for before, after in zip(positions, positions[1:]))
    ):
        return visual_angle
    return float(cardinal)


def _resolve_pdftext_formula_candidate_angle(
    pdf_line: dict[str, Any],
    *,
    page_rotation: int,
    supported_angles: Sequence[float],
) -> int | None:
    """保留小角度字体矩阵下的公式专用粗行，未被公式认领时不回流正文。"""
    visual_angle = (_pdftext_angle_degrees(pdf_line.get("rotation")) + int(page_rotation or 0)) % 360.0
    visual_angle = _correct_negative_scale_angle(pdf_line, visual_angle)
    nearest = min(supported_angles, key=lambda angle: _circular_angle_distance(visual_angle, angle))
    if _circular_angle_distance(visual_angle, nearest) > _PDFTEXT_SHEARED_HORIZONTAL_MAX_ANGLE_DEGREES:
        return None
    spans = [span for span in (pdf_line.get("spans") or []) if isinstance(span, dict)]
    compact_text = "".join(str(span.get("text") or "") for span in spans)
    compact_text = "".join(char for char in compact_text if char.isprintable() and not char.isspace())
    if not compact_text:
        return None
    has_script_flag = any(span.get("superscript") is True or span.get("subscript") is True for span in spans)
    has_math_operator = any(char in _PDFTEXT_FORMULA_OPERATOR_CHARS for char in compact_text)
    font_sizes = [
        float(size)
        for span in spans
        if isinstance(span.get("font"), dict) and isinstance((size := span["font"].get("size")), (int, float)) and size > 0
    ]
    has_mixed_sizes = bool(font_sizes) and max(font_sizes) >= 1.35 * min(font_sizes)
    is_compact_identifier = len(compact_text) <= 3 and all(char.isalnum() for char in compact_text)
    return int(nearest) if has_script_flag or has_math_operator or has_mixed_sizes or is_compact_identifier else None


def _pdftext_line_has_horizontal_char_baseline(
    pdf_line: dict[str, Any],
) -> bool:
    """用字符中心的水平基线确认小角度只来自仿斜体变换，而非真实旋转。"""

    visible_bboxes: list[BBox] = []
    for span in pdf_line.get("spans") or []:
        if not isinstance(span, dict):
            continue
        for char in span.get("chars") or []:
            if not isinstance(char, dict):
                continue
            raw_char = str(char.get("char") or "")
            bbox = _coerce_bbox(char.get("bbox"))
            if bbox is not None and raw_char.isprintable() and not raw_char.isspace():
                visible_bboxes.append(bbox)
    if len(visible_bboxes) < 4:
        return False

    line_bbox = _bbox_union_many(visible_bboxes)
    line_width = line_bbox[2] - line_bbox[0]
    line_height = line_bbox[3] - line_bbox[1]
    glyph_heights = [bbox[3] - bbox[1] for bbox in visible_bboxes]
    median_height = statistics.median(glyph_heights)
    if line_height <= 0 or median_height <= 0 or line_width / line_height < 3.0:
        return False

    ordered = sorted(
        visible_bboxes,
        key=lambda bbox: ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
    )
    first_center = (
        (ordered[0][0] + ordered[0][2]) / 2.0,
        (ordered[0][1] + ordered[0][3]) / 2.0,
    )
    last_center = (
        (ordered[-1][0] + ordered[-1][2]) / 2.0,
        (ordered[-1][1] + ordered[-1][3]) / 2.0,
    )
    baseline_width = last_center[0] - first_center[0]
    if baseline_width <= 0:
        return False
    baseline_angle = abs(math.degrees(math.atan2(last_center[1] - first_center[1], baseline_width)))
    centers_y = [(bbox[1] + bbox[3]) / 2.0 for bbox in ordered]
    return (
        baseline_angle <= _PDFTEXT_HORIZONTAL_BASELINE_MAX_ANGLE_DEGREES
        and max(centers_y) - min(centers_y) <= _PDFTEXT_HORIZONTAL_BASELINE_MAX_DISPERSION_RATIO * median_height
    )


def _split_native_visual_runs(
    line: _LineItem,
    page_size: tuple[float, float],
    *,
    visual_bboxes: Mapping[int, BBox] | None = None,
    preserve_vertical_bbox: BBox | None = None,
) -> list[_LineItem]:
    """批量处理视觉 run 几何，Python 保留文字清洗及原对象物化。"""
    from ...._compute_backend import get_native
    from ._native_geometry import glyph_flags, raw_bbox

    native = get_native()
    tokens = [(char, str(char.get("char") or "")) for char in line.chars]
    tokens = [(char, text) for char, text in tokens if text not in {"\r", "\n"}]
    flags = [glyph_flags(text) for _char, text in tokens]
    boxes = [raw_bbox(char.get("bbox")) for char, _text in tokens]
    overrides = [
        raw_bbox(visual_bboxes.get(char.get("char_idx")))
        if visual_bboxes is not None and isinstance(char.get("char_idx"), int) and flag & 1
        else None
        for (char, _text), flag in zip(tokens, flags)
    ]
    ranges = native.visual_runs(boxes, overrides, flags, page_size, line.angle, _coerce_bbox)
    if not ranges:
        # 无可见字形是合法输入：保留原行对象和清洗后的文字。
        text = _normalize_native_run_text(line.text)
        if not text:
            return []
        local_bbox = _rotate_bbox_to_upright(line.bbox, page_size, line.angle)
        line.text = text
        line.effective_height = max(0.1, local_bbox[3] - local_bbox[1])
        return [line]
    output = []
    for run_index, (start, end, run_bbox) in enumerate(ranges):
        run_tokens = tokens[start:end]
        text = _normalize_native_run_text("".join(text for _char, text in run_tokens))
        if not text or run_bbox is None:
            continue
        if preserve_vertical_bbox is not None:
            local = _rotate_bbox_to_upright(run_bbox, page_size, line.angle)
            vertical = _rotate_bbox_to_upright(preserve_vertical_bbox, page_size, line.angle)
            run_bbox = _rotate_bbox_from_upright((local[0], vertical[1], local[2], vertical[3]), page_size, line.angle)
        item = _LineItem(
            text=text,
            bbox=run_bbox,
            angle=line.angle,
            source_index=-1,
            chars=[char for char, _text in run_tokens],
            visual_row_id=line.visual_row_id,
            run_index=run_index,
            split_from_row=len(ranges) > 1,
            formula_candidate_only=line.formula_candidate_only,
        )
        _fill_native_typography(item, page_size)
        output.append(item)
    return output


def _resplit_native_visual_runs(
    lines: list[_LineItem],
    page_size: tuple[float, float],
    visual_bboxes: Mapping[int, BBox],
    *,
    source_index_start: int | None = None,
) -> tuple[list[_LineItem], dict[int, _NativeVisualResplit]]:
    """重切跨栏粗行，并返回供行内 evidence 分片使用的来源映射。"""

    if not lines or not visual_bboxes:
        return list(lines), {}
    next_source_index = (
        source_index_start
        if source_index_start is not None
        else max(
            (line.source_index for line in lines),
            default=-1,
        )
        + 1
    )
    output: list[_LineItem] = []
    resplits: dict[int, _NativeVisualResplit] = {}
    for line in lines:
        local_bbox = _rotate_bbox_to_upright(
            line.bbox,
            page_size,
            line.angle,
        )
        local_page_height = page_size[0] if line.angle in {90, 270} else page_size[1]
        local_page_width = page_size[1] if line.angle in {90, 270} else page_size[0]
        if (
            line.semantic_type is not None
            or local_bbox[2] - local_bbox[0] < 0.45 * local_page_width
            or _bbox_center_y(local_bbox) < 0.07 * local_page_height
            or _bbox_center_y(local_bbox) > 0.93 * local_page_height
        ):
            output.append(line)
            continue
        members = _split_native_visual_runs(
            line,
            page_size,
            visual_bboxes=visual_bboxes,
            preserve_vertical_bbox=line.bbox,
        )
        if len(members) <= 1:
            output.append(line)
            continue
        local_members = sorted(
            (
                _rotate_bbox_to_upright(
                    member.bbox,
                    page_size,
                    member.angle,
                )
                for member in members
            ),
            key=lambda bbox: bbox[0],
        )
        column_split = (
            len(local_members) == 2
            and all(bbox[2] - bbox[0] >= 0.15 * local_page_width for bbox in local_members)
            and local_members[1][0] - local_members[0][2] >= 0.02 * local_page_width
            and local_members[0][2] <= 0.52 * local_page_width
            and local_members[1][0] >= 0.48 * local_page_width
        )
        if not column_split:
            output.append(line)
            continue
        for member_index, member in enumerate(members):
            member.source_index = line.source_index if member_index == 0 else next_source_index
            if member_index > 0:
                next_source_index += 1
            member.visual_row_id = line.visual_row_id
            member.run_index = line.run_index + member_index
            member.source_bbox = member.bbox
            member.baseline = line.baseline
            member.geometry_state = line.geometry_state
            member.geometry_confidence = line.geometry_confidence
            member.split_y_candidate = line.split_y_candidate
            member.em_height = line.em_height or member.effective_height
            member.split_from_row = True
            member.preserve_split_boundary = line.preserve_split_boundary
            member.semantic_type = line.semantic_type
            member.formula_candidate_only = line.formula_candidate_only
            member.style_scale_repaired = line.style_scale_repaired
            output.append(member)
        resplits[line.source_index] = _NativeVisualResplit(
            source=line,
            members=tuple(members),
        )
    output.sort(
        key=lambda item: (
            item.visual_row_id if item.visual_row_id is not None else math.inf,
            item.run_index,
            item.source_index,
        )
    )
    return output, resplits


def _normalize_native_run_text(text: str) -> str:
    """清理原生 run 文本，并把字母后的 PDF 软断词标记转换成 ASCII hyphen。"""

    normalized = _sanitize_pdf_control_text(text, preserve_newlines=False)
    normalized = re.sub(r"[\t\f\v ]+", " ", normalized)
    return normalized.strip()


def _sanitize_pdf_control_text(text: str, *, preserve_newlines: bool) -> str:
    """规范 PDF 排版空白与控制字符，并按调用场景决定是否保留物理换行。"""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.translate(_PDF_UNICODE_TEXT_TRANSLATION)
    normalized = _PDF_LINE_END_SOFT_HYPHEN_RE.sub("-", normalized)
    normalized = normalized.replace("\u00ad", "")
    normalized = normalized.replace("\t", " ")
    if not preserve_newlines:
        normalized = normalized.replace("\n", "")
    return _PDF_CONTROL_CHAR_RE.sub("", normalized)


def _owned_typography_metrics(native, line, page_size, glyph_flags, raw_bbox):
    """标准行直接批量读取当前字形；自定义坐标、字体转换和替换规则完整沿用原路径。"""
    if (
        type(page_size) not in (tuple, list)
        or len(page_size) != 2
        or any(type(v) not in (float, int) or not math.isfinite(v) or abs(v) > 2**50 for v in page_size)
    ):
        return None
    return native.typography_owned(line, page_size, _LineItem, _TypographyBbox, glyph_flags, _normalized_font_family)


def _fill_native_typography(line: _LineItem, page_size: tuple[float, float]) -> None:
    """按行聚合数值统计；字体类别仍由 Python 编码，缓存只存在于本次调用。"""
    from ...._compute_backend import get_native
    from ._native_geometry import glyph_flags, raw_bbox

    native = get_native()
    owned = _owned_typography_metrics(native, line, page_size, glyph_flags, raw_bbox)
    if owned is not None:
        (height, width, _winner, coverage, weight, emphasis, typography), signature = owned
        line.effective_height = height
        line.em_height = line.em_height if line.em_height > 0 else height
        line.median_glyph_width = width
        line.font_signature = signature
        line.font_coverage = coverage
        line.dominant_font_weight = weight
        line.leading_emphasis_width = emphasis
        line.leading_typography_width = typography
        line.paragraph_terminal = _native_sentence_terminal(line)
        return
    boxes = native.local_boxes(
        [raw_bbox(char.get("bbox")) if glyph_flags(str(char.get("char") or "")) & 1 else None for char in line.chars],
        page_size,
        line.angle,
        _coerce_bbox,
    )
    signatures, signature_ids, families, family_ids = [], {}, [], {}
    font_ids, weights = [], []
    # 字体字典由提取层复用；保留强引用避免本次调用内对象地址重用。
    font_cache = {}
    for char, box in zip(line.chars, boxes, strict=True):
        if box is None:
            font_ids.append(None)
            weights.append(None)
            continue
        font = char.get("font") or {}
        cached = font_cache.get(id(font)) if type(font) is dict else None
        if cached is None:
            name = str(font.get("name") or "")
            identifier = weight = None
            if name:
                try:
                    flags = int(font.get("flags") or 0)
                except (TypeError, ValueError):
                    flags = 0
                signature = (name, flags)
                identifier = signature_ids.get(signature)
                if identifier is None:
                    identifier = len(signatures)
                    signature_ids[signature] = identifier
                    signatures.append(signature)
                    family = _normalized_font_family(signature)
                    families.append(None if family is None else family_ids.setdefault(family, len(family_ids)))
                try:
                    value = float(font.get("weight"))
                except (TypeError, ValueError):
                    value = math.nan
                if math.isfinite(value) and value > 0:
                    weight = value
            cached = (font, identifier, weight)
            if type(font) is dict and all(
                type(font.get(key)) in (str, int, float, bool, type(None)) for key in ("name", "flags", "weight")
            ):
                font_cache[id(font)] = cached
        font_ids.append(cached[1])
        weights.append(cached[2])
    local = _rotate_bbox_to_upright(line.bbox, page_size, line.angle)
    result = native.typography_metrics(boxes, font_ids, weights, families, max(0.1, local[3] - local[1]))
    if result is None:
        raise ValueError("native typography rejected normalized metrics")
    height, width, winner, coverage, weight, emphasis, typography = result
    line.effective_height = height
    line.em_height = line.em_height if line.em_height > 0 else height
    line.median_glyph_width = width
    line.font_signature = signatures[winner] if winner is not None else None
    line.font_coverage = coverage
    line.dominant_font_weight = weight
    line.leading_emphasis_width = emphasis
    line.leading_typography_width = typography
    line.paragraph_terminal = _native_sentence_terminal(line)


def _native_sentence_terminal(line: _LineItem) -> bool:
    """识别真正句尾及其后的小字号引用，普通小数或同字号数字不能充当句末引用。"""
    if re.search(r"[.!?。！？][\]\)}）】》”’'\"]*$", line.text.rstrip()):
        return True
    match = re.search(r"[.!?。！？](?P<references>\d[\d,–—-]*)$", line.text.rstrip())
    if match is None:
        return False
    chars = [char for char in line.chars if str(char.get("char", "")).strip()]
    count = len(match.group("references"))
    if len(chars) <= count:
        return False
    # 一些嵌入字体把字号固定为 1 再用矩阵缩放，实际字形高度才反映引用的缩小比例。
    axis = 0 if line.angle in {90, 270} else 1
    sizes = [float(char["bbox"][axis + 2] - char["bbox"][axis]) for char in chars]
    body = [size for size in sizes[:-count] if size > 0]
    return bool(body) and all(0 < size <= 0.8 * statistics.median(body) for size in sizes[-count:])


def _is_detached_inline_script_candidate(
    small_bbox: BBox,
    base_bbox: BBox,
    base_height: float,
) -> bool:
    """仅依据紧凑宽度、边缘邻接和垂直偏移确认低重叠外置上下标。"""

    small_width = max(0.0, small_bbox[2] - small_bbox[0])
    edge_distance = min(
        abs(base_bbox[0] - small_bbox[2]),
        abs(small_bbox[0] - base_bbox[2]),
    )
    vertical_gap = max(
        0.0,
        base_bbox[1] - small_bbox[3],
        small_bbox[1] - base_bbox[3],
    )
    center_offset = abs(_bbox_center_y(small_bbox) - _bbox_center_y(base_bbox))
    outside_offset = max(
        base_bbox[1] - small_bbox[1],
        small_bbox[3] - base_bbox[3],
    )
    return (
        small_width <= 0.75 * base_height
        and edge_distance <= max(1.0, 0.1 * base_height)
        and vertical_gap <= max(0.5, 0.15 * base_height)
        and center_offset >= max(0.5, 0.25 * base_height)
        and outside_offset >= max(0.5, 0.2 * base_height)
    )


def _native_typographic_scale(line: _LineItem) -> float:
    """返回原生行的字体尺度，禁止 loose 空间高度参与上下标字号比较。"""

    cached = line.native_typographic_scale
    if cached is not None and math.isfinite(cached) and cached > 0:
        return cached
    font_sizes: list[float] = []
    for char in line.chars:
        try:
            font_size = float((char.get("font") or {}).get("size") or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(font_size) and font_size > 0:
            font_sizes.append(font_size)
    return max(
        0.1,
        statistics.median(font_sizes) if font_sizes else line.em_height or line.effective_height,
    )


def _native_inline_script_matches(lines, page_size):
    """对任意行数使用同一原生上下标配对；Python 只准备文字类别和行身份。"""
    from ...._compute_backend import get_native

    rows, angles = {}, {}
    records = []
    for line in lines:
        compact = "".join(char for char in line.text if not char.isspace())
        records.append(
            (
                _rotate_bbox_to_upright(line.bbox, page_size, line.angle),
                float(line.effective_height),
                float(_native_typographic_scale(line)),
                len(compact),
                _INLINE_REFERENCE_MARKER_RE.fullmatch(compact) is not None,
                rows.setdefault(line.visual_row_id, len(rows)),
                angles.setdefault(line.angle, len(angles)),
            )
        )
    result = get_native().inline_script_matches(records)
    if result is None:
        raise ValueError("native inline script matcher rejected normalized lines")
    matches, detached_pairs = {}, set()
    for small, base, prefix, detached in result:
        position = "prefix" if prefix else "suffix"
        matches.setdefault(base, {})[position] = small
        if detached:
            detached_pairs.add((small, base, position))
    return matches, detached_pairs


def _merge_native_inline_scripts(
    lines: list[_LineItem],
    page_size: tuple[float, float],
) -> list[_LineItem]:
    """以 mutual-nearest 规则把跨粗行的小字号前后置标记合入主体视觉行。"""

    matches, detached_candidate_pairs = _native_inline_script_matches(lines, page_size)

    consumed_small_indices = {small_index for positions in matches.values() for small_index in positions.values()}
    merged_base_indices: set[int] = set()

    def merge_children(base_index: int, visiting: set[int], recurse) -> None:
        """显式传递递归函数，避免自引用闭包滞留已消费行；先合并更小标记。"""

        if base_index in merged_base_indices or base_index in visiting:
            return
        visiting.add(base_index)
        positions = matches.get(base_index, {})
        for child_index in positions.values():
            recurse(child_index, visiting, recurse)
        base = lines[base_index]
        stable_source_indices = [
            source_index
            for source_index in [base.source_index, *(lines[child_index].source_index for child_index in positions.values())]
            if source_index >= 0
        ]
        if stable_source_indices:
            base.source_index = min(stable_source_indices)
        formula_candidate_only = base.formula_candidate_only and all(
            lines[child_index].formula_candidate_only for child_index in positions.values()
        )
        merged_bbox = base.bbox
        merged_chars = list(base.chars)
        if "prefix" in positions:
            prefix_index = positions["prefix"]
            prefix = lines[prefix_index]
            base.text = f"{prefix.text.strip()} {base.text.lstrip()}"
            merged_bbox = _bbox_union(merged_bbox, prefix.bbox)
            merged_chars = [*prefix.chars, *merged_chars]
            base.split_from_row = base.split_from_row or prefix.split_from_row
            base.inline_math_regions.extend(prefix.inline_math_regions)
        if "suffix" in positions:
            suffix_index = positions["suffix"]
            suffix = lines[suffix_index]
            base.text = f"{base.text.rstrip()}{suffix.text.strip()}"
            merged_bbox = _bbox_union(merged_bbox, suffix.bbox)
            merged_chars.extend(suffix.chars)
            base.split_from_row = base.split_from_row or suffix.split_from_row
            base.inline_math_regions.extend(suffix.inline_math_regions)
        base.bbox = merged_bbox
        base.chars = merged_chars
        # 只有低重叠外置候选才需要按完整二维 bbox 计算后继行距；普通上下标保持原有基线行为。
        base.restored_inline_cluster = base.restored_inline_cluster or any(
            lines[child_index].restored_inline_cluster or (child_index, base_index, position) in detached_candidate_pairs
            for position, child_index in positions.items()
        )
        base.formula_candidate_only = formula_candidate_only
        _fill_native_typography(base, page_size)
        visiting.remove(base_index)
        merged_base_indices.add(base_index)

    # 从最终不会被消费的根主体开始，确保 small -> medium -> large 链不会丢失最小节点。
    root_base_indices = [base_index for base_index in matches if base_index not in consumed_small_indices]
    for base_index in root_base_indices:
        merge_children(base_index, set(), merge_children)
    for base_index in matches:
        merge_children(base_index, set(), merge_children)

    output = [line for index, line in enumerate(lines) if index not in consumed_small_indices]
    output.sort(key=lambda item: (item.visual_row_id if item.visual_row_id is not None else math.inf, item.run_index))
    return output


def _normalize_pdftext_angle(value: Any) -> int:
    """将 pdftext 弧度方向就近归一到四个标准角度。"""

    try:
        angle_radians = float(value or 0.0)
    except (TypeError, ValueError):
        return 0
    angle_degrees = math.degrees(angle_radians)
    normalized = int(round(angle_degrees / 90.0) * 90) % 360
    return normalized if normalized in {0, 90, 180, 270} else 0


def _get_pdf_drawing_lines(pdf_doc: PDFDocument, page_idx: int) -> list[_AxisLine]:
    """读取 PDFDocument 的公共绘图线结果，并隔离具体 PDFium 类型。"""

    return _coerce_pdf_drawing_lines(pdf_doc.get_page_drawing_lines(page_idx))


def _coerce_pdf_drawing_lines(drawing_lines: Sequence[PDFDrawingLine]) -> list[_AxisLine]:
    """把独立接口或批量快照中的绘图线统一转换为 Flash 内部坐标类型。"""

    output: list[_AxisLine] = []
    for drawing_line in drawing_lines:
        bbox = _coerce_bbox(drawing_line.bbox)
        if bbox is None:
            continue
        output.append(
            _AxisLine(
                bbox=bbox,
                width=max(0.0, float(drawing_line.width)),
                orientation=drawing_line.orientation,
            )
        )
    return output


def _median_native_glyph_width(line: _LineItem, page_size: tuple[float, float]) -> float | None:
    """返回单个原生 run 的可见字符中位宽度，缺少字符时返回空。"""

    if line.median_glyph_width is not None:
        return line.median_glyph_width
    widths: list[float] = []
    for char in line.chars:
        raw_char = str(char.get("char") or "")
        bbox = _clip_validated_bbox(_coerce_bbox(char.get("bbox")), page_size)
        if not raw_char.isprintable() or raw_char.isspace() or bbox is None:
            continue
        local_bbox = _rotate_bbox_to_upright(bbox, page_size, line.angle)
        widths.append(max(0.1, local_bbox[2] - local_bbox[0]))
    line.median_glyph_width = statistics.median(widths) if widths else None
    return line.median_glyph_width
