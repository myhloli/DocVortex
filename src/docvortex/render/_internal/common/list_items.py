"""List common to each format marker parsing and reference determination."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, TypeAlias

from ....content.inline import inline_plain_text, normalize_inline_spans, slice_inline_spans
from ....schema import BlockType, InlineSpan, ListBlock

ListItemKind: TypeAlias = Literal["unordered", "ordered", "explicit", "none"]
OrderedListStyle: TypeAlias = Literal["decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman"]

_LIST_ITEM_MARKER_RE = re.compile(
    r"^(?P<leading>\s*)(?P<marker>"
    r"(?P<unordered>[-*+])"
    r"|(?P<ordered>\d+\.|[A-Za-z]\.|[IVXLCDMivxlcdm]{2,}\.)"
    r"|(?P<explicit>\d+\)|\(\d+[.)]|[A-Za-z]\)|[IVXLCDMivxlcdm]{2,}\)|\[[^\]\n]+\])"
    r")(?P<separator>\s+)(?P<body>.*)$",
    re.DOTALL,
)
_LEADING_WHITESPACE_RE = re.compile(r"^[ \t]*")
# After removing the leading blank, the presence of the number Unicode within the first five visible characters is considered a single hit.
_REFERENCE_NUMBER_PREFIX_RE = re.compile(r"^\D{0,4}\d")
_MARKDOWN_UNORDERED_MARKER_RE = re.compile(r"^[ \t]*-[ \t]+")
_ROMAN_MARKER_RE = re.compile(r"[IVXLCDM]+", re.IGNORECASE)
_CANONICAL_ROMAN_RE = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_MAX_NATIVE_ORDERED_VALUE = 1_000_000


@dataclass(frozen=True, slots=True)
class ListItem:
    """Saves a list entry's original tags, text, and HTML required classification."""

    marker: str | None
    body: list[InlineSpan]
    kind: ListItemKind
    value: int | None
    ordered_style: OrderedListStyle | None
    leading: str
    separator: str


def parse_list_item_marker(content: list[InlineSpan]) -> ListItem:
    """marker at the beginning of the parsed list line; leading horizontal blanks are still removed when unrecognizable."""
    content = normalize_inline_spans(content)
    visible_text = inline_plain_text(content)
    match = _LIST_ITEM_MARKER_RE.match(visible_text)
    if match is None:
        leading_match = _LEADING_WHITESPACE_RE.match(visible_text)
        leading = leading_match.group(0) if leading_match is not None else ""
        return ListItem(
            marker=None,
            body=slice_inline_spans(content, len(leading)),
            kind="none",
            value=None,
            ordered_style=None,
            leading=leading,
            separator="",
        )

    marker = match.group("marker")
    if match.group("unordered") is not None:
        kind: ListItemKind = "unordered"
        value = None
        ordered_style = None
    elif match.group("ordered") is not None:
        ordered = _ordered_marker_value(marker)
        if ordered is None:
            kind = "explicit"
            value = None
            ordered_style = None
        else:
            kind = "ordered"
            value, ordered_style = ordered
    else:
        kind = "explicit"
        value = None
        ordered_style = None
    return ListItem(
        marker=marker,
        body=slice_inline_spans(content, match.start("body")),
        kind=kind,
        value=value,
        ordered_style=ordered_style,
        leading=match.group("leading"),
        separator=match.group("separator"),
    )


def _ordered_marker_value(marker: str) -> tuple[int, OrderedListStyle] | None:
    """Convert the bounded and standardized point number marker into a serial number; when it exceeds the limit or is abnormal, it returns None."""
    stem = marker[:-1]
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", stem):
        if len(stem) > 7:
            return None
        value = int(stem)
        return (value, "decimal") if value <= _MAX_NATIVE_ORDERED_VALUE else None
    # The single character i/v/x/l/c/d/m is always interpreted as Roman numerals to eliminate ambiguity with alphabetical numbers.
    if _ROMAN_MARKER_RE.fullmatch(stem):
        if _CANONICAL_ROMAN_RE.fullmatch(stem.upper()) is None:
            return None
        style: OrderedListStyle = "upper-roman" if stem.isupper() else "lower-roman"
        return _roman_marker_value(stem), style
    if re.fullmatch(r"[A-Za-z]", stem) is None:
        return None
    style = "upper-alpha" if stem.isupper() else "lower-alpha"
    return ord(stem.lower()) - ord("a") + 1, style


def _roman_marker_value(marker: str) -> int:
    """Calculate the value of Roman marker according to subtraction notation rules, compatible with loose combinations of producer."""
    total = 0
    previous = 0
    for character in reversed(marker.upper()):
        current = _ROMAN_VALUES[character]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def has_markdown_unordered_marker(content: list[InlineSpan]) -> bool:
    """Determine whether the entry already has Markdown dash marker, and keep the existing supplementary bullet rule."""
    return _MARKDOWN_UNORDERED_MARKER_RE.match(inline_plain_text(normalize_inline_spans(content))) is not None


def reference_list_needs_bullets(block: ListBlock) -> bool:
    """Determine whether to fill the disorder according to the strict majority rule of the numerical prefix of the immediate non-null entry marker."""
    if block.sub_type != BlockType.REF_TEXT:
        return False

    item_count = 0
    numbered_count = 0
    for child in block.content:
        if isinstance(child, ListBlock):
            continue
        visible_text = inline_plain_text(child.content).lstrip()
        if not visible_text:
            continue
        item_count += 1
        if _REFERENCE_NUMBER_PREFIX_RE.match(visible_text):
            numbered_count += 1
    return item_count > 0 and numbered_count * 2 <= item_count


__all__ = [
    "ListItem",
    "ListItemKind",
    "OrderedListStyle",
    "has_markdown_unordered_marker",
    "parse_list_item_marker",
    "reference_list_needs_bullets",
]
