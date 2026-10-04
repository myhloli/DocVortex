"""A focused regression test of visual block post-processing rules."""

from __future__ import annotations

from docvortex.postprocess.visual import (
    VISUAL_MAIN_TYPES,
    _bbox_for_calculation,
    fallback_inline_caption_fragments,
    fallback_leading_table_continuation_captions,
    fallback_no_bbox_caption_fragments,
    find_best_visual_parent,
    is_block_outside_visual_gap,
    regroup_visual_blocks,
)
from docvortex.schema import RAW_CAPTION, RAW_FOOTNOTE, BlockType


def _line_metadata(count: int) -> list[dict[str, list[float]]]:
    """Constructs dict row-level metadata containing only bbox."""
    return [{"bbox": [0.0, float(index), 1.0, float(index + 1)]} for index in range(count)]


def test_bbox_for_calculation_scales_only_all_unit_range_coordinates() -> None:
    """A magnified copy of the calculation is generated only when verifying that none of the four coordinates is greater than one."""
    normalized_bbox = (0.123, 0.221, 0.445, 0.556)

    assert _bbox_for_calculation(normalized_bbox) == (123.0, 221.0, 445.0, 556.0)
    assert normalized_bbox == (0.123, 0.221, 0.445, 0.556)
    assert _bbox_for_calculation((123, 221, 445, 556)) == (123, 221, 445, 556)
    assert _bbox_for_calculation((0.123, 0.221, 0.445, 1.0)) == (123.0, 221.0, 445.0, 1000.0)
    assert _bbox_for_calculation((0.123, 0.221, 0.445, 1.001)) == (0.123, 0.221, 0.445, 1.001)


def test_normalized_dict_bbox_matches_scaled_caption_geometry_without_rewrite() -> None:
    """Verify that the dict normalization box uses the same header geometry as the thousand times coordinates and does not write back bbox."""
    companion = {
        "index": 1,
        "type": BlockType.TEXT,
        "bbox": [0.02, 0.125, 0.1, 0.145],
        "content": "unrelated text",
        "lines": _line_metadata(1),
    }
    normalized_blocks = [
        {
            "index": 0,
            "type": RAW_CAPTION,
            "bbox": [0.7, 0.1, 0.95, 0.12],
            "content": "Table 1",
            "lines": _line_metadata(1),
        },
        companion,
        {
            "index": 2,
            "type": BlockType.TABLE_BODY,
            "bbox": [0.7, 0.15, 0.95, 0.8],
            "content": "",
        },
    ]
    original_bboxes = [list(block["bbox"]) for block in normalized_blocks]
    scaled_blocks = [
        {
            **block,
            "bbox": [value * 1000 for value in block["bbox"]],
        }
        for block in normalized_blocks
    ]

    fallback_inline_caption_fragments(normalized_blocks, VISUAL_MAIN_TYPES)
    fallback_inline_caption_fragments(scaled_blocks, VISUAL_MAIN_TYPES)

    assert companion["type"] == BlockType.TEXT
    assert scaled_blocks[1]["type"] == BlockType.TEXT
    assert [block["bbox"] for block in normalized_blocks] == original_bboxes


def test_normalized_dict_bbox_matches_scaled_visual_parent_without_rewrite() -> None:
    """Verify that raw dict selects the same visual parent block for the normalized frame and thousand times coordinates and retains the original frame."""
    previous_table = {"index": 0, "type": BlockType.TABLE_BODY, "bbox": [0.1, 0.1, 0.4, 0.4]}
    caption = {"index": 1, "type": RAW_CAPTION, "bbox": [0.1, 0.42, 0.4, 0.44]}
    following_table = {"index": 2, "type": BlockType.TABLE_BODY, "bbox": [0.1, 0.8, 0.4, 0.95]}
    normalized_blocks = [previous_table, caption, following_table]
    original_bboxes = [list(block["bbox"]) for block in normalized_blocks]
    scaled_blocks = [{**block, "bbox": [value * 1000 for value in block["bbox"]]} for block in normalized_blocks]

    normalized_parent = find_best_visual_parent(
        caption,
        [previous_table, following_table],
        normalized_blocks,
        {block["index"]: position for position, block in enumerate(normalized_blocks)},
    )
    scaled_parent = find_best_visual_parent(
        scaled_blocks[1],
        [scaled_blocks[0], scaled_blocks[2]],
        scaled_blocks,
        {block["index"]: position for position, block in enumerate(scaled_blocks)},
    )

    assert normalized_parent is previous_table
    assert scaled_parent is scaled_blocks[0]
    assert [block["bbox"] for block in normalized_blocks] == original_bboxes


