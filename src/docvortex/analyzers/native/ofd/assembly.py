"""Restore visual lines, style fragments and conservative paragraphs in the area belonging to OFD."""

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
    """Rotate the page point to the text reading direction and unify the geometric calculations of horizontal and rectangular rotation."""
    radians = math.radians(angle)
    cosine, sine = round(math.cos(radians), 12), round(math.sin(radians), 12)
    return cosine * x + sine * y, -sine * x + cosine * y


def upright_box(bbox: BBox, angle: int) -> BBox:
    """Calculate the rectangular bounding box within the text direction coordinates."""
    points = [upright_point(x, y, angle) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def line_baseline(line: TextLine) -> float:
    """The real glyph baseline is read first, and the center of the visual frame is returned only when the glyph is missing."""
    if line.glyphs:
        return statistics.median(upright_point(*glyph.origin, line.angle)[1] for glyph in line.glyphs)
    box = upright_box(line.bbox, line.angle)
    return (box[1] + box[3]) / 2


def line_runs(line: TextLine) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Reads the styled fragments of the assembled row, returning the original row as a single fragment styled by itself."""
    return line.runs or ((line.text, line.styles),)


def line_spans(line: TextLine) -> list[dict[str, object]]:
    """Project each style fragment of the visual row to the existing structured Span."""
    return [span for text, styles in line_runs(line) for span in format_line_spans(text, styles)]


def line_html(line: TextLine) -> str:
    """Serialize cell text by fragment style without spreading the first fragment style to the entire row."""
    return "".join(format_line_html(text, styles) for text, styles in line_runs(line))


def _fragment_separator(first: TextLine, second: TextLine) -> str:
    """Fill spaces only when there is visible word space between English word fragments."""
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
    """Cluster according to the true baseline and splice along the reading direction, preserving style and layer and template isolation."""
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
    """Read OFD private visual line text to avoid relying on serialization details of public Span."""
    return block.get("text_line", "")


def _paragraph_separator(previous: str, current: str) -> str:
    """Chinese line breaks are directly continued, English words and sentence end punctuation are filled with spaces after line breaks, and existing blanks and hyphens are retained."""
    last, first = previous[-1:], current[:1]
    if not last or not first or not (last.isascii() and first.isascii()):
        return ""
    return " " if (last.isalnum() or last in '.,;:!?)]}"') and (first.isalnum() or first in '(["') else ""


def merge_paragraph_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Within the reading order, paragraphs are combined according to local column width, line spacing and indentation, and visual blocks and auxiliary blocks serve as barriers."""
    output: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if block["type"] != BlockType.TEXT:
            output.append(block)
            continue
        angle = block["angle"]
        current_box = upright_box(block["bbox_mm"], angle)
        size = block["font_size_mm"]
        # Only observe adjacent and horizontally overlapping text to prevent another column or distant areas from affecting the size of this column.
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
            # Copies the list to be appended, ensuring that the group segment does not modify the caller's original visual line.
            output.append({**block, "content": list(block["content"]), "line_bboxes_mm": list(block["line_bboxes_mm"])})
    return output
