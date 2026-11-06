"""跨页段落延续规则的聚焦回归测试。"""

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
    """构造使用归一化 block/line bbox 的 dict text/ref_text block。"""
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
    """构造带直属文本子项和临时行框的 dict list block。"""
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
    """构造一组满足横排段落延续规则的前后 text block。"""
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
    """构造首行缩进但其余规则满足续接条件的 ref_text 块对。"""
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
    """构造可控制首行缩进的横排多行正文块。"""
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
    """构造需要使用后续行补足虚拟宽度的横排单行块。"""
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
    """构造从右向左排列且可控制首列上边界的竖排多列正文块。"""
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
    """构造需要使用后续列补足虚拟高度的竖排单列块。"""
    return _text_block(
        index,
        content,
        [left, top, right, bottom],
        [[left, top, right, bottom]],
    )


def _vertical_pair() -> tuple[dict, dict]:
    """构造一组满足竖排段落延续规则的前后 text block。"""
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
    """验证倒序处理会给连续链的后续块加标记，但不搬运正文或改写 bbox。"""
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
    """验证跨页 text 仅允许从前一连续页延续。"""
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
    """验证视觉根块和页面装饰块可跨过，其他语义块会阻断 text 查找。"""
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
    """验证跨页正文会跳过前页尾部和后页头部的全部页面装饰块。"""
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
    """验证页面装饰块透明后，标题、公式和列表仍会阻断跨页正文连接。"""
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
    """验证横排单行会忽略缩进首行并使用其余四行补足虚拟栏宽。"""
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
    """验证横排跨页单行使用当前页后续五行补足宽度而不依赖栏位数量。"""
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
    """验证竖排单列会忽略上边界缩进首列并使用其余四列补足虚拟栏高。"""
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
    """验证横排或竖排后续同轴行列不足三条时不会构造虚拟尺寸。"""
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
    """验证标题屏障会阻止横排或竖排单行读取更后的参考行列。"""
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
    """验证后续五行或列的轴起点与当前单行不对齐时不会补足虚拟尺寸。"""
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
    """验证当前页没有参考行列时不会读取再下一页的五行或列。"""
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
    """验证横排规则会拒绝终止标点、特殊段首以及不满足几何条件的候选。"""
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
    """验证竖排块可以连续，而横竖方向不一致时不会误标记。"""
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
    """验证 ref_text 放宽当前起始边界以及数字或大写字符开头限制。"""
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
    """验证页内 ref_text 只在后块写标记，并保留内容与 bbox。"""
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
    """验证数字页码范围开头的 ref_text 可以续接前一条未结束参考文献。"""
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
    """验证 ref_text 仅允许从同一阅读链的前一连续页续接。"""
    previous_block, current_block = _ref_text_horizontal_pair()
    pages = [
        {"page_idx": page_indices[0], "blocks": [previous_block]},
        {"page_idx": page_indices[1], "blocks": [current_block]},
    ]

    merge_para_text_blocks(pages)

    assert current_block.get("continues_prev", False) is expected_continuation


def test_merge_para_text_blocks_skips_page_auxiliary_blocks_between_ref_text() -> None:
    """验证跨页 ref_text 会跳过全部页面辅助块查找前序 ref_text。"""
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
    """验证 ref_text 不能跨越页面辅助类型之外的语义块。"""
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
    """验证 ref_text 单行只使用当前页后续 ref_text 补足虚拟栏宽。"""
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
    """验证 xhigh 风格结果中的 ref list 与顶层 ref_text 分别保存续接标记。"""
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
    """验证连续页的相邻参考文献 list 只增加标记，不搬运列表子项。"""
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
    """验证跨页参考文献会跳过前后页的全部页面辅助块建立延续关系。"""
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
    """验证页面辅助块透明后，其他语义块仍会阻断参考文献列表延续。"""
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
    """验证非法 text 不合并，同时递归清理临时行框和过期标记。"""
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


def _layout_continuation_blocks(*, previous_ending: str = "", current_start: str = "Orion") -> tuple[dict, dict, dict]:
    """构造可指定首尾文本的跨栏正文及唯一的同页首行缩进样本。"""
    previous = _horizontal_multiline_block(
        0, f"the comparison includes{previous_ending}", left=0.1, right=0.45, top=0.65, line_count=3
    )
    current = _horizontal_multiline_block(
        1, f"{current_start} observations continue here.", left=0.55, right=0.9, top=0.1, line_count=3
    )
    witness = _horizontal_multiline_block(
        2,
        "A separate paragraph supplies indentation evidence.",
        left=0.55,
        right=0.9,
        top=0.4,
        line_count=3,
        first_indent=0.04,
    )
    witness["_paragraph_boundary"] = True
    return previous, current, witness


