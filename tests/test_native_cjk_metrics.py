"""验证字符度量阶段复用、视图隔离、几何修复边界和页面引用释放。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import gc
from types import SimpleNamespace
import weakref

import pytest

from docvortex.analyzers.native.pdf import graphics, native_cjk_bands, pipeline, table_detection, table_filled_grid
from docvortex.analyzers.native.pdf.models import _PageSource


def _chars(count=20, *, text="中", width=10.0, loose=24.0, ink=10.0):
    """构造独立原始字符记录，直接控制旧判据的数量、宽松框和墨迹框边界。"""
    return [
        {"char": text, "bbox": (index * width, 0.0, (index + 1) * width, loose), "tight_bbox": (0.0, 0.0, width, ink)}
        for index in range(count)
    ]


@pytest.fixture
def calculations(monkeypatch):
    """只记录实际完整统计次数，不把命中复用的查询误计为字符重扫。"""
    original = table_filled_grid._compute_inflated_native_cjk_metrics
    calls = []

    def observed(chars):
        """保留真实计算和异常，仅记录输入视图的身份及大小。"""
        calls.append((id(chars), len(chars)))
        return original(chars)

    monkeypatch.setattr(table_filled_grid, "_compute_inflated_native_cjk_metrics", observed)
    return calls


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"count": 0}, False),
        ({"count": 19}, False),
        ({"count": 20}, True),
        ({"text": "A"}, False),
        ({"text": "中文"}, False),
        ({"text": "㐀"}, False),
        ({"text": "鿿"}, True),
        ({"width": 0.0}, False),
        ({"loose": 21.99}, False),
        ({"loose": 22.0, "ink": 5.0}, True),
        ({"loose": 22.0, "ink": 14.0}, True),
        ({"ink": 4.99}, False),
        ({"ink": 14.01}, False),
        ({"loose": 10.0}, False),
    ],
)
def test_metric_thresholds_and_false_results_remain_exact(options, expected, calculations):
    """保持 CJK 范围、二十字符门槛和原有不等式，正负结果均只统计一次。"""
    chars = _chars(**options)
    before = deepcopy(chars)
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars) is expected
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars) is expected
    assert len(calculations) == 1
    assert chars == before


@pytest.mark.parametrize("field", ["bbox", "tight_bbox"])
def test_missing_geometry_does_not_count_as_an_anchor(field):
    """缺失或空几何继续排除在锚点之外，不能把数量缓存误当有效字符数。"""
    chars = _chars()
    chars[0].pop(field)
    assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)
    chars[0][field] = ()
    assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)


def test_replaced_and_filtered_views_compute_independently(calculations):
    """同一字符对象组成的新列表、过滤子集和原视图分别计算，不能按字符下标或内容集合串用。"""
    chars = _chars()
    copied = list(chars)
    filtered = chars[:19]
    with table_filled_grid._native_cjk_metric_scope():
        for view, expected in ((chars, True), (copied, True), (filtered, False), (chars, True), (filtered, False)):
            assert table_filled_grid.has_inflated_native_cjk_metrics(view) is expected
    assert [identity for identity, _ in calculations] == [id(chars), id(copied), id(filtered)]


def test_standalone_calls_read_current_mutable_geometry(calculations):
    """作用域外重复调用不缓存，同一列表中的宽松框原地修复立即影响结果。"""
    chars = _chars()
    assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
    for char in chars:
        left, top, right, _ = char["bbox"]
        char["bbox"] = (left, top, right, 10.0)
    assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)
    assert len(calculations) == 2


def test_new_stage_reads_repaired_geometry_and_changed_members(calculations):
    """几何修复与成员变化发生在阶段之间时，同一列表身份也不会复用上一阶段结论。"""
    chars = _chars()
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
    for char in chars:
        char["tight_bbox"] = (0.0, 0.0, 10.0, 20.0)
    with table_filled_grid._native_cjk_metric_scope():
        assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)
    chars[:] = _chars()
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
    assert len(calculations) == 3


def test_nested_stage_invalidates_outer_evidence_after_shared_repair(calculations):
    """重入阶段修复共享字符后，恢复外层上下文也必须重新读取几何。"""
    chars = _chars()
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
        with table_filled_grid._native_cjk_metric_scope():
            chars[0]["char"] = "A"
            assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)
        assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)
    assert len(calculations) == 3
    assert table_filled_grid._NATIVE_CJK_METRIC_CACHE.get() is None


def test_exception_releases_stage_and_does_not_cache_failed_computation(calculations):
    """异常不得污染后续页面，统计失败修正输入后仍应重新计算。"""
    chars = _chars()
    chars[0]["char"] = None
    with pytest.raises(TypeError), table_filled_grid._native_cjk_metric_scope():
        table_filled_grid.has_inflated_native_cjk_metrics(chars)
    assert table_filled_grid._NATIVE_CJK_METRIC_CACHE.get() is None
    chars[0]["char"] = "中"
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
    assert len(calculations) == 2


def test_phase_releases_character_views_after_exit():
    """阶段内保留真实视图防止身份复用，阶段结束后不延长字符生命周期。"""

    class CharacterView(list):
        """允许用弱引用观测字符容器是否仍被阶段状态持有。"""

    chars = CharacterView(_chars())
    observed = weakref.ref(chars)
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
        del chars
        assert observed() is not None
    gc.collect()
    assert observed() is None


def test_consumable_iterable_keeps_uncached_behavior():
    """非列表迭代器仍按每次当前内容计算，不能复用已经消费前的结果。"""
    chars = iter(_chars())
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid.has_inflated_native_cjk_metrics(chars)
        assert not table_filled_grid.has_inflated_native_cjk_metrics(chars)


def test_graphic_and_table_rules_share_only_the_same_source_view(calculations):
    """图形、表格和 source.replace 使用同一字符列表时共享，段落的重新组合视图仍独立判断。"""
    chars = _chars(text="A")
    source = _PageSource(page_size=(300.0, 400.0), lines=[], chars=chars, drawing_lines=[])
    selected = replace(source, lines=[])
    with table_filled_grid._native_cjk_metric_scope():
        assert table_filled_grid._detect_tiled_fill_table_candidates(source, []) == []
        assert graphics._detect_native_diagram_components(selected, 10.0) == []
        assert graphics._detect_stacked_column_graphics(source, 10.0) == []
        table_detection._bind_short_native_grid_captions(selected, [])
        assert len(calculations) == 1
        native_cjk_bands.restore_native_cjk_bands([SimpleNamespace(chars=chars)], source.page_size, [], 0)
        assert len(calculations) == 2


def test_page_preparation_starts_and_finishes_its_own_scope(calculations):
    """真实页面准备入口在每次调用中复用，退出后不把空页面状态带入下一次调用。"""
    source = _PageSource(page_size=(300.0, 400.0), lines=[], chars=[], drawing_lines=[])
    pipeline._prepare_page_source(source)
    first = len(calculations)
    assert first == 2  # 原始空字符视图与段落阶段重新组合的空视图各一次。
    assert table_filled_grid._NATIVE_CJK_METRIC_CACHE.get() is None
    pipeline._prepare_page_source(source)
    assert len(calculations) == 2 * first
