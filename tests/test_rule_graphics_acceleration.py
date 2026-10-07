"""以旧规则独立验证新增批量内核，不由候选输出生成期望。"""

from dataclasses import replace
import math
import random

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import _graphic_geometry
from docvortex.analyzers.native.pdf.models import _PageSource
from docvortex.document.pdf.native_contracts import PDFPathInfo


def reference_flags(boxes, em):
    """逐候选边完整扫描矩形，保留原实现的容差和单框计票语义。"""
    return tuple(
        len(boxes) <= 1
        or any(
            sum(min(abs(box[e] - baseline) for e in edges) <= 0.35 * em for box in boxes) >= 0.75 * len(boxes)
            for seed in boxes
            for baseline in (seed[edges[0]], seed[edges[1]])
        )
        for edges in ((0, 2), (1, 3))
    )


@pytest.fixture
def native():
    """强制 Rust 时要求兼容扩展，Python 任务跳过直接内核差分。"""
    extension = get_native()
    if extension is None:
        pytest.skip("Rust backend required")
    return extension


def test_compound_baselines_randomized_and_large_nonchart_grid(native):
    """随机复合路径和大表格背景均逐项等价，未共线矩形不能贡献伪基线。"""
    rng = random.Random(8127)
    groups = [[], [(0.0, 0.0, 2.0, 4.0)]]
    groups.extend([tuple(float(rng.randrange(-10, 10)) for _ in range(4)) for _ in range(n)] for n in range(2, 70))
    groups.append([(float(x * 3), float(y * 3), float(x * 3 + 2), float(y * 3 + 2)) for x in range(24) for y in range(24)])
    for em in (0.0, 0.7, 1.0, 1.8):
        assert native.compound_baselines(groups, em) == [reference_flags(group, em) for group in groups]


@pytest.mark.parametrize("offset", [0.35, math.nextafter(0.35, math.inf), math.nextafter(0.35, -math.inf)])
def test_compound_baselines_support_and_tolerance_boundary(native, offset):
    """恰好三票和容差两侧按原浮点比较裁决，重复边不会给同框计两票。"""
    boxes = [(0.0, 0.0, 0.0, 2.0), (offset, 4.0, offset, 6.0), (offset, 8.0, offset, 10.0), (8.0, 12.0, 8.0, 14.0)]
    assert native.compound_baselines([boxes], 1.0) == [reference_flags(boxes, 1.0)]


def test_compound_cache_reuses_geometry_but_rechecks_em_and_replacement(native, monkeypatch):
    """过滤文本可复用几何，字号改变或矩形被替换必须建立新判定。"""
    calls = []

    def measured(groups, em):
        """记录真正进入批量内核的路径，不用环境变量冒充命中次数。"""
        calls.append((groups, em))
        return native.compound_baselines(groups, em)

    from types import SimpleNamespace

    monkeypatch.setattr(_graphic_geometry, "get_native", lambda: SimpleNamespace(compound_baselines=measured))
    boxes = ((0.0, 0.0, 2.0, 5.0), (0.35, 8.0, 2.35, 13.0), (0.35, 16.0, 2.35, 21.0), (9.0, 24.0, 11.0, 29.0))
    path = PDFPathInfo((0.0, 0.0, 11.0, 29.0), 16, True, False, 0, 0, (10, 20, 30, 255), boxes)
    source = _PageSource((100.0, 100.0), [], [], [], path_infos=[path])
    assert _graphic_geometry.compound_baseline_flags(source, 1.0)[id(boxes)] == reference_flags(boxes, 1.0)
    _graphic_geometry.compound_baseline_flags(replace(source, lines=[]), 1.0)
    assert len(calls) == 1
    _graphic_geometry.compound_baseline_flags(source, 0.5)
    assert len(calls) == 2
    source.path_infos = [replace(path, rectangle_bboxes=tuple(reversed(boxes)))]
    _graphic_geometry.compound_baseline_flags(source, 0.5)
    assert len(calls) == 3


