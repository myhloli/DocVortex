"""Lightweight public facade for strictly MiddleJson to PDF bytes."""

from __future__ import annotations

from ..schema import MiddleJson
from .contracts import AssetResolver, PdfLayout


def render_pdf(
    middle_json: MiddleJson,
    *,
    asset_resolver: AssetResolver | None = None,
    document_title: str | None = None,
    layout: PdfLayout = PdfLayout.AUTO,
) -> bytes:
    """Lazy loading PDF implements and renders strict MiddleJson."""
    from ._internal.pdf.renderer import render_pdf as _render_pdf

    return _render_pdf(
        middle_json,
        asset_resolver=asset_resolver,
        document_title=document_title,
        layout=layout,
    )


__all__ = ["render_pdf"]