@pytest.mark.parametrize("placement", ["same_page_before", "same_page_after", "cross_page_after"])
@pytest.mark.parametrize("previous_ending", ["", ".", "!", "?", "。", "！", "？"])
@pytest.mark.parametrize("current_start", ["Orion", "continued"])
def test_layout_continuation_uses_one_witness_on_current_page(placement: str, previous_ending: str, current_start: str) -> None:
    """同页可信缩进与续排几何可同时放宽普通句末符和大写开头，并保持内容及幂等性。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    if placement == "cross_page_after":
        pages = [{"page_idx": 0, "blocks": [previous]}, {"page_idx": 1, "blocks": [current, witness]}]
    else:
        blocks = [witness, previous, current] if placement == "same_page_before" else [previous, current, witness]
        pages = [{"page_idx": 0, "blocks": blocks}]
    original_contents = [deepcopy(block["content"]) for page in pages for block in page["blocks"]]
    original_boxes = [deepcopy(block["bbox"]) for page in pages for block in page["blocks"]]
    merge_para_text_blocks(pages)
    assert current.get("continues_prev") is True
    assert not witness.get("continues_prev", False)
    assert original_contents == [block["content"] for page in pages for block in page["blocks"]]
    assert original_boxes == [block["bbox"] for page in pages for block in page["blocks"]]
    finalized = deepcopy(pages)
    merge_para_text_blocks(pages)
    assert pages == finalized


@pytest.mark.parametrize(
    "witness_kind",
    [
        "absent",
        "previous_page",
        "next_page",
        "footnote",
        "caption",
        "list_child",
        "reference_start",
        "reference_continuation",
        "numbered",
        "bullet",
        "whole_block_offset",
        "hanging",
        "invalid_line",
        "missing_lines",
        "single_line",
        "boundary_only",
        "unstable_following",
        "vertical",
    ],
)
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_layout_continuation_rejects_unreliable_or_other_page_witnesses(
    witness_kind: str, previous_ending: str, current_start: str
) -> None:
    """其他页、非正文角色和未证实首行回落的块不能解除大写或句末否决。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    blocks = [previous, current, witness]
    pages = [{"page_idx": 1, "blocks": blocks}]
    if witness_kind in {"absent", "previous_page", "next_page"}:
        blocks.remove(witness)
        if witness_kind == "previous_page":
            pages.insert(0, {"page_idx": 0, "blocks": [witness]})
        elif witness_kind == "next_page":
            pages.append({"page_idx": 2, "blocks": [witness]})
    elif witness_kind == "footnote":
        witness["type"] = BlockType.PAGE_FOOTNOTE
    elif witness_kind == "caption":
        witness["type"] = BlockType.IMAGE_CAPTION
    elif witness_kind == "list_child":
        blocks[-1] = {"type": BlockType.LIST, "index": 2, "sub_type": BlockType.TEXT, "content": [witness]}
    elif witness_kind in {"reference_start", "reference_continuation"}:
        witness["_reference_start"] = witness_kind == "reference_start"
    elif witness_kind in {"numbered", "bullet"}:
        witness["content"] = _inline("1. Another item" if witness_kind == "numbered" else "• Another item")
    elif witness_kind == "whole_block_offset":
        for line in witness["lines"]:
            line["bbox"][0] = 0.59
    elif witness_kind == "hanging":
        witness["lines"][0]["bbox"][0] = 0.55
        for line in witness["lines"][1:]:
            line["bbox"][0] = 0.59
    elif witness_kind == "invalid_line":
        witness["lines"][1]["bbox"] = [0.55, 0.44, 0.55, 0.48]
    elif witness_kind == "missing_lines":
        witness.pop("lines")
    elif witness_kind == "single_line":
        witness["lines"] = witness["lines"][:1]
    elif witness_kind == "boundary_only":
        witness["lines"][0]["bbox"][0] = 0.55
    elif witness_kind == "unstable_following":
        witness["lines"][1]["bbox"][0] = 0.58
    elif witness_kind == "vertical":
        witness.update(
            _vertical_multicolumn_block(2, "A vertical paragraph", right=0.8, top=0.1, bottom=0.9, first_top_indent=0.08)
        )
    merge_para_text_blocks(pages)
    assert not current.get("continues_prev", False)


