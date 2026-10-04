"""Lightweight public facade for strictly MiddleJson to HTML."""

from __future__ import annotations

from ..schema import MiddleJson
from .contracts import RenderMode


def render_html(
    middle_json: MiddleJson,
    *,
    mode: RenderMode = RenderMode.DEFAULT,
    asset_base_url: str = "",
    standalone: bool = True,
    document_title: str | None = None,
) -> str:
    """Lazy loading HTML implements and renders strict MiddleJson."""
    from ._internal.html.renderer import render_html as _render_html

    return _render_html(
        middle_json,
        mode=mode,
        asset_base_url=asset_base_url,
        standalone=standalone,
        document_title=document_title,
    )


__all__ = ["render_html"]
