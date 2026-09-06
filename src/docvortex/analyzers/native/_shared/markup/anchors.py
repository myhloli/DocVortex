"""保留原有导入入口；共享实现由下层模块唯一维护。"""

from docvortex.content.markup.anchors import (
    AnchorTextNormalization as AnchorTextNormalization,
    AnchorVisibilityScope as AnchorVisibilityScope,
    MarkupAnchorDocument as MarkupAnchorDocument,
    MarkupAnchorPolicy as MarkupAnchorPolicy,
    MarkupAnchorRegistry as MarkupAnchorRegistry,
    MarkupStylesheet as MarkupStylesheet,
    TextStyle as TextStyle,
    _HEADING_TAGS as _HEADING_TAGS,
    _XHTML_WHITESPACE_RE as _XHTML_WHITESPACE_RE,
    _XML_ID as _XML_ID,
    canonical_anchor as canonical_anchor,
    element_id as element_id,
    local_name as local_name,
    visible_element_text as visible_element_text,
    visible_raw_text_with_style as visible_raw_text_with_style,
)

__all__ = [
    "AnchorTextNormalization",
    "AnchorVisibilityScope",
    "MarkupAnchorDocument",
    "MarkupAnchorPolicy",
    "MarkupAnchorRegistry",
    "canonical_anchor",
    "element_id",
    "visible_element_text",
]
