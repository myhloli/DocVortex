"""The original import entry is retained; the shared implementation is uniquely maintained by the lower module."""

from docvortex.content.mathml import (
    _GREEK_MAP as _GREEK_MAP,
    _LATEX_ESCAPE_RE as _LATEX_ESCAPE_RE,
    _LATEX_MATH_TOKEN_ESCAPES as _LATEX_MATH_TOKEN_ESCAPES,
    _OPERATOR_MAP as _OPERATOR_MAP,
    _children as _children,
    _convert as _convert,
    _escape_math_token as _escape_math_token,
    _escape_text as _escape_text,
    _join_children as _join_children,
    local_name as local_name,
    mathml_to_latex as mathml_to_latex,
)

__all__ = ["mathml_to_latex"]
