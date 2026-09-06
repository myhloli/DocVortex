"""DocVortex HTML v1 canonical wire 的轻量内部入口。"""

from __future__ import annotations

from lxml import etree  # type: ignore[reportMissingImports]

from ...analyzers.native.html.resources import HtmlResourceContext
from .contracts import DOCVORTEX_HTML_VERSION, WireDecodeResult


def decode_docvortex_html_wire(body: etree._Element, resources: HtmlResourceContext) -> WireDecodeResult:
    """只对 canonical v1 wire 精确解码，其余输入返回通用投影信号。"""
    from .materializer import materialize_docvortex_html_wire
    from .parser import parse_docvortex_html_wire

    plan, fallback_reason = parse_docvortex_html_wire(body)
    if plan is None:
        return WireDecodeResult(None, fallback_reason)
    return WireDecodeResult(materialize_docvortex_html_wire(plan, resources))


__all__ = ["DOCVORTEX_HTML_VERSION", "WireDecodeResult", "decode_docvortex_html_wire"]
