# Copyright (c) Opendatalab. All rights reserved.
"""严格 MiddleJson 到 Markdown 的轻量公共门面。"""

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
    """惰性加载 Markdown 实现并渲染严格 MiddleJson。"""
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
    """为文档库等局部读取调用方渲染单个顶层块，不执行文档级合并。"""
    from ._internal.markdown.blocks import render_single_block as render_block

    return render_block(block, delimiters=delimiters, asset_base_url=asset_base_url, image_renderer=image_renderer)


def build_markdown_image(source: str, alt: str = "") -> str:
    """通过公开接口构造转义后的图片引用，避免调用方依赖 renderer 私有文件。"""
    from ._internal.markdown.assets import build_markdown_image as build_image

    return build_image(source, alt)


__all__ = ["render_markdown", "render_single_block", "build_markdown_image"]
