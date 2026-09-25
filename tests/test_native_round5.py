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


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1e200])
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
