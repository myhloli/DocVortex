"""PDF Shared pure data interface from characters to text fragments and lines."""

from __future__ import annotations

from ._contracts import Bbox, Char, Line, Span
from .groups import assign_scripts, get_lines


def get_spans(chars: list[Char], superscript_height_threshold: float = 0.8, line_distance_threshold: float = 0.1) -> list[Span]:
    """Build fragments directly from own character records, avoiding container and array round-trip conversions."""
    spans: list[Span] = []
    for char in chars:
        current = spans[-1] if spans else None
        box = char["bbox"]
        new_span = current is None
        if current is not None:
            previous = current["chars"][-1]
            height = current["bbox"].height
            new_span = (
                char["font"] != current["font"]
                or char["rotation"] != current["rotation"]
                or previous["char"] in {"\x02", "\n"}
                or (
                    box.y_start < current["bbox"].y_start - height * line_distance_threshold
                    and box.y_end < height * superscript_height_threshold + current["bbox"].y_start
                    and box.x_start > current["bbox"].x_end
                )
            )
        if new_span:
            spans.append(
                {
                    "bbox": box.copy(),
                    "text": char["char"],
                    "font": char["font"],
                    "chars": [char],
                    "char_start_idx": char["char_idx"],
                    "char_end_idx": char["char_idx"],
                    "rotation": char["rotation"],
                    "url": "",
                    "superscript": False,
                    "subscript": False,
                }
            )
        else:
            current["bbox"].merge_inplace(box)
            current["text"] += char["char"]
            current["chars"].append(char)
            current["char_end_idx"] = char["char_idx"]
    return spans


def get_lines_from_chars(
    chars: list[Char], superscript_height_threshold: float = 0.7, line_distance_threshold: float = 0.1
) -> list[Line]:
    """Generates base text lines from materialized characters without accessing PDFium or the source document."""
    from ...._compute_backend import get_native

    native = get_native()
    if (
        native is not None
        and type(chars) is list
        and type(superscript_height_threshold) is float
        and type(line_distance_threshold) is float
    ):
        result = native.group_text_lines(chars, superscript_height_threshold, line_distance_threshold)
        if result is not None:
            return result
    return _get_lines_from_chars_python(chars, superscript_height_threshold, line_distance_threshold)


def _get_lines_from_chars_python(
    chars: list[Char], superscript_height_threshold: float = 0.7, line_distance_threshold: float = 0.1
) -> list[Line]:
    """The pure Python continuous stage is reserved for use in back-end differential and compatible environments."""
    spans = get_spans(chars, superscript_height_threshold, line_distance_threshold)
    lines = get_lines(spans)
    assign_scripts(lines, superscript_height_threshold, line_distance_threshold)
    return lines


__all__ = ["Bbox", "Char", "Line", "Span", "get_lines_from_chars"]
