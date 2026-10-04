"""Verify equivalence of table performance paths across tolerance, source index, and cache lifetime boundaries."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput
from docvortex.analyzers.native.pdf._script_geometry import (
    ScriptBaselineCluster,
    _cluster_baselines,
    build_script_features,
)
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


def test_separator_index_matches_full_scan_at_float_boundaries() -> None:
    """Check coverage for bit-by-bit consistency using deterministic random line segments and tolerant adjacent floats."""
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
    for orientation in ("horizontal", "vertical"):
        for aliases in ((10.0,), (9.75, 10.25), (10.0, 10.0), (0.0, 20.0)):
            track = _CanonicalTrack(10.0, aliases)
            for start, end in ((0.0, 50.0), (5.0, 35.0), (0.0, 100.0), (7.0, 7.0), (9.0, 2.0)):
                for tolerance in (0.0, 0.5, 2.5):
                    expected = _separator_coverage_for_track(rules, orientation, track, start, end, tolerance)
                    for _ in range(2):
                        assert _separator_coverage_for_track(indexed, orientation, track, start, end, tolerance) == expected
                    assert _separator_coverage(indexed, orientation, 10.0, start, end, tolerance) == (
                        _separator_coverage(rules, orientation, 10.0, start, end, tolerance)
                    )


def test_separator_cache_does_not_cross_rule_sets() -> None:
    """The same coordinates can be queried independently in different tables or physical evidence to prevent incorrect reuse of acceptance conditions."""
    full = _IndexedRules([_MergedRule("horizontal", 10.0, 0.0, 100.0)])
    partial = _IndexedRules([_MergedRule("horizontal", 10.0, 0.0, 30.0)])
    assert _separator_coverage(full, "horizontal", 10.0, 0.0, 100.0, 0.5) == 1.0
    assert _separator_coverage(partial, "horizontal", 10.0, 0.0, 100.0, 0.5) == 0.3
    assert _separator_coverage(full, "horizontal", 10.0, 0.0, 100.0, 0.5) == 1.0


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_character_preparation_keeps_source_order_and_explicit_boundaries(angle: int) -> None:
    """The index of duplicate and abnormal sources maintains a stable sorting, and space breaks do not occupy the visible character index."""
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
    """Fixed degenerate box, NaN and signed zero semantics for old two-element sorting."""
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
    """The own box can directly read the array, and the third-party subclass still retains the overridden subscript semantics."""

    class ShiftedBbox(Bbox):
        """An outer box subclass that simulates converting coordinates when reading."""

        def __getitem__(self, index):
            """Translate the coordinates on the original subscript protocol to confirm that the fast path does not ignore coverage."""
            return super().__getitem__(index) + 100.0

    assert normalize_bbox(Bbox([4.0, 5.0, 1.0, 2.0])) == (1.0, 2.0, 4.0, 5.0)
    assert normalize_bbox(ShiftedBbox([4.0, 5.0, 1.0, 2.0])) == (101.0, 102.0, 104.0, 105.0)


def test_indexed_assignment_preserves_ties_and_outside_glyphs() -> None:
    """Covers single-cell fast paths, cross-cell draws, outer edge cropping and merged cells, and compares exhaustive results character by character."""
    specs = (
        GridCellSpec(0, 0, 1, 2, (0.0, 0.0, 20.0, 10.0)),
        GridCellSpec(1, 0, 1, 1, (0.0, 10.0, 10.0, 20.0)),
        GridCellSpec(1, 1, 1, 1, (10.0, 10.0, 20.0, 20.0)),
    )
    grid = _build_grid_spec_index(2, 2, specs)
    assert grid is not None
    for x in (-10.0, -1.0, 0.0, 5.0, 9.0, 10.0, 19.0, 20.0, 30.0):
        for y in (-10.0, -1.0, 0.0, 5.0, 9.0, 10.0, 19.0, 20.0, 30.0):
            glyph = NativeTableGlyph(0, 0, "x", (x, y, x + 2.0, y + 2.0), 0)
            assert _choose_cell_for_glyph_indexed(glyph, specs, grid) == _choose_cell_for_glyph(glyph, specs)


def test_baseline_median_reuse_preserves_cluster_membership() -> None:
    """Comparison with the old decision of successive statistics.median at critical offset and repeated origin."""
    randomizer = random.Random(115)
    for _ in range(60):
        origins = {index: (float(index), 10.0 + randomizer.choice((0, 0.35, 0.4, 0.8, 2.0))) for index in range(40)}
        chars = [{"char": "a", "char_idx": index, "bbox": (float(index), 1.0, float(index + 1), 11.0)} for index in origins]
        tight = {index: char["bbox"] for index, char in enumerate(chars)}
        features = build_script_features(chars, tight, origins, set())
        groups = []
        for feature in sorted(features, key=lambda item: item.origin[1]):
            if not groups or abs(feature.origin[1] - statistics.median(origins[index][1] for index in groups[-1])) > 0.4:
                groups.append([feature.index])
            else:
                groups[-1].append(feature.index)
        expected = [
            ScriptBaselineCluster(statistics.median(origins[index][1] for index in group), tuple(group)) for group in groups
        ]
        actual, tolerance = _cluster_baselines(features, list(range(len(features))))
        assert tolerance == 0.4
        assert actual == expected


@pytest.mark.parametrize("second_visual_row", (0, 1))
def test_lazy_typography_keeps_cell_script_roles(monkeypatch: pytest.MonkeyPatch, second_visual_row: int) -> None:
    """Single-line and multi-line character scripts compute exactly the same characters as the old unconditional typesetting feature."""
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
    actual = table_text_styles._cell_script_roles(*args)
    build_lines = table_text_styles._cell_visual_lines

    def eager_lines(*args):
        """Replaying the old path as a single line also populates the full layout statistics."""
        lines = build_lines(*args)
        if len(lines) == 1:
            table_text_styles._fill_native_typography(lines[0], args[2])
        return lines

    monkeypatch.setattr(table_text_styles, "_cell_visual_lines", eager_lines)
    assert actual == table_text_styles._cell_script_roles(*args)
    if second_visual_row == 0:
        assert actual == {1: "sup"}
