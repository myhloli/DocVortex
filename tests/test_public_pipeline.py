"""验证中性协议、宿主协议适配和独立的渲染后半程。"""

from __future__ import annotations

import pytest

from docgale.codecs.json import load_middle
from docgale.compat.mineru import from_mineru_middle, to_mineru_middle
from docgale.options import LatexDelimiterConfig, LatexDelimitersConfig
from docgale.render import RenderFormat, render
from docgale.render.markdown import render_markdown
from docgale.schema import BlockType, EquationBlock, MiddleJson, PageInfo, TextBlock, TextSpan


def document() -> MiddleJson:
    """建立不携带宿主信息的最小语义文档。"""
    return MiddleJson(pages=[PageInfo(page_idx=0, blocks=[
        TextBlock(type=BlockType.TEXT, index=0, content=[TextSpan(type="text", content="Hello 文档")]),
        EquationBlock(type=BlockType.EQUATION, index=1, content="x^2"),
    ])], is_full_document=True, file_suffix="html")


def test_neutral_roundtrip() -> None:
    """协议默认值完整输出，读取后无需构造 MinerU 元数据。"""
    middle = document()
    payload = middle.to_dict()
    assert payload["schema"] == "docgale.middle"
    assert payload["producer"] == {"name": "docgale", "version": "0.1.0"}
    assert "mineru_version" not in payload
    assert load_middle(payload) == middle


@pytest.mark.parametrize("target", list(RenderFormat))
def test_all_renderers_are_independent_and_do_not_mutate(target: RenderFormat) -> None:
    """九种输出共享同一文档，而且渲染不会修改源对象。"""
    middle = document()
    before = middle.to_dict(skip_defaults=False)
    assert render(middle, target)
    assert middle.to_dict(skip_defaults=False) == before


def test_mineru_envelope_roundtrip() -> None:
    """旧 JSON 经过中性对象后保持产品字段和页面语义不变。"""
    payload = document().to_dict(skip_defaults=False)
    for key in ("schema", "schema_version", "producer", "extensions"):
        payload.pop(key, None)
    payload.update(schema_version="2.0", effort="flash", parse_mode="txt", mineru_version="3.4.5")
    assert to_mineru_middle(from_mineru_middle(payload), skip_defaults=False) == payload


def test_render_options_are_per_call() -> None:
    """两个调用可以使用不同分隔符，不读写全局配置。"""
    middle = document()
    custom = LatexDelimitersConfig(display=LatexDelimiterConfig(left="\\[", right="\\]"))
    assert "\\[" in render_markdown(middle, latex_delimiters=custom)
    assert "$$" in render_markdown(middle)


def test_schema_has_no_filesystem_export_method() -> None:
    """基础数据类型不再承担目录写入。"""
    assert not hasattr(document(), "export")
