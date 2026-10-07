"""填充行带快照必须保留全部筛选边界、重叠去重与首个分组规则。"""
import math
from random import Random

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules


@pytest.fixture
def native():
    """纯 Python 测试跳过原生内核，Rust 测试验证真实注册接口。"""
    kernel = get_native()
    if kernel is None:
        pytest.skip("native backend is not selected")
    return kernel


@pytest.mark.parametrize("seed", range(48))
def test_fill_bands_all_queries_match_python(native, seed):
    """随机阴影、重复框和区间逐项对照，遍历原顺序并覆盖负坐标及正负零。"""
    rng = Random(seed)
    boxes = []
    for _ in range(80):
        x = rng.choice([-0.0, 0.0, 3.0, rng.uniform(-20, 80)])
        y = rng.uniform(-30, 100)
        boxes.append((x, y, x + rng.uniform(0, 120), y + rng.uniform(0, 25)))
    boxes += boxes[:10]
    owner = native.prepare_fill_bands(boxes)
    assert owner is not None
    prepared = [(None, box) for box in boxes]
    for em in (0.0, -1.0, 0.5, 1.0, 5.0, 10.0):
        for bottom in (0.0, 10.0, 50.0, 100.0):
            rule = (0.0, -20.0, 100.0, bottom)
            expected = table_rules._count_repeated_fill_bands([], rule, (100.0, 100.0), 0, em, prepared)
            assert owner.count(rule, em) == expected


@pytest.mark.parametrize("delta", [0.0, math.ulp(0.8), -math.ulp(0.8)])
def test_fill_bands_boundary_deduplication_and_first_group(native, delta):
    """重叠率边界及端点容差保持包含比较，重复阴影只计一次。"""
    boxes = [(0.0, 0.0, 100.0, 1.0), (0.0, 0.0, 100.0, 1.0)]
    boxes += [(20.0 + delta * 100, y, 120.0 + delta * 100, y + 1.0) for y in (2.0, 4.0)]
    boxes += [(3.0, 6.0, 103.0, 7.0), (6.0, 8.0, 106.0, 9.0)]
    rule = (0.0, 0.0, 100.0, 10.0)
    expected = table_rules._count_repeated_fill_bands([], rule, (100.0, 100.0), 0, 1.0, [(None, box) for box in boxes])
    assert native.prepare_fill_bands(boxes).count(rule, 1.0) == expected


def test_fill_bands_unknown_values_and_rebuilt_geometry(native, monkeypatch):
    """未知坐标完整回退，新的页面输入状态创建新快照而不沿用先前计数。"""
    rule = (0.0, 0.0, 100.0, 10.0)
    first = [(None, (0.0, 0.0, 100.0, 1.0))]
    assert table_rules._prepare_native_fill_bands(first).count(rule, 1.0) == 1
    first.append((None, (0.0, 2.0, 100.0, 3.0)))
    assert table_rules._prepare_native_fill_bands(first).count(rule, 1.0) == 2
    assert native.prepare_fill_bands([(0.0, 0.0, 100.0, math.nan)]) is None
    assert native.prepare_fill_bands([[0.0, 0.0, 100.0, 1.0]]) is None
    assert native.prepare_fill_bands([]).count(rule, math.inf) is None
    assert native.prepare_fill_bands([]).count((0, 0.0, 100.0, 10.0), 1.0) is None
    monkeypatch.setattr(table_rules, "_count_repeated_fill_bands", lambda *args: 123)
    assert table_rules._prepare_native_fill_bands(first) is None
