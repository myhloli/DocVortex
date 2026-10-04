"""Strictly a lightweight public facade for MiddleJson to TeX Live LaTeX source code."""

from __future__ import annotations

from ..schema import MiddleJson


def render_latex(
    middle_json: MiddleJson,
    *,
    asset_base_path: str = "",
    document_title: str | None = None,
) -> str:
    """Lazy loading of LaTeX implementation and return of complete UTF-8 document source code."""
    from ._internal.latex.renderer import render_latex as _render_latex

    return _render_latex(
        middle_json,
        asset_base_path=asset_base_path,
        document_title=document_title,
    )


__all__ = ["render_latex"]
