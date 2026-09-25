"""第四轮密集候选优化的逐前缀数值与完整表注差分。"""

import math
import random
import struct
import sys
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules as rules


@pytest.fixture
def native():
    """纯 Python 安装跳过内核专用断言，强制 Rust 的加载错误不得吞掉。"""
    value = get_native()
    if value is None:
        pytest.skip("native backend is not selected")
    return value


def visual_rows(values):
    """创建只含聚类所需数值的行，保留传入坐标类型以验证特殊输入回退。"""
    return [SimpleNamespace(fragments=[SimpleNamespace(local_bbox=(a, 0.0, b, 1.0)) for a, b in row]) for row in values]


def bits(value):
    """保留正负零并逐位比较浮点计算结果。"""
    return struct.pack("d", value)


@pytest.mark.parametrize("seed", range(24))
def test_stable_columns_every_prefix(native, seed):
    """逐前缀核对首命中、均值、求和、重复成员行和三类对齐覆盖率。"""
    rng = random.Random(seed)
    values = [
        [
            (x, x + rng.choice([0.0, 1.0, 3.0]))
            for x in [rng.choice([-0.0, 0.0, 3.0, 6.0, rng.uniform(-10, 30)]) for _ in range(rng.randrange(0, 8))]
        ]
        for _ in range(60)
    ]
    rows = visual_rows(values)
    state = native.StableColumnClusters(sys.version_info >= (3, 12))
    reference = rules._new_stable_column_clusters()
    cache = rules._StableColumnCache()
    for i, row in enumerate(rows):
        result = state.extend([values[i]], 3.0)
        rules._extend_stable_column_clusters(reference, [row], start_row_index=i, tolerance=3.0)
        assert result == rules._stable_column_result(reference, i + 1)
        assert rules._count_stable_columns(rows[: i + 1], 4.0, cache, allow_prefix_reuse=True) == result
        for expected, actual in zip(reference.values(), state.snapshot()):
            assert len(expected) == len(actual)
            for cluster, (mean, total, count, row_count) in zip(expected, actual):
                assert bits(mean) == bits(cluster["mean"])
                assert bits(total) == bits(sum(cluster["values"]))
                assert count == len(cluster["values"])
                assert row_count == len(cluster["rows"])


@pytest.mark.parametrize(
    "values", [[-0.0, -0.0, 0.0], [1e16, 1.0, -1e16, 1.0], [5e-324, -5e-324, 1e-323], [1e200, -1e200, 1e-100]]
)
def test_stable_sum_cancellation(native, values):
    """巨大阈值强制同簇，验证抵消和次正规数且读取不破坏累计补偿。"""
    state = native.StableColumnClusters(sys.version_info >= (3, 12))
    for end, value in enumerate(values, 1):
        assert state.extend([[(value, value)]], 1e300) is not None
        mean, total, _, _ = state.snapshot()[0][0]
        assert bits(total) == bits(sum(values[:end]))
        assert bits(mean) == bits(value if end == 1 else sum(values[:end]) / end)


@pytest.mark.parametrize("value", [0, math.inf, math.nan, 1e308])
def test_stable_columns_special_values(native, value):
    """特殊输入或中心溢出必须整体回退，不能留下部分更新的可复用状态。"""
    rows = visual_rows([[(value, value)], [(value, value)]])
    cache = rules._StableColumnCache()
    assert rules._count_stable_columns(rows, 4.0, cache, allow_prefix_reuse=True) == rules._count_stable_columns_python(
        rows, 4.0
    )


def test_stable_columns_nonprefix_identity(native):
    """缩短、交换和重复行引用分别重算，同时不改写输入成员或坐标对象。"""
    rows = visual_rows([[(0.0, 4.0)], [(3.0, 8.0)], [(8.0, 12.0)]])
    coordinates = [r.fragments[0].local_bbox for r in rows]
    cache = rules._StableColumnCache()
    for batch in [rows[:1], rows, rows[:2], rows[::-1], [rows[0], rows[0], rows[2]], rows]:
        assert rules._count_stable_columns(batch, 4.0, cache, allow_prefix_reuse=True) == rules._count_stable_columns_python(
            batch, 4.0
        )
    assert all(r.fragments[0].local_bbox is b for r, b in zip(rows, coordinates))
