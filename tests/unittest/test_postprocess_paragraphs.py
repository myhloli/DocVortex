"""Focused regression testing of cross-page paragraph continuation rules."""

from copy import deepcopy

import pytest
from _span_test_utils import inline as _inline
from _span_test_utils import inline_text

from docvortex.postprocess.paragraphs import can_auto_merge_ref_text_blocks, can_auto_merge_text_blocks, merge_para_text_blocks
from docvortex.schema import BlockType


def _text_block(
    index: int,
    content: str,
    bbox: list[float],
    line_bboxes: list[list[float]],
    *,
    block_type: str = BlockType.TEXT,
) -> dict:
    """Construct dict text/ref_text block using normalized block/line bbox."""
    return {
        "index": index,
        "type": block_type,
        "bbox": list(bbox),
        "content": _inline(content),
        "lines": [{"bbox": list(line_bbox)} for line_bbox in line_bboxes],
    }


def _list_block(
    index: int,
    content: str,
    *,
    sub_type: str = BlockType.REF_TEXT,
) -> dict:
    """Constructs dict list block with immediate text children and temporary line boxes."""
    child_type = BlockType.REF_TEXT if sub_type == BlockType.REF_TEXT else BlockType.TEXT
    return {
        "index": index,
        "type": BlockType.LIST,
        "sub_type": sub_type,
        "bbox": [0.1, 0.1, 0.9, 0.3],
        "content": [
            {
                "type": child_type,
                "content": _inline(content),
                "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}],
            }
        ],
    }


def _horizontal_pair() -> tuple[dict, dict]:
    """Construct a set of text before and after block that satisfy the horizontal paragraph continuation rules."""
    previous_block = _text_block(
        0,
        "previous continuation",
        [0.1, 0.1, 0.9, 0.3],
        [[0.1, 0.1, 0.9, 0.15], [0.1, 0.2, 0.9, 0.25]],
    )
    current_block = _text_block(
        1,
        "current continuation",
        [0.1, 0.25, 0.9, 0.45],
        [[0.1, 0.25, 0.9, 0.3], [0.1, 0.35, 0.9, 0.4]],
    )
    return previous_block, current_block


def _ref_text_horizontal_pair() -> tuple[dict, dict]:
    """Construct a ref_text block pair in which the first line is indented but the remaining rules satisfy the continuation condition."""
    previous_block, current_block = _horizontal_pair()
    previous_block["type"] = BlockType.REF_TEXT
    current_block["type"] = BlockType.REF_TEXT
    current_block["lines"][0]["bbox"][0] = 0.2
    return previous_block, current_block


def _horizontal_multiline_block(
    index: int,
    content: str,
    *,
    left: float,
    right: float,
    top: float,
    line_count: int = 5,
    line_height: float = 0.04,
    first_indent: float = 0.0,
) -> dict:
    """Constructs a horizontal multi-line text block that controls the indentation of the first line."""
    line_bboxes = [
        [
            left + (first_indent if line_index == 0 else 0.0),
            top + line_index * line_height,
            right,
            top + (line_index + 1) * line_height,
        ]
        for line_index in range(line_count)
    ]
    return _text_block(
        index,
        content,
        [min(line[0] for line in line_bboxes), top, right, top + line_count * line_height],
        line_bboxes,
    )


def _horizontal_single_line_block(
    index: int,
    content: str,
    *,
    left: float,
    right: float,
    top: float,
    line_height: float = 0.04,
) -> dict:
    """Constructs a horizontal single-line block that requires subsequent lines to make up the virtual width."""
    return _text_block(
        index,
        content,
        [left, top, right, top + line_height],
        [[left, top, right, top + line_height]],
    )


def _vertical_multicolumn_block(
    index: int,
    content: str,
    *,
    right: float,
    top: float,
    bottom: float,
    column_count: int = 5,
    column_width: float = 0.04,
    column_gap: float = 0.02,
    first_top_indent: float = 0.0,
) -> dict:
    """Construct vertically arranged multi-column text blocks that are arranged from right to left and can control the upper boundary of the first column."""
    column_bboxes = [
        [
            right - column_width - column_index * (column_width + column_gap),
            top + (first_top_indent if column_index == 0 else 0.0),
            right - column_index * (column_width + column_gap),
            bottom,
        ]
        for column_index in range(column_count)
    ]
    return _text_block(
        index,
        content,
        [min(column[0] for column in column_bboxes), top, right, bottom],
        column_bboxes,
    )


