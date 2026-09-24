"""验证批量统计的阈值、稳定排序和来源成员，不以近似误差放行。"""
import math
import random
import statistics

import pytest

from docvortex._compute_backend import get_native


@pytest.fixture
def native():
    """强制 Rust 时加载错误直接失败，纯 Python 回归跳过原生专用断言。"""
    value = get_native()
    if value is None:
        pytest.skip("native backend is not selected")
    return value


def reference_clusters(values, tolerance, relative, last_only):
    """逐步重算原中位数，作为独立且保留历史判断顺序的参考。"""
    groups = []
    for index in sorted(range(len(values)), key=values.__getitem__):
        targets = groups[-1:] if last_only else groups
        target = None
        for group in targets:
            middle = statistics.median(values[i] for i in group)
            limit = relative * middle if relative else tolerance
            delta = abs(values[index] - middle)
            if (not delta > limit) if last_only else delta <= limit:
                target = group
                break
        if target is None:
            groups.append([index])
        else:
            target.append(index)
    return groups


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("relative,last_only", [(0.0, False), (0.0, True), (0.1, False)])
def test_ordered_clusters_exact_parity(native, seed, relative, last_only):
    """奇偶中位数、重复键、多簇命中和阈值相等均须保持完整索引一致。"""
    rng = random.Random(seed)
    batches = [[], [0.0, -0.0], [0.0, 0.5, 0.75, 1.0, 1.25], [1e308, 1e308, 1e308]]
    batches += [[rng.choice([0.0, 0.5, 1.0, 2.0, rng.uniform(-4, 30)]) for _ in range(120)]]
    for values in batches:
        before = values.copy()
        assert native.ordered_clusters(values, 0.5, relative, last_only) == reference_clusters(values, 0.5, relative, last_only)
        assert values == before


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_clusters_reference_path(native, value):
    """原生拒绝不适合稳定数值排序的输入，由 Python 参考路径决定行为。"""
    assert native.ordered_clusters([value], 0.5, 0.0, False) is None
