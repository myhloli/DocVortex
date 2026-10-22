"""单一原生表注聚合覆盖小候选、精确整数及原坐标身份。"""

from types import SimpleNamespace
import math

import pytest

from docvortex.analyzers.native.pdf import table_annotations as notes


def _row(source, box):
    """构造最小片段运输对象，几何规则全部由生产原生入口执行。"""
    return SimpleNamespace(fragments=[SimpleNamespace(line_index=source, bbox=box, local_bbox=box)])


@pytest.mark.parametrize("size", [0, 1, 15, 16, 31, 32])
def test_annotation_all_candidate_sizes_use_native_geometry(size):
    """原先阈值两侧共享同一原生聚合并保留每个单片段的框身份。"""
    rows = [_row(index, (float(index), 0.0, float(index + 1), 1.0)) for index in range(size)]
    prepared = notes._prepare_annotation_geometry(rows)
    assert prepared.native is not None
    result = notes._build_table_annotation("caption", rows, prepared_geometry=prepared)
    if not size:
        assert result is None
    else:
        assert result.line_indices == set(range(size))
        assert all(result.line_bboxes[index] is row.fragments[0].bbox for index, row in enumerate(rows))


def test_annotation_retains_first_equal_coordinate_objects():
    """相等值和有符号零按首次来源选择，不能在数值传输中重新创建坐标。"""
    first = (-0.0, float("3.25"), 10.0, float("7.25"))
    second = (0.0, float("3.25"), 10.0, float("7.25"))
    result = notes._build_table_annotation("footnote", [_row(7, first), _row(7, second)])
    assert all(actual is expected for actual, expected in zip(result.bbox, first))
    assert math.copysign(1.0, result.bbox[0]) == -1.0


def test_annotation_exclusion_preserves_large_integer_widths():
    """任意整数相减保持精确，不因 f64 折叠边界而漏掉应排除的片段。"""
    anchor = 2**200
    rows = [_row(0, (anchor + 1, 0, anchor + 3, 1)), _row(1, (anchor + 10, 0, anchor + 12, 1))]
    result = notes._build_table_annotation("caption", rows, excluded_local_bbox=(anchor + 2, 0, anchor + 4, 1))
    assert result.line_indices == {1}
    assert result.bbox is rows[1].fragments[0].bbox


def test_annotation_nonfinite_coordinates_preserve_first_source():
    """非有限坐标遵循原 min/max 次序，原生聚合不以准入失败替代结果。"""
    first = (float("nan"), 0.0, 10.0, 1.0)
    result = notes._build_table_annotation("caption", [_row(0, first), _row(0, (2.0, 0.0, 12.0, 1.0))])
    assert result.bbox[0] is first[0]
    assert math.isnan(result.bbox[0])
    assert result.bbox[2] == 12.0