def _vertical_single_column_block(
    index: int,
    content: str,
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> dict:
    """Constructs vertical single-column blocks that require subsequent columns to make up the virtual height."""
    return _text_block(
        index,
        content,
        [left, top, right, bottom],
        [[left, top, right, bottom]],
    )


def _vertical_pair() -> tuple[dict, dict]:
    """Construct a set of text and block that satisfy the vertical paragraph continuation rules."""
    previous_block = _text_block(
        0,
        "previous vertical",
        [0.65, 0.1, 0.9, 0.9],
        [[0.82, 0.1, 0.87, 0.9], [0.72, 0.1, 0.77, 0.9]],
    )
    current_block = _text_block(
        1,
        "current vertical",
        [0.5, 0.1, 0.75, 0.9],
        [[0.62, 0.1, 0.67, 0.9], [0.52, 0.1, 0.57, 0.9]],
    )
    return previous_block, current_block


def test_merge_para_text_blocks_marks_reverse_chain_without_moving_content() -> None:
    """Verification reverse processing marks subsequent blocks in the continuous chain but does not move the text or overwrite bbox."""
    first_block, second_block = _horizontal_pair()
    third_block = _text_block(
        2,
        "third continuation",
        [0.1, 0.4, 0.9, 0.6],
        [[0.1, 0.4, 0.9, 0.45], [0.1, 0.5, 0.9, 0.55]],
    )
    pages = [{"page_idx": 0, "blocks": [first_block, second_block, third_block]}]
    original_contents = [block["content"] for block in pages[0]["blocks"]]
    original_bboxes = deepcopy([block["bbox"] for block in pages[0]["blocks"]])

    merge_para_text_blocks(pages)

    assert "continues_prev" not in first_block
    assert second_block["continues_prev"] is True
    assert third_block["continues_prev"] is True
    assert [block["content"] for block in pages[0]["blocks"]] == original_contents
    assert [block["bbox"] for block in pages[0]["blocks"]] == original_bboxes
    assert all("lines" not in block for block in pages[0]["blocks"])

    first_result = deepcopy(pages)
    merge_para_text_blocks(pages)
    assert pages == first_result


@pytest.mark.parametrize(
    ("page_indices", "expected_continuation"),
    [
        ((0, 1), True),
        ((0, 2), False),
        ((2, 1), False),
    ],
)
def test_merge_para_text_blocks_requires_consecutive_cross_page_indices(
    page_indices: tuple[int, int],
    expected_continuation: bool,
) -> None:
    """Verification Spread Page text Only allows continuation from the previous consecutive page."""
    previous_block, current_block = _horizontal_pair()
    pages = [
        {"page_idx": page_indices[0], "blocks": [previous_block]},
        {"page_idx": page_indices[1], "blocks": [current_block]},
    ]

    merge_para_text_blocks(pages)

    assert current_block.get("continues_prev", False) is expected_continuation


@pytest.mark.parametrize(
    ("middle_type", "expected_continuation"),
    [
        (BlockType.IMAGE, True),
        (BlockType.CODE, True),
        (BlockType.HEADER, True),
        (BlockType.FOOTER, True),
        (BlockType.PAGE_NUMBER, True),
        (BlockType.PAGE_FOOTNOTE, True),
        (BlockType.ASIDE_TEXT, True),
        (BlockType.DOC_TITLE, False),
        (BlockType.LIST, False),
        (BlockType.REF_TEXT, False),
    ],
)
def test_merge_para_text_blocks_respects_transparent_and_barrier_types(
    middle_type: str,
    expected_continuation: bool,
) -> None:
    """Verify that visual root blocks and page decoration blocks are traversable, other semantic blocks block text lookups."""
    previous_block, current_block = _horizontal_pair()
    middle_block = {
        "index": 1,
        "type": middle_type,
        "bbox": [0.1, 0.22, 0.9, 0.24],
        "content": "middle",
    }
    current_block["index"] = 2
    pages = [{"page_idx": 0, "blocks": [previous_block, middle_block, current_block]}]

    merge_para_text_blocks(pages)

    assert current_block.get("continues_prev", False) is expected_continuation


def test_merge_para_text_blocks_ignores_cross_page_decorations() -> None:
    """Validating cross-page text will skip all page decoration blocks at the footer of the previous page and the header of the next page."""
    previous_block, current_block = _horizontal_pair()
    current_block["index"] = 3
    pages = [
        {
            "page_idx": 0,
            "blocks": [
                previous_block,
                {"index": 1, "type": BlockType.FOOTER, "content": "footer"},
                {"index": 2, "type": BlockType.PAGE_FOOTNOTE, "content": "page footnote"},
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                {"index": 1, "type": BlockType.PAGE_NUMBER, "content": "2"},
                {"index": 2, "type": BlockType.ASIDE_TEXT, "content": "aside"},
                current_block,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True
    assert inline_text(previous_block["content"]) == "previous continuation"
    assert inline_text(current_block["content"]) == "current continuation"
    assert "lines" not in previous_block
    assert "lines" not in current_block


@pytest.mark.parametrize(
    "barrier_type",
    [BlockType.DOC_TITLE, BlockType.PARAGRAPH_TITLE, BlockType.EQUATION, BlockType.LIST],
)
def test_merge_para_text_blocks_keeps_cross_page_semantic_barriers(barrier_type: str) -> None:
    """After verifying that the page decoration block is transparent, titles, formulas, and lists will still block cross-page body connections."""
    previous_block, current_block = _horizontal_pair()
    current_block["index"] = 2
    pages = [
        {
            "page_idx": 0,
            "blocks": [
                previous_block,
                {"index": 1, "type": BlockType.PAGE_FOOTNOTE, "content": "page footnote"},
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                {"index": 1, "type": barrier_type, "content": "barrier"},
                current_block,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


def test_merge_para_text_blocks_uses_following_lines_for_horizontal_single_line_width() -> None:
    """Validating a single horizontal line ignores the indentation of the first line and uses the remaining four lines to make up the virtual column width."""
    previous_block = _horizontal_multiline_block(
        0,
        "unfinished",
        left=0.1,
        right=0.45,
        top=0.5,
        line_count=4,
    )
    current_block = _horizontal_single_line_block(1, "tail.", left=0.55, right=0.67, top=0.1)
    following_block = _horizontal_multiline_block(
        2,
        "following paragraph.",
        left=0.55,
        right=0.9,
        top=0.2,
        first_indent=0.08,
    )
    pages = [{"page_idx": 0, "blocks": [previous_block, current_block, following_block]}]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True


def test_merge_para_text_blocks_uses_next_page_lines_for_horizontal_single_line_width() -> None:
    """Verify that a horizontal cross-page single line uses the next five lines of the current page to fill the width and does not depend on the number of columns."""
    previous_block = _horizontal_multiline_block(
        0,
        "unfinished",
        left=0.55,
        right=0.9,
        top=0.5,
        line_count=4,
    )
    current_block = _horizontal_single_line_block(0, "tail.", left=0.1, right=0.22, top=0.1)
    following_block = _horizontal_multiline_block(
        1,
        "following paragraph.",
        left=0.1,
        right=0.45,
        top=0.2,
    )
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block, following_block]},
    ]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True


def test_merge_para_text_blocks_uses_following_columns_for_vertical_single_column_height() -> None:
    """Verify that vertical single column will ignore the upper boundary indentation of the first column and use the remaining four columns to make up the virtual column height."""
    previous_block = _vertical_multicolumn_block(
        0,
        "未完",
        right=0.9,
        top=0.1,
        bottom=0.9,
        column_count=4,
    )
    current_block = _vertical_single_column_block(
        0,
        "续。",
        left=0.85,
        right=0.9,
        top=0.1,
        bottom=0.3,
    )
    following_block = _vertical_multicolumn_block(
        1,
        "後續正文。",
        right=0.83,
        top=0.1,
        bottom=0.9,
        first_top_indent=0.08,
    )
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block, following_block]},
    ]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True


@pytest.mark.parametrize("is_vertical", [False, True])
def test_merge_para_text_blocks_requires_three_aligned_lookahead_lines(is_vertical: bool) -> None:
    """Verify that virtual dimensions will not be constructed when there are less than three subsequent coaxial rows in horizontal or vertical rows."""
    if is_vertical:
        previous_block = _vertical_multicolumn_block(
            0,
            "未完",
            right=0.9,
            top=0.1,
            bottom=0.9,
            column_count=4,
        )
        current_block = _vertical_single_column_block(
            0,
            "續。",
            left=0.85,
            right=0.9,
            top=0.1,
            bottom=0.3,
        )
        following_block = _vertical_multicolumn_block(
            1,
            "短參考。",
            right=0.83,
            top=0.1,
            bottom=0.9,
            column_count=2,
        )
    else:
        previous_block = _horizontal_multiline_block(
            0,
            "unfinished",
            left=0.1,
            right=0.45,
            top=0.5,
            line_count=4,
        )
        current_block = _horizontal_single_line_block(0, "tail.", left=0.55, right=0.67, top=0.1)
        following_block = _horizontal_multiline_block(
            1,
            "short reference.",
            left=0.55,
            right=0.9,
            top=0.2,
            line_count=2,
        )
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block, following_block]},
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


