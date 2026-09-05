# Copyright (c) Opendatalab. All rights reserved.
"""严格 MiddleJson 到 Content List V2 的轻量公共门面。"""

from __future__ import annotations

from typing import Any

from ..options import LatexDelimitersConfig
from ..schema import MiddleJson


def render_content_list_v2(
    middle_json: MiddleJson,
    *,
    asset_base_url: str = "",
    latex_delimiters: LatexDelimitersConfig | None = None,
) -> list[list[dict[str, Any]]]:
    """惰性加载 Content List V2 实现并渲染严格 MiddleJson。"""
    from ._internal.content_list.v2 import render_content_list_v2 as _render_content_list_v2

    return _render_content_list_v2(middle_json, asset_base_url=asset_base_url)


__all__ = ["render_content_list_v2"]
