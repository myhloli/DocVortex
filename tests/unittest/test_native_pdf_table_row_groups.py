"""用原生原语验证正文阶梯横线、稳定数据列与纵向归属。"""

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule, recover_native_pdf_table
from docvortex.analyzers.native.pdf._table_recovery.row_groups import build_row_group_candidate
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from test_native_pdf_table import _char_items


def _stepped_table(dx=0, dy=0, gap=36, mode="groups"):
    """生成两个外组、四个内组及八条独立正文行，允许单独改变起点或文字位置。"""
    width = 70 + 3 * gap
    columns = [22, 40 + gap, 40 + 2 * gap, 40 + 3 * gap]
    pitch = 13
    first = 19
    data = [first + pitch * i for i in range(8)]
    bottom = data[-1] + 11
    entries = []

    def add(col, y, text):
        """以可复核的文字宽度构造中心相同、物理字框独立的单元值。"""
        center = dx + columns[col]
        half = 1.4 * len(text)
        entries.append((text, (center - half, dy + y - 3, center + half, dy + y + 3)))

    for col, text in enumerate(("Place", "Period", "Units", "Rate")):
        add(col, 6, text)
    for idx, y in enumerate(data):
        add(2, y, str(10 + idx))
        if mode != "missing_data" or idx != 2:
            add(3, y, f"{idx}.0")
    for start, title in ((0, "Coast"), (4, "Valley")):
        add(0, (data[start] + data[start + 3]) / 2, title)
    for start, label in zip(range(0, 8, 2), ("Early", "Late", "Spring", "Winter"), strict=True):
        y = (data[start] + data[start + 1]) / 2
        add(1, data[start] if mode == "unbalanced" and start == 2 else y, label)
        if mode == "duplicate_member" and start == 2:
            add(1, data[start + 1], "Partial")

    def rail(y, left=0, right=width):
        """用有限线段表达分组横线是否覆盖左侧标签列。"""
        return NativeTableRule((dx + left, dy + y, dx + right, dy + y + 0.2), 0.2, "horizontal")

    rules = [rail(y) for y in (0, 11, (data[3] + data[4]) / 2, bottom)]
    for i in (2, 6):
        start = columns[0] if mode == "starts_inside_group" and i == 2 else (columns[0] + columns[1]) / 2
        y = data[i] if mode == "cuts_data_line" and i == 2 else (data[i - 1] + data[i]) / 2
        if mode != "no_delimiter":
            rules.append(rail(y, start))
    if mode == "no_bottom_evidence":
        del rules[3]
    return NativeTableInput((dx, dy, dx + width, dy + bottom + 0.2), (500, 500), 0, _char_items(entries), tuple(rules))


@pytest.mark.parametrize("dx,dy,gap", [(0, 0, 36), (39, 60, 59)])
def test_repeated_row_groups_keep_every_independent_data_value(dx, dy, gap):
    """组名留在跨行起点，包含的各条数据记录按正常入口原文原位输出。"""
    table = _stepped_table(dx, dy, gap)
    result = recover_native_pdf_table(table)
    assert result is not None
    assert "evidence=stepped_body_row_groups" in result.diagnostics
    assert (result.rows, result.cols) == (9, 4)
    cells = {(c.row, c.col): (c.content, c.rowspan, c.colspan) for c in result.cells}
    assert [cells[r, 0] for r in (1, 5)] == [("Coast", 4, 1), ("Valley", 4, 1)]
    assert [cells[r, 1] for r in (1, 3, 5, 7)] == [(word, 2, 1) for word in ("Early", "Late", "Spring", "Winter")]
    assert [cells[r, 3] for r in range(1, 9)] == [(f"{r - 1}.0", 1, 1) for r in range(1, 9)]
    sources = [index for c in result.cells for index in c.source_char_indices]
    assert len(sources) == len(set(sources))
    assert set(sources) == {g.source_index for g in result.text.glyphs}


@pytest.mark.parametrize(
    "mode",
    [
        "starts_inside_group",
        "cuts_data_line",
        "no_delimiter",
        "missing_data",
        "unbalanced",
        "duplicate_member",
        "no_bottom_evidence",
    ],
)
def test_stepped_rules_cannot_assign_missing_values_or_ambiguous_groups(mode):
    """局部线、独立数据以及组值完整性均需成立，模糊情形保留原有处理路径。"""
    table = _stepped_table(mode=mode)
    assert build_row_group_candidate(table, build_native_table_text(table)) is None
