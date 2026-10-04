"""Strictly a lightweight public facade with stable exceptions from MiddleJson to DOCX."""

from __future__ import annotations

from ..schema import MiddleJson
from .contracts import AssetResolver


class DocxRenderError(RuntimeError):
    """Indicates that renderer cannot generate DOCX without losing necessary content."""

    def __init__(
        self,
        message: str,
        *,
        page_idx: int,
        block_index: int | None,
        block_type: str,
    ) -> None:
        """Save error message and stable page/block location field."""
        self.page_idx = page_idx
        self.block_index = block_index
        self.block_type = block_type
        super().__init__(f"{message} (page_idx={page_idx}, block_index={block_index}, block_type={block_type})")


def render_docx(
    middle_json: MiddleJson,
    *,
    asset_resolver: AssetResolver | None = None,
) -> bytes:
    """Lazy loading DOCX implements and renders strict MiddleJson."""
    from ._internal.docx.renderer import render_docx as _render_docx

    return _render_docx(middle_json, asset_resolver=asset_resolver)


__all__ = ["DocxRenderError", "render_docx"]