@pytest.mark.parametrize("is_vertical", [False, True])
def test_merge_para_text_blocks_stops_single_line_lookahead_at_semantic_barrier(is_vertical: bool) -> None:
    """Validation header barriers prevent horizontal or vertical rows from reading later reference rows."""
    if is_vertical:
        previous_block = _vertical_multicolumn_block(
            0,
            "未完",
            right=0.9,
            top=0.1,
            bottom=0.9,
            column_count=4,
        )
        current_block = _vertical_single_column_block(
            0,
            "續。",
            left=0.85,
            right=0.9,
            top=0.1,
            bottom=0.3,
        )
        following_block = _vertical_multicolumn_block(
            2,
            "後續正文。",
            right=0.83,
            top=0.1,
            bottom=0.9,
        )
    else:
        previous_block = _horizontal_multiline_block(
            0,
            "unfinished",
            left=0.1,
            right=0.45,
            top=0.5,
            line_count=4,
        )
        current_block = _horizontal_single_line_block(0, "tail.", left=0.55, right=0.67, top=0.1)
        following_block = _horizontal_multiline_block(
            2,
            "following paragraph.",
            left=0.55,
            right=0.9,
            top=0.2,
        )
    barrier = {"index": 1, "type": BlockType.PARAGRAPH_TITLE, "content": "barrier"}
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block, barrier, following_block]},
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


