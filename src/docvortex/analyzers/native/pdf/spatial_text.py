"""A shared algorithm for projecting PDF characters or OCR results into spatial text."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

import numpy as np

from ....document.pdf.text._contracts import Char
from ....document.pdf.text.spacing import needs_tight_space
from ....schema import BBox
from .table_geometry import normalize_bbox as _coerce_bbox
from .table_geometry import rotate_local_bbox as _rotate_local_bbox

# For spatial projection ideas, refer to the character segmentation and grid projection of LiteParse v2.6.0;
# This module only re-implements the existing table bbox and does not introduce LiteParse runtime dependencies.
_MAX_INLINE_GAP = 15.0
_Y_TOLERANCE = 2.0
_PENDING_SPACE_SPLIT_RATIO = 2.2
_MISSING_SPACE_HEIGHT_RATIO = 0.35
_LINE_HEIGHT_RATIO = 1.8

_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u0002": "-",
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "ft",
        "\ufb06": "st",
    }
)


@dataclass(slots=True)
class _SpatialTextItem:
    """Save the text, bounding box, and recognition confidence in table local coordinates."""

    text: str
    bbox: BBox
    confidence: float = 1.0


def _normalize_table_text(value: Any) -> str:
    """Unify ligatures, control lines, and typographic punctuation in table text."""
    if not isinstance(value, str):
        return ""
    return value.translate(_PUNCTUATION_TRANSLATION)


def _bbox_union(bbox1: BBox, bbox2: BBox) -> BBox:
    """Merge two character boxes to get the bounding box of the current text fragment."""
    return (
        min(bbox1[0], bbox2[0]),
        min(bbox1[1], bbox2[1]),
        max(bbox1[2], bbox2[2]),
        max(bbox1[3], bbox2[3]),
    )


def _normalize_angle(angle: Any) -> int:
    """Limit existing table angles to four standard directions, and illegal values are treated as zero degrees."""
    try:
        normalized_angle = int(float(angle or 0)) % 360
    except (TypeError, ValueError):
        return 0
    return normalized_angle if normalized_angle in {0, 90, 180, 270} else 0


def _select_table_chars(chars: list[Char], table_bbox: BBox) -> list[Char]:
    """Select the characters in the table according to the center point of the character box and maintain the original character flow order."""
    x0, y0, x1, y1 = table_bbox
    selected_chars: list[tuple[int, int, Char]] = []
    for fallback_idx, char in enumerate(chars):
        try:
            char_bbox = [float(value) for value in char.get("bbox", [])]
        except (TypeError, ValueError):
            continue
        if len(char_bbox) != 4:
            continue
        center_x = (char_bbox[0] + char_bbox[2]) / 2.0
        center_y = (char_bbox[1] + char_bbox[3]) / 2.0
        if not (x0 <= center_x <= x1 and y0 <= center_y <= y1):
            continue
        try:
            char_idx = int(char.get("char_idx", fallback_idx))
        except (TypeError, ValueError):
            char_idx = fallback_idx
        selected_chars.append((char_idx, fallback_idx, char))

    selected_chars.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in selected_chars]


def _build_pdf_spatial_items(
    chars: list[Char], table_bbox: BBox, angle: int, *, infer_tight_spaces: bool = False
) -> list[_SpatialTextItem]:
    """Split the PDF character stream into spatial text items based on line breaks, character spacing, and geometric continuity."""
    table_x0, table_y0, table_x1, table_y1 = table_bbox
    table_width = table_x1 - table_x0
    table_height = table_y1 - table_y0
    normalized_angle = _normalize_angle(angle)
    spatial_items: list[_SpatialTextItem] = []

    segment_parts: list[str] = []
    segment_bbox: BBox | None = None
    last_char_bbox: BBox | None = None
    char_widths: list[float] = []
    pending_space = False
    last_source_char = None

    def flush_segment() -> None:
        """Submits the current PDF text fragment and resets the fragment accumulation status."""
        nonlocal segment_parts, segment_bbox, last_char_bbox, char_widths, pending_space, last_source_char
        text = _normalize_table_text("".join(segment_parts)).strip()
        if text and segment_bbox is not None:
            rotated_bbox = _rotate_local_bbox(
                segment_bbox,
                table_width,
                table_height,
                normalized_angle,
            )
            spatial_items.append(_SpatialTextItem(text=text, bbox=rotated_bbox))
        segment_parts = []
        segment_bbox = None
        last_char_bbox = None
        char_widths = []
        pending_space = False
        last_source_char = None

    for char in _select_table_chars(chars, table_bbox):
        raw_char = str(char.get("char") or "")
        if raw_char in {"\r", "\n"}:
            flush_segment()
            continue
        if raw_char.isspace():
            if segment_parts:
                pending_space = True
            continue

        absolute_bbox = _coerce_bbox(char.get("bbox"))
        if absolute_bbox is None:
            continue
        local_bbox = (
            absolute_bbox[0] - table_x0,
            absolute_bbox[1] - table_y0,
            absolute_bbox[2] - table_x0,
            absolute_bbox[3] - table_y0,
        )
        char_height = local_bbox[3] - local_bbox[1]
        if char_height < 0.5:
            continue

        normalized_char = _normalize_table_text(raw_char)
        if not normalized_char:
            continue

        if segment_parts and segment_bbox is not None and last_char_bbox is not None:
            gap = local_bbox[0] - last_char_bbox[2]
            y_overlap = local_bbox[1] < segment_bbox[3] + _Y_TOLERANCE and local_bbox[3] > segment_bbox[1] - _Y_TOLERANCE
            strict_below = local_bbox[1] > last_char_bbox[3]
            segment_width = segment_bbox[2] - segment_bbox[0]
            line_changed = (
                local_bbox[1] > last_char_bbox[3] + _Y_TOLERANCE
                or (strict_below and gap < -5.0)
                or (segment_width > 20.0 and gap < -(segment_width * 0.5))
            )
            average_char_width = sum(char_widths) / len(char_widths)
            should_split = (
                not y_overlap
                or line_changed
                or gap >= _MAX_INLINE_GAP
                or (pending_space and gap > average_char_width * _PENDING_SPACE_SPLIT_RATIO)
            )
            if should_split:
                flush_segment()
            elif pending_space:
                segment_parts.append(" ")
                pending_space = False
            else:
                previous_char = segment_parts[-1][-1] if segment_parts[-1] else ""
                current_char = normalized_char[0]
                if (previous_char.isalnum() and current_char.isalnum() and gap > char_height * _MISSING_SPACE_HEIGHT_RATIO) or (
                    infer_tight_spaces and last_source_char is not None and needs_tight_space(last_source_char, char)
                ):
                    segment_parts.append(" ")

        if segment_bbox is None:
            segment_bbox = local_bbox
        else:
            segment_bbox = _bbox_union(segment_bbox, local_bbox)
        segment_parts.append(normalized_char)
        last_char_bbox = local_bbox
        last_source_char = char
        char_widths.append(local_bbox[2] - local_bbox[0])

    flush_segment()
    return spatial_items


def _build_ocr_spatial_items(ocr_result: Any, table_size: tuple[int, int]) -> list[_SpatialTextItem]:
    """Converts the MinerU OCR four-point box result into a projectable spatial text item."""
    table_width, table_height = table_size
    spatial_items: list[_SpatialTextItem] = []
    for raw_item in ocr_result or []:
        if not raw_item or len(raw_item) < 2:
            continue
        rec_result = raw_item[1]
        if not rec_result or len(rec_result) < 2:
            continue
        text = _normalize_table_text(rec_result[0]).strip()
        if not text:
            continue
        try:
            points = np.asarray(raw_item[0], dtype=np.float32).reshape(-1, 2)
        except (TypeError, ValueError):
            continue
        if points.size == 0 or not np.isfinite(points).all():
            continue
        x0 = max(0.0, float(np.min(points[:, 0])))
        y0 = max(0.0, float(np.min(points[:, 1])))
        x1 = min(float(table_width), float(np.max(points[:, 0])))
        y1 = min(float(table_height), float(np.max(points[:, 1])))
        bbox = _coerce_bbox((x0, y0, x1, y1))
        if bbox is None:
            continue
        try:
            confidence = float(rec_result[1] or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        spatial_items.append(_SpatialTextItem(text=text, bbox=bbox, confidence=confidence))
    return spatial_items


def _compute_text_grid_size(items: list[_SpatialTextItem]) -> tuple[float, float]:
    """Compute a robust median grid size using the average character width and box height of the text item."""
    char_widths = [
        (item.bbox[2] - item.bbox[0]) / max(1, len(item.text)) for item in items if item.bbox[2] > item.bbox[0] and item.text
    ]
    heights = [item.bbox[3] - item.bbox[1] for item in items if item.bbox[3] > item.bbox[1]]
    median_width = statistics.median(char_widths) if char_widths else 1.0
    median_height = statistics.median(heights) if heights else 1.0
    return max(0.1, float(median_width)), max(0.1, float(median_height))


def _form_spatial_lines(
    items: list[_SpatialTextItem],
    median_width: float,
    median_height: float,
) -> list[list[_SpatialTextItem]]:
    """Group text items into visual lines based on the y grid, vertical overlap, and horizontal collision relationships."""
    y_sort_tolerance = max(5.0, median_height * 0.5)
    sorted_items = sorted(
        items,
        key=lambda item: (round(item.bbox[1] / y_sort_tolerance), item.bbox[0]),
    )
    lines: list[list[_SpatialTextItem]] = []
    for item in sorted_items:
        if not lines:
            lines.append([item])
            continue

        current_line = lines[-1]
        line_top = min(line_item.bbox[1] for line_item in current_line)
        line_bottom = max(line_item.bbox[3] for line_item in current_line)
        horizontal_collision = any(
            min(line_item.bbox[2], item.bbox[2]) - max(line_item.bbox[0], item.bbox[0]) > max(5.0, median_width / 3.0)
            for line_item in current_line
        )
        proposed_top = min(line_top, item.bbox[1])
        proposed_bottom = max(line_bottom, item.bbox[3])
        item_center_y = (item.bbox[1] + item.bbox[3]) / 2.0
        vertically_compatible = line_top <= item_center_y <= line_bottom or line_top <= item.bbox[1] <= line_bottom
        if (
            not horizontal_collision
            and vertically_compatible
            and proposed_bottom - proposed_top <= median_height * _LINE_HEIGHT_RATIO
        ):
            current_line.append(item)
        else:
            lines.append([item])

    for line in lines:
        line.sort(key=lambda item: item.bbox[0])
    lines.sort(key=lambda line: min(item.bbox[1] for item in line))
    return lines


def _trim_projected_lines(lines: list[str]) -> str:
    """Clean up end-of-line spaces and common left indentation while preserving internal table column spacing."""
    trimmed_lines = [line.rstrip() for line in lines]
    while trimmed_lines and not trimmed_lines[0]:
        trimmed_lines.pop(0)
    while trimmed_lines and not trimmed_lines[-1]:
        trimmed_lines.pop()
    if not trimmed_lines:
        return ""

    non_empty_lines = [line for line in trimmed_lines if line]
    common_indent = min(len(line) - len(line.lstrip(" ")) for line in non_empty_lines)
    if common_indent:
        trimmed_lines = [line[common_indent:] if line else "" for line in trimmed_lines]
    return "\n".join(trimmed_lines)


def _project_spatial_items(
    items: list[_SpatialTextItem],
    *,
    preserve_blank_rows: bool = False,
) -> str:
    """Projects spatial text items to a constant-width character grid, preserving visible whitespace lines as needed."""
    valid_items = [item for item in items if item.text and item.bbox[2] > item.bbox[0] and item.bbox[3] > item.bbox[1]]
    if not valid_items:
        return ""

    median_width, median_height = _compute_text_grid_size(valid_items)
    spatial_lines = _form_spatial_lines(valid_items, median_width, median_height)
    projected_lines: list[str] = []
    previous_bottom: float | None = None
    for line in spatial_lines:
        line_top = min(item.bbox[1] for item in line)
        if preserve_blank_rows and previous_bottom is not None and line_top - previous_bottom > 1.25 * median_height:
            blank_count = max(
                1,
                round((line_top - previous_bottom) / median_height) - 1,
            )
            projected_lines.extend("" for _ in range(blank_count))
        projected_line = ""
        for item in line:
            target_column = max(0, round(item.bbox[0] / median_width))
            minimum_column = len(projected_line)
            if projected_line and not projected_line.endswith(" "):
                minimum_column += 1
            target_column = max(target_column, minimum_column)
            projected_line += " " * (target_column - len(projected_line))
            projected_line += item.text
        projected_lines.append(projected_line)
        previous_bottom = max(item.bbox[3] for item in line)
    return _trim_projected_lines(projected_lines)


def project_pdf_spatial_text(
    chars: list[Char],
    region_bbox: BBox,
    angle: int = 0,
    *,
    preserve_blank_rows: bool = False,
    infer_tight_spaces: bool = False,
) -> str:
    """Extracts characters from the range specified by PDF and returns spatially projected text, optionally preserving blank lines."""

    normalized_bbox = _coerce_bbox(region_bbox)
    if normalized_bbox is None:
        return ""
    return _project_spatial_items(
        _build_pdf_spatial_items(chars, normalized_bbox, angle, infer_tight_spaces=infer_tight_spaces),
        preserve_blank_rows=preserve_blank_rows,
    )


def project_pdf_table_text(chars: list[Char], table_bbox: BBox, angle: int = 0) -> str:
    """Extracts the specified table from PDF native characters and returns spatially projected plain text."""

    return project_pdf_spatial_text(chars, table_bbox, angle, infer_tight_spaces=True)


def project_ocr_table_text(ocr_result: Any, table_size: tuple[int, int]) -> str:
    """Generates spatially projected plain text of the specified table from the MinerU OCR results."""
    table_width, table_height = table_size
    if table_width <= 0 or table_height <= 0:
        return ""
    return _project_spatial_items(_build_ocr_spatial_items(ocr_result, table_size))
