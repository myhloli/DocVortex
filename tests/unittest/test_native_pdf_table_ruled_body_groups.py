"""以全部数据基线和实线边界控制首列跨行，以及上方独立的列组。"""

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule, recover_native_pdf_table
from docvortex.analyzers.native.pdf._table_recovery.row_groups import build_row_group_candidate
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from test_native_pdf_table import _char_items


def _ruled_table(dx=0, dy=0, gap=30, mode="grouped", counts=(5, 6), single_header=False):
    """生成两段数据、各自可移除的竖轨和两个横向列组。"""
    cols = [20, *(38 + gap * i for i in range(1, 6))]
    width = cols[-1] + 22
    edges = [0, 29, 88, 157]
    entries = []

    def add(x, y, text):
        """以字符长度分配字框，并保留列心之间的共同净空。"""
        w = 1.4 * len(text)
        entries.append((text, (dx + x - w, dy + y - 3, dx + x + w, dy + y + 3)))

    if not single_header:
        for i, word in zip((2, 4), ("Old", "New"), strict=True):
            shift = 0.3 * gap if mode == "offset_heading" and i == 2 else 0
            center = cols[i] if mode == "unproved_three_leaves" and i == 2 else (cols[i] + cols[i + 1]) / 2 + shift
            add(center, 7, word)
    for x, word in zip(cols, ("Kind", "Size", "Min", "Max", "Min", "Max"), strict=True):
        add(x, 20, word)
    for i, n in enumerate(counts):
        start = edges[i + 1] + 8
        for j in range(n):
            y = start + j * 10
            for k, x in enumerate(cols[1:]):
                if not (mode == "missing_value" and (i, j, k) == (1, 2, 3)):
                    add(x, y, f"{j + k}.0")
        center = start + (10 if mode == "normal_blanks" else 15)
        add(cols[0], center, ("Moor", "Delta")[i])
        if mode == "wrapped_label" and i == 1:
            add(cols[0], center + 6, "Edge")
    rules = [NativeTableRule((dx, dy + y, dx + width, dy + y + 0.2), 0.2, "horizontal") for y in edges]
    stub = (cols[0] + cols[1]) / 2
    for i, (top, bottom) in enumerate(zip(edges, edges[1:])):
        if mode == "broken_stub" and i == 2:
            bottom -= 21
        rules.append(NativeTableRule((dx + stub, dy + top + 2, dx + stub + 0.2, dy + bottom - 1), 0.2, "vertical"))
    rail = (cols[1] + cols[2]) / 2
    for top, bottom in zip(edges, edges[1:]):
        rules.append(NativeTableRule((dx + rail, dy + top + 2, dx + rail + 0.2, dy + bottom - 1), 0.2, "vertical"))
    return NativeTableInput((dx, dy, dx + width, dy + edges[-1] + 0.2), (500, 500), 0, _char_items(entries), tuple(rules))


@pytest.mark.parametrize("dx,dy,gap", [(0, 0, 30), (43, 22, 42)])
def test_full_band_rails_prove_repeated_first_column_spans(dx, dy, gap):
    """两段的组名停在各自起始格，数据行和双列标题仍有独立归属。"""
    table = _ruled_table(dx, dy, gap)
    result = recover_native_pdf_table(table)
    assert result is not None
    assert "evidence=ruled_body_row_groups" in result.diagnostics
    assert (result.rows, result.cols) == (13, 6)
    cells = {(c.row, c.col): (c.content, c.rowspan, c.colspan) for c in result.cells}
    assert [cells[r, 0] for r in (2, 7)] == [("Moor", 5, 1), ("Delta", 6, 1)]
    assert [cells[0, c] for c in (2, 4)] == [("Old", 1, 2), ("New", 1, 2)]
    assert [cells[r, 5] for r in range(2, 13)] == [(f"{j + 4}.0", 1, 1) for j in (*range(5), *range(6))]
    sources = [source for c in result.cells for source in c.source_char_indices]
    assert len(sources) == len(set(sources))
    assert set(sources) == {g.source_index for g in result.text.glyphs}


@pytest.mark.parametrize("gap", [22, 53])
def test_single_header_and_variable_body_group_length(gap):
    """单层列名和宽窄不同的网格都能保留独立的三行、四行组。"""
    table = _ruled_table(gap=gap, counts=(3, 4), single_header=True)
    result = recover_native_pdf_table(table)
    assert result is not None
    assert "evidence=ruled_body_row_groups" in result.diagnostics
    assert (result.rows, result.cols) == (8, 6)
    assert [(c.row, c.content, c.rowspan) for c in result.cells if c.rowspan > 1] == [(1, "Moor", 3), (4, "Delta", 4)]


@pytest.mark.parametrize(
    "mode",
    ["normal_blanks", "wrapped_label", "broken_stub", "missing_value", "offset_heading", "unproved_three_leaves"],
)
def test_full_band_rail_alone_does_not_turn_blank_rows_into_group(mode):
    """普通数据旁的空单元、换行名、断裂竖轨、缺数据及偏心列组不能触发跨行。"""
    table = _ruled_table(mode=mode)
    assert build_row_group_candidate(table, build_native_table_text(table)) is None
