"""独立引擎的显式渲染选项，不读取环境或宿主配置。"""
from pydantic import BaseModel, Field

class LatexDelimiterConfig(BaseModel):
    """单组 LaTeX 左右定界符配置。"""

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)

def _default_display_latex_delimiter() -> LatexDelimiterConfig:
    """构造缺省行间公式定界符。"""
    return LatexDelimiterConfig(left="$$", right="$$")

def _default_inline_latex_delimiter() -> LatexDelimiterConfig:
    """构造缺省行内公式定界符。"""
    return LatexDelimiterConfig(left="$", right="$")

class LatexDelimitersConfig(BaseModel):
    """Markdown 行内与行间公式定界符配置。"""

    display: LatexDelimiterConfig = Field(default_factory=_default_display_latex_delimiter)
    inline: LatexDelimiterConfig = Field(default_factory=_default_inline_latex_delimiter)

__all__ = ["LatexDelimiterConfig", "LatexDelimitersConfig"]
