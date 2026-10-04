"""Sixth-round independent differentiation of character batch classification and fractional spatial indexing."""

import random
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import _script_geometry
from docvortex.analyzers.native.pdf.inline import scripts
from docvortex.analyzers.native.pdf import line_merging
from docvortex.analyzers.native.pdf import table_annotations, table_rules
from docvortex.analyzers.native.pdf.models import _LineItem


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("seed", range(16))
def test_script_batch_keeps_python_roles_and_sources(angle, seed, monkeypatch):
    """Compare original source to full scripted characters for segmentation, font size, and rotated characters."""

    if get_native() is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    chars, tight, origins = [], {}, {}
    alphabet = "A1αβ²₃中文/⁄[] x"
    for index in range(50):
        x = float(index * 4)
        y = rng.choice([10.0, 10.35, 9.0, 12.0])
        height = rng.choice([3.0, 5.0, 10.0])
        chars.append(
            {
                "char": rng.choice(alphabet),
                "char_idx": index,
                "bbox": (x, y, x + 3.0, y + height),
                "font": {"name": rng.choice(["A", "B"]), "flags": 0, "weight": 400},
            }
        )
        tight[index] = (x, y + 0.5, x + 3.0, y + height - 0.5)
        origins[index] = (x, y + height)
    line = _LineItem("".join(char["char"] for char in chars), (0.0, 0.0, 220.0, 30.0), angle, 0, chars=chars)
    line.inline_math_regions = [(60.0, 0.0, 130.0, 30.0)]
    original = deepcopy((line, tight, origins))
    actual = scripts._script_line_char_roles(line, (300.0, 300.0), tight, origins, set())
    with monkeypatch.context() as context:
        context.setattr(scripts, "get_native", lambda: None, raising=False)
        context.setattr(_script_geometry, "get_native", lambda: None)
        context.setattr("docvortex._compute_backend.get_native", lambda: None)
        expected = scripts._script_line_char_roles(line, (300.0, 300.0), tight, origins, set())
    assert actual == expected
    assert (line, tight, origins) == original


@pytest.mark.parametrize("seed", range(24))
def test_fraction_index_matches_exhaustive(seed, monkeypatch):
    """Use the original full character scan to compare fraction members, covering distant horizontal lines, boundaries and degenerate boxes."""

    rng = random.Random(seed)
    chars, tight = [], {}
    for index in range(100):
        x = float(rng.randrange(0, 140))
        y = float(rng.randrange(0, 30))
        chars.append({"char": rng.choice("A1α-/ "), "char_idx": index})
        tight[index] = (x, y, x + rng.choice([0.0, 2.0, 5.0]), y + 4.0)
    rules = [(float(x), 12.0, float(x + width), 12.1) for x, width in [(0, 3), (20, 10), (50, 25), (100, 80)]]
    result = scripts._fraction_member_indices((200.0, 200.0), chars, tight, rules, 0)
    with monkeypatch.context() as context:
        context.setattr(scripts._FractionCharIndex, "query", lambda self, _rule, _scale: self.chars)
        expected = scripts._fraction_member_indices((200.0, 200.0), chars, tight, rules, 0)
    assert result == expected


def test_fraction_rules_are_reused_without_changing_cell_scale():
    """Prerotating horizontal lines does not replace cell scale with full page scale."""

    rules = [(0.0, 10.0, 8.0, 10.1), (20.0, 11.0, 30.0, 11.1)]
    prepared = scripts._prepare_fraction_rules(rules, (100.0, 100.0), 90)
    for heights in ([4.0, 4.0], [10.0, 10.0]):
        chars = [{"char": "A", "char_idx": 0}, {"char": "1", "char_idx": 1}]
        tight = {i: (0.0, float(i * 8), 4.0, float(i * 8) + heights[i]) for i in range(2)}
        assert scripts._fraction_member_indices(
            (100.0, 100.0), chars, tight, rules, 90, prepared
        ) == scripts._fraction_member_indices((100.0, 100.0), chars, tight, rules, 90)