@pytest.mark.parametrize("is_vertical", [False, True])
def test_merge_para_text_blocks_rejects_unaligned_following_geometry(is_vertical: bool) -> None:
    """Verifies that virtual dimensions will not be made up if the axis start points of the next five rows or columns are not aligned with the current single row."""
    if is_vertical:
        previous_block = _vertical_multicolumn_block(
            0,
            "未完",
            right=0.9,
            top=0.1,
            bottom=0.9,
            column_count=4,
        )
        current_block = _vertical_single_column_block(
            0,
            "續。",
            left=0.85,
            right=0.9,
            top=0.1,
            bottom=0.3,
        )
        following_block = _vertical_multicolumn_block(
            1,
            "錯位正文。",
            right=0.83,
            top=0.25,
            bottom=0.9,
        )
    else:
        previous_block = _horizontal_multiline_block(
            0,
            "unfinished",
            left=0.1,
            right=0.45,
            top=0.5,
            line_count=4,
        )
        current_block = _horizontal_single_line_block(0, "tail.", left=0.55, right=0.67, top=0.1)
        following_block = _horizontal_multiline_block(
            1,
            "misaligned paragraph.",
            left=0.7,
            right=0.95,
            top=0.2,
        )
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block, following_block]},
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


@pytest.mark.parametrize("is_vertical", [False, True])
def test_merge_para_text_blocks_does_not_look_beyond_current_page(is_vertical: bool) -> None:
    """Verify that five rows or columns on the next page will not be read when the current page has no reference rows and columns."""
    if is_vertical:
        previous_block = _vertical_multicolumn_block(
            0,
            "未完",
            right=0.9,
            top=0.1,
            bottom=0.9,
            column_count=4,
        )
        current_block = _vertical_single_column_block(
            0,
            "續。",
            left=0.85,
            right=0.9,
            top=0.1,
            bottom=0.3,
        )
        later_block = _vertical_multicolumn_block(
            0,
            "更後正文。",
            right=0.83,
            top=0.1,
            bottom=0.9,
        )
    else:
        previous_block = _horizontal_multiline_block(
            0,
            "unfinished",
            left=0.1,
            right=0.45,
            top=0.5,
            line_count=4,
        )
        current_block = _horizontal_single_line_block(0, "tail.", left=0.55, right=0.67, top=0.1)
        later_block = _horizontal_multiline_block(
            0,
            "later paragraph.",
            left=0.55,
            right=0.9,
            top=0.2,
        )
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {"page_idx": 1, "blocks": [current_block]},
        {"page_idx": 2, "blocks": [later_block]},
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


