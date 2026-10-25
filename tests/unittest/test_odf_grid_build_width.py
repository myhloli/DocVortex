"""ODF 表格构建期宽度复用及公开可变网格的回归。"""

from lxml import etree
import pytest

from docvortex.analyzers.native.office.odf.models import GridCell, TableGrid
from docvortex.analyzers.native.office.odf.table import OdfTableExpansionBudget, _ensure_row, parse_table_grid


def test_tall_table_does_not_rescan_existing_rows_for_each_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """长表展开应保持线性宽度工作量，同时完整保留重复行列及预算。"""
    row_visits = 0
    original_width = TableGrid.width.fget
    assert original_width is not None

    def observed_width(grid: TableGrid) -> int:
        """累计公开宽度查询扫描的已有行数，捕获逐格重复全表扫描。"""
        nonlocal row_visits
        row_visits += len(grid.rows)
        return original_width(grid)

    monkeypatch.setattr(TableGrid, "width", property(observed_width))
    table = etree.fromstring(
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<table:table-row table:number-rows-repeated="200">'
        '<table:table-cell table:number-columns-repeated="10"/>'
        "</table:table-row></table:table>"
    )
    budget = OdfTableExpansionBudget()
    grid = parse_table_grid(table, lambda _cell: "text", expansion_budget=budget)

    assert type(grid) is TableGrid
    assert len(grid.rows) == 200
    assert grid.width == 10
    assert all(len(row) == 10 and all(cell is not None and cell.html == "text" for cell in row) for row in grid.rows)
    assert budget.used_grid_slots == 2000
    assert budget.used_duplicated_text_bytes == 4 * 1999
    assert row_visits <= 2000 * 4


def test_parsed_grid_width_remains_live_after_direct_row_mutation() -> None:
    """解析结果允许调用方直接增删行列，宽度不得残留构建期缓存。"""
    table = etree.fromstring(
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        "<table:table-row><table:table-cell/></table:table-row></table:table>"
    )
    grid = parse_table_grid(table, lambda _cell: "text")
    assert grid.width == 1
    grid.rows[0].extend([None] * 3)
    assert grid.width == 4
    grid.rows.append([GridCell(html="later")] * 6)
    assert grid.width == 6
    grid.rows.pop()
    assert grid.width == 4
    grid.rows.clear()
    assert grid.width == 0


def test_plain_grid_expansion_observes_external_row_width() -> None:
    """直接使用扩容辅助函数仍应根据普通可变网格的当前行宽预留预算。"""
    grid = TableGrid(rows=[[None] * 3])
    grid.rows[0].extend([None] * 2)
    budget = OdfTableExpansionBudget()
    _ensure_row(grid, 1, 2, budget.start_table())
    assert grid.width == 5
    assert [len(row) for row in grid.rows] == [5, 2]
    assert budget.used_grid_slots == 10
