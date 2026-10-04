from __future__ import annotations

from typing import Any


def inline(content: str, *, styles: list[str] | None = None) -> list[dict[str, Any]]:
    """Construct test text into a minimal Middle JSON 2.0 TextSpan list."""
    span: dict[str, Any] = {"type": "text", "content": content}
    if styles:
        span["styles"] = styles
    return [span]


def equation(latex: str) -> dict[str, str]:
    """Construct the inline formula Span for testing."""
    return {"type": "equation_inline", "content": latex}


def code(content: str) -> dict[str, str]:
    """Construct the inline code Span for testing."""
    return {"type": "code_inline", "content": content}


def hyperlink(url: str, content: str) -> dict[str, Any]:
    """Construct hyperlink Span for testing."""
    return {"type": "hyperlink", "url": url, "content": inline(content)}


def inline_text(spans: Any) -> str:
    """Extract the visible text of the raw or Pydantic InlineSpan sequence for assertion reuse."""
    if not isinstance(spans, list):
        return ""
    parts: list[str] = []
    for span in spans:
        if isinstance(span, dict):
            span_type = span.get("type")
            content = span.get("content")
        else:
            span_type = getattr(span, "type", None)
            content = getattr(span, "content", None)
        if str(span_type) == "hyperlink":
            parts.append(inline_text(content))
        elif isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def inline_items(spans: Any) -> list[Any]:
    """Depth-first expands InlineSpan, preserving HyperlinkSpan itself and its child Span."""
    if not isinstance(spans, list):
        return []
    output: list[Any] = []
    for span in spans:
        output.append(span)
        content = span.get("content") if isinstance(span, dict) else getattr(span, "content", None)
        span_type = span.get("type") if isinstance(span, dict) else getattr(span, "type", None)
        if str(span_type) == "hyperlink":
            output.extend(inline_items(content))
    return output


def inline_urls(spans: Any) -> list[str]:
    """Extract all hyperlink targets in the InlineSpan sequence."""
    urls: list[str] = []
    for span in inline_items(spans):
        span_type = span.get("type") if isinstance(span, dict) else getattr(span, "type", None)
        url = span.get("url") if isinstance(span, dict) else getattr(span, "url", None)
        if str(span_type) == "hyperlink" and isinstance(url, str):
            urls.append(url)
    return urls


def visible_content(content: Any) -> str:
    """Unified extraction of visible text from dedicated string content or InlineSpan content."""
    return content if isinstance(content, str) else inline_text(content)


__all__ = [
    "code",
    "equation",
    "hyperlink",
    "inline",
    "inline_items",
    "inline_text",
    "inline_urls",
    "visible_content",
]
