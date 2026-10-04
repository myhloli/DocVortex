"""Verification of neutral protocols and independent rendering second half."""

from __future__ import annotations

import pytest

from docvortex.codecs.json import load_middle
from docvortex.options import LatexDelimiterConfig, LatexDelimitersConfig
from docvortex.render import RenderFormat, render
from docvortex.render.markdown import render_markdown
from docvortex.schema import BlockType, EquationBlock, MiddleJson, PageInfo, TextBlock, TextSpan


def document() -> MiddleJson:
    """Create a minimal semantic document that does not carry host information."""
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
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
    )


def test_neutral_roundtrip() -> None:
    """The protocol default values are output completely, and there is no need to construct MinerU metadata after reading."""
    middle = document()
    payload = middle.to_dict()
    assert payload["schema"] == "docvortex.middle"
    assert payload["metadata"]["producer"] == {"name": "docvortex", "version": "0.2.0"}
    assert "mineru_version" not in payload
    assert load_middle(payload) == middle


@pytest.mark.parametrize("target", list(RenderFormat))
def test_all_renderers_are_independent_and_do_not_mutate(target: RenderFormat) -> None:
    """Seven outputs share the same document, and rendering does not modify the source object."""
    middle = document()
    before = middle.to_dict(skip_defaults=False)
    assert render(middle, target)
    assert middle.to_dict(skip_defaults=False) == before


def test_render_options_are_per_call() -> None:
    """The two calls can use different delimiters and do not read or write global configuration."""
    middle = document()
    custom = LatexDelimitersConfig(display=LatexDelimiterConfig(left="\\[", right="\\]"))
    assert "\\[" in render_markdown(middle, latex_delimiters=custom)
    assert "$$" in render_markdown(middle)


def test_schema_has_no_filesystem_export_method() -> None:
    """The underlying data type is no longer responsible for directory writing."""
    assert not hasattr(document(), "export")


@pytest.mark.parametrize("target_name", ["markdown", "structured_content"])
def test_inline_delimiters_reach_each_text_renderer(target_name: str) -> None:
    """Both shared text targets actually use the caller's formula delimiter to prevent the facade from losing options."""
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
        """Read all text leaves in the target to avoid relying on the format's respective encapsulation level."""
        if isinstance(item, str):
            return [item]
        if isinstance(item, dict):
            return [text for child in item.values() for text in strings(child)]
        if isinstance(item, list):
            return [text for child in item for text in strings(child)]
        return []

    assert any("\\(x\\)" in text for text in strings(value))


def test_nested_json_extensions_roundtrip() -> None:
    """Pydantic Strictly nested types and values in the JSON extension are retained after the upgrade."""
    middle = document()
    middle.extensions = {"consumer": {"items": [None, True, 3, 1.25, "文档", {"enabled": False}]}}
    restored = load_middle(middle.to_dict(skip_defaults=False))
    assert restored.extensions == middle.extensions
    assert type(restored.extensions["consumer"]["items"][1]) is bool
    assert type(restored.extensions["consumer"]["items"][2]) is int


@pytest.mark.parametrize("invalid", [object(), {"nested": object()}, {"values": {1, 2}}, {1: "non-string key"}])
def test_extensions_reject_non_json_values(invalid: object) -> None:
    """Extended information must not receive any Python objects or non-JSON containers."""
    from pydantic import ValidationError

    middle = document()
    with pytest.raises(ValidationError):
        middle.extensions = {"consumer": invalid}
