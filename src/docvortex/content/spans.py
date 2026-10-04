"""Flash Each format constructor Middle JSON 2.0 A lightweight tool inline for Span."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..schema import (
    INLINE_STYLE_ORDER,
    CodeInlineSpan,
    EquationInlineSpan,
    HyperlinkSpan,
    InlineSpan,
    TextSpan,
    parse_inline_spans,
)


def append_text_span(output: list[dict[str, Any]], content: str, styles: Iterable[str] = ()) -> None:
    """Append non-empty TextSpan and merge adjacent text of the same style."""
    if not content:
        return
    style_set = set(styles)
    normalized_styles = [style for style in INLINE_STYLE_ORDER if style in style_set]
    if output and output[-1].get("type") == "text" and output[-1].get("styles", []) == normalized_styles:
        output[-1]["content"] = f"{output[-1].get('content', '')}{content}"
        return
    span: dict[str, Any] = {"type": "text", "content": content}
    if normalized_styles:
        span["styles"] = normalized_styles
    output.append(span)


def text_spans(content: str, styles: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Construct a text into zero or one specification TextSpan."""
    output: list[dict[str, Any]] = []
    append_text_span(output, content, styles)
    return output


def append_equation_span(output: list[dict[str, Any]], latex: str) -> None:
    """Append non-blank inline formula Span."""
    normalized = latex.strip()
    if normalized:
        output.append({"type": "equation_inline", "content": normalized})


def append_code_span(output: list[dict[str, Any]], content: str) -> None:
    """Append non-blank inline code Span."""
    if content:
        output.append({"type": "code_inline", "content": content})


def append_hyperlink_span(output: list[dict[str, Any]], children: list[dict[str, Any]], url: str | None) -> None:
    """Add safe hyperlinks; when the target is missing, directly downgrade the tag Span to normal content."""
    if not children:
        return
    normalized_url = (url or "").strip()
    if not normalized_url:
        extend_inline_spans(output, children)
        return
    non_link_children = [child for child in children if child.get("type") != "hyperlink"]
    if not non_link_children:
        return
    output.append({"type": "hyperlink", "url": normalized_url, "content": non_link_children})


def extend_inline_spans(output: list[dict[str, Any]], spans: Iterable[dict[str, Any]]) -> None:
    """Append the Span sequence and merge the same style text at the border."""
    for span in spans:
        if span.get("type") == "text" and isinstance(span.get("content"), str):
            append_text_span(output, str(span["content"]), span.get("styles", []))
        else:
            output.append(span)


def normalize_span_dicts(spans: Iterable[dict[str, Any] | InlineSpan]) -> list[dict[str, Any]]:
    """Strictly validate Span and return a deterministic dictionary list of JSON."""
    parsed = parse_inline_spans(list(spans))
    output: list[dict[str, Any]] = []
    for span in parsed:
        payload = span.model_dump(mode="json")
        if payload.get("type") == "text":
            append_text_span(output, str(payload["content"]), payload.get("styles", []))
        else:
            output.append(payload)
    return output


def inline_span_plain_text(spans: Iterable[dict[str, Any]]) -> str:
    """Extract visible text from the raw Span dictionary."""
    parts: list[str] = []
    for span in spans:
        span_type = span.get("type")
        if span_type in {"text", "equation_inline", "code_inline"}:
            content = span.get("content")
            if isinstance(content, str):
                parts.append(content)
        elif span_type == "hyperlink":
            children = span.get("content")
            if isinstance(children, list):
                parts.append(inline_span_plain_text(child for child in children if isinstance(child, dict)))
    return "".join(parts)


def slice_span_dicts(
    spans: Iterable[dict[str, Any] | InlineSpan], start: int = 0, end: int | None = None
) -> list[dict[str, Any]]:
    """Crop raw Span by visible character offset, preserving styles and links."""
    parsed = parse_inline_spans(list(spans))
    visible_length = len(_typed_plain_text(parsed))
    resolved_start = min(max(start, 0), visible_length)
    resolved_end = visible_length if end is None else min(max(end, resolved_start), visible_length)
    output: list[InlineSpan] = []
    cursor = 0
    for span in parsed:
        span_length = len(_typed_plain_text([span]))
        span_end = cursor + span_length
        overlap_start = max(resolved_start, cursor)
        overlap_end = min(resolved_end, span_end)
        if overlap_start < overlap_end:
            sliced = _slice_typed_span(span, overlap_start - cursor, overlap_end - cursor)
            if sliced is not None:
                output.append(sliced)
        cursor = span_end
        if cursor >= resolved_end:
            break
    return normalize_span_dicts(output)


def strip_span_dicts(spans: Iterable[dict[str, Any] | InlineSpan]) -> list[dict[str, Any]]:
    """Cut the leading and trailing blanks of Span and keep the internal style and link structure."""
    parsed = parse_inline_spans(list(spans))
    visible = _typed_plain_text(parsed)
    start = len(visible) - len(visible.lstrip())
    end = len(visible.rstrip())
    return slice_span_dicts(parsed, start, end)


def _typed_plain_text(spans: Iterable[InlineSpan]) -> str:
    """Extract the visible text of verified Span."""
    parts: list[str] = []
    for span in spans:
        if isinstance(span, (TextSpan, EquationInlineSpan, CodeInlineSpan)):
            parts.append(span.content)
        elif isinstance(span, HyperlinkSpan):
            parts.append(_typed_plain_text(span.content))
    return "".join(parts)


def _slice_typed_span(span: InlineSpan, start: int, end: int) -> InlineSpan | None:
    """Clip a local interval of verified Span."""
    if start >= end:
        return None
    if isinstance(span, TextSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content else None
    if isinstance(span, EquationInlineSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content.strip() else None
    if isinstance(span, CodeInlineSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content else None
    if isinstance(span, HyperlinkSpan):
        children = slice_span_dicts(span.content, start, end)
        parsed_children = parse_inline_spans(children)
        non_links = [child for child in parsed_children if not isinstance(child, HyperlinkSpan)]
        return span.model_copy(update={"content": non_links}) if non_links else None
    return None


__all__ = [
    "append_code_span",
    "append_equation_span",
    "append_hyperlink_span",
    "append_text_span",
    "extend_inline_spans",
    "inline_span_plain_text",
    "normalize_span_dicts",
    "slice_span_dicts",
    "strip_span_dicts",
    "text_spans",
]