def test_can_auto_merge_horizontal_text_blocks_rejects_paragraph_boundaries() -> None:
    """Validation of horizontal alignment rules rejects candidates with terminating punctuation, special paragraph headers, and those that do not meet geometric conditions."""
    previous_block, current_block = _horizontal_pair()
    assert can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    previous_block["content"] = _inline("finished.")
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    previous_block["content"] = "<hyperlink>finished.<url>https://example.test/no-period</url></hyperlink>"
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    for current_content in ("1 numbered", "Uppercase"):
        previous_block, current_block = _horizontal_pair()
        current_block["content"] = current_content
        assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    current_block["content"] = "<hyperlink>Uppercase<url>https://example.test/lowercase</url></hyperlink>"
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    current_block["lines"][0]["bbox"][0] = 0.2
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    previous_block["lines"][-1]["bbox"][2] = 0.7
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    for line in current_block["lines"]:
        line["bbox"][2] = 0.4
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    previous_block["lines"] = previous_block["lines"][:1]
    current_block["lines"] = current_block["lines"][:1]
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _horizontal_pair()
    current_block["bbox"][1] = 0.31
    assert not can_auto_merge_text_blocks(current_block, previous_block)


def test_merge_para_text_blocks_supports_vertical_but_rejects_mixed_orientation() -> None:
    """Verify that the vertical blocks can be continuous and will not be mislabeled when the horizontal and vertical directions are inconsistent."""
    previous_vertical, current_vertical = _vertical_pair()
    vertical_pages = [{"page_idx": 0, "blocks": [previous_vertical, current_vertical]}]

    merge_para_text_blocks(vertical_pages)

    assert current_vertical["continues_prev"] is True

    previous_vertical, _ = _vertical_pair()
    _, current_horizontal = _horizontal_pair()
    mixed_pages = [{"page_idx": 0, "blocks": [previous_vertical, current_horizontal]}]

    merge_para_text_blocks(mixed_pages)

    assert "continues_prev" not in current_horizontal


