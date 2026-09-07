"""验证中性协议和独立的渲染后半程。"""

from __future__ import annotations

import pytest

from docvortex.codecs.json import load_middle
from docvortex.options import LatexDelimiterConfig, LatexDelimitersConfig
from docvortex.render import RenderFormat, render
from docvortex.render.markdown import render_markdown
from docvortex.schema import BlockType, EquationBlock, MiddleJson, PageInfo, TextBlock, TextSpan


def document() -> MiddleJson:
    """建立不携带宿主信息的最小语义文档。"""
    return MiddleJson(
        pages=[
            PageInfo(
                page_idx=0,
                blocks=[
                    TextBlock(type=BlockType.TEXT, index=0, content=[TextSpan(type="text", content="Hello 文档")]),
                    EquationBlock(type=BlockType.EQUATION, index=1, content="x^2"),
                ],
            )
        ],
        is_full_document=True,
        file_suffix="html",
    )


def test_neutral_roundtrip() -> None:
    """协议默认值完整输出，读取后无需构造 MinerU 元数据。"""
    middle = document()
    payload = middle.to_dict()
    assert payload["schema"] == "docvortex.middle"
    assert payload["producer"] == {"name": "docvortex", "version": "0.1.0"}
    assert "mineru_version" not in payload
    assert load_middle(payload) == middle


@pytest.mark.parametrize("target", list(RenderFormat))
def test_all_renderers_are_independent_and_do_not_mutate(target: RenderFormat) -> None:
    """七种输出共享同一文档，而且渲染不会修改源对象。"""
    middle = document()
    before = middle.to_dict(skip_defaults=False)
    assert render(middle, target)
    assert middle.to_dict(skip_defaults=False) == before


def test_render_options_are_per_call() -> None:
    """两个调用可以使用不同分隔符，不读写全局配置。"""
    middle = document()
    custom = LatexDelimitersConfig(display=LatexDelimiterConfig(left="\\[", right="\\]"))
    assert "\\[" in render_markdown(middle, latex_delimiters=custom)
    assert "$$" in render_markdown(middle)


def test_schema_has_no_filesystem_export_method() -> None:
    """基础数据类型不再承担目录写入。"""
    assert not hasattr(document(), "export")


@pytest.mark.parametrize("target_name", ["markdown", "structured_content"])
def test_inline_delimiters_reach_each_text_renderer(target_name: str) -> None:
    """两种共享文本目标实际采用调用方的公式分隔符，防止门面丢失选项。"""
    from docvortex.schema import EquationInlineSpan, ChartBlock, ChartBodyBlock
    from docvortex.render.contracts import (
        MarkdownRenderOptions,
        StructuredContentRenderOptions,
    )

    middle = document()
    middle.pages[0].blocks[0].content.append(EquationInlineSpan(type="equation_inline", content="x"))
    middle.pages[0].blocks.append(
        ChartBlock(
            type="chart",
            index=2,
            content=[ChartBodyBlock(type="chart_body", index=2, content="<table><tr><td><eq>x</eq></td></tr></table>")],
        )
    )
    delimiters = LatexDelimitersConfig(inline=LatexDelimiterConfig(left="\\(", right="\\)"))
    option_types = {
        "markdown": MarkdownRenderOptions,
        "structured_content": StructuredContentRenderOptions,
    }
    value = render(middle, RenderFormat(target_name), options=option_types[target_name](latex_delimiters=delimiters))

    def strings(item: object) -> list[str]:
        """读取目标中所有文本叶子，避免依赖格式各自的封装层级。"""
        if isinstance(item, str):
            return [item]
        if isinstance(item, dict):
            return [text for child in item.values() for text in strings(child)]
        if isinstance(item, list):
            return [text for child in item for text in strings(child)]
        return []

    assert any("\\(x\\)" in text for text in strings(value))


def test_nested_json_extensions_roundtrip() -> None:
    """Pydantic 升级后仍保留严格 JSON 扩展中的嵌套类型与数值。"""
    middle = document()
    middle.extensions = {"consumer": {"items": [None, True, 3, 1.25, "文档", {"enabled": False}]}}
    restored = load_middle(middle.to_dict(skip_defaults=False))
    assert restored.extensions == middle.extensions
    assert type(restored.extensions["consumer"]["items"][1]) is bool
    assert type(restored.extensions["consumer"]["items"][2]) is int


@pytest.mark.parametrize("invalid", [object(), {"nested": object()}, {"values": {1, 2}}, {1: "non-string key"}])
def test_extensions_reject_non_json_values(invalid: object) -> None:
    """扩展信息不得接收任意 Python 对象或非 JSON 容器。"""
    from pydantic import ValidationError

    middle = document()
    with pytest.raises(ValidationError):
        middle.extensions = {"consumer": invalid}
