"""Lightweight public facade for strictly MiddleJson to Markdown."""

from __future__ import annotations

from ..options import LatexDelimitersConfig
from ..schema import MiddleJson, PageBlock
from .contracts import ImageRenderer, RenderMode


def render_markdown(
    middle_json: MiddleJson,
    *,
    mode: RenderMode = RenderMode.DEFAULT,
    asset_base_url: str = "",
    image_renderer: ImageRenderer | None = None,
    latex_delimiters: LatexDelimitersConfig | None = None,
) -> str:
    """Lazy loading Markdown implements and renders strict MiddleJson."""
    from ._internal.markdown.renderer import render_markdown as _render_markdown

    return _render_markdown(
        middle_json,
        latex_delimiters=latex_delimiters,
        mode=mode,
        asset_base_url=asset_base_url,
        image_renderer=image_renderer,
    )


def render_single_block(
    block: PageBlock,
    *,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None = None,
) -> str:
    """Renders a single top-level block for local read callers such as document libraries, and does not perform document-level merging."""
    from ._internal.markdown.blocks import render_single_block as render_block

    return render_block(block, delimiters=delimiters, asset_base_url=asset_base_url, image_renderer=image_renderer)


def build_markdown_image(source: str, alt: str = "") -> str:
    """Construct the escaped image reference through the public interface to avoid the caller relying on the renderer private file."""
    from ._internal.markdown.assets import build_markdown_image as build_image

    return build_image(source, alt)


__all__ = ["render_markdown", "render_single_block", "build_markdown_image"]
