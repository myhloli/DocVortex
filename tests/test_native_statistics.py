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


@pytest.mark.parametrize("seed", range(16))
def test_typography_full_state_parity(native, seed):
    """整行比较所有字段，覆盖字体平局、旋转、无效框、缺字重及共享字体字典。"""
    from copy import deepcopy
    from dataclasses import asdict
    from docvortex.analyzers.native.pdf import native_text as text
    from docvortex.analyzers.native.pdf.models import _LineItem
    from docvortex.document.pdf.text import Bbox

    rng = random.Random(seed)
    fonts = [
        {"name": n, "flags": f, "weight": w}
        for n, f, w in [("ABC+Times-Bold", 0, 700), ("Times-Roman", 0, 400), ("Arial", "invalid", None), ("", 0, 0)]
    ]
    chars = []
    for i in range(120):
        y = rng.choice([10.0, 10.5, 13.0])
        chars.append(
            {
                "char": rng.choice(["a", "中", " ", "\n", "x"]),
                "bbox": Bbox([i, y, i + rng.choice([0.0, 3.0, 6.0]), y + 9.0]),
                "font": rng.choice(fonts),
            }
        )
    line = _LineItem(
        "Text.", (0.0, 0.0, 180.0, 40.0), rng.choice([0, 90, 180, 270]), 0, chars=chars, em_height=rng.choice([0.0, 12.0])
    )
    expected = deepcopy(line)
    before = deepcopy(chars)
    text._fill_native_typography_python(expected, (200.0, 300.0))
    text._fill_native_typography(line, (200.0, 300.0))
    # Bbox 容器没有值相等运算，字符部分单独按协议比较。
    left, right = asdict(line), asdict(expected)
    left.pop("chars")
    right.pop("chars")
    assert left == right
    assert [(c["char"], list(c["bbox"]), c["font"]) for c in chars] == [(c["char"], list(c["bbox"]), c["font"]) for c in before]


@pytest.mark.parametrize("seed", range(16))
def test_lane_gap_snapshot_parity(native, seed):
    """数值输出和原列表排序副作用均须匹配，并保留成员引用。"""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf import line_layout as layout
    from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane

    rng = random.Random(seed)
    items = []
    for i in range(70):
        b = (rng.choice([0.0, 5.0, 30.0]), rng.choice([0.0, 10.0, 15.0, 25.0]), 100.0, 40.0)
        line = _LineItem(
            "a",
            b,
            0,
            i,
            effective_height=rng.choice([0.0, 9.0, 12.0, 16.0]),
            visual_row_id=rng.choice([None, 1, 2]),
            split_from_row=rng.choice([False, True]),
            restored_inline_cluster=rng.choice([False, True]),
        )
        items.append((line, b))
    lane = _TextLane(0.0, 100.0, items)
    expected = deepcopy(lane)
    original_list = lane.lines
    references = set(map(id, lane.lines))
    assert layout._estimate_lane_gap(lane) == layout._estimate_lane_gap_python(expected)
    assert lane.lines is original_list and set(map(id, lane.lines)) == references
    assert [item[0].source_index for item in lane.lines] == [item[0].source_index for item in expected.lines]


@pytest.mark.parametrize("seed", range(24))
def test_table_visual_rows_member_parity(native, seed):
    """逐项核对反向遍历的平局、阈值边界和原对象身份，输入不排序改写。"""
    from docvortex.analyzers.native.pdf._table_recovery import text

    rng = random.Random(seed)
    pending = []
    for i in range(120):
        x, y = rng.choice([0.0, 5.0, 10.0, 20.0]), rng.choice([0.0, 4.0, 8.0, 10.0, 14.0])
        pending.append(text._PendingGlyph(i, i, "x", (x, y, x + 5.0, y + rng.choice([0.5, 4.0, 10.0]))))
    original = list(pending)
    expected = text._assign_visual_rows_python(pending, 10.0)
    actual = text._assign_visual_rows(pending, 10.0)
    assert [[g.glyph_id for g in row] for row in actual] == [[g.glyph_id for g in row] for row in expected]
    assert all(g is pending[g.glyph_id] for row in actual for g in row)
    assert pending == original


def test_occupancy_boundary_and_cache_contract(native):
    """公共边界优先左列；恢复调用缓存不暴露可变集合，且不修改文本字形。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf._table_recovery.sparse_hybrid import _RowOccupancy

    rows = [[-1.0, 0.0, 10.0, 20.0, 21.0], [], [5.0, 10.0, 15.0]]
    assert native.table_row_occupancy(rows, [0.0, 10.0, 20.0]) == [[0, 1], [], [0, 1]]
    assert native.table_row_occupancy([[math.nan]], [0.0, 10.0]) is None
    text = SimpleNamespace(
        glyphs=tuple(SimpleNamespace(glyph_id=i, bbox=(x, 0.0, x, 1.0)) for i, x in enumerate(rows[0])),
        rows=(SimpleNamespace(glyph_ids=tuple(range(5))),),
    )
    context = _RowOccupancy(text, native)
    first = context.columns((0.0, 10.0, 20.0))
    assert first == (frozenset({0, 1}),)
    assert context.columns((0.0, 10.0, 20.0)) is first
    assert len(context.cache) == 1
