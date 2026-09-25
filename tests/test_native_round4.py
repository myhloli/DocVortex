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


@pytest.mark.parametrize("seed", range(24))
def test_note_rank_tree_and_member_ranges(native, seed):
    """比较秩树和精确成员回退，覆盖重复来源、边界相等、样本不足与奇偶分位数。"""
    import statistics

    rng = random.Random(seed)
    items = [
        (rng.randrange(30), rng.choice([0.0, 10.0, 20.0, rng.uniform(-50, 50)]), rng.choice([0.0, -0.0, 0.1, 5.0, 10.0]))
        for _ in range(100)
    ]
    metrics = native.TableNoteMetrics(items)
    members = [[rng.randrange(30) for _ in range(3)] for _ in range(20)]
    indexed = metrics.prepare_rows(members)
    empty = metrics.prepare_rows([[]])
    for _ in range(60):
        top, bottom = sorted(rng.choices([-100.0, 0.0, 10.0, 20.0, 100.0], k=2))
        start, end = sorted(rng.choices(range(21), k=2))
        core = {value for row in members[start:end] for value in row}
        for context, a, b, excluded in [(indexed, start, end, core), (empty, 0, 1, set())]:
            heights = sorted(height for index, center, height in items if index not in excluded and not top <= center <= bottom)
            expected = 6.25 if len(heights) < 4 else statistics.median(heights[-((len(heights) + 3) // 4) :])
            assert bits(metrics.height_for_rows(context, a, b, top, bottom, 6.25)) == bits(expected)
    assert metrics.height_for_rows(indexed, 10, 9, 0.0, 10.0, 6.25) is None
    assert metrics.height_for_rows(indexed, 0, 21, 0.0, 10.0, 6.25) is None
    assert native.TableNoteMetrics(items).height_for_rows(indexed, 0, 20, 0.0, 10.0, 6.25) is None


def test_core_interval_reference_and_marker_index(native):
    """连续引用可查询，交换行不得误用；通用标记索引与原始核心行判断相同。"""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem

    lines = [
        _LineItem(text, (0.0, float(i), 10.0, float(i + 1)), 0, i, chars=[])
        for i, text in enumerate(["1", "body text long", "a", "b", "a"])
    ]
    rows = [SimpleNamespace(fragments=[SimpleNamespace(line_index=i)]) for i in range(5)]
    metrics = notes._prepare_table_note_body_metrics(lines, (100.0, 100.0), 0)
    context = notes._prepare_table_core_rows(rows, lines, metrics)
    assert context.marker_safe
    assert context.interval(rows[1:4]) == (1, 4)
    assert context.interval(rows[::-1]) is None
    for marker in ["a", "1", "b", "missing"]:
        cache = {}
        for start in range(5):
            for end in range(start + 1, 6):
                expected = notes._table_core_references_marker(marker, lines[start:end], (100.0, 100.0), 0)
                assert context.references(marker, start, end, lines, (100.0, 100.0), 0, cache) == expected
