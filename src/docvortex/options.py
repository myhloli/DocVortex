"""Explicit rendering options for independent engines, without reading environment or host configuration."""

from pydantic import BaseModel, Field


class LatexDelimiterConfig(BaseModel):
    """Single group LaTeX left and right delimiter configuration."""

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)


def _default_display_latex_delimiter() -> LatexDelimiterConfig:
    """Constructs the default interline formula delimiter."""
    return LatexDelimiterConfig(left="$$", right="$$")


def _default_inline_latex_delimiter() -> LatexDelimiterConfig:
    """Constructs the default inline formula delimiter."""
    return LatexDelimiterConfig(left="$", right="$")


class LatexDelimitersConfig(BaseModel):
    """Markdown Inline and interline formula delimiter configuration."""

    display: LatexDelimiterConfig = Field(default_factory=_default_display_latex_delimiter)
    inline: LatexDelimiterConfig = Field(default_factory=_default_inline_latex_delimiter)


__all__ = ["LatexDelimiterConfig", "LatexDelimitersConfig"]
