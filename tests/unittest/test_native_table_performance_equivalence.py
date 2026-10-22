"""验证表格性能路径在容差、来源索引和缓存生命周期边界上的等价性。"""

from __future__ import annotations

import math
import json
import random
import statistics
from pathlib import Path

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput
from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf._script_geometry import classify_char_script_roles
from docvortex.analyzers.native.pdf._table_recovery.candidate import (
    GridCellSpec,
    _build_grid_spec_index,
    _choose_cell_for_glyph,
    _choose_cell_for_glyph_indexed,
)
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph
from docvortex.analyzers.native.pdf._table_recovery.text import _select_pending_glyphs
from docvortex.analyzers.native.pdf._table_recovery.vector import (
    _CanonicalTrack,
    _IndexedRules,
    _MergedRule,
    _separator_coverage,
    _separator_coverage_for_track,
)
from docvortex.analyzers.native.pdf.table_geometry import normalize_bbox
from docvortex.analyzers.native.pdf import table_text_styles
from docvortex.document.pdf.text._contracts import Bbox

_PERFORMANCE_GOLDEN = json.loads(
    (Path(__file__).parents[1] / "fixtures/native_table_performance_06_golden.json").read_text(encoding="utf-8")
)


def test_separator_index_matches_full_scan_at_float_boundaries() -> None:
    """用确定性随机线段及容差相邻浮点数检查覆盖率逐位一致。"""
    randomizer = random.Random(70915)
    rules = [
        _MergedRule(
            randomizer.choice(("horizontal", "vertical")),
            randomizer.uniform(0, 20),
            randomizer.uniform(-10, 100),
            randomizer.uniform(-10, 100),
        )
        for _ in range(150)
    ]
    rules.extend(
        _MergedRule("horizontal", coordinate, 0.0, 50.0)
        for coordinate in (
            9.5,
            10.5,
            math.nextafter(9.5, -math.inf),
            math.nextafter(10.5, math.inf),
        )
    )
    indexed = _IndexedRules(rules)
    expected_queries = iter(_PERFORMANCE_GOLDEN["separator"])
    for orientation in ("horizontal", "vertical"):
        for aliases in ((10.0,), (9.75, 10.25), (10.0, 10.0), (0.0, 20.0)):
            track = _CanonicalTrack(10.0, aliases)
            for start, end in ((0.0, 50.0), (5.0, 35.0), (0.0, 100.0), (7.0, 7.0), (9.0, 2.0)):
                for tolerance in (0.0, 0.5, 2.5):
                    expected_track, expected_center = next(expected_queries)
                    assert (
                        float(_separator_coverage_for_track(rules, orientation, track, start, end, tolerance)).hex()
                        == expected_track
                    )
                    for _ in range(2):
                        assert (
                            float(_separator_coverage_for_track(indexed, orientation, track, start, end, tolerance)).hex()
                            == expected_track
                        )
                    assert (
                        float(_separator_coverage(indexed, orientation, 10.0, start, end, tolerance)).hex() == expected_center
                    )
                    assert float(_separator_coverage(rules, orientation, 10.0, start, end, tolerance)).hex() == expected_center
    assert next(expected_queries, None) is None


def test_separator_cache_does_not_cross_rule_sets() -> None:
    """同一坐标在不同表格或物理证据中独立查询，防止错误复用接受条件。"""
    full = _IndexedRules([_MergedRule("horizontal", 10.0, 0.0, 100.0)])
    partial = _IndexedRules([_MergedRule("horizontal", 10.0, 0.0, 30.0)])
    assert _separator_coverage(full, "horizontal", 10.0, 0.0, 100.0, 0.5) == 1.0
    assert _separator_coverage(partial, "horizontal", 10.0, 0.0, 100.0, 0.5) == 0.3
    assert _separator_coverage(full, "horizontal", 10.0, 0.0, 100.0, 0.5) == 1.0


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_character_preparation_keeps_source_order_and_explicit_boundaries(angle: int) -> None:
    """重复及异常来源索引保持稳定排序，空格换行不占用可见字符索引。"""
    chars = (
        {"char": "A", "bbox": Bbox([1.0, 1.0, 3.0, 9.0]), "char_idx": 2},
        {"char": " ", "bbox": (3.0, 1.0, 4.0, 9.0), "char_idx": 2},
        {"char": "B", "bbox": (4.0, 1.0, 6.0, 9.0), "char_idx": "bad"},
        {"char": "\n", "bbox": (6.0, 1.0, 7.0, 9.0)},
        {"char": "C", "bbox": (7.0, 1.0, 9.0, 9.0), "char_idx": None},
        {"char": "outside", "bbox": (50.0, 50.0, 60.0, 60.0)},
        {"char": "invalid", "bbox": None},
    )
    table = NativeTableInput((0.0, 0.0, 10.0, 10.0), (100.0, 100.0), angle, chars)
    selected = _select_pending_glyphs(table)
    assert [(glyph.text, glyph.source_index, glyph.glyph_id) for glyph in selected] == [
        ("A", 2, 0),
        ("B", 2, 1),
        ("C", 4, 2),
    ]
    assert [(glyph.explicit_space_before, glyph.explicit_break_before) for glyph in selected] == [
        (False, False),
        (True, False),
        (False, True),
    ]
    assert chars[0]["bbox"].bbox == [1.0, 1.0, 3.0, 9.0]


