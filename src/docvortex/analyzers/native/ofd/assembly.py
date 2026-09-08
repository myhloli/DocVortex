"""在 OFD 所属区域内恢复视觉行、样式片段和保守段落。"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import replace
from typing import Any

from ....schema import BBox, BlockType
from .geometry import bbox_union
from .models import TextLine
from .text import format_line_html, format_line_spans

_LIST_START = re.compile(r"^\s*(?:[•●▪◆]|[-*]\s|\d+[.)、]\s*|[一二三四五六七八九十]+[、）])")


def upright_point(x: float, y: float, angle: int) -> tuple[float, float]:
    """将页面点旋转到文字阅读方向，统一横排与直角旋转的几何计算。"""
    radians = math.radians(angle)
    cosine, sine = round(math.cos(radians), 12), round(math.sin(radians), 12)
    return cosine * x + sine * y, -sine * x + cosine * y


def upright_box(bbox: BBox, angle: int) -> BBox:
    """在文字方向坐标内计算矩形外接框。"""
    points = [upright_point(x, y, angle) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def line_baseline(line: TextLine) -> float:
    """优先读取真实字形基线，缺少字形时才退回视觉框中心。"""
    if line.glyphs:
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


def merge_same_baseline_lines(lines: list[TextLine]) -> list[TextLine]:
    """按真实基线聚行并沿阅读方向拼接，保留样式及层和模板隔离。"""
    metrics = {id(line): (line_baseline(line), upright_box(line.bbox, line.angle)) for line in lines}
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
    output: list[TextLine] = []
    for row in rows:
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
                    runs = line_runs(previous) + (((separator, ()),) if separator else ()) + line_runs(line)
                    assembled[-1] = replace(
                        previous,
                        text=previous.text + separator + line.text,
                        bbox=bbox_union((previous.bbox, line.bbox)) or previous.bbox,
                        glyphs=previous.glyphs + line.glyphs,
                        paint_order=min(previous.paint_order, line.paint_order),
                        runs=runs,
                    )
                    continue
            assembled.append(line)
        output.extend(assembled)
    return output


def _block_text(block: dict[str, Any]) -> str:
    """读取 OFD 私有视觉行文本，避免依赖公共 Span 的序列化细节。"""
    return block.get("text_line", "")


def _paragraph_separator(previous: str, current: str) -> str:
    """中文换行直接续接，英文词和句末标点后的换行补空格，保留已有空白与连字符。"""
    last, first = previous[-1:], current[:1]
    if not last or not first or not (last.isascii() and first.isascii()):
        return ""
    return " " if (last.isalnum() or last in '.,;:!?)]}"') and (first.isalnum() or first in '(["') else ""


def merge_paragraph_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在阅读顺序内按局部栏宽、行距及缩进合段，视觉块和辅助块作为屏障。"""
    output: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if block["type"] != BlockType.TEXT:
            output.append(block)
            continue
        angle = block["angle"]
        current_box = upright_box(block["bbox_mm"], angle)
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
                box = upright_box(candidate["bbox_mm"], angle)
                if abs(box[1] - current_box[1]) > 20 * size or min(box[2], current_box[2]) <= max(box[0], current_box[0]):
                    break
                if abs(box[0] - current_box[0]) <= 3 * size:
                    neighbors.append(candidate)
        context = neighbors + [block]
        boxes = [upright_box(item["bbox_mm"], angle) for item in context]
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
            last_box = upright_box(previous["line_bboxes_mm"][-1], angle)
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
