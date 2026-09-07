"""保留原有导入入口；共享实现由下层模块唯一维护。"""

from docvortex.content.markup.styles import (
    ElementStyle as ElementStyle,
    MarkupStylesheet as MarkupStylesheet,
    TextStyle as TextStyle,
    TextStyleDelta as TextStyleDelta,
    _CSS_COMMENT_RE as _CSS_COMMENT_RE,
    _CSS_IMPORTANT_RE as _CSS_IMPORTANT_RE,
    _ParsedDeclarations as _ParsedDeclarations,
    _SelectorCascade as _SelectorCascade,
    _TEXT_STYLE_FIELDS as _TEXT_STYLE_FIELDS,
    _VISIBILITY_FIELDS as _VISIBILITY_FIELDS,
    _numeric_font_weight as _numeric_font_weight,
    _parse_declarations as _parse_declarations,
    local_name as local_name,
)

__all__ = ["ElementStyle", "MarkupStylesheet", "TextStyle", "TextStyleDelta"]
