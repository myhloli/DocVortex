"""DocVortex HTML v1 canonical Lightweight interior entry for wire."""

from __future__ import annotations

from lxml import etree  # type: ignore[reportMissingImports]

from .resources import WireResourceContext
from .contracts import DOCVORTEX_HTML_VERSION, WireDecodeResult


def decode_docvortex_html_wire(body: etree._Element, resources: WireResourceContext) -> WireDecodeResult:
    """Only canonical v1 wire is accurately decoded, the rest of the inputs return a universal projection signal."""
    from .materializer import materialize_docvortex_html_wire
    from .parser import parse_docvortex_html_wire

    plan, fallback_reason = parse_docvortex_html_wire(body)
    if plan is None:
        return WireDecodeResult(None, fallback_reason)
    return WireDecodeResult(materialize_docvortex_html_wire(plan, resources))


__all__ = ["DOCVORTEX_HTML_VERSION", "WireDecodeResult", "decode_docvortex_html_wire"]
