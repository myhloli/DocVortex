"""第五轮候选超集、配对顺序及几何物化的独立差分。"""

import math
import random
from copy import deepcopy

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import line_merging as merging
from docvortex.analyzers.native.pdf.models import _LineItem


def make_lines(seed, count=80):
    """生成含来源框、旋转、同分及表格边界的稳定随机文本行。"""
    rng = random.Random(seed)
    lines = []
    for i in range(count):
        x, y, w, h = (
            rng.randrange(12) * 4.0,
            rng.randrange(10) * 3.0,
            rng.choice([2.0, 8.0, 15.0]),
            rng.choice([4.0, 10.0, 15.0]),
        )
        line = _LineItem(str(i), (x, y, x + w, y + h), rng.choice([0, 0, 90, 180, 270]), i, chars=[])
        line.effective_height = h
        line.visual_row_id = rng.choice([None, 0, 1, 2, 3])
        line.split_from_row = rng.choice([False, True])
        line.formula_candidate_only = rng.choice([False, False, True])
        line.source_bbox = (x - 2.0, y - 4.0, x + w + 2.0, y + h + 4.0) if rng.random() < 0.5 else None
        line.baseline = y + h if rng.random() < 0.5 else None
        lines.append(line)
    return lines


@pytest.mark.parametrize("seed", range(24))
def test_pair_prefilters_preserve_every_accepted_edge(seed):
    """与全配对原判定比较完整有效边，不以新索引自身作为预期。"""
    lines = make_lines(seed)
    boxes = [merging._rotate_bbox_to_upright(line.bbox, (100.0, 100.0), line.angle) for line in lines]
    groups = {}
    for i, line in enumerate(lines):
        groups.setdefault((line.angle, line.formula_candidate_only, line.semantic_type), []).append(i)
    baseline = merging._same_baseline_candidate_pairs(lines, boxes, groups)
    overlap = merging._overlapping_candidate_pairs(list(zip(lines, boxes)))
    tables = [(20.0, 15.0, 35.0, 25.0)]
    for i in range(len(lines)):
        for mode, candidates in [("baseline", baseline), ("overlap", overlap)]:
            partners = list(range(i + 1, len(lines))) if candidates is None else candidates[i]
            assert partners == sorted(set(partners))

            def accepted(j):
                """通过未改写的业务判定计算独立真值。"""
                if mode == "baseline":
                    return merging._can_merge_same_baseline_pair(lines[i], boxes[i], lines[j], boxes[j], tables)
                return merging._overlapping_inline_cluster_pair_is_connected(
                    (lines[i], boxes[i]), (lines[j], boxes[j]), 10.0, tables, local_page_width=100.0
                )

            assert [j for j in partners if accepted(j)] == [j for j in range(i + 1, len(lines)) if accepted(j)]


def test_overlap_closure_matches_exhaustive(monkeypatch):
    """比较完整闭包对象，防止有效边相同但顺序或物化改变。"""
    lines = make_lines(11)
    actual = merging.merge_text_line_clusters(deepcopy(lines), (100.0, 100.0), [])
    monkeypatch.setattr(merging, "_overlapping_candidate_pairs", lambda members: None)
    monkeypatch.setattr(merging, "_same_baseline_candidate_pairs", lambda *args: None)
    expected = merging.merge_text_line_clusters(deepcopy(lines), (100.0, 100.0), [])
    assert actual == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1e200, 10**400])
def test_overlap_special_geometry_falls_back(value):
    """异常几何不进入有限索引，不改变参考路径处理方式。"""
    lines = make_lines(2)
    boxes = [line.bbox for line in lines]
    boxes[0] = (value, 0.0, 10.0, 10.0)
    assert merging._overlapping_candidate_pairs(list(zip(lines, boxes))) is None


