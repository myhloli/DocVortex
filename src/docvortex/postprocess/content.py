"""raw block Cleanup rules for text, code, and formula content."""

from __future__ import annotations

import re
from typing import Any

from ..content.inline import map_text_span_content, normalize_inline_spans


def code_content_clean(content: str | None) -> str:
    """Remove the outer Markdown fence of the code block and retain the code body."""
    if not content:
        return ""
    lines = content.splitlines()
    start_idx = 1 if lines and lines[0].startswith("```") else 0
    end_idx = len(lines)
    if lines and end_idx > start_idx and lines[end_idx - 1].strip() == "```":
        end_idx -= 1
    if start_idx < end_idx:
        return "\n".join(lines[start_idx:end_idx]).strip()
    return ""


def clean_content(content: str | None) -> str | None:
    """Changed paired interline formula delimiters to square brackets compatible with text sanitization."""
    if content and content.count("\\[") == content.count("\\]") and content.count("\\[") > 0:

        def replace_pattern(match: re.Match[str]) -> str:
            """Replaces a single pair of formula fragments."""
            return f"[{match.group(1)}]"

        content = re.sub(r"\\\[(.*?)\\\]", replace_pattern, content)
    return content


def clean_inline_content(content: Any) -> list[dict[str, Any]]:
    """Strictly normalizes the raw Span list and returns a JSON dictionary that can be post-processed further."""
    if content is None:
        return []
    if not isinstance(content, list):
        raise TypeError("inline content must be a list of spans")
    spans = normalize_inline_spans(content)
    return [span.model_dump(mode="json") for span in spans]


def collapse_inline_newlines(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converge newlines and subsequent whitespace in header Span to a single space."""
    spans = map_text_span_content(normalize_inline_spans(content), lambda value: re.sub(r"\n\s*", " ", value))
    return [span.model_dump(mode="json") for span in spans]


__all__ = ["clean_content", "clean_inline_content", "code_content_clean", "collapse_inline_newlines"]