@pytest.mark.parametrize(
    "boundary",
    ["indent", "hard_break", "digit", "terminal", "short_tail", "width", "title", "invalid_line", "list_tail", "reference"],
)
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_layout_permission_preserves_existing_boundaries(boundary: str, previous_ending: str, current_start: str) -> None:
    """同页缩进证据不能绕过候选自身段界、数字、语义屏障和现有几何保护。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    blocks = [previous, current, witness]
    if boundary == "indent":
        current["lines"][0]["bbox"][0] += 0.04
    elif boundary == "hard_break":
        current["_paragraph_boundary"] = True
    elif boundary == "digit":
        current["content"] = _inline("2026 observations continue here.")
    elif boundary == "terminal":
        previous["content"] = _inline("The comparison includes:")
    elif boundary == "short_tail":
        previous["lines"][-1]["bbox"][2] -= 0.1
    elif boundary == "width":
        for line in current["lines"]:
            line["bbox"][2] = 0.65
    elif boundary == "title":
        blocks.insert(1, {"type": BlockType.PARAGRAPH_TITLE, "index": 10, "content": _inline("Another section")})
    elif boundary == "invalid_line":
        current["lines"][1]["bbox"] = [0.9, 0.1, 0.55, 0.2]
    elif boundary == "list_tail":
        blocks[0] = {"type": BlockType.LIST, "sub_type": BlockType.TEXT, "index": 0, "content": [previous]}
    elif boundary == "reference":
        current["_reference_start"] = True
    merge_para_text_blocks([{"page_idx": 0, "blocks": blocks}])
    assert not current.get("continues_prev", False)


@pytest.mark.parametrize("ending", [")", "）", '"', "”", ":", "：", ";", "；", ".)", '."', "。”"])
def test_layout_permission_preserves_structural_endings(ending: str) -> None:
    """可信缩进只放宽普通句末符，闭括号、引号、冒号和分号仍构成边界。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=ending, current_start="continued")
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, witness]}])
    assert not current.get("continues_prev", False)


@pytest.mark.parametrize("line_count,offset,expected", [(3, 0, True), (2, 0, False), (3, 0.02, False), (3, 0.019, True)])
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_single_line_layout_permission_requires_current_page_peer_left_edge(
    line_count: int, offset: float, expected: bool, previous_ending: str, current_start: str
) -> None:
    """单行的大写与句末放宽须由至少三条后续同栏行证明贴左，半行高临界偏移仍拒绝。"""
    previous, _, witness = _layout_continuation_blocks(previous_ending=previous_ending)
    current = _horizontal_single_line_block(1, f"{current_start}.", left=0.55 + offset, right=0.67, top=0.1)
    peers = _horizontal_multiline_block(2, "another paragraph.", left=0.55, right=0.9, top=0.2, line_count=line_count)
    # 隔断后续行查找，确保证据段只开启页开关，不替不足三行的局部参照凑数。
    barrier = {"type": BlockType.PARAGRAPH_TITLE, "index": 3, "content": _inline("Next section")}
    witness["index"] = 4
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, peers, barrier, witness]}])
    assert current.get("continues_prev", False) is expected


@pytest.mark.parametrize("wide", [False, True])
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_single_line_layout_permission_cannot_bypass_peer_check_on_normal_path(
    wide: bool, previous_ending: str, current_start: str
) -> None:
    """单行即使宽度直接满足普通连接分支，也不能以自身块框证明贴左。"""
    previous, _, witness = _layout_continuation_blocks(previous_ending=previous_ending)
    current = _horizontal_single_line_block(1, f"{current_start}.", left=0.58, right=0.9 if wide else 0.67, top=0.1)
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, witness]}])
    assert not current.get("continues_prev", False)


@pytest.mark.parametrize("location", ["same_page", "previous_page"])
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
@pytest.mark.parametrize("page_gap", [1, 2])
def test_cross_page_layout_permission_uses_consecutive_destination_page_only(
    location: str, previous_ending: str, current_start: str, page_gap: int
) -> None:
    """大写与句末放宽只使用相邻后页的缩进证据，跨缺页或前页证据仍拒绝。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    pages = [{"page_idx": 3, "blocks": [previous]}, {"page_idx": 3 + page_gap, "blocks": [current]}]
    pages[1 if location == "same_page" else 0]["blocks"].append(witness)
    merge_para_text_blocks(pages)
    assert current.get("continues_prev", False) is (location == "same_page" and page_gap == 1)


@pytest.mark.parametrize("scale,shift,width", [(0.7, 0.05, 0.35), (1, 0, 0.28), (1.1, -0.05, 0.31)])
@pytest.mark.parametrize("word", ["Orion", "NASA", "Different"])
@pytest.mark.parametrize("previous_ending", ["", "."])
def test_layout_permission_depends_on_layout_instead_of_specific_text(
    scale: float, shift: float, width: float, word: str, previous_ending: str
) -> None:
    """独立改变正文、栏宽、位置和尺度后，单个同页缩进样本仍提供相同判断。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending)
    current["content"] = _inline(f"{word} observations continue here.")
    for block in (previous, current, witness):
        left = block["bbox"][0]
        block["bbox"][2] = left + width
        for line in block["lines"]:
            line["bbox"][2] = left + width
        for bbox in [block["bbox"], *(line["bbox"] for line in block["lines"])]:
            bbox[:] = [value * scale + (shift if axis % 2 == 0 else 0) for axis, value in enumerate(bbox)]
    merge_para_text_blocks([{"page_idx": 5, "blocks": [previous, current, witness]}])
    assert current.get("continues_prev") is True


