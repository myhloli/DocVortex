"""HTML wire 物化依赖的资源协议，不依赖具体输入解析器。"""

from __future__ import annotations

from typing import Protocol
from lxml import etree

from ...content.markup import MarkupContext


class WireAnchorResolver(Protocol):
    """描述精确 HTML 文档提供的锚点解析能力。"""

    def resolve_fragment(self, fragment: str) -> str | None:
        """将源 fragment 解析为规范内部链接。"""

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """返回标题的规范锚点。"""

    def heading_label(self, anchor: str) -> str | None:
        """返回标题锚点对应的文字。"""

    def note_anchor(self, note: etree._Element) -> str | None:
        """返回页面脚注的规范锚点。"""


class WireResourceContext(MarkupContext, Protocol):
    """在共享 markup 能力上增加绑定精确 wire 锚点的操作。"""

    def bind_anchors(self, anchors: WireAnchorResolver) -> None:
        """绑定经过整棵 wire 验证的锚点解析器。"""


__all__ = ["WireAnchorResolver", "WireResourceContext"]
