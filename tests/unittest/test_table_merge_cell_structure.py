"""验证显式跨页续接同时恢复内容、空白区域和真实跨行结构。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from docvortex.content.table import build_row_rendered_cell_segments, build_table_state_from_html, merge_table_content
from docvortex.render import RenderMode, render_html
from docvortex.schema import MiddleJson

from test_table_merge_grid import _html, _middle, _table


def _assert_rectangular(html: str, columns: int) -> None:
    """检查每行都连续覆盖固定视觉列，防止结构合并造成缺格或错位。"""
    state = build_table_state_from_html(html)
    assert state is not None and state.total_cols == columns
    for row_index in range(len(state.rows)):
        segments = build_row_rendered_cell_segments(state.rows, row_index)
        assert segments[0].start_col == 0
        assert segments[-1].end_col == columns
        assert all(left.end_col == right.start_col for left, right in zip(segments, segments[1:]))


@pytest.mark.parametrize("flags,expected_span", [([1, 0], 5), ([0, 0], 2)])
def test_empty_shared_cell_connects_two_and_three_rows_only_when_requested(flags: list[int], expected_span: int) -> None:
    """两行所属组接三行空白延续时连接为五行，零标记必须保留原结构。"""
    previous = _table("<table><tr><td rowspan='2'>Group</td><td>A</td></tr><tr><td>B</td></tr></table>")
    current = _table("<table><tr><td rowspan='3'></td><td>C</td></tr><tr><td>D</td></tr><tr><td>E</td></tr></table>", flags)
    originals = deepcopy((previous, current))
    merged = merge_table_content(previous, current)
    assert merged is not None
    soup = BeautifulSoup(_html(merged), "html.parser")
    rows = soup.find_all("tr")
    assert len(rows) == 5 and rows[0].td["rowspan"] == str(expected_span)
    assert [cell.get_text() for row in rows for cell in row.find_all("td") if cell.get_text()] == [
        "Group",
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    assert len(rows[2].find_all("td")) == (1 if flags[0] else 2)
    _assert_rectangular(_html(merged), 2)
    assert (previous, current) == originals


def test_full_boundary_row_collapse_preserves_longer_shared_span() -> None:
    """整行消费只减去折叠的一行，续入的多行单元格仍覆盖后续数据。"""
    previous = _table("<table><tr><td>A</td><td>X</td></tr></table>")
    current = _table("<table><tr><td rowspan='2'>B</td><td>Y</td></tr><tr><td>Z</td></tr></table>", [1, 1])
    merged = merge_table_content(previous, current)
    assert merged is not None
    soup = BeautifulSoup(_html(merged), "html.parser")
    assert [[c.get_text() for c in r.find_all("td")] for r in soup.find_all("tr")] == [["A B", "X Y"], ["Z"]]
    assert soup.td["rowspan"] == "2"
    _assert_rectangular(_html(merged), 2)


def test_unmerged_blank_cell_keeps_its_row_and_boundary() -> None:
    """只合并一个空白区域时，另一列的零标记阻止整行被语义空白检查删除。"""
    merged = merge_table_content(
        _table("<table><tr><td>A</td><td>X</td></tr></table>"), _table("<table><tr><td></td><td></td></tr></table>", [1, 0])
    )
    assert merged is not None
    soup = BeautifulSoup(_html(merged), "html.parser")
    rows = soup.find_all("tr")
    assert len(rows) == 2 and rows[0].td["rowspan"] == "2"
    assert len(rows[1].find_all("td")) == 1 and rows[1].td.get_text() == ""
    _assert_rectangular(_html(merged), 2)


def test_shared_colspan_cell_preserves_both_dimensions() -> None:
    """空白续接保留跨列宽度，并将共享区域延长至三行。"""
    previous = _table("<table><tr><td colspan='2'>A</td><td>X</td></tr></table>")
    current = _table("<table><tr><td colspan='2' rowspan='2'></td><td>Y</td></tr><tr><td>Z</td></tr></table>", [1, 1, 0])
    merged = merge_table_content(previous, current)
    assert merged is not None
    cell = BeautifulSoup(_html(merged), "html.parser").td
    assert cell["rowspan"] == "3" and cell["colspan"] == "2"
    _assert_rectangular(_html(merged), 3)


def test_badcase111_correct_flags_join_shared_regions_across_three_pages() -> None:
    """重放真实 OCR 结果，31号折叠为一行，33号共享字段跨五行且不吞掉独立子项。"""
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/table_merge_blank_continuation.json").read_text(encoding="utf-8"))
    tables = fixture["tables"]
    for table, flags in zip(tables, fixture["flags"]):
        if flags is not None:
            table["cell_merge"] = flags
    middle: MiddleJson = _middle(*tables)
    original = middle.model_dump_json()
    rendered = render_html(middle)
    soup = BeautifulSoup(rendered, "html.parser")
    assert len(soup.find_all("table")) == 1
    rows = soup.find_all("tr")
    record31 = next(row for row in rows if row.td and row.td.get_text() == "31.")
    assert "QJ457-88" in record31.get_text() and "QJ2754-95" in record31.get_text()
    record33 = next(row for row in rows if row.td and row.td.get_text() == "33.")
    cells = record33.find_all("td", recursive=False)
    assert [cells[index]["rowspan"] for index in (0, 1, 5, 6)] == ["5"] * 4
    assert cells[3]["rowspan"] == cells[4]["rowspan"] == "2"
    child_row = next(row for row in rows if "抗拉强度大于或等于1300MPa的钢制件" in row.get_text())
    assert len(child_row.find_all("td", recursive=False)) == 3
    assert "易产生氢脆" in child_row.get_text()
    assert "QJ450B-2005 金属镀覆层厚度系列与选择原则" in cells[5].get_text()
    _assert_rectangular(str(soup.table), 7)
    assert len(BeautifulSoup(render_html(middle, mode=RenderMode.FULL), "html.parser").find_all("table")) == 3
    assert middle.model_dump_json() == original
