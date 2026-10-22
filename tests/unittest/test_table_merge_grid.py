"""验证跨页表格的真实列对齐、保守拒绝及原页续接标志投影。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

from docvortex.content.table import build_row_rendered_cell_segments, build_table_state_from_html, merge_table_content
from docvortex.render import RenderMode, render_markdown
from docvortex.schema import MiddleJson


def _table(html: str, flags: object = None) -> dict[str, Any]:
    """构造最小 PDF 表格，保留传入的非法标志供降级回归使用。"""
    table = {
        "type": "table",
        "index": 0,
        "bbox": [0.1, 0.1, 0.9, 0.9],
        "content": [{"type": "table_body", "index": 0, "bbox": [0.1, 0.1, 0.9, 0.9], "content": html}],
    }
    if flags is not None:
        table["cell_merge"] = flags
    return table


def _html(table: dict[str, Any]) -> str:
    """读取表格唯一主体的 HTML。"""
    return next(child["content"] for child in table["content"] if child["type"] == "table_body")


def _row_texts(html: str) -> list[list[str]]:
    """按显式单元格读取合并前后的文字，避免只用列数断言完整性。"""
    soup = BeautifulSoup(html, "html.parser")
    return [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in soup.find_all("tr")]


def _middle(*tables: dict[str, Any]) -> MiddleJson:
    """构造连续页的严格协议，触发真实 renderer 的多页合并链。"""
    copied_tables = deepcopy(tables)
    for table in copied_tables[1:]:
        table["continues_prev"] = True
    return MiddleJson.model_validate(
        {
            "pages": [{"page_idx": index, "blocks": [table]} for index, table in enumerate(copied_tables)],
            "is_full_document": True,
            "metadata": {"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "test"}},
        }
    )


def test_issue_5652_aligns_all_columns_without_changing_source() -> None:
    """用真实模型输出锁定十四列与五列续表，继承格也必须保持原列。"""
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "table_merge_issue_5652.json").read_text(encoding="utf-8"))
    previous = _table(fixture["previous_html"])
    current = _table(fixture["current_html"], fixture["cell_merge"])
    originals = deepcopy((previous, current))

    merged = merge_table_content(previous, current)

    assert merged is not None
    state = build_table_state_from_html(_html(merged))
    assert state is not None
    assert state.total_cols == 14
    for row_index in range(len(state.rows) - 2, len(state.rows)):
        segments = build_row_rendered_cell_segments(state.rows, row_index)
        assert [(segment.start_col, segment.end_col) for segment in segments] == [(0, 1), (1, 2), (2, 8), (8, 10), (10, 14)]
    assert _row_texts(_html(merged))[-3:] == [
        ["人2", "工2"],
        ["", "2", "任务 2", "人 1", "公 3"],
        ["人 2", "公 4"],
    ]
    assert (previous, current) == originals

    middle = _middle(previous, current)
    original_middle = middle.model_copy(deep=True)
    rendered = render_markdown(middle)
    assert rendered.count("<table>") == 1
    assert 'colspan="6" rowspan="2">任务 2' in rendered
    assert 'colspan="4">公 3' in rendered
    assert render_markdown(middle, mode=RenderMode.FULL).count("<table>") == 2
    assert middle == original_middle


def test_issue_5652_cell_flags_never_move_work_text_into_person_column() -> None:
    """即使模型错误要求续接人员和工作内容，也只能转入各自的同列目标。"""
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "table_merge_issue_5652.json").read_text(encoding="utf-8"))
    previous = _table(fixture["previous_html"])
    current = _table(fixture["current_html"], [0] * 8 + [1] * 6)

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(_html(merged))[-3:] == [
        ["人2人 1", "工2公 3"],
        ["", "2", "任务 2", "", ""],
        ["人 2", "公 4"],
    ]
    assert "人2公" not in _html(merged)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("flags", [[0, 0], [1, 0], [1, 1]])
def test_narrower_previous_and_current_keep_cell_flags_in_original_grid(reverse: bool, flags: list[int]) -> None:
    """双向扩列时标志始终按原前页解释，内容只能续入同列。"""
    narrow = "<table><tr><td>A</td><td>B</td></tr></table>"
    wide = "<table><tr><td colspan='2'>C</td><td>D</td></tr></table>"
    if reverse:
        previous, current = _table(wide), _table(narrow, [flags[0], flags[0], flags[1]])
        first, second = ["C", "D"], ["A", "B"]
    else:
        previous, current = _table(narrow), _table(wide, flags)
        first, second = ["A", "B"], ["C", "D"]

    merged = merge_table_content(previous, current)

    assert merged is not None
    expected_first = [left + right if flag else left for left, right, flag in zip(first, second, flags)]
    expected_second = ["" if flag else text for text, flag in zip(second, flags)]
    expected_rows = [expected_first] + ([expected_second] if any(expected_second) else [])
    assert _row_texts(_html(merged)) == expected_rows
    state = build_table_state_from_html(_html(merged))
    assert state is not None and state.total_cols == 3


def test_inherited_rowspan_owner_receives_only_its_matching_column() -> None:
    """上页末行继承的单元格也可定位，不能把续文落入旁边的显式格。"""
    previous = _table("<table><tr><td rowspan='2'>组A</td><td>X1</td></tr><tr><td>X2</td></tr></table>")
    current = _table("<table><tr><td>组B</td><td>Y</td></tr></table>", [1, 0])

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(_html(merged)) == [["组A组B", "X1"], ["X2"], ["", "Y"]]


def test_repeated_rowspan_header_is_removed_after_grid_alignment() -> None:
    """重复表头含跨行跨列时，先对齐完整网格再移除整个表头。"""
    previous = _table(
        "<table><tr><th rowspan='2' colspan='2'>H1</th><th colspan='2'>H2</th></tr>"
        "<tr><th colspan='2'>H3</th></tr><tr><td colspan='2'>A</td><td colspan='2'>B</td></tr></table>"
    )
    current = _table(
        "<table><tr><th rowspan='2'>H1</th><th>H2</th></tr><tr><th>H3</th></tr><tr><td>C</td><td>D</td></tr></table>"
    )

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(_html(merged)) == [["H1", "H2"], ["H3"], ["A", "B"], ["C", "D"]]
    cells = BeautifulSoup(_html(merged), "html.parser").find_all("tr")[-1].find_all("td")
    assert [cell["colspan"] for cell in cells] == ["2", "2"]


@pytest.mark.parametrize("reverse", [False, True])
def test_unknown_internal_boundary_preserves_independent_tables(reverse: bool) -> None:
    """只在边界行出现的粗网格不能推断内部细格，双向均保守拆开。"""
    narrow = _table("<table><tr><td colspan='2'>A</td><td>B</td></tr><tr><td>C</td><td>D</td><td>E</td></tr></table>")
    wide = _table("<table><tr><td colspan='4'>X</td><td colspan='2'>Y</td></tr></table>")
    previous, current = (narrow, wide) if reverse else (wide, narrow)
    originals = deepcopy((previous, current))

    assert merge_table_content(previous, current) is None
    assert (previous, current) == originals
    assert render_markdown(_middle(previous, current)).count("<table>") == 2


@pytest.mark.parametrize(
    ("previous_html", "current_html"),
    [
        (
            "<table><tr><td colspan='2'>A</td><td>B</td></tr></table>",
            "<table><tr><td>C</td><td colspan='2'>D</td></tr></table>",
        ),
        (
            "<table><tr><td colspan='3'>H</td></tr><tr><td>A</td><td>B</td></tr></table>",
            "<table><tr><td>C</td><td>D</td></tr></table>",
        ),
        (
            "<table><tr><td rowspan='2'>A</td><td>B</td><td rowspan='2'>C</td></tr><tr><td colspan='2'>D</td></tr></table>",
            "<table><tr><td>E</td><td>F</td><td>G</td></tr></table>",
        ),
        (
            "<table><tr><td rowspan='2'>A</td><td>B</td><td>C</td></tr></table>",
            "<table><tr><td>D</td><td>E</td><td>F</td></tr></table>",
        ),
        (
            "<table><tr><td rowspan='0'>A</td><td>B</td></tr></table>",
            "<table><tr><td>C</td><td>D</td></tr></table>",
        ),
    ],
)
def test_ambiguous_or_conflicting_grids_are_not_merged(previous_html: str, current_html: str) -> None:
    """同列异构、缺格、占位重叠和越界跨度不得通过强补列继续合并。"""
    previous, current = _table(previous_html), _table(current_html)
    originals = deepcopy((previous, current))

    assert merge_table_content(previous, current) is None
    assert (previous, current) == originals


@pytest.mark.parametrize("flags", [[1], [1, 0, 0], [True, 0], [2, 0], "11"])
def test_invalid_cell_merge_flags_preserve_all_text(flags: object) -> None:
    """非法标志只关闭内容续接，可靠的结构合并仍完整保留原文。"""
    previous = _table("<table><tr><td>A</td><td>B</td></tr></table>")
    current = _table("<table><tr><td>C</td><td>D</td></tr></table>", flags)

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(_html(merged)) == [["A", "B"], ["C", "D"]]


def test_partial_flag_inside_colspan_does_not_consume_the_cell() -> None:
    """一个跨度内存在相反标志时保留源格，不能按首次命中搬空。"""
    previous = _table("<table><tr><td colspan='2'>A</td><td>B</td></tr></table>")
    current = _table("<table><tr><td colspan='2'>C</td><td>D</td></tr></table>", [1, 0, 0])

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(_html(merged)) == [["A", "B"], ["C", "D"]]


@pytest.mark.parametrize("consume_middle_page", [False, True])
def test_renderer_tracks_original_middle_page_grid_after_full_consumption(consume_middle_page: bool) -> None:
    """三页连续扩列时仍按原第二页解释标志，包括第二页已全消费的情况。"""
    previous = _table("<table><tr><td colspan='2'>A</td><td colspan='2'>B</td></tr></table>")
    middle = _table("<table><tr><td>C</td><td>D</td></tr></table>", [int(consume_middle_page)] * 4)
    current = _table("<table><tr><td colspan='3'>E</td><td colspan='3'>F</td></tr></table>", [0, 1])
    document = _middle(previous, middle, current)
    original = document.model_copy(deep=True)

    rendered = render_markdown(document)

    assert rendered.count("<table>") == 1
    expected = [["AC", "BDF"], ["E", ""]] if consume_middle_page else [["A", "B"], ["C", "DF"], ["E", ""]]
    assert _row_texts(rendered) == expected
    for row in BeautifulSoup(rendered, "html.parser").find_all("tr"):
        assert [cell["colspan"] for cell in row.find_all("td")] == ["3", "3"]
    assert document == original
    assert "boundaries" not in document.model_dump_json()


@pytest.mark.parametrize("last_flags", [[0, 0, 1, 1], [0, 0, 1, 0]])
def test_renderer_projects_original_flags_when_previous_grid_expands_again(last_flags: list[int]) -> None:
    """前页先扩列再继续扩列时，仅对已知区间内一致的标志进行投影。"""
    first = _table("<table><tr><td>A</td><td>B</td></tr></table>")
    second = _table("<table><tr><td colspan='2'>C</td><td colspan='2'>D</td></tr></table>", [1, 0])
    third = _table("<table><tr><td colspan='3'>E</td><td colspan='3'>F</td></tr></table>", last_flags)

    rendered = render_markdown(_middle(first, second, third))

    assert rendered.count("<table>") == 1
    expected = [["AC", "B"], ["", "DF"], ["E", ""]] if last_flags[-1] else [["AC", "B"], ["", "D"], ["E", "F"]]
    assert _row_texts(rendered) == expected
