from __future__ import annotations

from unittest.mock import Mock

import pytest
from _span_test_utils import inline as _inline

from docvortex.options import LatexDelimitersConfig
from docvortex.render import ImageRenderer
from docvortex.render._internal.markdown.blocks import render_single_block
from docvortex.schema import (
    BlockBase,
    BlockType,
    ChartBlock,
    ChartBodyBlock,
    EquationBlock,
    ImageAnnotationBlock,
    ImageBlock,
    ImageBodyBlock,
    TableBlock,
    TableBodyBlock,
)

_DATA_URI = "data:image/png;base64," + "A" * 40000


@pytest.mark.parametrize("kind", ["image", "equation", "table", "chart"])
@pytest.mark.parametrize("source_field", ["image_base64", "image_path", "image_url"])
def test_empty_callback_suppresses_all_visual_image_fallbacks(kind: str, source_field: str) -> None:
    """显式空回调必须抑制所有素材来源，默认导出仍然保留图片。"""
    source = {"image_base64": _DATA_URI, "image_path": "images/test.png", "image_url": "https://example.com/image.png"}[
        source_field
    ]
    if kind == "equation":
        block = EquationBlock(type=BlockType.EQUATION, content="", **{source_field: source})
    else:
        parent_type, body_type = {
            "image": (ImageBlock, ImageBodyBlock),
            "table": (TableBlock, TableBodyBlock),
            "chart": (ChartBlock, ChartBodyBlock),
        }[kind]
        body = body_type(type=f"{kind}_body", content="", **{source_field: source})
        block = parent_type(type=kind, content=[body])
    original = block.model_dump()
    callback = Mock(return_value="")

    assert render_single_block(block, delimiters=_delimiters(), asset_base_url="", image_renderer=callback) == ""
    callback.assert_called_once_with(block)
    assert source in render_single_block(block, delimiters=_delimiters(), asset_base_url="")
    assert block.model_dump() == original


@pytest.mark.parametrize("kind", ["image", "chart", "table"])
@pytest.mark.parametrize("replacement", ["", "![Visual](doc:aaaaaaa/tier:flash/page:1/block:1)"])
def test_custom_callback_keeps_visual_text_without_embedded_images(kind: str, replacement: str) -> None:
    """带文字的图表也遵守回调，并移除视觉内容中的 HTML 图片且不修改原文。"""
    parent_type, body_type = {
        "image": (ImageBlock, ImageBodyBlock),
        "table": (TableBlock, TableBodyBlock),
        "chart": (ChartBlock, ChartBodyBlock),
    }[kind]
    content = f'<table><tr><td>recognized text<img src="{_DATA_URI}"></td></tr></table>'
    body = body_type(type=f"{kind}_body", content=content, image_base64=_DATA_URI)
    block = parent_type(type=kind, content=[body])
    original = block.model_dump()
    callback = Mock(return_value=replacement)

    rendered = render_single_block(block, delimiters=_delimiters(), asset_base_url="", image_renderer=callback)

    assert "recognized text" in rendered
    assert "base64" not in rendered and "<img" not in rendered
    if kind != "table":
        callback.assert_called_once_with(block)
        assert replacement in rendered
    assert _DATA_URI in render_single_block(block, delimiters=_delimiters(), asset_base_url="")
    assert block.model_dump() == original


def _delimiters() -> LatexDelimitersConfig:
    """提供显式默认定界符，不读取宿主全局配置。"""
    return LatexDelimitersConfig()


def _image_block() -> ImageBlock:
    """构造带图片载体与说明文字的最小块。"""
    body = ImageBodyBlock(
        type=BlockType.IMAGE_BODY,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.1),
        content="",
        image_path="internal/hash.jpg",
    )
    caption = ImageAnnotationBlock(
        type=BlockType.IMAGE_CAPTION,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.01),
        content=_inline("Figure caption"),
    )
    return ImageBlock(
        type=BlockType.IMAGE,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.1),
        content=[body, caption],
    )


