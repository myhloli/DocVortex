"""验证正文跨列分组标题的物理证据、逐格内容和保守回退。"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule, recover_native_pdf_table
from docvortex.analyzers.native.pdf._table_recovery.engine import diagnose_native_pdf_table
from docvortex.analyzers.pdf import prepare_table_page
from docvortex.document.pdf import PDFDocument
from test_native_pdf_table import _char_items, _local_to_page_bbox

ROOT = Path(__file__).parents[2]
TRUTH = json.loads((ROOT / "tests/fixtures/native_pdf_grouped_tables.json").read_text(encoding="utf-8"))
SOURCE = ROOT / "tests/unittest/pdfs/grouped_tables/emnlp2022_grouped_tables.pdf"


def _grouped_input(cols: int = 6, col_width: float = 44.0, angle: int = 0, short_title: bool = True) -> NativeTableInput:
    """构造含长短分组标题、空首列和独立可变栏宽的少线表。"""
    width, height = cols * col_width, 144.0
    page_width, page_height = (height, width) if angle in {90, 270} else (width, height)
    entries = []
    for row in range(9):
        top = 4.0 + 16.0 * row
        if row in {1, 5}:
            title = "Alpha model" if row == 1 else "Beta" if short_title else "Second expanded method"
            entries.append((title, (8.0, top, 8.0 + len(title) * 3, top + 8)))
            continue
        for col in range(cols):
            if col == 0 and row in {0, 2}:
                continue
            value = f"S{col}" if row == 0 else f"r{row}" if col == 0 else f"0.{row}{col}00"
            left = col * col_width + 8.0
            entries.append((value, (left, top, left + 3.0 * len(value), top + 8.0)))
    chars = tuple(
        {**char, "bbox": _local_to_page_bbox(char["bbox"], page_width, page_height, angle)} for char in _char_items(entries)
    )
    rules = tuple(
        NativeTableRule(
            _local_to_page_bbox((0.0, y - 0.2, width, y + 0.2), page_width, page_height, angle),
            0.4,
            "vertical" if angle in {90, 270} else "horizontal",
        )
        for y in (0.2, 16.0, 32.0, 80.0, 96.0, 143.8)
    )
    dx, dy = 11.0, 17.0
    shifted_chars = tuple(
        {
            **char,
            "bbox": tuple(v + (dx if i % 2 == 0 else dy) for i, v in enumerate(char["bbox"])),
            "font": {"name": "UnrelatedFont", "size": 8},
        }
        for char in chars
    )
    shifted_rules = tuple(
        replace(rule, bbox=tuple(v + (dx if i % 2 == 0 else dy) for i, v in enumerate(rule.bbox))) for rule in rules
    )
    return NativeTableInput(
        (dx, dy, dx + page_width, dy + page_height), (page_width + 30, page_height + 40), angle, shifted_chars, shifted_rules
    )


@pytest.mark.parametrize("cols,col_width,angle", [(3, 44, 0), (6, 44, 0), (6, 60, 0), (6, 44, 90), (6, 44, 180), (6, 44, 270)])
def test_grouped_titles_preserve_spans_and_empty_stub(cols: int, col_width: float, angle: int) -> None:
    """验证独立栏宽、平移、字体和四种方向不改变分组归属。"""
    result = recover_native_pdf_table(_grouped_input(cols, col_width, angle))
    assert result is not None
    assert (result.rows, result.cols) == (9, cols)
    assert [(cell.row, cell.col, cell.colspan, cell.content) for cell in result.cells if cell.colspan > 1] == [
        (1, 0, cols, "Alpha model"),
        (5, 0, cols, "Beta"),
    ]
    assert next(cell for cell in result.cells if cell.row == 2 and cell.col == 0).content == ""
    assert next(cell for cell in result.cells if cell.row == 2 and cell.col == 1).content == "0.2100"
    sources = [index for cell in result.cells for index in cell.source_char_indices]
    assert len(sources) == len(set(sources)) == len(result.text.glyphs)


@pytest.mark.parametrize("region", TRUTH["regions"], ids=lambda r: f"page-{r['page']}-{r['bbox'][0]:.0f}")
def test_real_grouped_tables_match_source_rows(region: dict) -> None:
    """逐行核对原件字符证据，不能以候选生成的内容充当金标。"""
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == TRUTH["source_sha256"]
    with PDFDocument(SOURCE.read_bytes()) as document:
        page = prepare_table_page(document[region["page"] - 1])
        table = NativeTableInput(
            tuple(region["bbox"]), page.page_size, 0, tuple(page.geometry.chars), page.drawing_lines, page.rectangles
        )
        result = recover_native_pdf_table(table)
    assert result is not None
    assert (result.rows, result.cols) == (region["rows"], region["cols"])
    assert [cell.row for cell in result.cells if cell.colspan == 6] == region["group_rows"]
    for row_index, tokens in enumerate(region["text_rows"]):
        expected = (
            [region["group_titles"][str(row_index)]]
            if row_index in region["group_rows"]
            else ([""] + tokens if len(tokens) == 5 else tokens)
        )
        actual = [cell.content for cell in result.cells if cell.row == row_index]
        assert actual == expected, (row_index, actual, expected)
    sources = [index for cell in result.cells for index in cell.source_char_indices]
    assert len(sources) == len(set(sources)) == len(result.text.glyphs)


@pytest.mark.parametrize(
    "damage", ["missing_rule", "partial_rule", "crossing_data", "vertical_group", "empty_middle", "tall_formula", "body_prose"]
)
def test_grouped_candidate_rejects_unproven_spans(damage: str) -> None:
    """分组边线不足、数据穿列或组内竖线时不能豁免安全门。"""
    table = _grouped_input(short_title=False)
    rules = list(table.drawing_lines)
    if damage == "missing_rule":
        del rules[2]
    elif damage == "partial_rule":
        rules[2] = replace(rules[2], bbox=(11, 48.8, 140, 49.2))
    elif damage == "vertical_group":
        rules.append(NativeTableRule((50.8, 33, 51.2, 49), 0.4, "vertical"))
    elif damage == "empty_middle":
        # 只允许首列明确为空，普通数据列缺失不能被误当成分组。
        table = replace(
            table,
            chars=tuple(char for char in table.chars if not (69 <= char["bbox"][1] <= 72 and 107 <= char["bbox"][0] < 151)),
        )
    elif damage == "body_prose":
        # 普通正文行横跨数据列，但没有包围该行的横线，不能借分组豁免越界。
        chars = tuple(char for char in table.chars if not 69 <= char["bbox"][1] <= 72)
        prose = _char_items([("This ordinary prose crosses several columns", (19, 69, 220, 77))])
        table = replace(table, chars=chars + tuple(prose))
    else:
        chars = list(table.chars)
        char = next(i for i, value in enumerate(chars) if value["char"] == "r")
        # 变宽但保持该物理数据行的原高度，独立制造真正穿列字符。
        left, top, _, bottom = chars[char]["bbox"]
        chars[char] = {
            **chars[char],
            "bbox": (left, top, 70, bottom)
            if damage == "crossing_data"
            else (left, top - 16, chars[char]["bbox"][2], bottom + 16),
        }
        table = replace(table, chars=tuple(chars))
    table = replace(table, drawing_lines=tuple(rules))
    diagnostics = diagnose_native_pdf_table(table)
    assert diagnostics["adopted"] is None


def test_grouped_recovery_does_not_depend_on_literal_text() -> None:
    """独立改变所有字母及数值，确认规则依赖几何证据而非标题词汇。"""
    table = _grouped_input()
    translation = str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        "BCDEFGHIJKLMNOPQRSTUVWXYZAbcdefghijklmnopqrstuvwxyza1234567890",
    )
    changed = replace(table, chars=tuple({**char, "char": char["char"].translate(translation)} for char in table.chars))
    before = recover_native_pdf_table(table)
    after = recover_native_pdf_table(changed)
    assert before is not None and after is not None
    assert [(cell.row, cell.col, cell.colspan, cell.content.translate(translation)) for cell in before.cells] == [
        (cell.row, cell.col, cell.colspan, cell.content) for cell in after.cells
    ]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_group_title_preserves_explicit_space_with_degenerate_bbox(angle: int) -> None:
    """PDF 显式空格可以只有定位点，四个方向均须保留词界而不增加可见字符来源。"""
    table = _grouped_input(angle=angle)
    chars = []
    for char in table.chars:
        if char["char"].isspace():
            left, top, right, bottom = char["bbox"]
            point = ((left + right) / 2, (top + bottom) / 2)
            char = {**char, "bbox": (*point, *point)}
        chars.append(char)
    result = recover_native_pdf_table(replace(table, chars=tuple(chars)))
    assert result is not None
    assert next(cell.content for cell in result.cells if cell.row == 1) == "Alpha model"
    sources = [index for cell in result.cells for index in cell.source_char_indices]
    assert len(sources) == len(set(sources)) == len(result.text.glyphs)
