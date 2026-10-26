"""在 OFD 所属区域内恢复视觉行、样式片段和保守段落。"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import replace
from typing import Any

from ....schema import BBox, BlockType
from .geometry import bbox_union
from .models import GlyphItem, TextLine
from .text import format_line_html, format_line_spans

_LIST_START = re.compile(
    r"^\s*(?:[•●▪◆]|[-*]\s|\d+[.)、]\s*|[一二三四五六七八九十]+[、）]|(?P<article>第[零〇一二三四五六七八九十百千万两\d]+条))"
)
# 复杂碎片行继续使用既有规则，限制新增邻域证据扫描和字符串拼接的最坏开销。
_MAX_SOURCE_ROW_FRAGMENTS = 64


def starts_new_article(text: str) -> bool:
    """识别独立条款开头，将同一段落边界约束传递给页内合段和公共跨页续接。"""
    match = _LIST_START.match(text)
    return match is not None and match.lastgroup == "article"


def upright_point(x: float, y: float, angle: int) -> tuple[float, float]:
    """将页面点旋转到文字阅读方向，统一横排与直角旋转的几何计算。"""
    radians = math.radians(angle)
    cosine, sine = round(math.cos(radians), 12), round(math.sin(radians), 12)
    return cosine * x + sine * y, -sine * x + cosine * y


def _default_projection(angle: int) -> bool:
    """仅复用默认纯数值投影，非标准角度及动态三角函数/投影钩子继续逐点执行。"""
    return (
        type(angle) is int
        and 0 <= angle < 360
        and upright_point is _ORIGINAL_UPRIGHT_POINT
        and (math.radians, math.cos, math.sin, round) == _ORIGINAL_ROTATION_FUNCTIONS
    )


def _plain_numbers(values: tuple) -> bool:
    """限制优化路径为无自定义运算的内置数值，超大整数保持旧错误顺序。"""
    return all(type(value) is float or (type(value) is int and abs(value) <= 2**53) for value in values)


def upright_box(bbox: BBox, angle: int) -> BBox:
    """在文字方向坐标内计算矩形外接框。"""
    if type(bbox) is tuple and len(bbox) == 4 and _default_projection(angle) and _plain_numbers(bbox):
        radians = math.radians(angle)
        cosine, sine = round(math.cos(radians), 12), round(math.sin(radians), 12)
        # 保留原点序和乘零运算，不能将直角投影直接简化为交换坐标。
        points = [(cosine * x + sine * y, -sine * x + cosine * y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    else:
        points = [upright_point(x, y, angle) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _plain_origins(glyphs: list[GlyphItem]) -> list[tuple[float, float]] | None:
    """单次提取标准不可变浮点原点，遇到定制对象即保留旧逐点路径，不逐字创建检查生成器。"""
    origins = []
    for glyph in glyphs:
        if type(glyph) is not GlyphItem:
            return None
        origin = glyph.origin
        if type(origin) is not tuple or len(origin) != 2 or type(origin[0]) is not float or type(origin[1]) is not float:
            return None
        origins.append(origin)
    return origins


def line_baseline(line: TextLine) -> float:
    """优先读取真实字形基线，缺少字形时才退回视觉框中心。"""
    if line.glyphs:
        if (
            type(line) is TextLine
            and TextLine.__getattribute__ is object.__getattribute__
            and GlyphItem.__getattribute__ is object.__getattribute__
            and statistics.median is _ORIGINAL_MEDIAN
            and type(line.glyphs) is list
            and len(line.glyphs) >= 8
            and _default_projection(line.angle)
            and GlyphItem.origin is _ORIGINAL_GLYPH_ORIGIN
        ):
            origins = _plain_origins(line.glyphs)
            if origins is not None:
                radians = math.radians(line.angle)
                cosine, sine = round(math.cos(radians), 12), round(math.sin(radians), 12)
                return statistics.median(-sine * x + cosine * y for x, y in origins)
        return statistics.median(upright_point(*glyph.origin, line.angle)[1] for glyph in line.glyphs)
    box = upright_box(line.bbox, line.angle)
    return (box[1] + box[3]) / 2


def line_runs(line: TextLine) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """读取已组装行的样式片段，原始行按其自身样式返回单个片段。"""
    return line.runs or ((line.text, line.styles),)


def line_spans(line: TextLine) -> list[dict[str, object]]:
    """将视觉行的各样式片段投影为既有结构化 Span。"""
    return [span for text, styles in line_runs(line) for span in format_line_spans(text, styles)]


def line_html(line: TextLine) -> str:
    """按片段样式序列化单元格文字，不把首片段样式扩散到整行。"""
    return "".join(format_line_html(text, styles) for text, styles in line_runs(line))


def _fragment_separator(first: TextLine, second: TextLine) -> str:
    """仅在英文单词片段之间存在可见词间距时补空格。"""
    if not first.text or not second.text:
        return ""
    if not (first.text[-1].isascii() and second.text[0].isascii() and first.text[-1].isalnum() and second.text[0].isalnum()):
        return ""
    first_box = upright_box(first.glyphs[-1].bbox if first.glyphs else first.bbox, first.angle)
    second_box = upright_box(second.glyphs[0].bbox if second.glyphs else second.bbox, second.angle)
    widths = [box[2] - box[0] for box in (first_box, second_box)]
    threshold = 0.2 * max(min(first.font_size, second.font_size), statistics.median(widths), 0.5)
    return " " if second_box[0] - first_box[2] >= threshold else ""


def _baseline_rows(lines: list[TextLine], metrics: dict[int, tuple[float, BBox]]) -> list[list[TextLine]]:
    """按相同来源和首片段基线分行，供原有紧邻拼接与源布局补充规则复用。"""
    ordered = sorted(lines, key=lambda line: (line.angle, line.layer_type, line.template_id or -1, metrics[id(line)][0]))
    rows: list[list[TextLine]] = []
    for line in ordered:
        previous = rows[-1][0] if rows else None
        if (
            previous is not None
            and (line.angle, line.layer_type, line.template_id) == (previous.angle, previous.layer_type, previous.template_id)
            and abs(metrics[id(line)][0] - metrics[id(previous)][0]) <= 0.2 * min(line.font_size, previous.font_size)
        ):
            rows[-1].append(line)
        else:
            rows.append([line])
    return rows


def _join_text_lines(first: TextLine, second: TextLine, separator: str) -> TextLine:
    """新建保留局部样式和真实字形框的合并行，源布局证据仅在两侧都有效时延续。"""
    return replace(
        first,
        text=first.text + separator + second.text,
        bbox=bbox_union((first.bbox, second.bbox)) or first.bbox,
        glyphs=first.glyphs + second.glyphs,
        paint_order=min(first.paint_order, second.paint_order),
        runs=line_runs(first) + (((separator, ()),) if separator else ()) + line_runs(second),
        source_bbox=(
            bbox_union((first.source_bbox, second.source_bbox))
            if first.source_bbox is not None and second.source_bbox is not None else None
        ),
    )


def merge_same_baseline_lines(lines: list[TextLine]) -> list[TextLine]:
    """按真实基线聚行并沿阅读方向拼接，保留样式及层和模板隔离。"""
    metrics = {id(line): (line_baseline(line), upright_box(line.bbox, line.angle)) for line in lines}
    output: list[TextLine] = []
    for row in _baseline_rows(lines, metrics):
        fragments = sorted(row, key=lambda line: (metrics[id(line)][1][0], line.paint_order))
        assembled: list[TextLine] = []
        for line in fragments:
            previous = assembled[-1] if assembled else None
            if previous is not None:
                first_box, second_box = upright_box(previous.bbox, line.angle), metrics[id(line)][1]
                size = min(previous.font_size, line.font_size)
                gap = second_box[0] - first_box[2]
                if size > 0 and max(previous.font_size, line.font_size) <= 1.35 * size and -0.25 * size <= gap <= 0.8 * size:
                    separator = _fragment_separator(previous, line)
                    assembled[-1] = _join_text_lines(previous, line, separator)
                    continue
            assembled.append(line)
        output.extend(assembled)
    return output


def _source_row_supported(
    left: TextLine,
    right: TextLine,
    previous_row: list[TextLine],
    following_row: list[TextLine],
    metrics: dict[int, tuple[float, BBox]],
    barriers: list[BBox],
) -> bool:
    """仅在源框相邻、紧接的正文跨过间隙且没有分栏或视觉屏障时补充同行关联。"""
    if left.source_bbox is None or right.source_bbox is None or left.angle not in {0, 90, 180, 270}:
        return False
    size = min(left.font_size, right.font_size)
    if size <= 0 or max(left.font_size, right.font_size) > 1.35 * size:
        return False
    left_box, right_box = upright_box(left.bbox, left.angle), metrics[id(right)][1]
    baseline = line_baseline(left)
    if abs(baseline - metrics[id(right)][0]) > 0.2 * size or right_box[0] - left_box[2] <= 0.8 * size:
        return False
    left_source, right_source = upright_box(left.source_bbox, left.angle), upright_box(right.source_bbox, right.angle)
    if not 0 <= right_source[0] - left_source[2] <= 0.8 * size:
        return False
    # 只信任贴近真实文字的相向容器边，防止大幅留白的单行容器制造虚假邻接。
    if left_source[2] - left_box[2] > size or right_box[0] - right_source[0] > size:
        return False
    scope = (left.angle, left.layer_type, left.template_id)
    previous_boxes = [
        metrics[id(line)][1] for line in previous_row
        if (line.angle, line.layer_type, line.template_id) == scope
        and 0.5 * size <= baseline - metrics[id(line)][0] <= 3 * size
    ]
    if (
        any(box[0] < left_box[2] and box[2] <= right_box[0] for box in previous_boxes)
        and any(box[0] >= left_box[2] and box[2] > right_box[0] for box in previous_boxes)
        and not any(box[0] <= left_box[2] and box[2] >= right_box[0] for box in previous_boxes)
    ):
        return False
    for following in following_row:
        if (following.angle, following.layer_type, following.template_id) != scope:
            continue
        following_baseline, box = metrics[id(following)]
        if (
            max(size, following.font_size) > 1.25 * min(size, following.font_size)
            or not 0.5 * size <= following_baseline - baseline <= 3 * size
            or not -2.5 * size <= box[0] - left_box[0] <= 0.8 * size
            or not (box[0] <= left_box[2] and box[2] >= right_box[0])
            or len(following.text.strip()) <= 4
            or _LIST_START.match(following.text)
        ):
            continue
        # 此处只证明同行间隙属于连续正文；末行是否写满由完整组行后的合段规则判断。
        region = bbox_union((left_box, right_box, box))
        assert region is not None
        if any(
            min(region[2], barrier[2]) > max(region[0], barrier[0])
            and min(region[3], barrier[3]) > max(region[1], barrier[1])
            for barrier in barriers
        ):
            continue
        return True
    return False


def merge_source_adjacent_lines(
    lines: list[TextLine], *, barriers: list[BBox] | None = None, excluded_line_ids: frozenset[int] = frozenset()
) -> list[TextLine]:
    """仅替换既有组行结果中证据充分的片段，不重排其他行或跨中间行寻找通栏证据。"""
    if sum(line.source_bbox is not None and id(line) not in excluded_line_ids for line in lines) < 2:
        return lines
    metrics = {id(line): (line_baseline(line), upright_box(line.bbox, line.angle)) for line in lines}
    rows = _baseline_rows(lines, metrics)
    projected_barriers: dict[int, list[BBox]] = {}
    replacements: dict[int, TextLine] = {}
    consumed: set[int] = set()
    anchors: dict[int, int] = {}
    for index, row in enumerate(rows):
        if any(len(neighbor) > _MAX_SOURCE_ROW_FRAGMENTS for neighbor in rows[max(0, index - 1):index + 2]):
            continue
        previous_row = [line for line in rows[index - 1] if id(line) not in excluded_line_ids] if index else []
        following_row = (
            [line for line in rows[index + 1] if id(line) not in excluded_line_ids] if index + 1 < len(rows) else []
        )
        assembled: list[TextLine] = []
        for line in sorted(row, key=lambda item: (metrics[id(item)][1][0], item.paint_order)):
            previous = assembled[-1] if assembled else None
            if previous is not None and id(previous) not in excluded_line_ids and id(line) not in excluded_line_ids:
                if line.angle not in projected_barriers:
                    projected_barriers[line.angle] = [upright_box(box, line.angle) for box in barriers or []]
                if _source_row_supported(
                    previous, line, previous_row, following_row, metrics, projected_barriers[line.angle]
                ):
                    separator = "" if previous.text[-1:].isspace() or line.text[:1].isspace() else " "
                    merged = _join_text_lines(previous, line, separator)
                    anchor = anchors.pop(id(previous), id(previous))
                    anchors[id(merged)] = anchor
                    replacements[anchor] = merged
                    consumed.add(id(line))
                    assembled[-1] = merged
                    continue
            assembled.append(line)
    return [replacements.get(id(line), line) for line in lines if id(line) not in consumed]


def _block_text(block: dict[str, Any]) -> str:
    """读取 OFD 私有视觉行文本，避免依赖公共 Span 的序列化细节。"""
    return block.get("text_line", "")


def _paragraph_separator(previous: str, current: str) -> str:
    """中文换行直接续接，英文词和句末标点后的换行补空格，保留已有空白与连字符。"""
    last, first = previous[-1:], current[:1]
    if not last or not first or not (last.isascii() and first.isascii()):
        return ""
    return " " if (last.isalnum() or last in '.,;:!?)]}"') and (first.isalnum() or first in '(["') else ""


def _paragraph_box(bbox: BBox, angle: int, cache: dict) -> BBox:
    """只在本次合段内缓存至多64个不可变数值框，按对象身份保留正负零并防止ID复用。"""
    if upright_box is _ORIGINAL_UPRIGHT_BOX and type(bbox) is tuple and len(bbox) == 4 and _default_projection(angle):
        key = (id(bbox), angle)
        entry = cache.get(key)
        if entry is not None and entry[0] is bbox:
            return entry[1]
        if _plain_numbers(bbox):
            projected = upright_box(bbox, angle)
            if len(cache) >= 64:
                del cache[next(iter(cache))]
            # 强引用保存原tuple；同值但零符号不同的框不会共用记录。
            cache[key] = (bbox, projected)
            return projected
    return upright_box(bbox, angle)


def merge_paragraph_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在阅读顺序内按局部栏宽、行距及缩进合段，视觉块和辅助块作为屏障。"""
    output: list[dict[str, Any]] = []
    projection_cache: dict = {}
    for index, block in enumerate(blocks):
        if block["type"] != BlockType.TEXT:
            output.append(block)
            continue
        angle = block["angle"]
        current_box = _paragraph_box(block["bbox_mm"], angle, projection_cache)
        size = block["font_size_mm"]
        # 只观察邻近且横向重叠的正文，防止另一栏或远处区域影响本栏尺度。
        neighbors: list[dict[str, Any]] = []
        for direction in (-1, 1):
            for offset in range(1, 9):
                position = index + direction * offset
                if not 0 <= position < len(blocks):
                    break
                candidate = blocks[position]
                if (
                    candidate["type"] != BlockType.TEXT
                    or candidate["angle"] != angle
                    or candidate["text_scope"] != block["text_scope"]
                ):
                    break
                box = _paragraph_box(candidate["bbox_mm"], angle, projection_cache)
                if abs(box[1] - current_box[1]) > 20 * size or min(box[2], current_box[2]) <= max(box[0], current_box[0]):
                    break
                if abs(box[0] - current_box[0]) <= 3 * size:
                    neighbors.append(candidate)
        context = neighbors + [block]
        boxes = [_paragraph_box(item["bbox_mm"], angle, projection_cache) for item in context]
        right = max(box[2] for box in boxes)
        baselines = sorted({item["baseline_mm"] for item in context})
        gaps = [b - a for a, b in zip(baselines[:-1], baselines[1:], strict=True) if b - a >= 0.5 * size]
        pitch = statistics.median(gaps) if gaps else 1.5 * size
        previous = output[-1] if output else None
        can_merge = (
            previous is not None
            and previous["type"] == BlockType.TEXT
            and previous["angle"] == angle
            and previous["text_scope"] == block["text_scope"]
        )
        if can_merge:
            last_box = _paragraph_box(previous["line_bboxes_mm"][-1], angle, projection_cache)
            previous_size = previous["font_size_mm"]
            delta = block["baseline_mm"] - previous["baseline_mm"]
            previous_text, current_text = _block_text(previous).strip(), _block_text(block).strip()
            can_merge = (
                min(size, previous_size) > 0
                and max(size, previous_size) <= 1.25 * min(size, previous_size)
                and 0.5 * size <= delta <= min(1.4 * pitch, 3 * size)
                and -2.5 * size <= current_box[0] - last_box[0] <= 0.8 * size
                and right - last_box[2] <= 1.5 * size
                and min(last_box[2], current_box[2]) > max(last_box[0], current_box[0])
                and not _LIST_START.match(current_text)
                and not (len(previous_text) <= 4 or len(current_text) <= 4)
            )
        if can_merge:
            previous_text, current_text = _block_text(previous), _block_text(block)
            separator = _paragraph_separator(previous_text, current_text)
            previous["content"].extend(format_line_spans(separator, ()))
            previous["content"].extend(block["content"])
            previous["bbox_mm"] = bbox_union((previous["bbox_mm"], block["bbox_mm"]))
            previous["line_bboxes_mm"].extend(block["line_bboxes_mm"])
            previous["baseline_mm"] = block["baseline_mm"]
            previous["text_line"] = current_text
        else:
            # 复制将被追加的列表，确保组段不会修改调用方的原始视觉行。
            output.append({**block, "content": list(block["content"]), "line_bboxes_mm": list(block["line_bboxes_mm"])})
    return output


_ORIGINAL_UPRIGHT_POINT = upright_point
_ORIGINAL_UPRIGHT_BOX = upright_box
_ORIGINAL_ROTATION_FUNCTIONS = (math.radians, math.cos, math.sin, round)
_ORIGINAL_MEDIAN = statistics.median
_ORIGINAL_GLYPH_ORIGIN = GlyphItem.origin
