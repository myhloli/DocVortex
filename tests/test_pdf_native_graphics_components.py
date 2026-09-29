"""绘图线连通分量的扫描线剪枝必须与朴素全配对结果一致（#20 回归）。

`_connected_drawing_line_components()` 用左缘排序限制配对范围：第二条线起点
超过第一条线右缘加容差时水平净空已超容差，欧氏距离必然更大，可以安全跳过。
差分测试对比修复前的朴素实现，覆盖堆叠、共 x 跨度、零宽、端点相接等形态。
"""

from __future__ import annotations

import random

import pytest

from docvortex.analyzers.native.pdf import graphics
from docvortex.analyzers.native.pdf.geometry import _bbox_distance
from docvortex.analyzers.native.pdf.models import _AxisLine, _PageSource
from docvortex.document.pdf.native_contracts import PDFPathInfo

BBoxLike = tuple[float, float, float, float]


def _reference_components(
    drawing_lines: list[_AxisLine],
    tolerance: float,
) -> list[list[_AxisLine]]:
    """修复前的朴素实现：全部配对逐一计算欧氏距离。"""
    parents = list(range(len(drawing_lines)))

    def find(index: int) -> int:
        """路径压缩并返回参考并查集根节点。"""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for first_index, first in enumerate(drawing_lines):
        for second_index in range(first_index + 1, len(drawing_lines)):
            if _bbox_distance(first.bbox, drawing_lines[second_index].bbox) <= tolerance:
                first_root, second_root = find(first_index), find(second_index)
                if first_root != second_root:
                    parents[second_root] = first_root

    components: dict[int, list[_AxisLine]] = {}
    for line_index, line in enumerate(drawing_lines):
        components.setdefault(find(line_index), []).append(line)
    return list(components.values())


def _canonical(components: list[list[_AxisLine]]) -> list[tuple[BBoxLike, ...]]:
    """分量转成按 bbox 排序的规范形，消除并查集树形与根身份差异。"""
    return sorted(tuple(sorted(line.bbox for line in component)) for component in components)


def _random_lines(rng: random.Random, count: int) -> list[_AxisLine]:
    """网格加抖动的横竖短线，混入同 x 跨度堆叠与端点相接的形态。"""
    lines: list[_AxisLine] = []
    for index in range(count):
        column, row = index % 30, index // 30
        x = 20.0 + column * 37.0 + rng.uniform(-2.0, 2.0)
        y = 20.0 + row * 41.0 + rng.uniform(-2.0, 2.0)
        if index % 2 == 0:
            length = rng.choice((4.0, 37.0, 1110.0))
            bbox = (x, y, x + length, y + rng.choice((0.0, 0.5, 2.0)))
            orientation = "horizontal"
        else:
            length = rng.choice((4.0, 41.0, 780.0))
            bbox = (x, y, x + rng.choice((0.0, 0.5, 2.0)), y + length)
            orientation = "vertical"
        lines.append(_AxisLine(bbox=bbox, width=0.3, orientation=orientation))
    return lines


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("tolerance", [2.0, 5.5, 12.0])
def test_sweep_components_match_naive_pair_loop(seed: int, tolerance: float) -> None:
    """随机形态下扫描线分量的成员划分与朴素实现逐位一致。"""
    rng = random.Random(seed)
    lines = _random_lines(rng, 420)

    assert _canonical(graphics._connected_drawing_line_components(lines, tolerance)) == _canonical(
        _reference_components(lines, tolerance)
    )


