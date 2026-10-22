"""原生唯一后端必须保留原先合法的空、整数、非有限与大编号输入。"""

import math

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf._table_recovery import text
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph
from _table_golden import table_golden


@pytest.mark.parametrize(
    "case,boxes",
    [
        (0, []),
        (1, [(0, 0, 5, 5), (5, 0, 10, 5)]),
        (2, [(0.0, math.nan, 5.0, 5.0), (5.0, 0.0, 10.0, 5.0)]),
        (3, [(0.0, 0.0, 5.0, math.inf), (5.0, 0.0, 10.0, 5.0)]),
    ],
)
def test_native_visual_and_cell_groups_keep_special_values(case, boxes):
    """完整比较冻结成员顺序，并确认负数和任意精度ID未被强制缩窄或替换对象。"""
    glyphs = [NativeTableGlyph((-1, 2**100)[index % 2], index, "a", box, 2**100) for index, box in enumerate(boxes)]
    positions = {id(glyph): index for index, glyph in enumerate(glyphs)}
    rows = text._assign_visual_rows(glyphs, 5.0)
    cells = text._native_cell_visual_groups(glyphs, 5.0)
    assert [[positions[id(glyph)] for glyph in row] for row in rows] == table_golden()["special_visual_rows"][case]
    assert [[positions[id(glyph)] for glyph in row] for row in cells] == table_golden()["special_cell_groups"][case]


def test_large_integer_overlap_does_not_round_away_a_valid_box():
    """两端只差一的巨大整数仍有面积，不能因提前转换float退化成空框。"""
    box = (2**60, 0, 2**60 + 1, 1)
    assert get_native().overlap_member_groups([box], [box], 0.8) == [[0]]


def test_large_integer_header_keeps_python_float_boundary_comparison():
    """整数与浮点容差混合比较保留精确端点，不能先把两端一并舍入。"""
    line = (2**60 + 1, 0, 2**60 + 2, 1)
    rule = (2**60, 1, 2**60 + 2, 1)
    assert get_native().year_header_groups([line], [rule], 0.0) == [[]]


def test_generic_fragment_rows_keep_large_coordinates_and_visual_identity():
    """特殊数值由原生组行处理，保留巨大整数坐标、成员次序及原视觉ID。"""
    from docvortex.analyzers.native.pdf.models import _Fragment
    from docvortex.analyzers.native.pdf.table_rules import _cluster_fragment_rows

    boxes = [(2**60 + 1, 0, 2**60 + 3, 2), (2**60, 0, 2**60 + 2, 2)]
    fragments = [_Fragment(str(index), box, box, index, 2**100) for index, box in enumerate(boxes)]
    (row,) = _cluster_fragment_rows(fragments, 2.0)
    assert row.fragments == [fragments[1], fragments[0]]
    assert all(member is fragments[index] for member, index in zip(row.fragments, (1, 0)))
    assert row.bbox == (2**60, 0, 2**60 + 3, 2)
    assert all(type(value) is int for value in row.bbox)
    assert row.visual_row_id == 2**100


def test_native_note_height_accepts_nonfinite_samples_and_large_source_ids():
    """原生通用表注统计保留非有限排除边界、空样本及任意精度来源成员。"""
    native = get_native()
    assert native.TableNoteMetrics.height_generic([], 0.0, 10.0, [], 6.25, False) == 6.25
    items = [(2**100 + index, float(index), height) for index, height in enumerate((1.0, 2.0, 3.0, math.inf))]
    assert native.TableNoteMetrics.height_generic(items, math.nan, 10.0, [], 6.25, False) == math.inf
    assert native.TableNoteMetrics.height_generic(items, math.nan, 10.0, [2**100 + 3], 6.25, False) == 6.25
