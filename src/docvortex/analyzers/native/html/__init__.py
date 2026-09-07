"""HTML 静态 Flash 解析实现。"""

from docvortex.document.contracts import HtmlSourceContext
from .errors import HtmlParseError, HtmlResourceLimitError

__all__ = ["HtmlParseError", "HtmlResourceLimitError", "HtmlSourceContext"]
