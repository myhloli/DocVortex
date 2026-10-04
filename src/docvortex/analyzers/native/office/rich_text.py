import html
from dataclasses import dataclass
from typing import Any, Optional

from .._shared.hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from ....content.spans import append_hyperlink_span, append_text_span, extend_inline_spans, normalize_span_dicts

VISIBLE_SPACE_STYLES = {"underline", "emphasis", "strikethrough"}


@dataclass(frozen=True)
class OfficeRichTextSegment:
    """Represents Office inline rich text fragment for uniform styling and hyperlink output."""

    text: str
    style: str | list[str] | tuple[str, ...] | None = None
    hyperlink: Optional[str] = None


def _style_list(style: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a style string or list into a style list."""
    if not style:
        return []
    if isinstance(style, str):
        return [item.strip() for item in style.split(",") if item.strip()]
    return [str(item).strip() for item in style if str(item).strip()]


def _style_str(style: str | list[str] | tuple[str, ...] | None) -> Optional[str]:
    """Normalizes a style string or list to a comma-separated string."""
    styles = _style_list(style)
    return ",".join(styles) if styles else None


def _script_to_style_name(format_obj: Any) -> Optional[str]:
    """Convert DOCX superscript and subscript script positions to Office internal rich text style names."""
    script = getattr(format_obj, "script", None)
    script_value = getattr(script, "value", script)
    if script_value == "super":
        return "superscript"
    if script_value == "sub":
        return "subscript"
    return None


def formatting_to_style_str(format_obj: Any) -> Optional[str]:
    """Extracts the Office internal rich text style string from the Formatting-like object."""
    if format_obj is None:
        return None
    styles = []
    if getattr(format_obj, "bold", False):
        styles.append("bold")
    if getattr(format_obj, "italic", False):
        styles.append("italic")
    if getattr(format_obj, "underline", False):
        styles.append("underline")
    if getattr(format_obj, "emphasis", False):
        styles.append("emphasis")
    if getattr(format_obj, "strikethrough", False):
        styles.append("strikethrough")
    script_style = _script_to_style_name(format_obj)
    if script_style:
        styles.append(script_style)
    return ",".join(styles) if styles else None


def has_visible_style(format_obj: Any) -> bool:
    """Determines whether the format contains styles that make blank text visible."""
    if format_obj is None:
        return False
    return bool(
        getattr(format_obj, "underline", False)
        or getattr(format_obj, "emphasis", False)
        or getattr(format_obj, "strikethrough", False)
    )


def has_non_visible_text_style(format_obj: Any) -> bool:
    """Determines whether the format only contains invisible glyph styles for blank text."""
    if format_obj is None:
        return False
    return bool(getattr(format_obj, "bold", False) or getattr(format_obj, "italic", False))


def normalize_format_for_text(
    format_obj: Any,
    text: str,
    *,
    preserve_blank_non_visible_style: bool = False,
) -> Any:
    """Standardize run format according to text content to avoid blank run from accidentally bringing invisible styles to the output."""
    if format_obj is None:
        return None
    if text.strip():
        return format_obj

    updates = {}
    if getattr(format_obj, "underline_style", "") == "words":
        updates["underline"] = False
        updates["underline_style"] = ""
    if has_non_visible_text_style(format_obj) and not preserve_blank_non_visible_style:
        updates["bold"] = False
        updates["italic"] = False

    if updates and hasattr(format_obj, "model_copy"):
        format_obj = format_obj.model_copy(update=updates)

    if not has_visible_style(format_obj):
        if preserve_blank_non_visible_style and has_non_visible_text_style(format_obj):
            return format_obj
        return None
    return format_obj


def should_keep_group_text(
    text: str,
    format_obj: Any,
    *,
    preserve_plain_blank: bool = False,
) -> bool:
    """Determines whether the current accumulated text should be output, preserving visible styles or explicitly preserving whitespace."""
    if not text:
        return False
    if text.strip():
        return True
    if has_visible_style(format_obj):
        return True
    return preserve_plain_blank


def append_rich_text_element(
    paragraph_elements: list[tuple[str, Any, Any]],
    text: str,
    format_obj: Any,
    hyperlink: Any,
) -> None:
    """Append paragraph elements; adjacent fragments with the same URL and the same format are merged into one element."""
    if (
        hyperlink is not None
        and paragraph_elements
        and paragraph_elements[-1][2] is not None
        and str(paragraph_elements[-1][2]) == str(hyperlink)
        and paragraph_elements[-1][1] == format_obj
    ):
        previous_text, previous_format, previous_hyperlink = paragraph_elements[-1]
        paragraph_elements[-1] = (
            f"{previous_text}{text}",
            previous_format,
            previous_hyperlink,
        )
        return
    paragraph_elements.append((text, format_obj, hyperlink))


def format_text_spans(
    text: str,
    hyperlink: Any = None,
    style: str | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Construct Office text, styles, and safe hyperlinks directly into Span."""
    if not text:
        return []
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    children: list[dict[str, Any]] = []
    append_text_span(children, normalized_text, _style_list(style))
    safe_target = sanitize_hyperlink_target(
        hyperlink,
        allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
        allow_relative=True,
        allow_fragment=True,
    )
    if safe_target is None:
        return children
    output: list[dict[str, Any]] = []
    append_hyperlink_span(output, children, safe_target)
    return output


def is_valid_hyperlink_target(hyperlink: Any) -> bool:
    """Determine whether the hyperlink target can be output as a real link."""
    return (
        sanitize_hyperlink_target(
            hyperlink,
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        is not None
    )


def _format_hyperlink_segments(group: list[OfficeRichTextSegment]) -> list[dict[str, Any]]:
    """Constructs multiple consecutive segments of the same URL into a single HyperlinkSpan."""
    if not group:
        return []
    safe_target = sanitize_hyperlink_target(
        group[0].hyperlink,
        allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
        allow_relative=True,
        allow_fragment=True,
    )
    children: list[dict[str, Any]] = []
    for segment in group:
        append_text_span(children, segment.text, _style_list(segment.style))
    if safe_target is None:
        return children
    output: list[dict[str, Any]] = []
    append_hyperlink_span(output, children, safe_target)
    return output


def format_hyperlink_group(
    group: list[tuple[str, Any, Any]],
) -> list[dict[str, Any]]:
    """Construct groups of DOCX paragraph element into a single HyperlinkSpan."""
    return _format_hyperlink_segments(
        [
            OfficeRichTextSegment(
                text=text,
                style=formatting_to_style_str(format_obj),
                hyperlink=str(hyperlink) if hyperlink is not None else None,
            )
            for text, format_obj, hyperlink in group
        ]
    )


def _style_has_visible_space(style: str | list[str] | tuple[str, ...] | None) -> bool:
    """Determines whether the style list will make blank text visible in the rendered result."""
    return any(style_name in VISIBLE_SPACE_STYLES for style_name in _style_list(style))


def _trim_plain_edge_spaces(
    segments: list[OfficeRichTextSegment],
) -> list[OfficeRichTextSegment]:
    """Only the normal white space at the beginning and end of the paragraph will be cut, and the white space with visible styles will not be cut."""
    trimmed_segments = [segment for segment in segments if segment.text is not None]
    if not trimmed_segments:
        return []

    start_idx = 0
    while start_idx < len(trimmed_segments):
        segment = trimmed_segments[start_idx]
        if segment.text.strip() or _style_has_visible_space(segment.style):
            if not _style_has_visible_space(segment.style):
                trimmed_segments[start_idx] = OfficeRichTextSegment(
                    segment.text.lstrip(),
                    segment.style,
                    segment.hyperlink,
                )
            break
        start_idx += 1
    if start_idx == len(trimmed_segments):
        return []

    trimmed_segments = trimmed_segments[start_idx:]
    end_idx = len(trimmed_segments) - 1
    while end_idx >= 0:
        segment = trimmed_segments[end_idx]
        if segment.text.strip() or _style_has_visible_space(segment.style):
            if not _style_has_visible_space(segment.style):
                trimmed_segments[end_idx] = OfficeRichTextSegment(
                    segment.text.rstrip(),
                    segment.style,
                    segment.hyperlink,
                )
            break
        end_idx -= 1
    if end_idx < 0:
        return []
    return trimmed_segments[: end_idx + 1]


def _merge_non_link_segments(
    segments: list[OfficeRichTextSegment],
) -> list[OfficeRichTextSegment]:
    """Merge adjacent non-hyperlink fragments of the same style to avoid outputting fragmented style markup."""
    merged: list[OfficeRichTextSegment] = []
    for segment in segments:
        if (
            merged
            and not is_valid_hyperlink_target(merged[-1].hyperlink)
            and not is_valid_hyperlink_target(segment.hyperlink)
            and _style_str(merged[-1].style) == _style_str(segment.style)
        ):
            previous = merged[-1]
            merged[-1] = OfficeRichTextSegment(
                f"{previous.text}{segment.text}",
                previous.style,
                previous.hyperlink,
            )
            continue
        merged.append(segment)
    return merged


def build_rich_text_from_segments(
    segments: list[OfficeRichTextSegment],
    *,
    trim_plain_edges: bool = False,
) -> list[dict[str, Any]]:
    """Build normalized inline Span directly from Office rich text fragments."""
    normalized_segments = [
        OfficeRichTextSegment(
            segment.text,
            _style_str(segment.style),
            sanitize_hyperlink_target(
                segment.hyperlink,
                allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
                allow_relative=True,
                allow_fragment=True,
            ),
        )
        for segment in segments
        if segment.text is not None and segment.text != ""
    ]
    if trim_plain_edges:
        normalized_segments = _trim_plain_edge_spaces(normalized_segments)
    normalized_segments = _merge_non_link_segments(normalized_segments)

    rendered_spans: list[dict[str, Any]] = []
    index = 0
    while index < len(normalized_segments):
        segment = normalized_segments[index]
        if is_valid_hyperlink_target(segment.hyperlink):
            group = [segment]
            index += 1
            while index < len(normalized_segments):
                next_segment = normalized_segments[index]
                if not is_valid_hyperlink_target(next_segment.hyperlink) or str(next_segment.hyperlink) != str(
                    segment.hyperlink
                ):
                    break
                group.append(next_segment)
                index += 1
            extend_inline_spans(rendered_spans, _format_hyperlink_segments(group))
            continue

        extend_inline_spans(
            rendered_spans,
            format_text_spans(
                segment.text,
                segment.hyperlink,
                segment.style,
            ),
        )
        index += 1

    return normalize_span_dicts(rendered_spans)


def build_spans_from_elements(
    paragraph_elements: list[tuple[str, Any, Any]],
) -> list[dict[str, Any]]:
    """Construct DOCX paragraph element directly into structured Span."""
    return build_rich_text_from_segments(
        [
            OfficeRichTextSegment(
                text=text,
                style=formatting_to_style_str(format_obj),
                hyperlink=str(hyperlink) if hyperlink is not None else None,
            )
            for text, format_obj, hyperlink in paragraph_elements
            if text
        ]
    )


def build_rich_text_html_from_segments(
    segments: list[OfficeRichTextSegment],
    *,
    trim_plain_edges: bool = False,
) -> str:
    """Serialize Office rich text fragment to secure HTML for table cell use."""
    normalized = _trim_plain_edge_spaces(segments) if trim_plain_edges else list(segments)
    parts: list[str] = []
    for segment in normalized:
        if not segment.text:
            continue
        rendered = html.escape(segment.text, quote=False).replace("\r\n", "\n").replace("\r", "\n")
        styles = _style_list(segment.style)
        if "superscript" in styles:
            rendered = f"<sup>{rendered}</sup>"
        elif "subscript" in styles:
            rendered = f"<sub>{rendered}</sub>"
        if "underline" in styles:
            rendered = f"<u>{rendered}</u>"
        if "bold" in styles:
            rendered = f"<strong>{rendered}</strong>"
        if "italic" in styles:
            rendered = f"<em>{rendered}</em>"
        if "strikethrough" in styles:
            rendered = f"<s>{rendered}</s>"
        safe_target = sanitize_hyperlink_target(
            segment.hyperlink,
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        if safe_target:
            rendered = f'<a href="{html.escape(safe_target, quote=True)}">{rendered}</a>'
        parts.append(rendered)
    return "".join(parts)


__all__ = [
    "OfficeRichTextSegment",
    "append_rich_text_element",
    "build_rich_text_from_segments",
    "build_rich_text_html_from_segments",
    "build_spans_from_elements",
    "format_hyperlink_group",
    "format_text_spans",
    "formatting_to_style_str",
    "has_non_visible_text_style",
    "has_visible_style",
    "is_valid_hyperlink_target",
    "normalize_format_for_text",
    "should_keep_group_text",
]
