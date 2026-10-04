"""Publicly share render fragment operations for hosts to compose specialized output without relying on private modules."""

from __future__ import annotations

from ..options import LatexDelimitersConfig
from ..schema import ImagePayloadBlock, InlineSpan
from ._internal.common.list_items import ListItem, ListItemKind, OrderedListStyle


def render_inline_content(content: list[InlineSpan], delimiters: LatexDelimitersConfig) -> str:
    """Encode inline semantics as Markdown, inheriting shared style and formula rules."""
    from ._internal.markdown.inline import render_inline_content as render_value

    return render_value(content, delimiters)


def render_internal_link(label: str, anchor: str) -> str:
    """Construct consistent internal links with shared Markdown renderer."""
    from ._internal.markdown.inline import render_internal_link as render_value

    return render_value(label, anchor)


def resolve_image_source(block: ImagePayloadBlock, asset_base_url: str = "") -> str | None:
    """Image references are parsed according to the priority of existing materials, and files are not written out."""
    from ._internal.markdown.assets import resolve_image_source as resolve_value

    return resolve_value(block, asset_base_url)


def normalize_image_source(source: str) -> str:
    """Normalize asset paths, preserving URL and path handling behavior of shared renderers."""
    from ._internal.markdown.assets import normalize_image_source as normalize_value

    return normalize_value(source)


def format_embedded_html(markup: str, *, asset_base_url: str, delimiters: LatexDelimitersConfig) -> str:
    """Reuse the image address and formula delimiter processing embedded in HTML."""
    from ._internal.markdown.table import format_embedded_html as format_value

    return format_value(markup, asset_base_url=asset_base_url, delimiters=delimiters)


def strip_index_page_tail(content: list[InlineSpan]) -> list[InlineSpan]:
    """Strip the page numbers at the end of the table of contents and maintain inline style and link semantics."""
    from ._internal.common.index import strip_index_page_tail as strip_value

    return strip_value(content)


def parse_list_item_marker(content: list[InlineSpan]) -> ListItem:
    """Parses a list tag and returns a shared immutable item description."""
    from ._internal.common.list_items import parse_list_item_marker as parse_value

    return parse_value(content)


__all__ = [
    "ListItem",
    "ListItemKind",
    "OrderedListStyle",
    "render_inline_content",
    "render_internal_link",
    "resolve_image_source",
    "normalize_image_source",
    "format_embedded_html",
    "strip_index_page_tail",
    "parse_list_item_marker",
]
