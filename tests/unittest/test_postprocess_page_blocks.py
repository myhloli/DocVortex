from __future__ import annotations

from _span_test_utils import inline as _inline

from docvortex.postprocess.lists import fix_pdf_list_blocks
from docvortex.postprocess.page_blocks import process_page_blocks
from docvortex.schema import BlockType


def test_page_blocks_keep_canonical_equation_type_and_clean_content() -> None:
    """Verify that the equation type is not overwritten when cleaning up interline formula content in single page post-processing."""
    equation = {
        "type": BlockType.EQUATION,
        "content": r"\[x+1\]",
    }

    blocks = process_page_blocks([equation], use_bbox=False)

    assert blocks == [
        {
            "type": BlockType.EQUATION,
            "content": "x+1",
            "index": 0,
        }
    ]


def test_fix_pdf_list_blocks_supports_unit_bbox_without_rewrite() -> None:
    """Verify that the normalized listbox calculates inclusion with the pixel textbox and does not write back bbox."""
    list_bbox = [0.0, 0.0, 1.0, 1.0]
    text_bbox = [100, 100, 200, 200]
    list_block = {
        "type": BlockType.LIST,
        "bbox": list_bbox,
        "content": "",
    }
    text_block = {
        "type": BlockType.TEXT,
        "bbox": text_bbox,
        "content": _inline("list item"),
    }

    list_blocks, text_blocks, ref_text_blocks = fix_pdf_list_blocks(
        [list_block],
        [text_block],
        [],
    )

    assert list_blocks == [list_block]
    assert text_blocks == []
    assert ref_text_blocks == []
    assert list_block["content"] == [text_block]
    assert list_block["sub_type"] == BlockType.TEXT
    assert list_bbox == [0.0, 0.0, 1.0, 1.0]
    assert text_bbox == [100, 100, 200, 200]


def test_page_blocks_group_bbox_dict_visual_blocks() -> None:
    """Verify single page post processing completes grouping of dict visual blocks with bbox."""
    blocks = process_page_blocks(
        [
            {
                "type": BlockType.IMAGE_CAPTION,
                "bbox": [0.0, 0.0, 80.0, 10.0],
                "content": _inline("Figure 1"),
            },
            {
                "type": BlockType.IMAGE,
                "bbox": [0.0, 15.0, 80.0, 60.0],
                "image_base64": "data:image/jpeg;base64,image",
            },
        ]
    )

    image_blocks = [block for block in blocks if block["type"] == BlockType.IMAGE]
    assert len(image_blocks) == 1
    image_block = image_blocks[0]
    assert isinstance(image_block, dict)
    assert [block["type"] for block in image_block["content"]] == [
        BlockType.IMAGE_CAPTION,
        BlockType.IMAGE_BODY,
    ]


def test_page_blocks_group_no_bbox_office_caption_by_prefix() -> None:
    """Verify single page post processing uses Office prefix rule grouping without bbox visual blocks."""
    blocks = process_page_blocks(
        [
            {
                "type": BlockType.IMAGE,
                "image_base64": "data:image/jpeg;base64,image",
            },
            {
                "type": BlockType.TEXT,
                "content": _inline("Figure 1"),
            },
        ]
    )

    assert not [block for block in blocks if block["type"] == BlockType.TEXT]
    image_blocks = [block for block in blocks if block["type"] == BlockType.IMAGE]
    assert len(image_blocks) == 1
    image_block = image_blocks[0]
    assert [block["type"] for block in image_block["content"]] == [
        BlockType.IMAGE_BODY,
        BlockType.IMAGE_CAPTION,
    ]


def test_page_blocks_group_no_bbox_chart_and_code_captions() -> None:
    """Verify chart/code caption mapping without bbox and code subtype retained."""
    blocks = process_page_blocks(
        [
            {"type": BlockType.CHART_CAPTION, "content": _inline("Chart 1")},
            {"type": BlockType.CHART, "content": "<div>chart</div>"},
            {"type": BlockType.CODE_CAPTION, "content": _inline("Algorithm 1")},
            {"type": BlockType.CODE, "content": "print('ok')"},
        ]
    )

    chart_block = next(block for block in blocks if block["type"] == BlockType.CHART)
    assert [block["type"] for block in chart_block["content"]] == [
        BlockType.CHART_CAPTION,
        BlockType.CHART_BODY,
    ]
    code_block = next(block for block in blocks if block["type"] == BlockType.CODE)
    assert code_block["sub_type"] == "code"
    assert code_block["guess_lang"]
    assert [block["type"] for block in code_block["content"]] == [
        BlockType.CODE_CAPTION,
        BlockType.CODE_BODY,
    ]
