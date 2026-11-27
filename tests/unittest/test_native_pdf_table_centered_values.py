"""验证正文跨列的原生参数值保留独立值、真实空格与源字符归属。"""

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule, recover_native_pdf_table
from docvortex.analyzers.native.pdf._table_recovery.centered_values import build_centered_value_candidate
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from test_native_pdf_table import _char_items


def _parameter_table(dx=0, dy=0, step=31, mode="shared"):
    """用独立行中心、源字符和可调整的横竖画线构造参数表。"""
    first = 75
    second = first + step
    shared = (first + second) / 2
    width = second + 19
    values = [
        ((first, "Before"), (second, "After")),
        ((first, "128"), (second, "256")),
        ((first, "2e-5"), (second, "1e-3")),
        ((shared, "random search"),),
        ((shared, "0.20"),),
        ((first, "125"),),
        ((second, "9"),),
        ((first, "2"), (second, "3")),
    ]
    if mode == "off_center":
        values[4] = ((shared + 0.27 * step, "0.20"),)
    if mode == "short_only":
        values[3] = ((shared, "4"),)
        values[4] = ((shared, "8"),)
    if mode == "no_independent_repetition":
        values[7] = ((second, "3"),)
    entries = []
    for i, (label, row) in enumerate(
        zip(("Setting", "Size", "Rate", "Scheduling", "Ratio", "Prior", "Stage", "Threads"), values, strict=True)
    ):
        y = dy + 4 + 11 * i
        entries.append((label, (dx + 4, y, dx + 4 + len(label) * 2.7, y + 6)))
        for center, value in row:
            radius = len(value) * 1.35
            entries.append((value, (dx + center - radius, y, dx + center + radius, y + 6)))
    rules = [NativeTableRule((dx, dy + y, dx + width, dy + y + 0.2), 0.2, "horizontal") for y in (0, 12.5, 90)]
    if mode != "no_label_line":
        rules.append(NativeTableRule((dx + 56, dy + 13, dx + 56.2, dy + 89), 0.2, "vertical"))
    if mode == "visible_divider":
        rules.append(NativeTableRule((dx + shared, dy + 34, dx + shared + 0.2, dy + 60), 0.2, "vertical"))
    return NativeTableInput((dx, dy, dx + width, dy + 90.2), (400, 400), 0, _char_items(entries), tuple(rules))


@pytest.mark.parametrize("dx,dy,step", [(0, 0, 31), (54, 90, 48)])
def test_centered_body_values_do_not_fill_genuinely_empty_cells(dx, dy, step):
    """正文共享值带跨列，其他对齐在单列的数值仍各有正确的空邻格。"""
    table = _parameter_table(dx, dy, step)
    result = recover_native_pdf_table(table)
    assert result is not None
    assert "evidence=centered_body_values" in result.diagnostics
    assert (result.rows, result.cols) == (8, 3)
    cells = {(c.row, c.col): (c.content, c.colspan) for c in result.cells}
    assert cells[3, 1] == ("random search", 2)
    assert cells[4, 1] == ("0.20", 2)
    assert [cells[r, c] for r, c in ((5, 1), (5, 2), (6, 1), (6, 2))] == [("125", 1), ("", 1), ("", 1), ("9", 1)]
    assert [cells[2, c] for c in (1, 2)] == [("2e-5", 1), ("1e-3", 1)]
    sources = [source for c in result.cells for source in c.source_char_indices]
    assert len(sources) == len(set(sources))
    assert set(sources) == {g.source_index for g in result.text.glyphs}


@pytest.mark.parametrize(
    "mode",
    ["off_center", "short_only", "no_independent_repetition", "no_label_line", "visible_divider"],
)
def test_centered_body_values_require_independent_columns_and_no_divider(mode):
    """拒绝漂移值、仅有短居中值、缺重复独立列、无标签线及实体隔断的近似情形。"""
    table = _parameter_table(mode=mode)
    candidate = build_centered_value_candidate(table, build_native_table_text(table))
    assert candidate is None
