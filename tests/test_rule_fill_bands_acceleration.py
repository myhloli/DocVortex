"""填充行带快照必须保留全部筛选边界、重叠去重与首个分组规则。"""

import json
import math
from pathlib import Path
from random import Random

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules

_COUNTS = json.loads((Path(__file__).parent / "fixtures/native_fill_band_counts.json").read_text(encoding="utf-8"))


@pytest.fixture
def native():
    """加载必需原生内核，缺失扩展时明确失败。"""
    kernel = get_native()
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
    index = 0
    for em in (0.0, -1.0, 0.5, 1.0, 5.0, 10.0):
        for bottom in (0.0, 10.0, 50.0, 100.0):
            rule = (0.0, -20.0, 100.0, bottom)
            expected = _COUNTS["random"][seed][index]
            assert owner.count(rule, em) == expected
            assert table_rules._count_repeated_fill_bands([], rule, (100.0, 100.0), 0, em, prepared) == expected
            index += 1


@pytest.mark.parametrize("delta", [0.0, math.ulp(0.8), -math.ulp(0.8)])
def test_fill_bands_boundary_deduplication_and_first_group(native, delta):
    """重叠率边界及端点容差保持包含比较，重复阴影只计一次。"""
    boxes = [(0.0, 0.0, 100.0, 1.0), (0.0, 0.0, 100.0, 1.0)]
    boxes += [(20.0 + delta * 100, y, 120.0 + delta * 100, y + 1.0) for y in (2.0, 4.0)]
    boxes += [(3.0, 6.0, 103.0, 7.0), (6.0, 8.0, 106.0, 9.0)]
    rule = (0.0, 0.0, 100.0, 10.0)
    index = (0.0, math.ulp(0.8), -math.ulp(0.8)).index(delta)
    expected = _COUNTS["boundaries"][index]
    assert native.prepare_fill_bands(boxes).count(rule, 1.0) == expected


def test_fill_bands_special_values_and_rebuilt_geometry(native):
    """特殊数值保持原计数，新页面输入状态创建新快照而不沿用先前结果。"""
    rule = (0.0, 0.0, 100.0, 10.0)
    first = [(None, (0.0, 0.0, 100.0, 1.0))]
    assert table_rules._prepare_native_fill_bands(first).count(rule, 1.0) == 1
    first.append((None, (0.0, 2.0, 100.0, 3.0)))
    assert table_rules._prepare_native_fill_bands(first).count(rule, 1.0) == 2
    assert native.prepare_fill_bands([(0.0, 0.0, 100.0, math.nan)]).count(rule, 1.0) == 0
    assert native.prepare_fill_bands([[0.0, 0.0, 100.0, 1.0]]).count(rule, 1.0) == 1
    assert native.prepare_fill_bands([]).count(rule, math.inf) == 0
    assert native.prepare_fill_bands([]).count((0, 0.0, 100.0, 10.0), 1.0) == 0
    offset = 2**100
    large = [(offset, 0, offset + 100, 1), (offset, 2, offset + 100, 3)]
    assert native.prepare_fill_bands(large).count((offset, 0, offset + 100, 10), 1) == 2
    with pytest.raises(ValueError, match="four"):
        native.prepare_fill_bands([(0, 1, 2)])
