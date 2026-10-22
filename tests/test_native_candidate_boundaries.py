"""原生候选索引覆盖小规模、任意精度输入与非连续查询边界。"""

from types import SimpleNamespace
import math

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules as rules


@pytest.mark.parametrize("count", [0, 1, 15, 16, 31, 32])
def test_row_bounds_small_inputs_keep_original_coordinates(count):
    """索引不再按页面大小退回 Python，单个坐标继续引用首个原框。"""
    rows = [SimpleNamespace(bbox=(float(index), -0.0, float(index + 2), 3.0)) for index in range(count)]
    prepared = rules._RuleRowBounds(rows)
    assert prepared.native is not None
    result = prepared.bbox(rows)
    if count:
        assert result == (0.0, -0.0, float(count + 1), 3.0)
        assert result[1] is rows[0].bbox[1]
    else:
        assert result is None


def test_generic_partition_preserves_repeated_boundaries_and_nonfinite_rows():
    """闭区间的最左边界归属与 NaN 比较沿用旧 bisect 语义。"""
    native = get_native()
    assert native.PreparedRuleCandidates.partition_generic(
        [0.0, 10.0, 10.0, 20.0], [-1.0, 0.0, 10.0, 15.0, 20.0, math.nan]
    ) == [[1, 2], [2], [3, 4]]


def test_generic_candidate_core_preserves_huge_integer_members_and_grouped_nan_union():
    """来源编号保持任意整数，NaN 极值仍先按规则与核心行分别归并后合并。"""
    native = get_native()
    rule = [10.0, 0.0, 20.0, 10.0]
    rows = [([2**100], [math.nan, 1.0, 21.0, 8.0]), ([-(2**100)], [0.0, 2.0, 22.0, 9.0])]
    members, indices = native.PreparedRuleCandidates.core_generic([rule], rows)
    assert members == {2**100, -(2**100)}
    assert indices == [0, 0, 2, 0]


def test_generic_row_union_does_not_round_large_integer_extrema():
    """巨大相邻整数仍有不同坐标来源，不能因双精度折叠改变首见极值。"""
    native = get_native()
    base = 2**200
    boxes = [[base + 1, 0, base + 3, 10], [base, 1, base + 4, 9]]
    assert native.TableRowGeometry.union_generic(boxes) == [1, 0, 1, 0]


def test_generic_segments_preserve_stable_order_and_row_gap():
    """相同中心保留原行顺序，实际垂直间距决定后续分段。"""
    native = get_native()
    assert native.PreparedRuleCandidates.segments_generic(
        [2, 1, 1, 20], [[0, 2, 1, 3], [0, 0, 1, 1], [0, 1, 1, 2], [0, 20, 1, 21]], 1
    ) == [[1, 2, 0], [3]]