def test_normalized_dict_bbox_matches_scaled_visual_gap_geometry() -> None:
    """Verify that the normalized box is consistent with thousand-fold coordinates in visual separation, intersection, and overlap judgments."""
    child = {"index": 0, "type": RAW_CAPTION, "bbox": [0.1, 0.1, 0.4, 0.2]}
    inside_gap = {"index": 1, "type": BlockType.TEXT, "bbox": [0.8, 0.3, 0.9, 0.4]}
    outside_gap = {"index": 2, "type": BlockType.TEXT, "bbox": [0.8, 0.8, 0.9, 0.9]}
    main = {"index": 3, "type": BlockType.TABLE_BODY, "bbox": [0.1, 0.6, 0.4, 0.7]}
    normalized_blocks = [child, inside_gap, outside_gap, main]
    scaled_blocks = [{**block, "bbox": [value * 1000 for value in block["bbox"]]} for block in normalized_blocks]

    assert is_block_outside_visual_gap(inside_gap, child, main) is False
    assert is_block_outside_visual_gap(outside_gap, child, main) is True
    assert is_block_outside_visual_gap(scaled_blocks[1], scaled_blocks[0], scaled_blocks[3]) is False
    assert is_block_outside_visual_gap(scaled_blocks[2], scaled_blocks[0], scaled_blocks[3]) is True


def test_dict_inline_caption_fragment_uses_common_fields() -> None:
    """Verify that the dict peer title fragment can be changed in-place to the generic caption."""
    companion = {
        "index": 1,
        "type": BlockType.TEXT,
        "bbox": [40.0, 0.0, 80.0, 10.0],
        "content": "continued caption",
        "lines": _line_metadata(1),
    }
    blocks = [
        {
            "index": 0,
            "type": RAW_CAPTION,
            "bbox": [0.0, 0.0, 40.0, 10.0],
            "content": "Table 1",
            "lines": _line_metadata(1),
        },
        companion,
        {
            "index": 2,
            "type": BlockType.TABLE_BODY,
            "bbox": [0.0, 15.0, 80.0, 60.0],
            "content": "",
        },
    ]

    fallback_inline_caption_fragments(blocks, VISUAL_MAIN_TYPES)

    assert companion["type"] == RAW_CAPTION


def test_dict_stacked_caption_fragment_uses_line_metadata() -> None:
    """Verify that stacked header fragments differentiate between single and multiple lines based on a dict's temporary lines."""
    single_line = {
        "index": 1,
        "type": BlockType.TEXT,
        "bbox": [0.0, 12.0, 80.0, 22.0],
        "content": "single line",
        "lines": _line_metadata(1),
    }
    blocks = [
        {
            "index": 0,
            "type": RAW_CAPTION,
            "bbox": [0.0, 0.0, 80.0, 10.0],
            "content": "Table 1",
            "lines": _line_metadata(1),
        },
        single_line,
        {
            "index": 2,
            "type": BlockType.TABLE_BODY,
            "bbox": [0.0, 25.0, 80.0, 60.0],
            "content": "",
        },
    ]

    fallback_inline_caption_fragments(blocks, VISUAL_MAIN_TYPES)

    assert single_line["type"] == RAW_CAPTION
    single_line["type"] = BlockType.TEXT
    single_line["lines"] = _line_metadata(2)

    fallback_inline_caption_fragments(blocks, VISUAL_MAIN_TYPES)

    assert single_line["type"] == BlockType.TEXT