def test_ref_text_rule_relaxes_leading_edge_and_initial_character() -> None:
    """Verify that ref_text relaxes the current starting boundary and the starting limit of digits or uppercase characters."""
    previous_block, current_block = _ref_text_horizontal_pair()
    assert can_auto_merge_ref_text_blocks(current_block, previous_block)
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    previous_block["content"] = "finished."
    assert not can_auto_merge_ref_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    current_block["content"] = _inline("Uppercase")
    assert can_auto_merge_ref_text_blocks(current_block, previous_block)
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    current_block["content"] = _inline("1470–1480, Beijing, China.")
    assert can_auto_merge_ref_text_blocks(current_block, previous_block)
    assert not can_auto_merge_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    previous_block["lines"][-1]["bbox"][2] = 0.7
    assert not can_auto_merge_ref_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    for line in current_block["lines"]:
        line["bbox"][2] = 0.4
    assert not can_auto_merge_ref_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    previous_block["lines"] = previous_block["lines"][:1]
    current_block["lines"] = current_block["lines"][:1]
    assert not can_auto_merge_ref_text_blocks(current_block, previous_block)

    previous_block, current_block = _ref_text_horizontal_pair()
    current_block["bbox"][1] = 0.31
    assert not can_auto_merge_ref_text_blocks(current_block, previous_block)

    previous_vertical, current_vertical = _vertical_pair()
    previous_vertical["type"] = BlockType.REF_TEXT
    current_vertical["type"] = BlockType.REF_TEXT
    current_vertical["lines"][0]["bbox"][1] = 0.2
    assert can_auto_merge_ref_text_blocks(current_vertical, previous_vertical)
    assert not can_auto_merge_text_blocks(current_vertical, previous_vertical)


def test_merge_para_text_blocks_marks_indented_ref_text_without_moving_content() -> None:
    """Verify that within the page ref_text only writes the tag after the block and preserves the content with bbox."""
    previous_block, current_block = _ref_text_horizontal_pair()
    pages = [{"page_idx": 0, "blocks": [previous_block, current_block]}]
    original_contents = [previous_block["content"], current_block["content"]]
    original_bboxes = deepcopy([previous_block["bbox"], current_block["bbox"]])

    merge_para_text_blocks(pages)

    assert "continues_prev" not in previous_block
    assert current_block["continues_prev"] is True
    assert [previous_block["content"], current_block["content"]] == original_contents
    assert [previous_block["bbox"], current_block["bbox"]] == original_bboxes
    assert "lines" not in previous_block
    assert "lines" not in current_block

    first_result = deepcopy(pages)
    merge_para_text_blocks(pages)
    assert pages == first_result


def test_merge_para_text_blocks_marks_numeric_reference_page_range() -> None:
    """Verify that ref_text at the beginning of the numerical page range can continue the previous unfinished reference."""
    previous_block, current_block = _ref_text_horizontal_pair()
    previous_block["content"] = _inline("Proceedings of the conference, pages")
    current_block["content"] = _inline("1470–1480, Beijing, China.")
    pages = [{"page_idx": 0, "blocks": [previous_block, current_block]}]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True


@pytest.mark.parametrize(
    ("page_indices", "expected_continuation"),
    [((0, 1), True), ((0, 2), False), ((2, 1), False)],
)
def test_merge_para_text_blocks_requires_consecutive_ref_text_pages(
    page_indices: tuple[int, int],
    expected_continuation: bool,
) -> None:
    """Verification ref_text only allows continuation from the previous consecutive page in the same read chain."""
    previous_block, current_block = _ref_text_horizontal_pair()
    pages = [
        {"page_idx": page_indices[0], "blocks": [previous_block]},
        {"page_idx": page_indices[1], "blocks": [current_block]},
    ]

    merge_para_text_blocks(pages)

    assert current_block.get("continues_prev", False) is expected_continuation


