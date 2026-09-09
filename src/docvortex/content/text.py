"""共享自然语言连接、字符清洗和公式编号文本规则。"""

from ..foundation._text import (
    build_tagged_formula_content,
    clean_isolated_formula,
    full_to_half,
    full_to_half_exclude_marks,
    is_hyphen_at_line_end,
    merge_text_line_contents,
    normalize_formula_content_for_tag,
    normalize_formula_tag_content,
    resolve_text_line_boundary,
)

__all__ = [
    "is_hyphen_at_line_end",
    "resolve_text_line_boundary",
    "merge_text_line_contents",
    "full_to_half_exclude_marks",
    "full_to_half",
    "clean_isolated_formula",
    "normalize_formula_tag_content",
    "normalize_formula_content_for_tag",
    "build_tagged_formula_content",
]