def test_dict_leading_table_continuation_reads_top_level_content() -> None:
    """Verification dict Top of page continuation table uses the top-level content and temporary lines to complete the judgment."""
    continuation = {
        "index": 0,
        "type": BlockType.TEXT,
        "bbox": [0.0, 0.0, 80.0, 10.0],
        "content": "Table 1 (continued)",
        "lines": _line_metadata(1),
    }
    blocks = [
        continuation,
        {
            "index": 1,
            "type": BlockType.TABLE_BODY,
            "bbox": [0.0, 15.0, 80.0, 60.0],
            "content": "",
        },
    ]

    fallback_leading_table_continuation_captions(blocks, VISUAL_MAIN_TYPES)

    assert continuation["type"] == RAW_CAPTION


def test_regroup_visual_blocks_supports_bbox_dicts() -> None:
    """Verify that dict with bbox can produce a dict two-layer block using existing visual relationship rules."""
    caption = {
        "index": 0,
        "type": RAW_CAPTION,
        "bbox": [0.0, 0.0, 80.0, 10.0],
        "content": "Figure 1",
        "lines": [{"bbox": [0.0, 0.0, 80.0, 10.0]}],
    }
    body = {
        "index": 1,
        "type": BlockType.IMAGE_BODY,
        "bbox": [0.0, 15.0, 80.0, 60.0],
        "content": "",
    }
    footnote = {
        "index": 2,
        "type": RAW_FOOTNOTE,
        "bbox": [0.0, 65.0, 80.0, 75.0],
        "content": "source",
        "lines": [{"bbox": [0.0, 65.0, 80.0, 75.0]}],
    }

    grouped_blocks, unmatched_blocks = regroup_visual_blocks([caption, body, footnote])

    assert unmatched_blocks == []
    assert len(grouped_blocks[BlockType.IMAGE]) == 1
    image_block = grouped_blocks[BlockType.IMAGE][0]
    assert isinstance(image_block, dict)
    assert image_block["type"] == BlockType.IMAGE
    assert image_block["bbox"] == body["bbox"]
    assert [block["type"] for block in image_block["content"]] == [
        BlockType.IMAGE_CAPTION,
        BlockType.IMAGE_BODY,
        BlockType.IMAGE_FOOTNOTE,
    ]
    image_caption, _, image_footnote = image_block["content"]
    assert "lines" not in image_caption
    assert "lines" not in image_footnote
    assert "lines" in caption
    assert "lines" in footnote
    assert caption["type"] == RAW_CAPTION
    assert footnote["type"] == RAW_FOOTNOTE


def test_regroup_visual_blocks_preserves_dict_visual_metadata() -> None:
    """Verify that the dict root node retains code, subtype, visual subtype, and table merge information."""
    blocks = [
        {
            "index": 0,
            "type": BlockType.CODE_BODY,
            "content": "print('ok')",
            "sub_type": "code",
        },
        {
            "index": 1,
            "type": BlockType.IMAGE_BODY,
            "content": "",
            "sub_type": "seal",
        },
        {
            "index": 2,
            "type": BlockType.TABLE_BODY,
            "content": "<table></table>",
            "angle": 0,
            "score": 0.0,
            "cell_merge": [1, 0],
        },
    ]

    grouped_blocks, _ = regroup_visual_blocks(blocks, use_bbox=False)

    code_block = grouped_blocks[BlockType.CODE][0]
    image_block = grouped_blocks[BlockType.IMAGE][0]
    table_block = grouped_blocks[BlockType.TABLE][0]
    assert code_block["sub_type"] == "code"
    assert "sub_type" not in code_block["content"][0]
    assert image_block["sub_type"] == "seal"
    assert "sub_type" not in image_block["content"][0]
    assert "sub_type" not in table_block
    assert "sub_type" not in table_block["content"][0]
    assert table_block["content"][0]["angle"] == 0
    assert table_block["content"][0]["score"] == 0.0
    assert table_block["cell_merge"] == [1, 0]
    assert "cell_merge" not in table_block["content"][0]