@pytest.mark.parametrize(
    "values",
    (
        (5, 8, 1, 2),
        (-0.0, 1.0, 3.0, 4.0),
        ("1", "2", "3", "4"),
        (math.nan, 1.0, 3.0, 4.0),
        (1.0, 2.0, math.nan, 4.0),
        (-math.inf, 1.0, math.inf, 4.0),
        (1, 2, 1, 4),
    ),
)
def test_bbox_normalization_preserves_stable_pair_order(values: tuple) -> None:
    """固定旧两元素排序的退化框、NaN 和带符号零语义。"""
    x0, y0, x1, y1 = map(float, values)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    expected = None if right <= left or bottom <= top else (left, top, right, bottom)
    actual = normalize_bbox(values)
    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        assert tuple(value.hex() for value in actual) == tuple(value.hex() for value in expected)


def test_owned_bbox_fast_read_preserves_subclass_protocol() -> None:
    """自有框可直接读取数组，第三方子类仍保留覆盖后的下标语义。"""

    class ShiftedBbox(Bbox):
        """模拟读取时转换坐标的外部框子类。"""

        def __getitem__(self, index):
            """在原下标协议上平移坐标，确认快路径不会忽略覆盖。"""
            return super().__getitem__(index) + 100.0

    assert normalize_bbox(Bbox([4.0, 5.0, 1.0, 2.0])) == (1.0, 2.0, 4.0, 5.0)
    assert normalize_bbox(ShiftedBbox([4.0, 5.0, 1.0, 2.0])) == (101.0, 102.0, 104.0, 105.0)


def test_indexed_assignment_preserves_ties_and_outside_glyphs() -> None:
    """覆盖单格快路径、跨格平局、外缘裁剪及合并格，逐字符比较穷举结果。"""
    specs = (
        GridCellSpec(0, 0, 1, 2, (0.0, 0.0, 20.0, 10.0)),
        GridCellSpec(1, 0, 1, 1, (0.0, 10.0, 10.0, 20.0)),
        GridCellSpec(1, 1, 1, 1, (10.0, 10.0, 20.0, 20.0)),
    )
    grid = _build_grid_spec_index(2, 2, specs)
    assert grid is not None
    expected_assignments = iter(_PERFORMANCE_GOLDEN["assignments"])
    for x in (-10.0, -1.0, 0.0, 5.0, 9.0, 10.0, 19.0, 20.0, 30.0):
        for y in (-10.0, -1.0, 0.0, 5.0, 9.0, 10.0, 19.0, 20.0, 30.0):
            glyph = NativeTableGlyph(0, 0, "x", (x, y, x + 2.0, y + 2.0), 0)
            expected = tuple(next(expected_assignments))
            assert _choose_cell_for_glyph_indexed(glyph, specs, grid) == expected
            assert _choose_cell_for_glyph(glyph, specs) == expected
    assert next(expected_assignments, None) is None


def test_baseline_median_reuse_preserves_cluster_membership() -> None:
    """临界偏移与重复 origin 保留冻结的簇成员、中位数及真实原生脚本角色。"""
    randomizer = random.Random(115)
    assert len(_PERFORMANCE_GOLDEN["baselines"]) == 60
    for expected in _PERFORMANCE_GOLDEN["baselines"]:
        origins = {index: (float(index), 10.0 + randomizer.choice((0, 0.35, 0.4, 0.8, 2.0))) for index in range(40)}
        chars = [{"char": "a", "char_idx": index, "bbox": (float(index), 1.0, float(index + 1), 11.0)} for index in origins]
        tight = {index: char["bbox"] for index, char in enumerate(chars)}
        assert float.fromhex(expected["tolerance"]) == 0.4
        groups = get_native().ordered_clusters([origin[1] for origin in origins.values()], 0.4, 0.0, True)
        assert groups == expected["groups"]
        assert [statistics.median(origins[index][1] for index in group).hex() for group in groups] == expected["medians"]
        assert classify_char_script_roles(chars, tight_bboxes=tight, origins=origins) == expected["roles"]


@pytest.mark.parametrize("second_visual_row", (0, 1))
def test_lazy_typography_keeps_cell_script_roles(monkeypatch: pytest.MonkeyPatch, second_visual_row: int) -> None:
    """单行省略排版统计、多行按需填充，两者脚本角色均保持冻结的旧输出。"""
    bboxes = ((10.0, 40.0, 20.0, 50.0), (20.0, 34.0, 26.0, 40.0))
    chars = {
        index: {"char": text, "char_idx": index, "bbox": bbox, "font": {"name": "Times", "flags": 0}}
        for index, (text, bbox) in enumerate(zip("A2", bboxes, strict=True))
    }
    glyphs = [
        NativeTableGlyph(index, index, chars[index]["char"], bbox, second_visual_row if index else 0)
        for index, bbox in enumerate(bboxes)
    ]
    args = glyphs, chars, (100.0, 100.0), 0, dict(enumerate(bboxes)), {0: (10.0, 49.0), 1: (20.0, 40.0)}, set()
    filled_rows = []
    original_fill = table_text_styles._fill_native_typography

    def observed_fill(line, page_size):
        """记录实际排版填充次数，仍执行同一原生统计入口。"""
        filled_rows.append(line.visual_row_id)
        return original_fill(line, page_size)

    monkeypatch.setattr(table_text_styles, "_fill_native_typography", observed_fill)
    assert table_text_styles._cell_script_roles(*args) == dict(_PERFORMANCE_GOLDEN["cell_scripts"][str(second_visual_row)])
    assert filled_rows == ([] if second_visual_row == 0 else [0, 1])
