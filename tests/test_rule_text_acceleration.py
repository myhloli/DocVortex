"""数字噪声的批量几何必须保留 Python 的候选覆盖、边界和参考行顺序。"""

from random import Random
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf.text_noise import (
    _nested_prose_numeric_outliers,
    _nested_prose_numeric_outliers_python,
)


def test_detached_formula_adjacency_matches_all_reference_pairs():
    """比较随机、退化和临界框的全部有序邻接，避免只验证最终公式数量。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    from docvortex.analyzers.native.pdf.geometry import _bbox_axis_overlap_ratio, _bbox_center_y

    rng = Random(853)
    for _ in range(150):
        em = rng.choice([0.1, 1.0, 10.0])
        boxes = [
            (x, y, x + w, y + h)
            for x, y, w, h in (
                (
                    rng.randrange(-5, 80) * em / 10,
                    rng.randrange(-5, 80) * em / 10,
                    rng.randrange(-2, 40) * em / 10,
                    rng.randrange(-2, 25) * em / 10,
                )
                for _ in range(25)
            )
        ]
        operators = [bool(rng.randrange(2)) for _ in boxes]
        expected = []
        for i, a in enumerate(boxes):
            neighbors = []
            for j, b in enumerate(boxes):
                if i == j or (
                    a[2] - a[0] > 3 * em
                    and b[2] - b[0] > 3 * em
                    and operators[i]
                    and operators[j]
                    and abs(_bbox_center_y(a) - _bbox_center_y(b)) > 0.75 * em
                ):
                    continue
                xgap, ygap = max(0.0, a[0] - b[2], b[0] - a[2]), max(0.0, a[1] - b[3], b[1] - a[3])
                if (
                    ygap <= 0.85 * em
                    and _bbox_axis_overlap_ratio(a, b, axis="x") > 0.1
                    or (xgap <= 2 * em and _bbox_axis_overlap_ratio(a, b, axis="y") >= 0.2)
                ):
                    neighbors.append(j)
            expected.append(neighbors)
        assert native.detached_formula_neighbors(boxes, operators, em) == expected
    assert native.detached_formula_neighbors([(0, 0, 1, 1)], [], 1) is None


@pytest.mark.parametrize("kind", ["note", "no_chart", "ordinary", "upper", "bold"])
def test_chart_note_references_are_read_only_after_candidate_proof(kind):
    """图体、页下方和编号证据不足时不读整页字符，真实候选仍使用完整上标集合。"""
    from docvortex.analyzers.native.pdf.models import _LineItem
    from docvortex.analyzers.native.pdf.auxiliary_text import _chart_referenced_note_groups

    text = "Ordinary explanation" if kind == "ordinary" else "1 Natural note about measurements"
    y = 100.0 if kind == "upper" else 220.0
    item = _LineItem(
        text, (0.0, y, 200.0, y + 6), 0, 0, effective_height=6, dominant_font_weight=700 if kind == "bold" else 400
    )
    charts = [] if kind == "no_chart" else [(0.0, 60.0, 210.0, 190.0)]
    calls = []

    def references():
        """完整参考集合只读取一次，不按当前候选裁剪编号覆盖。"""
        calls.append(True)
        return {"1", "2", "3"}

    result = _chart_referenced_note_groups([(item, item.bbox)], charts, references, (500, 300))
    assert result == ([{0}] if kind == "note" else [])
    assert len(calls) == (1 if kind == "note" else 0)


def line(index, text, bbox, font=("body", 1), height=10, angle=0, semantic=None):
    """创建只有规则所需字段的源行，索引保持原始输入次序。"""
    return SimpleNamespace(
        source_index=index,
        text=text,
        bbox=bbox,
        font_signature=font,
        effective_height=height,
        angle=angle,
        semantic_type=semantic,
    )


def signature(candidates):
    """比较候选与每个参考行的源索引，不能只比较数量或集合。"""
    return [(item.source_index, [p.source_index for p in refs]) for item, refs in candidates]


def test_numeric_noise_randomized_differential(monkeypatch):
    """逐页随机比较字体数量、几何、目录、旋转和语义过滤的全部结果。"""
    if get_native() is None:
        pytest.skip("native backend is not selected")
    rng = Random(731)
    texts = [
        "1",
        "１２",
        "1234",
        "abc",
        "alpha beta gamma delta epsilon",
        "中文正文内容不少于十二个文字",
        "1. Contents",
        "2、目录项",
    ]
    for _ in range(300):
        lines = []
        for i in range(rng.randrange(1, 65)):
            x, y = rng.randrange(0, 15), rng.randrange(0, 100)
            lines.append(
                line(
                    i,
                    rng.choice(texts),
                    (x, y, x + rng.randrange(1, 100), y + rng.randrange(1, 20)),
                    rng.choice([None, (), ("body", 1), ("digit", 2), ("title", 3)]),
                    rng.choice([0, 5, 7, 10, 20]),
                    rng.choice([0, 0, 0, 90]),
                    rng.choice([None, None, "text"]),
                )
            )
        source = SimpleNamespace(lines=lines)
        visual = [(0, 0, 5, 100), (10, 0, 50, 100)]
        assert signature(_nested_prose_numeric_outliers(source, visual)) == signature(
            _nested_prose_numeric_outliers_python(source, visual)
        )


@pytest.mark.parametrize("kind", ["prose", "directory", "gutter", "same-font-limit"])
def test_numeric_noise_reference_order_and_boundaries(kind):
    """实际候选覆盖正文、目录和图缝，保留稳定排序及同字体数量上限。"""
    if get_native() is None:
        pytest.skip("native backend is not selected")
    if kind == "directory":
        lines = [line(0, "9", (20, 21, 25, 23), ("digit", 2), 5)] + [
            line(i, f"{i}. Contents", (10, y, 100, y + 10), height=20) for i, y in [(1, 40), (2, 0), (3, 80)]
        ]
    else:
        lines = [line(0, "9", (20, 21, 25, 23), ("digit", 2))] + [
            line(i, "alpha beta gamma delta epsilon", (10, y, 100, y + 10)) for i, y in [(1, 10), (2, 20), (3, 30)]
        ]
    if kind == "same-font-limit":
        lines.extend(line(i, "2", (20, 200, 25, 210), ("digit", 2)) for i in range(4, 8))
    visual = [(0, 0, 15, 100), (30, 0, 100, 100)] if kind == "gutter" else []
    source = SimpleNamespace(lines=lines)
    expected = _nested_prose_numeric_outliers_python(source, visual)
    assert bool(expected) == (kind != "same-font-limit")
    assert signature(_nested_prose_numeric_outliers(source, visual)) == signature(expected)


def test_numeric_noise_recomputes_mutated_text_and_font():
    """同页输入改变后重新计算特征，避免缓存旧语义或字体判断。"""
    source = SimpleNamespace(
        lines=[line(0, "2", (20, 21, 25, 23), ("digit", 2))]
        + [line(i, "alpha beta gamma delta epsilon", (10, y, 100, y + 10)) for i, y in [(1, 10), (2, 20)]]
    )
    assert _nested_prose_numeric_outliers(source)
    source.lines[0].text = "23abcd"
    assert not _nested_prose_numeric_outliers(source)
    source.lines[0].text = "2"
    source.lines[0].font_signature = ("body", 1)
    assert not _nested_prose_numeric_outliers(source)


def test_raster_axis_grouping_randomized_differential(monkeypatch):
    """等差刻度与随机干扰混排，逐项比较两轴分组、稳定顺序和字号中位数。"""
    from docvortex import _compute_backend
    from docvortex.analyzers.native.pdf.graphics import _raster_axis_tick_groups

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = Random(911)
    for _ in range(150):
        lines = [line(i, str(100 - i * 10), (10, i * 20, 20, i * 20 + 10)) for i in range(7)]
        lines.extend(
            line(i, str(rng.randrange(10)), (x, y, x + 10, y + 10), height=rng.choice([5, 10, 20]))
            for i in range(7, rng.randrange(8, 45))
            for x, y in [(rng.randrange(0, 50), rng.randrange(0, 150))]
        )
        rng.shuffle(lines)
        actual = [(v, [p.source_index for p in ticks], em) for v, ticks, em in _raster_axis_tick_groups(lines)]
        with monkeypatch.context() as context:
            context.setattr(_compute_backend, "get_native", lambda: None)
            expected = [(v, [p.source_index for p in ticks], em) for v, ticks, em in _raster_axis_tick_groups(lines)]
        assert actual == expected


def test_formula_growth_randomized_differential():
    """包含旋转、表格阻断、重复源索引和反向传播，核验追加成员的完整次序。"""
    from docvortex.analyzers.native.pdf.formulas import _formula_lines_are_connected, _grow_formula_component_native
    from docvortex.analyzers.native.pdf.models import _LineItem

    if get_native() is None:
        pytest.skip("native backend is not selected")
    rng = Random(927)
    for _ in range(200):
        items = []
        for i in range(35):
            x, y = rng.randrange(0, 80), rng.randrange(0, 80)
            bbox = (x, y, x + rng.choice([0.05, 5, 10, 20]), y + rng.choice([0.05, 5, 10]))
            p = _LineItem("x", bbox, rng.choice([0, 0, 90]), rng.randrange(30), effective_height=rng.choice([5, 10, 20]))
            items.append((p, bbox))
        tables = [(30, 10, 40, 70)] if rng.choice([True, False]) else []
        seeds, candidates = items[:2], items[2:]
        expected = list(seeds)
        sources = {p.source_index for p, _ in seeds}
        changed = True
        while changed:
            changed = False
            for p, box in candidates:
                if p.source_index not in sources and any(
                    _formula_lines_are_connected(a, ab, p, box, tables) for a, ab in expected
                ):
                    expected.append((p, box))
                    sources.add(p.source_index)
                    changed = True
        actual = _grow_formula_component_native(seeds, candidates, tables)
        assert actual is not None
        assert [id(p) for p, _ in actual] == [id(p) for p, _ in expected[2:]]
