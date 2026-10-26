"""Native PDF 表格字符选择、视觉组行和单元格文本重建。"""

from __future__ import annotations

import statistics
import math
import re
import unicodedata
from dataclasses import dataclass, replace

from .....document.pdf.text._contracts import Bbox as _CharacterBbox
from .. import table_geometry as _pending_geometry
from .....foundation._text import resolve_text_line_boundary
from .....schema import BBox
from ..spatial_text import _normalize_table_text
from .contracts import NativeTableGlyph, NativeTableInput, NativeTableText, NativeTableTextRow, NativeTableToken
from .geometry import (
    bbox_intersection,
    bbox_union,
    normalize_angle,
    normalize_bbox,
    page_bbox_to_table_local,
    rotate_local_bbox,
)


@dataclass(frozen=True, slots=True)
class _PendingGlyph:
    """保存尚未分配视觉行的局部 PDF 字符。"""

    glyph_id: int
    source_index: int
    text: str
    bbox: BBox
    explicit_space_before: bool = False
    explicit_break_before: bool = False
    writing_angle: int | None = 0
    advance_bbox: BBox | None = None
    origin: tuple[float, float] | None = None


def _rotate_origin(origin: tuple[float, float], angle: int) -> tuple[float, float]:
    """旋转局部基线原点，平移量不参与单元格内的相对几何判定。"""
    x, y = origin
    return {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[angle]


def _local_origin(value: object, table_bbox: BBox, angle: int) -> tuple[float, float] | None:
    """读取有限基线原点并转换到与物理字形相同的整表坐标。"""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    x, y = _rotate_origin((x - table_bbox[0], y - table_bbox[1]), angle)
    width, height = table_bbox[2] - table_bbox[0], table_bbox[3] - table_bbox[1]
    dx, dy = {0: (0, 0), 90: (0, width), 180: (width, height), 270: (height, 0)}[angle]
    return x + dx, y + dy


def _relative_writing_angle(value: object, table_angle: int) -> int | None:
    """把已校正的页面书写弧度转换到整表坐标；缺失方向沿用整表语义。"""
    if value is None:
        return 0
    try:
        degrees = math.degrees(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(degrees):
        return None
    relative = (degrees % 360 - table_angle) % 360
    cardinal = round(relative / 90) * 90 % 360
    return cardinal if abs((relative - cardinal + 180) % 360 - 180) <= 0.1 else None


def _baseline_writing_angle(glyph: _PendingGlyph) -> int | None:
    """独立字符缺少 advance 时，以字体排版框相对基线的上下侧证据纠正半周反向。"""
    angle = glyph.writing_angle
    if not angle or glyph.origin is None:
        return angle
    # 轻量字符接口没有 loose 框时，字母数字的墨迹相对基线仍可证明半周反向；标点必须有字体排版框。
    reference = glyph.advance_bbox or (glyph.bbox if any(char.isalnum() for char in glyph.text) else None)
    if reference is None:
        return angle
    box = rotate_local_bbox(reference, 0, 0, angle)
    origin = _rotate_origin(glyph.origin, angle)
    above, below = origin[1] - box[1], box[3] - origin[1]
    # 正向字体框通常将上伸部放在基线上方；只有完整排版框强烈反向才纠偏，不能使用短横线的墨迹框猜方向。
    if above >= -0.1 * (box[3] - box[1]) and below > 0 and below >= 2 * above and below - above >= 0.4 * (box[3] - box[1]):
        return (angle + 180) % 360
    return angle


_PENDING_REFERENCES = (
    normalize_bbox,
    normalize_angle,
    _normalize_table_text,
    _CharacterBbox.__getattribute__,
    NativeTableInput.__getattribute__,
    _PendingGlyph.__init__,
    _PendingGlyph.__new__,
    object.__new__,
)
_PENDING_FIELDS = _PendingGlyph.__slots__
_PENDING_SLOTS = tuple(getattr(_PendingGlyph, name) for name in _PENDING_FIELDS)


def _select_pending_glyphs(table_input):
    """一次传输完整普通表格输入，不缓存字符或成员；定制对象完整使用原适配路径。"""
    from ....._compute_backend import get_native

    native = get_native()
    if (
        type(table_input) is NativeTableInput
        and (
            normalize_bbox,
            normalize_angle,
            _normalize_table_text,
            _CharacterBbox.__getattribute__,
            NativeTableInput.__getattribute__,
            _PendingGlyph.__init__,
            _PendingGlyph.__new__,
            object.__new__,
        )
        == _PENDING_REFERENCES
        and _pending_geometry.normalize_bbox is _PENDING_REFERENCES[0]
        and tuple(getattr(_PendingGlyph, name, None) for name in _PENDING_FIELDS) == _PENDING_SLOTS
        and type(table_input.angle) is int
    ):
        table_bbox = normalize_bbox(table_input.table_bbox)
        if table_bbox is None:
            return []
        values = native.table_pending_glyphs_owned(
            table_input.chars,
            table_bbox,
            normalize_angle(table_input.angle),
            _CharacterBbox,
            _normalize_table_text,
            _PendingGlyph,
            _PENDING_REFERENCES[-1],
        )
        if values is not None:
            return values
    return _select_pending_glyphs_adapter(table_input)


def _select_pending_glyphs_adapter(table_input: NativeTableInput) -> list[_PendingGlyph]:
    """批量筛选区域字符，保留 Python 的源索引排序、空白及文本规范化。"""
    from ....._compute_backend import get_native
    from .._native_geometry import raw_bbox

    native = get_native()
    table_bbox = normalize_bbox(table_input.table_bbox)
    if table_bbox is None:
        return []
    prepared = native.table_boxes(
        [raw_bbox(char.get("bbox")) for char in table_input.chars],
        table_bbox,
        normalize_angle(table_input.angle),
        normalize_bbox,
    )
    selected = []
    for index, _absolute, local in prepared:
        char = table_input.chars[index]
        try:
            source_index = int(char.get("char_idx", index))
        except (TypeError, ValueError):
            source_index = index
        selected.append((source_index, index, char, local))
    selected.sort(key=lambda item: (item[0], item[1]))
    output = []
    pending_space = pending_break = False
    for source_index, _index, char, local in selected:
        raw_text = str(char.get("char") or "")
        if not raw_text:
            continue
        if raw_text in {"\r", "\n"}:
            pending_break, pending_space = True, False
            continue
        if raw_text.isspace():
            if not pending_break:
                pending_space = True
            continue
        text = _normalize_table_text(raw_text)
        if not text or text.isspace() or local is None or (local[3] - local[1] < 0.5 and not _contains_cjk(text)):
            continue
        direction = _relative_writing_angle(char.get("writing_angle"), normalize_angle(table_input.angle))
        loose = normalize_bbox(char.get("loose_bbox")) if direction else None
        advance = page_bbox_to_table_local(loose, table_bbox, normalize_angle(table_input.angle)) if loose else None
        output.append(
            _PendingGlyph(
                len(output),
                source_index,
                text,
                local,
                pending_space,
                pending_break,
                direction,
                advance,
                _local_origin(char.get("origin"), table_bbox, normalize_angle(table_input.angle)),
            )
        )
        pending_space = pending_break = False
    return output


def _assign_visual_rows(pending: list[_PendingGlyph], median_height: float) -> list[list[_PendingGlyph]]:
    """由原生组行返回成员索引，保留原字形对象以及特殊坐标的解释器语义。"""
    from ....._compute_backend import get_native

    rows = get_native().table_visual_rows(
        [glyph.bbox for glyph in pending], [glyph.glyph_id for glyph in pending], median_height
    )
    return [[pending[index] for index in row] for row in rows]


def _contains_cjk(text: str) -> bool:
    """判断文本是否包含常见中日韩统一表意字符。"""

    # 组行热路径通常只传入相邻两个字符，直接遍历避免逐次创建生成器。
    for char in text:
        if "\u3400" <= char <= "\u9fff":
            return True
    return False


def _join_glyph_line(
    glyphs: list[NativeTableGlyph] | list[_PendingGlyph],
    median_height: float,
) -> str:
    """按字符间距重建单行文本，避免在中文字符之间强插空格。"""

    return "".join(text for text, _source_index in _glyph_line_parts(glyphs, median_height))


def _nearest_alphabetic_char(text: str, *, reverse: bool) -> str:
    """从物理行边界向内查找最近的 Unicode 字母。"""

    normalized = text.rstrip() if reverse else text.lstrip()
    characters = reversed(normalized) if reverse else iter(normalized)
    return next((char for char in characters if char.isalpha()), "")


def _is_latin_letter(char: str) -> bool:
    """判断单个 Unicode 字符规范化后是否只由 Latin 字母组成。"""

    if len(char) != 1:
        return False
    normalized = unicodedata.normalize("NFKC", char)
    return bool(normalized) and all(item.isalpha() and unicodedata.name(item, "").startswith("LATIN ") for item in normalized)


def _looks_like_compact_unit_or_identifier(
    previous_line: str,
    next_line: str,
) -> bool:
    """识别不应因视觉换行插入空格的紧凑单位或标识符片段。"""

    combined = previous_line.strip() + next_line.strip()
    if not combined or any(char.isspace() for char in combined):
        return False
    # 带单位后缀的长英文标题仍有词界，不能仅因末尾 /% 就把相邻单词粘连。
    if re.fullmatch(r"[A-Za-z]{4,}", previous_line.strip()) and re.match(r"[A-Za-z]{4,}(?:/|%)", next_line.strip()):
        return False
    return any(char.isdigit() or char in "/_^%°µμ" for char in combined)


def _starts_with_url(text: str) -> bool:
    """判断下一物理行是否自身以完整 URL 前缀开始。"""

    normalized = text.lstrip().casefold()
    return normalized.startswith(("http://", "https://", "ftp://", "www."))


def _cell_row_separator(
    accumulated_content: str,
    previous_line: str,
    next_line: str,
) -> str:
    """按相邻行边界词元返回安全分隔符，非 Latin 边界保持直连。"""

    # 日期或自然短语在逗号后续接数字仍有词界；纯数字、短变量与单位断行保持原紧凑规则。
    if (
        previous_line.rstrip().endswith((",", ";"))
        and re.search(r"[A-Za-z]{3,}", previous_line)
        and next_line.lstrip()[:1].isdigit()
    ):
        return " "
    previous_letter = _nearest_alphabetic_char(previous_line, reverse=True)
    next_letter = _nearest_alphabetic_char(next_line, reverse=False)
    if not (_is_latin_letter(previous_letter) and _is_latin_letter(next_letter)):
        return ""

    _processed_previous, separator = resolve_text_line_boundary(
        accumulated_content,
        next_content=next_line,
    )
    if separator != " ":
        # 表格 HTML 保留原始连字符，只复用正文规则的 URL/连字符直连判定。
        return separator
    if not _starts_with_url(next_line) and _looks_like_compact_unit_or_identifier(
        previous_line,
        next_line,
    ):
        return ""
    return separator


def _glyph_line_parts(
    glyphs: list[NativeTableGlyph] | list[_PendingGlyph],
    median_height: float,
    *,
    presorted: bool = False,
    use_advance: bool = False,
) -> list[tuple[str, int | None]]:
    """重建单行文本片段，并保留每个可见片段的原字符索引。"""

    if not glyphs:
        return []
    ordered = glyphs if presorted else sorted(glyphs, key=lambda glyph: (glyph.bbox[0], glyph.bbox[1], glyph.glyph_id))
    parts: list[tuple[str, int | None]] = [(ordered[0].text, ordered[0].source_index)]
    previous = ordered[0]
    for glyph in ordered[1:]:
        current_box = (glyph.advance_bbox if use_advance else None) or glyph.bbox
        previous_box = (previous.advance_bbox if use_advance else None) or previous.bbox
        gap = current_box[0] - previous_box[2]
        previous_tail = previous.text[-1:] or ""
        current_head = glyph.text[:1] or ""
        needs_space = glyph.explicit_space_before or (
            not glyph.explicit_break_before
            and gap > max(0.6, median_height * 0.20)
            and not _contains_cjk(previous_tail + current_head)
            and previous_tail.isprintable()
            and current_head.isprintable()
        )
        if needs_space and not parts[-1][0].endswith(" "):
            parts.append((" ", None))
        parts.append((glyph.text, glyph.source_index))
        previous = glyph
    return parts


def _tokenize_row(
    glyphs: list[NativeTableGlyph],
    median_width: float,
    median_height: float,
) -> tuple[NativeTableToken, ...]:
    """按显著水平间隙把视觉行拆成供列推断使用的文本项。"""

    if not glyphs:
        return ()
    split_gap = max(2.0 * median_width, 0.70 * median_height, 2.0)
    groups: list[list[NativeTableGlyph]] = [[glyphs[0]]]
    for glyph in glyphs[1:]:
        if glyph.bbox[0] - groups[-1][-1].bbox[2] > split_gap:
            groups.append([glyph])
        else:
            groups[-1].append(glyph)
    tokens: list[NativeTableToken] = []
    for group in groups:
        # group 是视觉行按同一排序键切出的连续片段，不必再次排序。
        content = "".join(text for text, _source_index in _glyph_line_parts(group, median_height, presorted=True))
        if not content:
            continue
        tokens.append(
            NativeTableToken(
                text=content,
                bbox=bbox_union(item.bbox for item in group),
                glyph_ids=tuple(item.glyph_id for item in group),
                source_char_indices=tuple(item.source_index for item in group),
            )
        )
    return tuple(tokens)


def build_native_table_text(table_input: NativeTableInput) -> NativeTableText | None:
    """把原生 PDF 字符转换为正向表格字形、视觉行和文本项。"""

    pending = _select_pending_glyphs(table_input)
    if not pending:
        return None
    widths = [glyph.bbox[2] - glyph.bbox[0] for glyph in pending if glyph.bbox[2] > glyph.bbox[0]]
    heights = [glyph.bbox[3] - glyph.bbox[1] for glyph in pending if glyph.bbox[3] > glyph.bbox[1]]
    median_width = max(0.1, float(statistics.median(widths)) if widths else 1.0)
    median_height = max(0.1, float(statistics.median(heights)) if heights else 1.0)
    pending_rows = _assign_visual_rows(pending, median_height)

    glyphs: list[NativeTableGlyph] = []
    rows: list[NativeTableTextRow] = []
    for row_index, pending_row in enumerate(pending_rows):
        row_glyphs = [
            NativeTableGlyph(
                glyph_id=item.glyph_id,
                source_index=item.source_index,
                text=item.text,
                bbox=item.bbox,
                visual_row=row_index,
                explicit_space_before=item.explicit_space_before,
                explicit_break_before=item.explicit_break_before,
                writing_angle=_baseline_writing_angle(item),
                advance_bbox=item.advance_bbox,
                origin=item.origin,
            )
            for item in pending_row
        ]
        glyphs.extend(row_glyphs)
        rows.append(
            NativeTableTextRow(
                row_index=row_index,
                bbox=bbox_union(item.bbox for item in row_glyphs),
                tokens=_tokenize_row(row_glyphs, median_width, median_height),
                glyph_ids=tuple(item.glyph_id for item in row_glyphs),
            )
        )
    glyphs.sort(key=lambda item: item.glyph_id)
    return NativeTableText(
        glyphs=tuple(glyphs),
        rows=tuple(rows),
        median_glyph_width=median_width,
        median_glyph_height=median_height,
    )


def build_cell_text(
    glyphs: list[NativeTableGlyph],
    median_height: float,
) -> str:
    """按视觉行和局部横向顺序重建文本，并将同一单元格内的多行直接拼接。"""

    return "".join(text for text, _source_index in build_cell_text_parts(glyphs, median_height))


def _native_cell_visual_groups(glyphs, median_height):
    """原生读取当前字形并细分视觉行，结果继续引用调用方的完整字形对象。"""
    from ....._compute_backend import get_native

    indices = get_native().cell_visual_groups_owned(glyphs, median_height, NativeTableGlyph)
    return [[glyphs[index] for index in row] for row in indices]


def cell_writing_angle(glyphs: list[NativeTableGlyph]) -> int | None:
    """用同向邻字的基线和墨迹侧向消歧孤立字形，真实混合方向保持拒绝。"""
    directions = {glyph.writing_angle for glyph in glyphs}
    if not directions:
        return 0
    if len(directions) == 1:
        return next(iter(directions))
    accepted = [angle for angle in directions if angle is not None and _cell_baseline_supports_angle(glyphs, angle)]
    return accepted[0] if len(accepted) == 1 else None


def _cell_baseline_supports_angle(glyphs: list[NativeTableGlyph], angle: int) -> bool:
    """同一书写行至少两个锚字才能纠正孤立字符的角度，墨迹须位于基线同侧。"""
    anchors = [
        (rotate_local_bbox(glyph.bbox, 0, 0, angle), _rotate_origin(glyph.origin, angle))
        for glyph in glyphs
        if glyph.writing_angle == angle and glyph.origin is not None
    ]
    if len(anchors) < 2:
        return False
    scale = float(statistics.median(box[3] - box[1] for box, _origin in anchors))
    for glyph in glyphs:
        if glyph.writing_angle == angle:
            continue
        if glyph.origin is None or glyph.writing_angle is not None and (glyph.writing_angle - angle) % 360 != 180:
            return False
        box = rotate_local_bbox(glyph.bbox, 0, 0, angle)
        origin = _rotate_origin(glyph.origin, angle)
        neighbors = [
            (peer, baseline)
            for peer, baseline in anchors
            if abs(origin[1] - baseline[1]) <= 0.15 * scale and max(peer[0] - box[2], box[0] - peer[2], 0) <= 2 * scale
        ]
        if (
            len(neighbors) < 2
            or max(point[0] for _, point in neighbors) - min(point[0] for _, point in neighbors) < 0.5 * scale
        ):
            return False
        center = (box[1] + box[3]) / 2 - origin[1]
        side = statistics.median((peer[1] + peer[3]) / 2 - baseline[1] for peer, baseline in neighbors)
        # 单字旋转 180 度时墨迹会落到基线另一侧；不能仅凭邻字占多数就强行改写。
        if center * side <= 0 or abs(center - side) > 0.6 * scale:
            return False
    return True


def cell_text_rows(
    glyphs: list[NativeTableGlyph],
    median_height: float,
) -> tuple[list[list[NativeTableGlyph]], float]:
    """仅为单元格阅读创建正向字形副本，保留落格坐标及源字符身份。"""
    angle = cell_writing_angle(glyphs)
    if angle is None:
        raise ValueError("ambiguous cell writing direction")
    if not angle:
        return _native_cell_visual_groups(glyphs, median_height), median_height
    from ....._compute_backend import get_native

    upright = [
        replace(
            glyph,
            bbox=rotate_local_bbox(glyph.bbox, 0, 0, angle),
            advance_bbox=rotate_local_bbox(glyph.advance_bbox, 0, 0, angle) if glyph.advance_bbox else None,
        )
        for glyph in glyphs
    ]
    height = float(statistics.median(glyph.bbox[3] - glyph.bbox[1] for glyph in upright))
    indices = get_native().table_visual_rows(
        [glyph.bbox for glyph in upright],
        [glyph.glyph_id for glyph in upright],
        height,
    )
    rows = [[replace(upright[index], visual_row=row_index) for index in row] for row_index, row in enumerate(indices)]
    return rows, height


def build_cell_text_parts(
    glyphs: list[NativeTableGlyph],
    median_height: float,
) -> list[tuple[str, int | None]]:
    """按视觉行重建 cell 文本片段，供安全插入字符级语义标签。"""

    if not glyphs:
        return []
    refined, median_height = cell_text_rows(glyphs, median_height)
    use_advance = bool(cell_writing_angle(glyphs))
    grouped = dict(enumerate(refined))
    parts: list[tuple[str, int | None]] = []
    previous_line = ""
    for row_index in sorted(grouped):
        row_parts = _glyph_line_parts(grouped[row_index], median_height, use_advance=use_advance)
        row_text = "".join(text for text, _source_index in row_parts)
        if not row_text:
            continue
        if parts:
            accumulated_content = "".join(text for text, _source_index in parts)
            separator = _cell_row_separator(
                accumulated_content,
                previous_line,
                row_text,
            )
            if separator and not accumulated_content.endswith(" ") and not row_text.startswith(" "):
                parts.append((separator, None))
        parts.extend(row_parts)
        previous_line = row_text
    return parts


def glyph_overlap_ratio(glyph: NativeTableGlyph, bbox: BBox) -> float:
    """返回字符框面积被目标单元格覆盖的比例。"""

    intersection = bbox_intersection(glyph.bbox, bbox)
    glyph_area = (glyph.bbox[2] - glyph.bbox[0]) * (glyph.bbox[3] - glyph.bbox[1])
    if intersection is None or glyph_area <= 0:
        return 0.0
    intersection_area = (intersection[2] - intersection[0]) * (intersection[3] - intersection[1])
    return min(1.0, intersection_area / glyph_area)


__all__ = [
    "build_cell_text",
    "build_cell_text_parts",
    "build_native_table_text",
    "glyph_overlap_ratio",
]
