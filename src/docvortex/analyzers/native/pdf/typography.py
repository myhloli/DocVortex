"""Provides font family normalization shared between native text and layout."""

from __future__ import annotations

import re


def _normalized_font_family(
    signature: tuple[str, int] | None,
) -> str | None:
    """Remove the PDF font subset prefix and normalize the font family name for geometric continuation for soft compatibility judgment."""

    if signature is None:
        return None
    name = re.sub(r"^[A-Z]{6}\+", "", signature[0])
    return re.sub(r"[\s_-]+", "", name).casefold() or None


__all__ = ["_normalized_font_family"]
