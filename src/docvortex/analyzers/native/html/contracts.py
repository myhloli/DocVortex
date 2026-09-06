"""保留原有导入入口；共享实现由下层模块唯一维护。"""

from docvortex.document.contracts import (
    HtmlSourceContext as HtmlSourceContext,
)

__all__ = ["HtmlSourceContext"]
