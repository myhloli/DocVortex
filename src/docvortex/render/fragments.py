"""公开共享渲染片段操作，供宿主组合专用输出而不依赖私有模块。"""

from __future__ import annotations

from ..options import LatexDelimitersConfig
from ..schema import ImagePayloadBlock, InlineSpan
from ._internal.common.list_items import ListItem, ListItemKind, OrderedListStyle


def render_inline_content(content: list[InlineSpan], delimiters: LatexDelimitersConfig) -> str:
    """将行内语义编码为 Markdown，沿用共享的样式与公式规则。"""
    from ._internal.markdown.inline import render_inline_content as render_value

    return render_value(content, delimiters)


def render_internal_link(label: str, anchor: str) -> str:
    """构造与共享 Markdown renderer 一致的内部链接。"""
    from ._internal.markdown.inline import render_internal_link as render_value

    return render_value(label, anchor)


def resolve_image_source(block: ImagePayloadBlock, asset_base_url: str = "") -> str | None:
    """按既有素材优先级解析图片引用，不写出文件。"""
    from ._internal.markdown.assets import resolve_image_source as resolve_value

    return resolve_value(block, asset_base_url)


def normalize_image_source(source: str) -> str:
    """规范化素材路径，保持共享渲染器的 URL 与路径处理行为。"""
    from ._internal.markdown.assets import normalize_image_source as normalize_value

    return normalize_value(source)


def format_embedded_html(markup: str, *, asset_base_url: str, delimiters: LatexDelimitersConfig) -> str:
    """复用嵌入 HTML 的图片地址和公式分隔符处理。"""
    from ._internal.markdown.table import format_embedded_html as format_value

    return format_value(markup, asset_base_url=asset_base_url, delimiters=delimiters)


def strip_index_page_tail(content: list[InlineSpan]) -> list[InlineSpan]:
    """剥离目录尾部页码，保持行内样式和链接语义。"""
    from ._internal.common.index import strip_index_page_tail as strip_value

    return strip_value(content)


def parse_list_item_marker(content: list[InlineSpan]) -> ListItem:
    """解析列表标记并返回共享的不可变条目描述。"""
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
