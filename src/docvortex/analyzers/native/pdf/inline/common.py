"""Character and geometric normalization primitives that provide inline evidence sharing."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Iterable, cast

from .....schema import BBox
from ....._compute_backend import get_native
from .types import (
    _LIGATURE_REPLACEMENTS,
    _PDF_CONTROL_CHAR_RE,
    _PDF_SEPARATOR_SPACE_CHARS,
    _PDF_ZERO_WIDTH_CHARS,
    PDF_TEXT_STYLE_ORDER,
    PDFTextStyle,
    PDFTextStyleLine,
)


def _style_line_reading_order_key(
    line: PDFTextStyleLine,
) -> tuple[int, float, float]:
    """Prioritize using the native source_index sorting, and then use bbox to maintain stability when repeating the index."""

    return line.source_index, line.bbox[1], line.bbox[0]


def _coerce_bbox(value: Any) -> BBox | None:
    """Convergence of list, tuple or pdftext bbox objects to legal finite bbox."""

    raw_bbox = getattr(value, "bbox", value)
    try:
        if raw_bbox is None or len(raw_bbox) != 4:
            return None
        bbox = tuple(float(item) for item in raw_bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in bbox):
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox  # type: ignore[return-value]


def _ordered_line_chars(line: Any) -> list[dict[str, Any]]:
    """Fix unexpected out-of-order characters by char_idx while preserving source order when index is missing."""

    chars = [char for char in getattr(line, "chars", []) if isinstance(char, dict)]
    indexed_chars = [char.get("char_idx") for char in chars]
    if (
        chars
        and all(isinstance(index, int) for index in indexed_chars)
        and any(first > second for first, second in zip(indexed_chars, indexed_chars[1:]))
    ):
        return sorted(chars, key=lambda char: int(char["char_idx"]))
    return chars


def _normalize_match_fragment(value: Any) -> str:
    """Normalizes a single character fragment into a deterministic match of text that ignores typographical whitespace."""
    text = str(value or "")
    if get_native() is not None and len(text) <= 8:
        return _cached_match_fragment(text)
    return _normalize_match_text(text)


@lru_cache(maxsize=4096)
def _cached_match_fragment(text: str) -> str:
    """Short Unicode signature that caches native batch boundaries, without caching entire rows, pages, or object addresses."""
    return _normalize_match_text(text)


def _normalize_match_text(text: str) -> str:
    """Compute matching text using existing Python Unicode rules for reuse with reference and cache paths."""

    output: list[str] = []
    for char in text:
        if char in _PDF_ZERO_WIDTH_CHARS or char == "\u00ad":
            continue
        if char == "\x02":
            output.append("-")
            continue
        if char.isspace() or char in _PDF_SEPARATOR_SPACE_CHARS:
            continue
        if _PDF_CONTROL_CHAR_RE.fullmatch(char):
            continue
        output.append(_LIGATURE_REPLACEMENTS.get(char, char))
    return "".join(output)


def _canonical_styles(styles: Iterable[str]) -> tuple[PDFTextStyle, ...]:
    """Filter, deduplicate, and normalize style collections by public rich text protocol order."""

    style_set = set(styles)
    return cast(
        tuple[PDFTextStyle, ...],
        tuple(style for style in PDF_TEXT_STYLE_ORDER if style in style_set),
    )


def _bbox_intersection_area(first: BBox, second: BBox) -> float:
    """Returns the intersection area of two legal bboxs."""

    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _bbox_overlap_ratio(first: BBox, second: BBox) -> float:
    """Returns the proportion of the area of first that falls within second."""

    intersection_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    intersection_height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    first_area = max(0.01, (first[2] - first[0]) * (first[3] - first[1]))
    return intersection_width * intersection_height / first_area


__all__ = [
    "_style_line_reading_order_key",
    "_coerce_bbox",
    "_ordered_line_chars",
    "_normalize_match_fragment",
    "_canonical_styles",
    "_bbox_intersection_area",
    "_bbox_overlap_ratio",
]