def test_pipeline_markdown_uses_custom_block_image_renderer_and_keeps_caption() -> None:
    """PDF 图片采用自定义渲染器并保留说明文字。"""
    block = _image_block()
    renderer_mock = Mock(return_value="![Image block](doc:aaaaaaa/tier:standard/page:1/block:1)")
    image_renderer: ImageRenderer = renderer_mock

    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=image_renderer,
    )

    assert "![Image block](doc:aaaaaaa/tier:standard/page:1/block:1)" in rendered
    assert "Figure caption" in rendered
    assert "internal/hash.jpg" not in rendered
    renderer_mock.assert_called_once_with(block)


def test_office_markdown_uses_custom_block_image_renderer_and_keeps_caption() -> None:
    """Office 图片采用自定义渲染器并保留说明文字。"""
    block = _image_block()

    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=lambda _block: "![Image block]()",
    )

    assert "![Image block]()" in rendered
    assert "Figure caption" in rendered
    assert "internal/hash.jpg" not in rendered


@pytest.mark.parametrize("block_type", [BlockType.TABLE, BlockType.CHART])
def test_pipeline_markdown_uses_custom_renderer_for_image_only_visual_blocks(
    block_type: BlockType,
) -> None:
    """仅有图片的视觉块也调用自定义渲染器。"""
    if block_type == BlockType.TABLE:
        block: BlockBase = TableBlock(
            type=BlockType.TABLE,
            index=0,
            bbox=(0.0, 0.0, 0.1, 0.1),
            content=[
                TableBodyBlock(
                    type=BlockType.TABLE_BODY,
                    index=0,
                    bbox=(0.0, 0.0, 0.1, 0.1),
                    content="",
                    image_path="internal/hash.jpg",
                )
            ],
        )
    else:
        block = ChartBlock(
            type=BlockType.CHART,
            index=0,
            bbox=(0.0, 0.0, 0.1, 0.1),
            content=[
                ChartBodyBlock(
                    type=BlockType.CHART_BODY,
                    index=0,
                    bbox=(0.0, 0.0, 0.1, 0.1),
                    content="",
                    image_path="internal/hash.jpg",
                )
            ],
        )

    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=lambda _block: "![Visual block](doc:locator)",
    )

    assert rendered == "![Visual block](doc:locator)"


def test_pipeline_markdown_uses_custom_renderer_for_image_only_formula() -> None:
    """仅有图片的公式使用自定义渲染器。"""
    block = EquationBlock(
        type=BlockType.EQUATION,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.1),
        content="",
        image_path="internal/hash.jpg",
    )

    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=lambda _block: "![Formula block](doc:locator)",
    )

    assert rendered == "![Formula block](doc:locator)"


def test_custom_renderer_removes_internal_images_from_structured_table_html() -> None:
    """表格自定义图片输出移除重复的内部图片。"""
    internal_path = "internal/cell-image.png"
    html = f'<table><tr><td>Text<img src="{internal_path}"></td></tr></table>'
    block = TableBlock(
        type=BlockType.TABLE,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.1),
        content=[
            TableBodyBlock(
                type=BlockType.TABLE_BODY,
                index=0,
                bbox=(0.0, 0.0, 0.1, 0.1),
                content=html,
            )
        ],
    )

    image_renderer = Mock(return_value="![Table block](doc:locator)")
    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=image_renderer,
    )

    assert "Text" in rendered
    assert internal_path not in rendered
    assert "<img" not in rendered
    assert block.content[0].content == html
    image_renderer.assert_not_called()


def test_custom_renderer_handles_table_html_containing_only_images() -> None:
    """只含图片的表格 HTML 保持自定义输出契约。"""
    internal_path = "internal/cell-image.png"
    block = TableBlock(
        type=BlockType.TABLE,
        index=0,
        bbox=(0.0, 0.0, 0.1, 0.1),
        content=[
            TableBodyBlock(
                type=BlockType.TABLE_BODY,
                index=0,
                bbox=(0.0, 0.0, 0.1, 0.1),
                content=f'<table><tr><td><img src="{internal_path}"></td></tr></table>',
            )
        ],
    )
    image_renderer = Mock(return_value="![Table block](doc:locator)")

    rendered = render_single_block(
        block,
        delimiters=_delimiters(),
        asset_base_url="",
        image_renderer=image_renderer,
    )

    assert rendered == "![Table block](doc:locator)"
    assert internal_path not in rendered
    image_renderer.assert_called_once_with(block)
