"""Lightweight public facade for strictly MiddleJson to EPUB 3.3."""

from __future__ import annotations

from datetime import datetime

from ..schema import MiddleJson
from .contracts import AssetResolver


def render_epub(
    middle_json: MiddleJson,
    *,
    title: str | None = None,
    authors: tuple[str, ...] = (),
    language: str = "und",
    identifier: str | None = None,
    modified_at: datetime | None = None,
    asset_resolver: AssetResolver | None = None,
) -> bytes:
    """Lazy-load the EPUB implementation and return the full EPUB 3.3 container bytes."""
    from ._internal.epub.renderer import render_epub as _render_epub

    return _render_epub(
        middle_json,
        title=title,
        authors=authors,
        language=language,
        identifier=identifier,
        modified_at=modified_at,
        asset_resolver=asset_resolver,
    )


__all__ = ["render_epub"]