@pytest.mark.parametrize("invalid", [0, math.nan, math.inf, 10**400])
def test_plain_script_batch_rejects_non_float_geometry(invalid):
    """Integers, non-finite values, and very large integers cannot silently change the original Python geometry admission."""

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    assert native.script_roles_plain_batch(
        [(0.0, 0.0, 5.0, 10.0)], [(0.0, 1.0, 5.0, 9.0)], [(0.0, 9.0)], [4], [0], [0, 1], lambda value: value
    ) == [b"\x00"]
    assert (
        native.script_roles_plain_batch(
            [(0.0, 0.0, 5.0, invalid)], [(0.0, 1.0, 5.0, 9.0)], [(0.0, 9.0)], [4], [0], [0, 1], lambda value: value
        )
        is None
    )


def test_script_batches_preserve_line_order_across_chunk_boundary(monkeypatch):
    """If there are more than 64 lines, all sidecar, including formula sections, will still be returned in the original source order."""

    if get_native() is None:
        pytest.skip("native backend is not selected")
    lines, tight, origins = [], {}, {}
    for number in range(130):
        chars = []
        for offset, text in enumerate("A12B"):
            index = number * 4 + offset
            left = float(offset * 5)
            chars.append({"char": text, "char_idx": index, "bbox": (left, 10.0, left + 4.0, 20.0), "font": {"name": "A"}})
            tight[index] = (left, 11.0, left + 4.0, 19.0)
            origins[index] = (left, 19.0)
        line = _LineItem("A12B", (0.0, 10.0, 20.0, 20.0), 0, number, chars=chars)
        if number % 3 == 0:
            line.inline_math_regions = [(5.0, 10.0, 15.0, 20.0)]
        lines.append(line)
    actual = scripts.detect_pdf_text_script_lines(lines, (100.0, 100.0), tight, origins)
    with monkeypatch.context() as context:
        context.setattr(_script_geometry, "get_native", lambda: None)
        context.setattr("docvortex._compute_backend.get_native", lambda: None)
        expected = scripts.detect_pdf_text_script_lines(lines, (100.0, 100.0), tight, origins)
    assert actual == expected
    assert [line.source_index for line in actual] == list(range(130))


@pytest.mark.parametrize("seed", range(20))
def test_post_semantic_candidates_match_exhaustive_merge(seed, monkeypatch):
    """The merge closure of rotation, shared visual line and text continuation line maintains the original exhaustive result."""

    rng = random.Random(seed)
    lines = []
    for index in range(80):
        x = float(rng.randrange(0, 12) * 25)
        y = float(rng.randrange(0, 18) * 12)
        angle = rng.choice([0, 0, 90, 180, 270])
        line = _LineItem("Sample words", (x, y, x + 12.0, y + 10.0), angle, index, chars=[])
        line.effective_height = 10.0
        line.font_signature = ("A", 0)
        line.paragraph_group = rng.randrange(0, 6)
        line.visual_row_id = rng.choice([None, 0, 1, 2, 3])
        line.formula_candidate_only = rng.choice([False, False, True])
        lines.append(line)
    actual = line_merging._merge_post_semantic_text_runs(deepcopy(lines), (400.0, 400.0), [(60.0, 40.0, 110.0, 80.0)])
    with monkeypatch.context() as context:
        context.setattr(line_merging, "_post_semantic_candidate_pairs", lambda *_args: None)
        expected = line_merging._merge_post_semantic_text_runs(deepcopy(lines), (400.0, 400.0), [(60.0, 40.0, 110.0, 80.0)])
    assert actual == expected


def test_post_semantic_special_geometry_uses_reference_pairs():
    """Non-finite box and integer inputs must not go into the new postprocessing row index."""

    lines = [_LineItem("x", (float(i), 0.0, float(i + 1), 10.0), 0, i, chars=[]) for i in range(40)]
    boxes = [line.bbox for line in lines]
    boxes[0] = (0, math.nan, 1.0, 10.0)
    assert line_merging._post_semantic_candidate_pairs(lines, boxes) is None
    boxes[0] = (0, 0.0, 1.0, 10.0)
    assert line_merging._post_semantic_candidate_pairs(lines, boxes) is None


