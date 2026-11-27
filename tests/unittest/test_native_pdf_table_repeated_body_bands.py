"""验证粗横线带中的组名跨行和独立数值记录。"""

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule, recover_native_pdf_table
from test_native_pdf_table import _char_items


def _repeated_table(records, dx, dy, width, em, mode="regular", cols=4):
    """构造不同栏宽、字号和位置的实体分组线、单列组名及数值数据行。"""
    tracks = (0, 0.21 * width, 0.44 * width, 0.74 * width, width) if cols == 4 else (0, 0.24 * width, 0.55 * width, width)
    entries = []

    def put(col, baseline, value):
        """在指定列中心放置一个完整源文字项。"""
        x = dx + (tracks[col] + tracks[col + 1]) / 2
        half = 0.23 * em * len(value)
        entries.append((value, (x - half, dy + baseline - 0.5 * em, x + half, dy + baseline + 0.5 * em)))

    for col, title in enumerate(("Round", "Size", "Score", "Count")[:cols]):
        put(col, em, title)
    band_height = 1.65 * em * records
    edges = [0, 2 * em, *(2 * em + j * band_height for j in range(1, 4))]
    for i, top in enumerate(edges[1:-1]):
        center = top + band_height / 2
        if mode == "stub-aligns-first" and i == 1:
            center = top + 0.825 * em
        put(0, center, ("Birch", "Cedar", "Pine")[i])
        if mode == "wrapped-stub" and i == 1:
            put(0, center - 1.2 * em, "North")
        for j in range(records):
            baseline = top + (j + 0.5) * 1.65 * em
            label = str(16 * 2**j) if records == 2 else ("Base", "Mid", "Final")[j]
            if mode == "unordered-label" and i == 1 and j == 0:
                label = "Wide"
            put(1, baseline, label)
            val = "at 4" if mode == "wrapped-data" else f"{i + j}.20"
            if mode != "blank-data" or (i, j) != (1, 0):
                put(2, baseline, val)
            if cols == 4:
                put(3, baseline, f"{i + j + 5}")
    rules = [NativeTableRule((dx, dy + edge - 0.1, dx + width, dy + edge + 0.1), 0.2, "horizontal") for edge in edges]
    rules.extend(NativeTableRule((dx + edge - 0.1, dy, dx + edge + 0.1, dy + edges[-1]), 0.2, "vertical") for edge in tracks)
    if mode == "no-group-rule":
        rules.pop(2)
    if mode == "partial-group-rule":
        y = dy + edges[2]
        rules[2] = NativeTableRule((dx + tracks[1], y - 0.1, dx + width, y + 0.1), 0.2, "horizontal")
    return NativeTableInput((dx, dy, dx + width, dy + edges[-1]), (500, 500), 0, _char_items(entries), tuple(rules))


@pytest.mark.parametrize(
    "records,dx,dy,width,em,cols", [(2, 50, 42, 225, 10, 4), (3, 36, 70, 280, 11, 4), (2, 80, 23, 190, 8, 3)]
)
def test_repeat_group_rail_preserves_logical_rows(records, dx, dy, width, em, cols):
    """原样保留表头、文本/数值键和精度，确保组内每条数据及源字形只落入一格。"""
    table = _repeated_table(records, dx, dy, width, em, cols=cols)
    result = recover_native_pdf_table(table)
    assert result is not None
    assert (result.rows, result.cols) == (1 + 3 * records, cols)
    assert "evidence=repeated_body_bands" in result.diagnostics
    cells = {(c.row, c.col): c for c in result.cells}
    assert [cells[0, c].content for c in range(cols)] == ["Round", "Size", "Score", "Count"][:cols]
    for i, value in enumerate(("Birch", "Cedar", "Pine")):
        start = 1 + i * records
        label = cells[start, 0]
        assert (label.content, label.rowspan, label.colspan) == (value, records, 1)
        assert [cells[start + j, 2].content for j in range(records)] == [f"{i + j}.20" for j in range(records)]
        if cols == 4:
            assert [cells[start + j, 3].content for j in range(records)] == [str(i + j + 5) for j in range(records)]
        assert all((start + j, 0) not in cells for j in range(1, records))
    sources = [source for c in result.cells for source in c.source_char_indices]
    assert len(sources) == len(set(sources))
    assert set(sources) == {g.source_index for g in result.text.glyphs}