@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_page_layout_permission_is_batch_independent_and_not_public_default(previous_ending: str, current_start: str) -> None:
    """增加其他页不会改变页内候选判断，缺少页面上下文的双块公开函数仍保持保守。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    assert not can_auto_merge_text_blocks(current, previous)
    standalone = [{"page_idx": 7, "blocks": [previous, current, witness]}]
    batched = [{"page_idx": 6, "blocks": []}, *deepcopy(standalone), {"page_idx": 8, "blocks": []}]
    merge_para_text_blocks(standalone)
    merge_para_text_blocks(batched)
    assert standalone[0] == batched[1]
    assert current.get("continues_prev") is True


@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_horizontal_witness_does_not_relax_vertical_boundaries(previous_ending: str, current_start: str) -> None:
    """同页横排缩进样本不能改变竖排正文的大写开头与句末保护。"""
    previous, current = _vertical_pair()
    previous["content"] = _inline(f"previous vertical text{previous_ending}")
    current["content"] = _inline(f"{current_start} vertical text")
    _, _, witness = _layout_continuation_blocks()
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, witness]}])
    assert not current.get("continues_prev", False)


@pytest.mark.parametrize("indent,expected", [(0.019, False), (0.02, True), (0.021, True)])
@pytest.mark.parametrize("previous_ending,current_start", [("", "Orion"), (".", "continued")])
def test_two_line_witness_uses_existing_half_height_threshold(
    indent: float, expected: bool, previous_ending: str, current_start: str
) -> None:
    """两行正文也可证明首行回落，并严格锁定既有半行高缩进门槛两侧。"""
    previous, current, witness = _layout_continuation_blocks(previous_ending=previous_ending, current_start=current_start)
    witness["lines"] = witness["lines"][:2]
    witness["lines"][0]["bbox"][0] = 0.55 + indent
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, witness]}])
    assert current.get("continues_prev", False) is expected


@pytest.mark.parametrize("layout", ["same_column_overlap", "backward_column", "side_by_side_labels", "centered_witness"])
@pytest.mark.parametrize("previous_ending", ["", "."])
def test_page_indentation_does_not_join_unrelated_layout_regions(layout: str, previous_ending: str) -> None:
    """真实回归中的同栏条目、逆向摘要、并排作者与居中表格标签均不得借页证据误接。"""
    previous, current, witness = _layout_continuation_blocks()
    if layout == "same_column_overlap":
        previous = _text_block(0, "Other author, 2025, volume 12", [0.1, 0.5, 0.4, 0.52], [[0.1, 0.5, 0.4, 0.52]])
        current = _text_block(
            1,
            "Different author, 2026, volume 13",
            [0.1, 0.519, 0.45, 0.559],
            [[0.1, 0.519, 0.45, 0.539], [0.12, 0.539, 0.45, 0.559]],
        )
    elif layout == "backward_column":
        for block, delta in [(previous, 0.45), (current, -0.45)]:
            for bbox in [block["bbox"], *(line["bbox"] for line in block["lines"])]:
                bbox[0] += delta
                bbox[2] += delta
    elif layout == "side_by_side_labels":
        previous = _text_block(
            0, "First author Second author", [0.1, 0.12, 0.4, 0.16], [[0.1, 0.12, 0.39, 0.14], [0.12, 0.14, 0.4, 0.16]]
        )
        current = _text_block(
            1, "Third author Fourth author", [0.45, 0.12, 0.75, 0.16], [[0.45, 0.12, 0.75, 0.14], [0.46, 0.14, 0.75, 0.16]]
        )
    elif layout == "centered_witness":
        witness["lines"][0]["bbox"][2] = 0.86
        witness["content"] = _inline("Grade eight Mathematics")
    previous["content"] = _inline(f"{inline_text(previous['content'])}{previous_ending}")
    if previous_ending:
        current["content"] = _inline(inline_text(current["content"]).lower())
    merge_para_text_blocks([{"page_idx": 0, "blocks": [previous, current, witness]}])
    assert not current.get("continues_prev", False)