def test_rule_interval_prefix_keeps_duplicate_boundary_assignment():
    """Increasing horizontal lines reuse prefixes; repeating boundaries and swapping rows are still consistent with the original binary allocation."""

    rows = [SimpleNamespace(center_y=value) for value in (10.0, 15.0, 20.0, 20.0, 25.0, 30.0)]
    rules = [SimpleNamespace(bbox=(0.0, value, 10.0, value)) for value in (10.0, 20.0, 30.0, 40.0)]
    previous = None
    for count in range(2, len(rules) + 1):
        groups, previous = table_rules._partition_rows_by_rule_intervals_cached(rows, rules[:count], previous)
        assert groups == table_rules._partition_rows_by_rule_intervals(rows, rules[:count])
    duplicate = [*rules, SimpleNamespace(bbox=(0.0, 40.0, 10.0, 40.0))]
    groups, previous = table_rules._partition_rows_by_rule_intervals_cached(rows, duplicate, previous)
    assert groups == table_rules._partition_rows_by_rule_intervals(rows, duplicate)
    reordered = rows[::-1]
    groups, _state = table_rules._partition_rows_by_rule_intervals_cached(reordered, duplicate, previous)
    assert groups == table_rules._partition_rows_by_rule_intervals(reordered, duplicate)


def test_prepared_rule_bands_match_every_contiguous_interval():
    """The first interval of the complete horizontal line group reproduces the original bisection rule on all consecutive line slices and closed boundaries."""

    rules = [SimpleNamespace(bbox=(0.0, value, 10.0, value)) for value in (0.0, 10.0, 20.0, 30.0, 40.0, 50.0)]
    rows = [SimpleNamespace(center_y=value) for value in (-5.0, 0.0, 5.0, 10.0, 10.0, 19.9, 20.0, 25.0, 40.0, 50.0, 55.0)]
    index = table_rules._prepare_rule_band_index(rows, rules)
    assert index is not None
    for first in range(len(rules) - 1):
        for last in range(first + 1, len(rules)):
            selected_rules = rules[first : last + 1]
            for start in range(len(rows)):
                for end in range(start + 1, len(rows) + 1):
                    selected_rows = rows[start:end]
                    assert table_rules._partition_rows_by_prepared_bands(
                        selected_rows, selected_rules, first, index
                    ) == table_rules._partition_rows_by_rule_intervals(selected_rows, selected_rules)
    assert table_rules._partition_rows_by_prepared_bands(rows[::-1], rules, 0, index) is None


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_prepared_marker_glyphs_match_reference(angle):
    """Maintain integrity of Unicode, font size and position judgment when multiplexing glyphs from the same source."""

    chars = [
        {"char": text, "bbox": box}
        for text, box in zip(
            ("A", "a", "1"),
            ((0.0, 4.0, 8.0, 16.0), (9.0, 1.0, 13.0, 7.0), (14.0, 2.0, 18.0, 8.0)),
        )
    ]
    line = _LineItem("Aa1", (0.0, 1.0, 18.0, 16.0), angle, 1, chars=chars)
    prepared = table_annotations._prepare_marker_line(line, (100.0, 100.0), angle)
    assert prepared is not None
    for marker in ("a", "1", "A", "missing"):
        assert table_annotations._line_has_superscript_marker(
            line, marker, (100.0, 100.0), angle, prepared[0]
        ) == table_annotations._line_has_superscript_marker(line, marker, (100.0, 100.0), angle)
        assert table_annotations._line_has_compact_marker_token(
            line.text, marker, prepared[1]
        ) == table_annotations._line_has_compact_marker_token(line.text, marker)


def test_marker_preparation_cache_is_bounded_and_rejects_special_values():
    """Only read-only results will be eliminated if the capacity is exceeded, and abnormal integer coordinates will be left to the original marking rules."""

    context = table_annotations._PreparedTableCoreRows([], {}, {}, None)
    for index in range(9000):
        line = _LineItem("a", (0.0, 0.0, 1.0, 1.0), 0, index, chars=[{"char": "a", "bbox": (0.0, 0.0, 1.0, 1.0)}])
        result = context.prepared_marker_line(line, (100.0, 100.0), 0)
        assert result is not None
        if index == 8999:
            assert context.prepared_marker_line(line, (100.0, 100.0), 0)[0] is result[0]
    assert len(context.marker_prepared.values) == 8192
    assert context.marker_prepared.glyph_count == 8192
    special = _LineItem("a", (0.0, 0.0, 1.0, 1.0), 0, 10000, chars=[{"char": "a", "bbox": (0, 0.0, 1.0, 1.0)}])
    assert context.prepared_marker_line(special, (100.0, 100.0), 0) is None