@pytest.mark.parametrize(
    "mode",
    [
        "stub-aligns-first",
        "wrapped-stub",
        "unordered-label",
        "wrapped-data",
        "blank-data",
        "no-group-rule",
        "partial-group-rule",
    ],
)
def test_line_band_needs_repeated_complete_data_and_physical_group_bounds(mode):
    """普通换行、空白格、组名偏心及不完整横隔不触发粗带拆行。"""
    result = recover_native_pdf_table(_repeated_table(2, 50, 42, 225, 10, mode))
    assert result is None or "evidence=repeated_body_bands" not in result.diagnostics


def _open_repeated_table(mode="regular"):
    """按少线表的字段锚点和完整横轨构造四组三行记录及尾部独立记录。"""
    dx, dy, width = 25, 37, 228
    entries = []

    def add(x, center, value):
        """模拟左对齐记录字段，保留重复组共享的字符起点。"""
        entries.append((value, (dx + x, dy + center - 4, dx + x + 4.3 * len(value), dy + center + 4)))

    add(120, 6, "Total")
    add(185, 6, "Result")
    for i in range(4):
        top = 13 + i * 36
        add(6, top + 18, str(i + 1))
        for j, key in enumerate(("Red model", "Green-2 model", "Black-3 model")):
            y = top + (j + 0.5) * 12
            add(25, y, key)
            add(120, y, f"{i + j + 5}.07" if mode != "prose" or i != 2 else "same line")
            add(185, y, f"{i + j + 14}.10")
    add(25, 163, "All results")
    add(120, 163, "-0.21")
    add(185, 163, "2.18")
    edges = (0, 13, 49, 85, 121, 157, 169)
    rules = tuple(
        NativeTableRule((dx, dy + y - 0.1, dx + width, dy + y + 0.1), 0.2, "horizontal")
        for y in edges
        if mode != "tail-without-rail" or y != 157
    )
    return NativeTableInput((dx, dy, dx + width, dy + edges[-1]), (500, 500), 0, _char_items(entries), rules)


def test_open_grid_uses_body_group_rails_and_preserves_independent_tail():
    """三行组按实体横隔起止，尾部的独立记录不继承前一组首列。"""
    result = recover_native_pdf_table(_open_repeated_table())
    assert result is not None
    assert "evidence=repeated_body_bands" in result.diagnostics
    assert (result.rows, result.cols) == (14, 4)
    cells = {(c.row, c.col): (c.content, c.rowspan) for c in result.cells}
    assert [cells[row, 0] for row in (1, 4, 7, 10, 13)] == [("1", 3), ("2", 3), ("3", 3), ("4", 3), ("", 1)]
    assert cells[0, 2] == ("Total", 1)
    assert cells[1, 1] == ("Red model", 1)
    assert cells[12, 3] == ("19.10", 1)
    assert cells[13, 1] == ("All results", 1)
    assert cells[13, 2] == ("-0.21", 1)


def test_open_grid_rejects_wrapped_prose_as_independent_records():
    """尾列仍有数值时，相邻列用于换行的文字不能触发拆分。"""
    result = recover_native_pdf_table(_open_repeated_table("prose"))
    assert result is not None and (result.rows, result.cols) == (5, 4)
    assert "evidence=repeated_body_bands" not in result.diagnostics


def test_open_grid_only_splits_bands_supported_by_real_group_bounds():
    """末组失去横界时保留末组原格，只拆有实体证据的前三组。"""
    result = recover_native_pdf_table(_open_repeated_table("tail-without-rail"))
    assert result is not None and (result.rows, result.cols) == (11, 4)
    assert [(c.row, c.content, c.rowspan) for c in result.cells if c.col == 0] == [
        (0, "", 1),
        (1, "1", 3),
        (4, "2", 3),
        (7, "3", 3),
        (10, "4", 1),
    ]
