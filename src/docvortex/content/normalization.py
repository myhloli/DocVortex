"""PDF Visible text cleaning of model output; original characters, layout evidence, and other input formats are not processed here."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from ..foundation._text import full_to_half_exclude_marks
from ..schema import RAW_CAPTION, RAW_FOOTNOTE, RAW_PHONETIC, BlockType

_FULLWIDTH_MODEL_TEXT = re.compile("[Ａ-Ｚａ-ｚ０-９：．／＼－＿％＋＝＠＃＆＊]")
_PDF_SYMBOL_TRANSLATION = str.maketrans(
    {
        "：": ":",
        "．": ".",
        "／": "/",
        "＼": "\\",
        "－": "-",
        "＿": "_",
        "％": "%",
        "＋": "+",
        "＝": "=",
        "＠": "@",
        "＃": "#",
        "＆": "&",
        "＊": "*",
    }
)
_FORMULA_OPENING = re.compile(r"\\[\(\[]")
_NATURAL_LANGUAGE_TYPES = frozenset(
    {
        BlockType.TEXT,
        BlockType.DOC_TITLE,
        BlockType.PARAGRAPH_TITLE,
        BlockType.ASIDE_TEXT,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.PAGE_FOOTNOTE,
        BlockType.REF_TEXT,
        BlockType.LIST,
        BlockType.INDEX,
        BlockType.IMAGE_CAPTION,
        BlockType.IMAGE_FOOTNOTE,
        BlockType.TABLE_CAPTION,
        BlockType.TABLE_FOOTNOTE,
        BlockType.CHART_CAPTION,
        BlockType.CHART_FOOTNOTE,
        BlockType.CODE_CAPTION,
        BlockType.CODE_FOOTNOTE,
        RAW_CAPTION,
        RAW_FOOTNOTE,
        RAW_PHONETIC,
    }
)
_OPAQUE_HTML_TAGS = frozenset(
    {"eq", "math", "pre", "code", "script", "style", "svg", "template", "textarea", "object", "embed", "canvas", "iframe"}
)


def _normalize_plain_text(content: str) -> str:
    """Converts alphanumeric and PDF symbol whitelists according to a one-to-one code point mapping, without changing the contract of the universal alphanumeric tool."""
    return full_to_half_exclude_marks(content).translate(_PDF_SYMBOL_TRANSLATION)


def _normalize_text(content: str) -> str:
    """Convert alphanumeric and whitelist symbols outside the formula range; unclosed formulas are protected to the end of the logical text field."""
    if not _FULLWIDTH_MODEL_TEXT.search(content):
        return content
    parts: list[str] = []
    cursor = 0
    while match := _FORMULA_OPENING.search(content, cursor):
        parts.append(_normalize_plain_text(content[cursor : match.start()]))
        closing = r"\)" if match.group() == r"\(" else r"\]"
        end = content.find(closing, match.end())
        if end < 0:
            parts.append(content[match.start() :])
            return "".join(parts)
        cursor = end + len(closing)
        parts.append(content[match.start() : cursor])
    parts.append(_normalize_plain_text(content[cursor:]))
    return "".join(parts)


def _normalize_parts(parts: Sequence[str]) -> list[str]:
    """First identify the cross-node formula, and then divide the nodes back according to the original length to maintain the boundaries of styles and links."""
    source = "".join(parts)
    normalized = _normalize_text(source)
    if source == normalized:
        return list(parts)
    # Alphanumeric and symbol mapping is always one-to-one; do not use overall Unicode normalization that may extend characters.
    result: list[str] = []
    offset = 0
    for part in parts:
        result.append(normalized[offset : offset + len(part)])
        offset += len(part)
    return result


def _span_parts(spans: list[Any]) -> Iterator[tuple[dict[str, Any] | None, str]]:
    """Recursively traverse visible TextSpan; do not read URL, and isolate the formula and code payload with an invisible barrier."""
    for span in spans:
        if not isinstance(span, dict):
            yield None, "\0"
            continue
        content = span.get("content")
        if span.get("type") == "text" and isinstance(content, str):
            yield span, content
        elif span.get("type") == "hyperlink" and isinstance(content, list):
            yield from _span_parts(content)
        else:
            yield None, "\0"


def _normalize_spans(spans: list[Any]) -> None:
    """Only the content of the text leaf is updated, neither rebuilding Span nor merging adjacent style fragments."""
    entries = list(_span_parts(spans))
    normalized = _normalize_parts([text for _span, text in entries])
    for (span, original), replacement in zip(entries, normalized):
        if span is not None and replacement != original:
            span["content"] = replacement


def _is_opaque_html_node(node: Any) -> bool:
    """Recognize existing HTML formulas, codes, and non-text vectors, keeping their content and properties intact."""
    name = str(node.name).split(":")[-1].lower()
    return (
        name in _OPAQUE_HTML_TAGS
        or node.get("data-block-type") in {"equation", "code", "code_body", "algorithm", "algorithm_body"}
        or node.has_attr("data-docvortex-latex")
        or node.has_attr("data-formula-display")
        or "docvortex-math" in (node.get("class") or [])
    )


def _normalize_table(markup: str) -> str:
    """Only the visible text node of the cell is modified; if there is no actual change, the original HTML byte representation is retained."""
    if not _FULLWIDTH_MODEL_TEXT.search(markup) and "&#" not in markup:
        return markup
    # Keeping the same HTML parser as existing table handling, exposing module imports does not trigger HTML dependencies.
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(markup, "html.parser")
    changed = False

    def cell_parts(node: Any) -> Iterator[tuple[NavigableString | None, str]]:
        """Style labels remain transparent, nested tables are handled by their own cells, and line breaks and loads cannot spell out delimiters."""
        if type(node) is NavigableString:
            yield node, str(node)
        elif isinstance(node, Tag):
            if _is_opaque_html_node(node) or node.name in {"table", "br", "hr", "img"}:
                yield None, "\0"
            else:
                for child in node.children:
                    yield from cell_parts(child)

    for cell in soup.find_all(["td", "th"]):
        if any(isinstance(parent, Tag) and _is_opaque_html_node(parent) for parent in (cell, *cell.parents)):
            continue
        entries = [part for child in cell.children for part in cell_parts(child)]
        normalized = _normalize_parts([text for _node, text in entries])
        for (node, original), replacement in zip(entries, normalized):
            if node is not None and replacement != original:
                node.replace_with(NavigableString(replacement))
                changed = True
    return str(soup) if changed else markup


def normalize_pdf_model_text(model_list: list[list[dict[str, Any]]]) -> None:
    """Unify PDF natural language and tables with visible alphanumeric and whitelist symbols, retaining formulas, codes, URL and structures.

    Should be called after the matching of styles, superscripts, subscripts and links is completed and before the construction of ModelJson; the function is idempotent and does not modify
    Raw character evidence, nor converting strings to Span or cleaning other model metadata.
    """
    for page in model_list:
        for block in page:
            kind, content = block.get("type"), block.get("content")
            if kind in _NATURAL_LANGUAGE_TYPES:
                if isinstance(content, str):
                    block["content"] = _normalize_text(content)
                elif isinstance(content, list):
                    _normalize_spans(content)
            elif kind in {BlockType.TABLE, BlockType.TABLE_BODY} and isinstance(content, str):
                block["content"] = _normalize_table(content)


__all__ = ["normalize_pdf_model_text"]
