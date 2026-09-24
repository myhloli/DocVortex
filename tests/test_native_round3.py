"""第三轮候选流、批量几何和原生读取的独立参考差分。"""

import math
import random

import pytest

from docvortex.analyzers.native.pdf import _interval_candidates as intervals


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("python_only", (False, True))
def test_interval_tree_matches_complete_pairs(monkeypatch, seed, python_only):
    """逐行比较完整区间交集，覆盖端点相等、重复键和跨组排除。"""
    if python_only:
        monkeypatch.setattr(intervals, "get_native", lambda: None)
    rng = random.Random(seed)
    bounds, groups, ids = [], {}, []
    for index in range(150):
        low = rng.choice([0.0, -0.0, 1.0, math.nextafter(1.0, math.inf), rng.uniform(-10, 20)])
        bounds.append((low, low + rng.choice([0.0, 1.0, 20.0])))
        group = rng.randrange(4)
        groups.setdefault(group, []).append(index)
        ids.append(group)
    original = list(bounds)
    index = intervals.IntervalCandidates(bounds, groups)
    for left in [*range(len(bounds)), 80, 0, 149]:
        expected = [
            right
            for right in range(left + 1, len(bounds))
            if ids[left] == ids[right] and bounds[right][0] <= bounds[left][1] and bounds[right][1] >= bounds[left][0]
        ]
        assert index[left] == expected
    assert bounds == original
    with pytest.raises(IndexError):
        index[len(bounds)]


def test_dense_interval_storage_is_linear():
    """复现新增密集页面规模，只允许线性索引及一批候选存活。"""
    count = 6555
    index = intervals.IntervalCandidates([(0.0, 10.0)] * count, {0: list(range(count))})
    assert index[0] == list(range(1, count))
    assert sum(map(len, index.cached_rows)) <= count + 8192
    assert index[count - 1] == []
    assert len(index) == count


def test_native_interval_rejects_invalid_records():
    """直接绑定也必须拒绝非法组号和非有限区间，不能越界或排序崩溃。"""
    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    for bounds, groups in [([(0, 1)], []), ([(0, 1)], [1]), ([(math.nan, 1)], [0]), ([(2, 1)], [0])]:
        with pytest.raises(ValueError):
            native.BaselineCandidates(bounds, groups)


@pytest.mark.parametrize("seed", range(12))
def test_table_note_query_matches_reference(seed):
    """比较重复来源、上下边界和核心成员排除，保留最高四分位奇偶中位数。"""
    from docvortex.analyzers.native.pdf import table_annotations as notes

    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    items = sorted(
        [
            (rng.randrange(80), rng.choice([0.0, 20.0, 50.0, rng.uniform(-50, 100)]), rng.choice([0.1, 5.0, 10.0, 20.0]))
            for _ in range(150)
        ],
        key=lambda item: item[1],
    )
    prepared = notes._PreparedTableNoteBodyMetrics(tuple(items), tuple(item[1] for item in items))
    accelerated = notes._PreparedTableNoteBodyMetrics(prepared.items, prepared.centers, native.TableNoteMetrics(items))
    for _ in range(30):
        top, bottom = sorted((rng.uniform(-20, 100), rng.uniform(-20, 100)))
        box = (0.0, top, 100.0, bottom)
        core = {rng.randrange(90) for _ in range(20)}
        expected = notes._table_note_body_reference_height([], box, 5.0, core, (100, 200), 0, prepared)
        assert notes._table_note_body_reference_height([], box, 5.0, core, (100, 200), 0, accelerated) == expected