def test_regroup_visual_blocks_lifts_cell_merge_to_table() -> None:
    """Verify that the raw dict meter body's cell_merge floats up to the table root block."""
    table_body = {
        "index": 0,
        "type": BlockType.TABLE_BODY,
        "bbox": [0.0, 0.0, 80.0, 60.0],
        "content": "<table></table>",
        "cell_merge": [1, 0],
    }

    grouped_blocks, unmatched_blocks = regroup_visual_blocks([table_body])

    assert unmatched_blocks == []
    table_block = grouped_blocks[BlockType.TABLE][0]
    assert table_block["cell_merge"] == [1, 0]
    assert len(table_block["content"]) == 1
    assert "cell_merge" not in table_block["content"][0]


def test_regroup_visual_blocks_without_bbox_prefers_previous_parent() -> None:
    """Verify that no bbox isometric matching preferentially selects the visual subject in front of caption."""
    image_body = {"index": 0, "type": BlockType.IMAGE_BODY, "content": ""}
    caption = {"index": 1, "type": RAW_CAPTION, "content": "Figure 1"}
    table_body = {"index": 2, "type": BlockType.TABLE_BODY, "content": ""}

    grouped_blocks, unmatched_blocks = regroup_visual_blocks(
        [image_body, caption, table_body],
        use_bbox=False,
    )

    assert unmatched_blocks == []
    assert "bbox" not in grouped_blocks[BlockType.IMAGE][0]
    assert [block["type"] for block in grouped_blocks[BlockType.IMAGE][0]["content"]] == [
        BlockType.IMAGE_BODY,
        BlockType.IMAGE_CAPTION,
    ]
    assert [block["type"] for block in grouped_blocks[BlockType.TABLE][0]["content"]] == [BlockType.TABLE_BODY]


def test_regroup_visual_blocks_supports_code_footnote() -> None:
    """Verify that the specific code_footnote can be directly subsumed into code and will not remain as an additional top-level block."""
    code_body = {
        "index": 0,
        "type": BlockType.CODE_BODY,
        "content": "print('ok')",
        "sub_type": "code",
    }
    code_footnote = {
        "index": 1,
        "type": BlockType.CODE_FOOTNOTE,
        "content": "runtime note",
    }

    grouped_blocks, unmatched_blocks = regroup_visual_blocks(
        [code_body, code_footnote],
        use_bbox=False,
    )

    assert unmatched_blocks == []
    assert [child["type"] for child in grouped_blocks[BlockType.CODE][0]["content"]] == [
        BlockType.CODE_BODY,
        BlockType.CODE_FOOTNOTE,
    ]


def test_regroup_visual_blocks_without_bbox_keeps_text_as_barrier() -> None:
    """Verify that the pattern without bbox does not cross the normal text association caption."""
    caption = {"index": 0, "type": RAW_CAPTION, "content": "Table 1"}
    text = {"index": 1, "type": BlockType.TEXT, "content": "paragraph"}
    table_body = {"index": 2, "type": BlockType.TABLE_BODY, "content": ""}

    grouped_blocks, unmatched_blocks = regroup_visual_blocks(
        [caption, text, table_body],
        use_bbox=False,
    )

    assert unmatched_blocks == [caption]
    assert [block["type"] for block in grouped_blocks[BlockType.TABLE][0]["content"]] == [BlockType.TABLE_BODY]


def test_no_bbox_caption_fallback_uses_office_prefixes() -> None:
    """Verify that table/image/chart suffix text without bbox is promoted by the Office prefix."""
    cases = [
        (BlockType.TABLE_BODY, "Table 1", RAW_CAPTION),
        (BlockType.IMAGE_BODY, "图 1", RAW_CAPTION),
        (BlockType.CHART_BODY, "Chart 1", RAW_CAPTION),
        (BlockType.IMAGE_BODY, "ordinary text", BlockType.TEXT),
    ]

    for main_type, content, expected_type in cases:
        blocks = [
            {"index": 0, "type": main_type, "content": ""},
            {"index": 1, "type": BlockType.TEXT, "content": content},
        ]

        fallback_no_bbox_caption_fragments(blocks, VISUAL_MAIN_TYPES)

        assert blocks[1]["type"] == expected_type