def test_merge_para_text_blocks_skips_page_auxiliary_blocks_between_ref_text() -> None:
    """Verifying cross-page ref_text will skip all page auxiliary block lookup preambles ref_text."""
    previous_block, current_block = _ref_text_horizontal_pair()
    current_block["index"] = 3
    pages = [
        {
            "page_idx": 8,
            "blocks": [
                previous_block,
                {"index": 1, "type": BlockType.FOOTER, "content": "footer"},
                {"index": 2, "type": BlockType.PAGE_FOOTNOTE, "content": "page footnote"},
            ],
        },
        {
            "page_idx": 9,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                {"index": 1, "type": BlockType.PAGE_NUMBER, "content": "9"},
                {"index": 2, "type": BlockType.ASIDE_TEXT, "content": "aside"},
                current_block,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert current_block["continues_prev"] is True


@pytest.mark.parametrize(
    "barrier_type",
    [BlockType.TEXT, BlockType.PARAGRAPH_TITLE, BlockType.IMAGE, BlockType.TABLE, BlockType.LIST],
)
def test_merge_para_text_blocks_keeps_semantic_barriers_between_ref_text(barrier_type: str) -> None:
    """Verification ref_text cannot span semantic blocks outside of page auxiliary types."""
    previous_block, current_block = _ref_text_horizontal_pair()
    current_block["index"] = 2
    barrier = {
        "index": 1,
        "type": barrier_type,
        "content": [] if barrier_type == BlockType.LIST else "barrier",
    }
    pages = [{"page_idx": 0, "blocks": [previous_block, barrier, current_block]}]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_block


@pytest.mark.parametrize(
    ("following_type", "expected_continuation"),
    [(BlockType.REF_TEXT, True), (BlockType.TEXT, False)],
)
def test_ref_text_single_line_lookahead_uses_only_same_type(
    following_type: str,
    expected_continuation: bool,
) -> None:
    """Verify that a ref_text single row only uses subsequent ref_text on the current page to make up the virtual column width."""
    previous_block = _horizontal_multiline_block(
        0,
        "unfinished",
        left=0.55,
        right=0.9,
        top=0.5,
        line_count=4,
    )
    current_block = _horizontal_single_line_block(0, "tail.", left=0.1, right=0.22, top=0.1)
    following_block = _horizontal_multiline_block(
        2,
        "following reference.",
        left=0.1,
        right=0.45,
        top=0.2,
    )
    previous_block["type"] = BlockType.REF_TEXT
    current_block["type"] = BlockType.REF_TEXT
    following_block["type"] = following_type
    pages = [
        {"page_idx": 0, "blocks": [previous_block]},
        {
            "page_idx": 1,
            "blocks": [
                current_block,
                {"index": 1, "type": BlockType.PAGE_NUMBER, "content": "2"},
                following_block,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert current_block.get("continues_prev", False) is expected_continuation


def test_ref_list_and_ref_text_store_independent_continuation_markers() -> None:
    """Verify that ref, list and the top-level ref_text in xhigh style results hold separate continuation markers."""
    previous_list = _list_block(0, "ref one")
    current_list = _list_block(1, "ref two")
    previous_ref_text, current_ref_text = _ref_text_horizontal_pair()
    previous_ref_text["index"] = 2
    current_ref_text["index"] = 3
    pages = [
        {
            "page_idx": 0,
            "blocks": [
                previous_list,
                {"index": 1, "type": BlockType.PAGE_FOOTNOTE, "content": "note"},
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                current_list,
                previous_ref_text,
                current_ref_text,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert current_list["continues_prev"] is True
    assert current_ref_text["continues_prev"] is True
    assert "continues_prev" not in previous_list
    assert "continues_prev" not in previous_ref_text


def test_merge_para_text_blocks_marks_adjacent_ref_lists_without_moving_items() -> None:
    """Verify adjacent references on consecutive pages list Only adds markers, does not move list items."""
    previous_list = {
        "index": 0,
        "type": BlockType.LIST,
        "sub_type": BlockType.REF_TEXT,
        "bbox": [0.1, 0.1, 0.9, 0.3],
        "content": [{"type": BlockType.REF_TEXT, "content": "ref one", "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}]}],
    }
    current_list = {
        "index": 0,
        "type": BlockType.LIST,
        "sub_type": BlockType.REF_TEXT,
        "bbox": [0.1, 0.1, 0.9, 0.3],
        "content": [{"type": BlockType.REF_TEXT, "content": "ref two", "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}]}],
    }
    pages = [
        {"page_idx": 4, "blocks": [previous_list]},
        {"page_idx": 5, "blocks": [current_list]},
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in previous_list
    assert current_list["continues_prev"] is True
    assert [child["content"] for child in previous_list["content"]] == ["ref one"]
    assert [child["content"] for child in current_list["content"]] == ["ref two"]
    assert "lines" not in previous_list["content"][0]
    assert "lines" not in current_list["content"][0]

    previous_list, current_list = deepcopy(previous_list), deepcopy(current_list)
    current_list.pop("continues_prev", None)
    gap_pages = [
        {"page_idx": 4, "blocks": [previous_list]},
        {"page_idx": 6, "blocks": [current_list]},
    ]
    merge_para_text_blocks(gap_pages)
    assert "continues_prev" not in current_list

    previous_list = _list_block(0, "ref one")
    current_list = _list_block(0, "ordinary item", sub_type=BlockType.TEXT)
    merge_para_text_blocks(
        [
            {"page_idx": 4, "blocks": [previous_list]},
            {"page_idx": 5, "blocks": [current_list]},
        ]
    )
    assert "continues_prev" not in current_list


def test_merge_para_text_blocks_skips_page_auxiliary_blocks_between_ref_lists() -> None:
    """Verifying cross-page references will skip all page auxiliary blocks on the previous and following pages to establish a continuation relationship."""
    previous_list = _list_block(0, "ref one")
    current_list = _list_block(3, "ref two")
    pages = [
        {
            "page_idx": 8,
            "blocks": [
                previous_list,
                {"index": 1, "type": BlockType.FOOTER, "content": "footer"},
                {"index": 2, "type": BlockType.PAGE_FOOTNOTE, "content": "page footnote"},
            ],
        },
        {
            "page_idx": 9,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                {"index": 1, "type": BlockType.PAGE_NUMBER, "content": "9"},
                {"index": 2, "type": BlockType.ASIDE_TEXT, "content": "aside"},
                current_list,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in previous_list
    assert current_list["continues_prev"] is True
    assert inline_text(previous_list["content"][0]["content"]) == "ref one"
    assert inline_text(current_list["content"][0]["content"]) == "ref two"
    assert "lines" not in previous_list["content"][0]
    assert "lines" not in current_list["content"][0]


@pytest.mark.parametrize(
    "barrier_type",
    [BlockType.TEXT, BlockType.PARAGRAPH_TITLE, BlockType.IMAGE, BlockType.TABLE, BlockType.LIST],
)
def test_merge_para_text_blocks_keeps_semantic_barriers_between_ref_lists(barrier_type: str) -> None:
    """After verifying that the page auxiliary block is transparent, other semantic blocks will still block the reference list continuation."""
    previous_list = _list_block(0, "ref one")
    current_list = _list_block(2, "ref two")
    barrier = {
        "index": 1,
        "type": barrier_type,
        "content": [] if barrier_type == BlockType.LIST else "barrier",
    }
    pages = [
        {
            "page_idx": 8,
            "blocks": [
                previous_list,
                {"index": 1, "type": BlockType.PAGE_FOOTNOTE, "content": "page footnote"},
            ],
        },
        {
            "page_idx": 9,
            "blocks": [
                {"index": 0, "type": BlockType.HEADER, "content": "header"},
                barrier,
                current_list,
            ],
        },
    ]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in current_list


def test_merge_para_text_blocks_cleans_nested_lines_and_invalid_stale_markers() -> None:
    """Verify that illegal text is not merged while recursively cleaning up temporary line boxes and expiration markers."""
    nested_child = {
        "type": BlockType.IMAGE_CAPTION,
        "content": "caption",
        "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}],
        "continues_prev": True,
    }
    visual_block = {
        "index": 0,
        "type": BlockType.IMAGE,
        "bbox": [0.1, 0.1, 0.9, 0.5],
        "content": [nested_child],
        "lines": [{"bbox": [0.1, 0.1, 0.9, 0.5]}],
    }
    invalid_text = {
        "index": 1,
        "type": BlockType.TEXT,
        "bbox": [0.1, 0.5, 0.9, 0.7],
        "content": "invalid lines",
        "lines": [{"bbox": [0.1, 0.5, 0.9]}],
        "continues_prev": True,
    }
    pages = [{"page_idx": 0, "blocks": [visual_block, invalid_text]}]

    merge_para_text_blocks(pages)

    assert "continues_prev" not in nested_child
    assert "continues_prev" not in invalid_text
    assert "lines" not in visual_block
    assert "lines" not in nested_child
    assert "lines" not in invalid_text
