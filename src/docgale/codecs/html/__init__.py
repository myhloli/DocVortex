# Copyright (c) Opendatalab. All rights reserved.
"""DocGale HTML v1 canonical wire 的轻量内部入口。"""

from __future__ import annotations

from lxml import etree  # type: ignore[reportMissingImports]

from ...analyzers.native.html.resources import HtmlResourceContext
from .contracts import DOCGALE_HTML_VERSION, WireDecodeResult


def decode_docgale_html_wire(body: etree._Element, resources: HtmlResourceContext) -> WireDecodeResult:
    """只对 canonical v1 wire 精确解码，其余输入返回通用投影信号。"""
    from .materializer import materialize_docgale_html_wire
    from .parser import parse_docgale_html_wire

    plan, fallback_reason = parse_docgale_html_wire(body)
    if plan is None:
        return WireDecodeResult(None, fallback_reason)
    return WireDecodeResult(materialize_docgale_html_wire(plan, resources))


__all__ = ["DOCGALE_HTML_VERSION", "WireDecodeResult", "decode_docgale_html_wire"]
