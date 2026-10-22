"""PDF 字符到文本片段及行的共享纯数据接口。"""

from __future__ import annotations

from ._contracts import Bbox, Char, Line, Span


def get_lines_from_chars(
    chars: list[Char], superscript_height_threshold: float = 0.7, line_distance_threshold: float = 0.1
) -> list[Line]:
    """由原生连续分组核生成文本行，Python 仅规范化公开数值参数。"""
    from ...._compute_backend import get_native

    result = get_native().group_text_lines(list(chars), float(superscript_height_threshold), float(line_distance_threshold))
    if result is None:
        raise ValueError("Invalid character records for native text grouping")
    return result


__all__ = ["Bbox", "Char", "Line", "Span", "get_lines_from_chars"]
