"""Identify and clean up the tail of table of contents page numbers common to all formats."""

from __future__ import annotations

import re

from ....content.inline import inline_plain_text, map_text_span_content, normalize_inline_spans, slice_inline_spans
from ....schema import InlineSpan

_INDEX_ROMAN_RE = re.compile(r"[ivxlcdm]+", re.IGNORECASE)


def strip_index_page_tail(content: list[InlineSpan]) -> list[InlineSpan]:
    """Delete the trusted page number at the end of the directory and convert the remaining tab to normal spaces."""
    content = normalize_inline_spans(content)
    visible_text = inline_plain_text(content)
    if "\t" not in visible_text:
        return content
    tab_offset = visible_text.rfind("\t")
    tail_text = visible_text[tab_offset + 1 :].strip()
    if looks_like_index_page_token(tail_text):
        content = slice_inline_spans(content, 0, tab_offset)
    return map_text_span_content(content, lambda value: value.replace("\t", " "))


def looks_like_index_page_token(content: str) -> bool:
    """Determine whether the suffix of the directory tab is a number, Roman numeral, or single-letter page number."""
    if not content or len(content) > 12:
        return False
    return bool(content.isdigit() or _INDEX_ROMAN_RE.fullmatch(content) or re.fullmatch(r"[A-Za-z]", content))


__all__ = ["looks_like_index_page_token", "strip_index_page_tail"]