def test_isolated_path_grouping_matches_reference_and_threshold_fallback(native):
    """比较逐次合并的完整顺序，hypot 临界值请求参考算法而不改变边界裁决。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf.geometry import _bbox_distance
    from docvortex.analyzers.native.pdf.isolated_graphics import _isolated_path_groups

    rng = random.Random(551)
    for count in (1, 10, 100, 350):
        boxes = [
            (float(x), float(y), float(x + 2), float(y + 2))
            for x, y in [(rng.randrange(100), rng.randrange(100)) for _ in range(count)]
        ]
        paths = [SimpleNamespace(bbox=box) for box in boxes]
        expected = []
        for i, box in enumerate(boxes):
            matches = [group for group in expected if any(_bbox_distance(box, boxes[j]) <= 0.35 * 2.0 for j in group)]
            if not matches:
                expected.append([i])
            else:
                merged = [i, *(j for group in matches for j in group)]
                expected = [group for group in expected if all(group is not match for match in matches)]
                expected.append(merged)
        actual = native.isolated_path_groups(boxes, 2.0)
        assert actual is None or actual == expected
        source = SimpleNamespace(isolated_path_cache={})
        result = _isolated_path_groups(source, paths, 2.0)
        identities = {id(path): index for index, path in enumerate(paths)}
        assert [[identities[id(path)] for path in group] for group in result] == expected
    assert native.isolated_path_groups([(0, 0, 1, 1), (1.35, 0, 2.35, 1)], 1.0) is None


def test_overpaint_order_and_inclusive_bounds(native):
    """完全包含采用闭区间，早绘制、同序号、轻微越界和 NaN 框均不能删除文字。"""
    texts = [
        (1, 11, (0.0, 0.0, 10.0, 10.0)),
        (2, 12, (0.0, 0.0, 10.0, 10.0)),
        (3, 13, (-0.00001, 0.0, 10.0, 10.0)),
        (0, 14, (math.nan, 0.0, 10.0, 10.0)),
    ]
    assert native.overpainted_addresses(texts, [(2, (0.0, 0.0, 10.0, 10.0))]) == [11]
def _rectangle_reference(subpath):
    """完整执行原四角取整及直边容差，独立裁决精确四角快捷证明。"""
    points = subpath.points
    if len(points) not in (4, 5) or len(subpath.straight_segments) not in (3, 4):
        return None
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    if x1 <= x0 or y1 <= y0:
        return None
    corners = {(round(x, 3), round(y, 3)) for x, y in points}
    if corners != {(round(x, 3), round(y, 3)) for x in (x0, x1) for y in (y0, y1)}:
        return None
    if any(abs(a[0] - b[0]) > 0.001 and abs(a[1] - b[1]) > 0.001 for a, b in subpath.straight_segments):
        return None
    return x0, y0, x1, y1


@pytest.mark.parametrize("seed", range(48))
def test_exact_rectangle_proof_preserves_rounding_and_mutation(seed):
    """精确、近似、细小矩形及破坏四角后的判定保留原取整边界和坐标符号。"""
    from docvortex.document.pdf.native_contracts import _PathSubpath
    from docvortex.document.pdf.native_objects import _filled_rectangle_bbox

    rng = random.Random(seed)
    x, y = rng.choice([-0.0, 1.0005, -1.0005, 2.0**50]), rng.choice([-0.0, 2.0005])
    width, height = rng.choice([0.0001, 1.0, 20.0]), rng.choice([0.0001, 1.0, 20.0])
    points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    for delta in (0.0, -0.00049, 0.00049, 0.00051, 0.00101):
        points[2] = (x + width + delta, y + height)
        subpath = _PathSubpath(points, list(zip(points, points[1:])), True)
        expected, actual = _rectangle_reference(subpath), _filled_rectangle_bbox(subpath)
        assert actual == expected
        if actual is not None:
            assert [v.hex() for v in actual] == [v.hex() for v in expected]


def test_exact_rectangle_custom_rounding_is_not_skipped():
    """坐标子类的取整副作用必须全部按原顺序执行。"""
    from docvortex.document.pdf.native_contracts import _PathSubpath
    from docvortex.document.pdf.native_objects import _filled_rectangle_bbox

    calls = []

    class Coordinate(float):
        """记录取整顺序，防止数学证明绕过自定义坐标行为。"""

        def __round__(self, digits=None):
            """保留原数值取整并保存调用参数。"""
            calls.append((float(self), digits))
            return round(float(self), digits)

    points = [(Coordinate(x), Coordinate(y)) for x, y in ((0, 0), (2, 0), (2, 4), (0, 4))]
    path = _PathSubpath(points, list(zip(points, points[1:])), True)
    expected = _rectangle_reference(path)
    expected_calls = list(calls)
    calls.clear()
    assert _filled_rectangle_bbox(path) == expected
    assert calls == expected_calls