def test_sweep_bounds_evaluated_pairs_on_dispersed_grid() -> None:
    """分散网格上实际计算距离的配对数被线性约束，而不是 n² 全扫。"""
    lines: list[_AxisLine] = []
    for index in range(1800):
        column, row = index % 60, index // 60
        x, y = 20.0 + column * 33.0, 20.0 + row * 27.0
        if index % 2 == 0:
            lines.append(_AxisLine(bbox=(x, y, x + 5.0, y + 0.4), width=0.3, orientation="horizontal"))
        else:
            lines.append(_AxisLine(bbox=(x, y, x + 0.4, y + 5.0), width=0.3, orientation="vertical"))

    counter = {"calls": 0}
    original = graphics._bbox_distance

    def counting(first, second):
        """统计空间索引筛选后仍需精确计算的候选配对。"""
        counter["calls"] += 1
        return original(first, second)

    graphics._bbox_distance = counting
    try:
        components = graphics._connected_drawing_line_components(lines, 2.0)
    finally:
        graphics._bbox_distance = original

    assert all(isinstance(component, list) for component in components)
    # 60 列共享 x 起点的线天然无法被 x 窗口剪枝（26100 次），但必须远低于 n²/2=1620000。
    assert counter["calls"] <= 20 * len(lines)


@pytest.mark.parametrize("shape", ["long_horizontal", "overlap_x_separate_y", "dense_cross"])
def test_sweep_components_match_naive_degenerate_shapes(shape: str) -> None:
    """长横线、同 x 远隔线和密集交叉线均保持原连通成员及顺序。"""
    lines: list[_AxisLine] = []
    for index in range(180):
        if shape == "long_horizontal":
            bbox = (0.0, index * 7.0, 2000.0, index * 7.0 + 0.5)
            orientation = "horizontal"
        elif shape == "overlap_x_separate_y":
            bbox = (10.0, index * 11.0, 11.0, index * 11.0 + 3.0)
            orientation = "vertical"
        elif index % 2:
            bbox = (0.0, index * 0.3, 2000.0, index * 0.3 + 0.5)
            orientation = "horizontal"
        else:
            bbox = (index * 0.3, 0.0, index * 0.3 + 0.5, 2000.0)
            orientation = "vertical"
        lines.append(_AxisLine(bbox=bbox, width=0.3, orientation=orientation))

    assert graphics._connected_drawing_line_components(lines, 2.0) == _reference_components(lines, 2.0)


def test_component_summaries_reuse_only_identical_lines_and_tolerance(monkeypatch) -> None:
    """同页相同输入只计算一次，换线对象或容差后必须重新计算。"""
    lines = _random_lines(random.Random(4), 80)
    source = _PageSource(page_size=(1200.0, 800.0), lines=[], chars=[], drawing_lines=lines)
    original = graphics._connected_drawing_line_components
    counter = {"calls": 0}

    def counting(drawing_lines, tolerance):
        """统计实际进入连通分量计算的次数。"""
        counter["calls"] += 1
        return original(drawing_lines, tolerance)

    monkeypatch.setattr(graphics, "_connected_drawing_line_components", counting)
    first = graphics._drawing_component_summaries(source, 2.0)
    assert graphics._drawing_component_summaries(source, 2.0) is first
    assert counter["calls"] == 1
    graphics._drawing_component_summaries(source, 3.0)
    source.drawing_lines = list(lines)
    graphics._drawing_component_summaries(source, 2.0)
    assert counter["calls"] == 3


def test_axis_index_preserves_boundary_hits_and_original_vertical_order() -> None:
    """纵轴索引在容差边缘命中，并按输入顺序输出相交图形。"""

    def path(bbox, segment_count=2):
        """构造仅包含轴线检测所需字段的真实路径记录。"""
        return PDFPathInfo(bbox, segment_count, False, True, 0, 0)

    horizontal = path((100.0, 99.5, 200.0, 100.5))
    right = path((209.5, 90.0, 210.5, 160.0))
    left = path((89.5, 90.0, 90.5, 170.0))
    middle = path((145.0, 90.0, 146.0, 170.0))
    complex_path = path((100.0, 105.0, 200.0, 130.0), 8)
    result = graphics._detect_axis_path_graphics(
        [horizontal, right, left, middle, complex_path], (500.0, 500.0), 10.0
    )
    assert result == [(100.0, 90.0, 210.5, 160.0), (89.5, 90.0, 200.0, 170.0)]