@pytest.mark.parametrize("dense", [False, True])
def test_overlap_storage_is_bounded_without_truncation(dense):
    """分离行只产生空批次，极密行保留全量候选且不缓存整页矩阵。"""
    lines = [_LineItem("x", (0.0, 0.0 if dense else i * 3.0, 1.0, 1.0 if dense else i * 3.0 + 1.0), 0, i) for i in range(300)]
    candidates = merging._overlapping_candidate_pairs([(line, line.bbox) for line in lines])
    assert candidates is not None
    assert sum(len(candidates[i]) for i in range(300)) == (300 * 299 // 2 if dense else 0)
    assert len(candidates.cached_rows) <= 64


def test_native_baseline_geometry_is_used():
    """强制检查已加载的私有内核，避免参考实现冒充 Rust 路径。"""
    if get_native() is None:
        pytest.skip("native backend is not selected")
    lines = make_lines(2)
    boxes = [line.bbox for line in lines]
    groups = {0: list(range(len(lines)))}
    candidates = merging._same_baseline_candidate_pairs(lines, boxes, groups)
    assert type(candidates.native).__name__ == "BaselineGeometryCandidates"


@pytest.mark.parametrize("seed", range(40))
def test_inline_matches_and_materialization_match_python(seed, monkeypatch):
    """对同分候选、前后缀及多级标记比较完整物化结果与原实现。"""
    from docvortex import _compute_backend
    from docvortex.analyzers.native.pdf import native_text

    rng = random.Random(seed)
    lines = make_lines(seed, 100)
    for i, line in enumerate(lines):
        line.text = rng.choice(["a", "１２", "12", "中文", "x2", "", "long reference"])
        line.effective_height = rng.choice([3.0, 4.0, 6.0, 10.0, 15.0])
        line.chars = [{"char": line.text, "bbox": line.bbox, "font": {"name": "A", "size": rng.choice([4.0, 6.0, 10.0, 15.0])}}]
    actual = native_text._merge_native_inline_scripts(deepcopy(lines), (100.0, 100.0))
    monkeypatch.setattr(_compute_backend, "get_native", lambda: None)
    expected = native_text._merge_native_inline_scripts(deepcopy(lines), (100.0, 100.0))
    assert actual == expected


def test_inline_kernel_is_used(monkeypatch):
    """普通批次不得绕过已加载内核，匹配列表空也需要真正执行原生函数。"""
    from docvortex.analyzers.native.pdf import native_text

    if get_native() is None:
        pytest.skip("native backend is not selected")

    def forbidden(*args):
        """使错误的静默 Python 回退明确失败。"""
        raise AssertionError("reference path executed")

    monkeypatch.setattr(native_text, "_inline_script_matches_python", forbidden)
    native_text._merge_native_inline_scripts(make_lines(3), (100.0, 100.0))


@pytest.mark.parametrize("size", [0, 1, 16, 200])
def test_inline_ties_keep_first_source(size):
    """完全重合的候选不能因 Rust 排序平局而改选后面的来源。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    # 前一个小行及主体应赢得所有相同度量的竞争。
    small = ((10.0, 0.0, 12.0, 4.0), 4.0, 4.0, 1, False, 0, 0)
    base = ((0.0, 2.0, 10.0, 12.0), 10.0, 10.0, 1, False, 1, 0)
    records = [small, base] * size
    assert native.inline_script_matches(records) == ([(0, 1, False, False)] if size else [])


@pytest.mark.parametrize("seed", range(24))
def test_annotation_geometry_matches_ordered_reference(seed):
    """覆盖重复来源、片段重排、排除和正负零，逐位保留原坐标。"""
    import struct
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf import table_annotations as notes

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    rows = []
    for i in range(48):
        fragments = []
        for j in range(4):
            box = tuple(rng.choice([-0.0, 0.0, 1.0, 2.0, 5.0, 10.0]) for _ in range(4))
            fragments.append(SimpleNamespace(line_index=rng.choice([-5, 10**30, 0, 2, 4, 9]), bbox=box, local_bbox=box))
        rows.append(SimpleNamespace(fragments=fragments))
    prepared = notes._PreparedAnnotationGeometry(rows, native)
    for selected in (rows, rows[::-1], rows[::2], rows[:8] + rows[:8]):
        for local in (None, (-0.0, 1.0, 3.0, 5.0)):
            kwargs = dict(excluded_line_indices={2, 10**30}, excluded_local_bbox=local)
            expected = notes._build_table_annotation("footnote", selected, **kwargs)
            actual = notes._build_table_annotation("footnote", selected, prepared_geometry=prepared, **kwargs)
            assert actual == expected
            if actual is not None:
                assert list(actual.line_bboxes) == list(expected.line_bboxes)
                assert struct.pack("4d", *actual.bbox) == struct.pack("4d", *expected.bbox)
                for key in actual.line_bboxes:
                    assert struct.pack("4d", *actual.line_bboxes[key]) == struct.pack("4d", *expected.line_bboxes[key])
                    assert all(a is b for a, b in zip(actual.line_bboxes[key], expected.line_bboxes[key]))


def test_annotation_keeps_single_fragment_bbox_identity():
    """单片段来源保留框对象，重复选中的来源则保留原来的坐标引用。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf import table_annotations as notes

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    fragments = [
        SimpleNamespace(line_index=i, bbox=(float(i), 0.0, float(i + 1), 1.0), local_bbox=(float(i), 0.0, float(i + 1), 1.0))
        for i in range(32)
    ]
    rows = [SimpleNamespace(fragments=fragments)]
    prepared = notes._PreparedAnnotationGeometry(rows, native)
    result = notes._build_table_annotation("caption", rows, prepared_geometry=prepared)
    assert all(result.line_bboxes[i] is fragment.bbox for i, fragment in enumerate(fragments))
    unknown = [
        SimpleNamespace(fragments=[SimpleNamespace(line_index=0, bbox=(0.0, 0.0, 1.0, 1.0), local_bbox=(0.0, 0.0, 1.0, 1.0))])
    ]
    assert prepared.build("caption", unknown, set(), None) is NotImplemented


def test_rule_bounds_noncontiguous_and_repeated_rows():
    """只允许完全连续的原行身份查询，其余输入不借用区间极值。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf.table_rules import _RuleRowBounds

    rows = [SimpleNamespace(bbox=(float(i), -0.0, float(i + 2), 3.0)) for i in range(40)]
    index = _RuleRowBounds(rows)
    if get_native() is not None:
        box = index.bbox(rows[2:8])
        assert box == (2.0, -0.0, 9.0, 3.0)
        assert box[1] is rows[2].bbox[1]
    for selected in (rows[::-1], rows[::2], [rows[0], rows[0]]):
        assert index.bbox(selected) is None


def test_original_source_geometry_survives_ordinary_rejection():
    """原始字符连续分支可跨修复后的 ink 间距，普通框不能提前否决。"""
    lines = make_lines(0, 32)
    first = _LineItem("a", (0.0, 0.0, 1.0, 1.0), 0, 0, chars=[{"source_indices": (0,)}])
    second = _LineItem("b", (50.0, 0.0, 51.0, 1.0), 0, 1, chars=[{"source_indices": (1,)}])
    first.source_bbox, second.source_bbox = (0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0)
    first.baseline = second.baseline = 10.0
    first.effective_height = second.effective_height = 1.0
    first.visual_row_id, second.visual_row_id = 0, 1
    lines[:2] = [first, second]
    boxes = [line.bbox for line in lines]
    candidates = merging._same_baseline_candidate_pairs(lines, boxes, {0: list(range(32))})
    assert merging._can_merge_same_baseline_pair(first, boxes[0], second, boxes[1], [])
    assert 1 in candidates[0]


def test_annotation_cache_is_bounded_and_not_mutated_by_consumers():
    """缓存只读快照，单个候选修改不能污染重用结果，淘汰不能删候选。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf import table_annotations as notes

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rows = [
        SimpleNamespace(
            fragments=[
                SimpleNamespace(
                    line_index=i, bbox=(float(i), 0.0, float(i + 1), 1.0), local_bbox=(float(i), 0.0, float(i + 1), 1.0)
                )
            ]
        )
        for i in range(180)
    ]
    prepared = notes._PreparedAnnotationGeometry(rows, native)
    first = prepared.build("footnote", rows, set(), None)
    expected = dict(first.line_bboxes)
    first.line_bboxes.clear()
    first.line_indices.clear()
    assert prepared.build("footnote", rows, set(), None).line_bboxes == expected
    for i in range(150):
        selected = rows[i:]
        actual = notes._build_table_annotation("footnote", selected, prepared_geometry=prepared)
        assert actual == notes._build_table_annotation("footnote", selected)
    assert len(prepared.selections) <= 128 and len(prepared.results) <= 128
    assert prepared.selection_weight <= 16384 and prepared.result_weight <= 16384
    assert prepared.build("footnote", rows, set(), None).line_bboxes == expected


@pytest.mark.parametrize("value", [0, 2**53 + 1, -(2**53) - 1])
def test_integer_arithmetic_is_not_coerced_to_native_float(value, monkeypatch):
    """整数坐标保留 Python 精确减法，不能因传入 f64 改变边缘判定。"""
    from docvortex.analyzers.native.pdf import native_text

    lines = make_lines(4)
    lines[0].bbox = (value, 0.0, value + 10, 10.0)
    assert merging._overlapping_candidate_pairs([(line, line.bbox) for line in lines]) is None
    calls = []
    original = native_text._inline_script_matches_python

    def reference(*args):
        """记录确实沿用原数值语义，不通过静默转换绕过回退。"""
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(native_text, "_inline_script_matches_python", reference)
    # 显式用零方向保留坐标类型，不在旋转阶段先发生合法的浮点转换。
    lines[0].angle = 0
    native_text._native_inline_script_matches(lines, (100.0, 100.0))
    assert calls == [True]
