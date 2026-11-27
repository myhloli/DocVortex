"""紧墨迹表格的物理落格与词内低位连接符阅读顺序保护。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph, NativeTableInput
from docvortex.analyzers.native.pdf._table_recovery import text as table_text


def _glyph(index, value, bbox, row, baseline, marked=False):
    """模拟已按各自墨迹落格的字母和连接符，保留原生字符索引及印刷基线。"""
    return NativeTableGlyph(index, index, value, bbox, row, origin=(bbox[0], baseline), calibrated_connector=marked)


@pytest.mark.parametrize("symbol", ["_", "‿"])
def test_low_connector_attaches_only_for_cell_reading(symbol):
    """同词连接符只在读格时归到邻行，字形编号及原始物理框保持稳定。"""
    word = [
        _glyph(10, "A", (10, 20, 16, 26), 1, 27),
        _glyph(11, symbol, (15.5, 26.6, 19.8, 27), 2, 26, marked=True),
        _glyph(12, "B", (19, 20, 25, 26), 1, 27),
    ]
    parts = table_text.build_cell_text_parts(word, 6)
    assert parts == [("A", 10), (symbol, 11), ("B", 12)]
    assert word[1].bbox == (15.5, 26.6, 19.8, 27) and word[1].visual_row == 2


@pytest.mark.parametrize(
    "condition",
    [
        "not_calibrated",
        "minus",
        "fraction_bar",
        "footnote",
        "word_gap",
        "underprint",
        "foreign_source",
        "source_break",
        "space",
    ],
)
def test_normal_marks_and_separate_printed_lines_keep_original_boxes(condition):
    """保护负号、分数线、数学下划线、脚注、断行及跨栏近邻，缺乏连续词证据时不移动标点。"""
    anchor = _glyph(40, "A", (15, 20, 21, 26), 1, 27)
    mark = _glyph(41, "_", (21, 26.5, 25, 27), 2, 26, marked=True)
    if condition == "not_calibrated":
        mark = replace(mark, calibrated_connector=False)
    elif condition in {"minus", "fraction_bar"}:
        mark = replace(mark, text="−" if condition == "minus" else "―", calibrated_connector=False)
    elif condition == "footnote":
        mark = replace(mark, origin=(21, 31), bbox=(21, 30.5, 25, 31))
    elif condition == "word_gap":
        mark = replace(mark, bbox=(27, 26.5, 31, 27))
    elif condition == "underprint":
        mark = replace(mark, bbox=(16, 26.5, 21, 27))
    elif condition == "foreign_source":
        mark = replace(mark, source_index=51)
    elif condition == "source_break":
        mark = replace(mark, explicit_break_before=True)
    else:
        mark = replace(mark, explicit_space_before=True)
    glyphs = [anchor, mark]
    assert table_text._connector_reading_glyphs(glyphs) is glyphs


def test_connector_eligibility_requires_ink_calibration_and_unicode_class(monkeypatch):
    """只有发生墨迹校准的连接标点进入阅读补偿，真实数学运算符和数字保持原始标记。"""
    chars = tuple(
        {
            "char": value,
            "char_idx": i,
            "bbox": (10 + i * 8, 10, 18 + i * 8, 20),
            "origin": (10 + i * 8, 18),
            "writing_angle": 0,
        }
        for i, value in enumerate("A_-1")
    )
    source = NativeTableInput((0, 0, 100, 100), (100, 100), 0, chars)
    normal = table_text.build_native_table_text(source)
    assert not any(g.calibrated_connector for g in normal.glyphs)
    monkeypatch.setattr(table_text, "calibrate_inflated_table_glyphs", lambda _src, glyphs, _rows, **_kwargs: list(glyphs))
    corrected = table_text.build_native_table_text(source)
    assert [(g.text, g.calibrated_connector) for g in corrected.glyphs] == [
        ("A", False),
        ("_", True),
        ("-", False),
        ("1", False),
    ]
