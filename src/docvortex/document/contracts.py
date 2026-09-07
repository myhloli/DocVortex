"""HTML Flash 解析使用的来源上下文契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from docvortex.foundation.type_identity import preserve_type_module


@dataclass(frozen=True, slots=True)
class HtmlSourceContext:
    """保存相对链接解析及 HTML 解码所需的来源上下文。"""

    source_uri: str | None = None
    local_resource_root: Path | None = None
    transport_encoding: str | None = None


__all__ = ["HtmlSourceContext"]

# 保持既有公开类型的 pickle 路径，所有旧、新入口指向同一个类。
preserve_type_module(HtmlSourceContext, "docvortex.analyzers.native.html.contracts")
