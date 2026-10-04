"""Independent differencing of fifth-round candidate supersets, pairing orders, and geometric materializations."""

import math
import random
from copy import deepcopy

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import line_merging as merging
from docvortex.analyzers.native.pdf.models import _LineItem


def make_lines(seed, count=80):
    """Generate stable random text lines with source boxes, rotations, divisions and table boundaries."""
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
    """Compare the complete valid edges with the original decision of full pairing, and do not use the new index itself as an expectation."""
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
                """Compute independent truth values from unwritten business decisions."""
                if mode == "baseline":
                    return merging._can_merge_same_baseline_pair(lines[i], boxes[i], lines[j], boxes[j], tables)
                return merging._overlapping_inline_cluster_pair_is_connected(
                    (lines[i], boxes[i]), (lines[j], boxes[j]), 10.0, tables, local_page_width=100.0
                )

            assert [j for j in partners if accepted(j)] == [j for j in range(i + 1, len(lines)) if accepted(j)]


def test_overlap_closure_matches_exhaustive(monkeypatch):
    """Compare complete closure objects to prevent valid edges from being the same but changing order or materialization."""
    lines = make_lines(11)
    actual = merging.merge_text_line_clusters(deepcopy(lines), (100.0, 100.0), [])
    monkeypatch.setattr(merging, "_overlapping_candidate_pairs", lambda members: None)
    monkeypatch.setattr(merging, "_same_baseline_candidate_pairs", lambda *args: None)
    expected = merging.merge_text_line_clusters(deepcopy(lines), (100.0, 100.0), [])
    assert actual == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1e200, 10**400])
def test_overlap_special_geometry_falls_back(value):
    """Abnormal geometries are not entered into finite indexes and do not change the way reference paths are handled."""
    lines = make_lines(2)
    boxes = [line.bbox for line in lines]
    boxes[0] = (value, 0.0, 10.0, 10.0)
    assert merging._overlapping_candidate_pairs(list(zip(lines, boxes))) is None


@pytest.mark.parametrize("dense", [False, True])
def test_overlap_storage_is_bounded_without_truncation(dense):
    """Split rows only produce empty batches, extremely dense rows retain the full number of candidates and do not cache the entire page of the matrix."""
    lines = [_LineItem("x", (0.0, 0.0 if dense else i * 3.0, 1.0, 1.0 if dense else i * 3.0 + 1.0), 0, i) for i in range(300)]
    candidates = merging._overlapping_candidate_pairs([(line, line.bbox) for line in lines])
    assert candidates is not None
    assert sum(len(candidates[i]) for i in range(300)) == (300 * 299 // 2 if dense else 0)
    assert len(candidates.cached_rows) <= 64


def test_native_baseline_geometry_is_used():
    """Force checking of loaded private kernels to avoid reference implementation impersonating Rust paths."""
    if get_native() is None:
        pytest.skip("native backend is not selected")
    lines = make_lines(2)
    boxes = [line.bbox for line in lines]
    groups = {0: list(range(len(lines)))}
    candidates = merging._same_baseline_candidate_pairs(lines, boxes, groups)
    assert type(candidates.native).__name__ == "BaselineGeometryCandidates"


@pytest.mark.parametrize("seed", range(40))
def test_inline_matches_and_materialization_match_python(seed, monkeypatch):
    """Compare the complete materialization results with the original implementation for identical candidates, suffixes and multi-level tags."""
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
    """Ordinary batches must not bypass the loaded kernel, and even if the matching list is empty, the native function must actually be executed."""
    from docvortex.analyzers.native.pdf import native_text

    if get_native() is None:
        pytest.skip("native backend is not selected")

    def forbidden(*args):
        """Silencing error Python fallback fails explicitly."""
        raise AssertionError("reference path executed")

    monkeypatch.setattr(native_text, "_inline_script_matches_python", forbidden)
    native_text._merge_native_inline_scripts(make_lines(3), (100.0, 100.0))


@pytest.mark.parametrize("size", [0, 1, 16, 200])
def test_inline_ties_keep_first_source(size):
    """Completely overlapping candidates cannot be reselected to a later source due to a Rust ranking tie."""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    # The previous row and body should win all competitions of the same metric.
    small = ((10.0, 0.0, 12.0, 4.0), 4.0, 4.0, 1, False, 0, 0)
    base = ((0.0, 2.0, 10.0, 12.0), 10.0, 10.0, 1, False, 1, 0)
    records = [small, base] * size
    assert native.inline_script_matches(records) == ([(0, 1, False, False)] if size else [])


@pytest.mark.parametrize("seed", range(24))
def test_annotation_geometry_matches_ordered_reference(seed):
    """Covers duplicate sources, segment rearrangements, exclusions, and positive and negative zeros, preserving original coordinates bit by bit."""
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
    """Single-segment sources retain box objects, and sources that are repeatedly selected retain their original coordinate references."""
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
    """Only completely continuous original row identity queries are allowed, and the remaining inputs do not borrow interval extreme values."""
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
    """Original character continuation branches can span the repaired ink spacing, and normal boxes cannot be vetoed in advance."""
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
    """Cache read-only snapshots, single candidate modifications cannot pollute the reuse results, and candidates cannot be deleted for elimination."""
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
    """The integer coordinates retain the precise subtraction of Python and cannot change the edge determination due to the incoming f64."""
    from docvortex.analyzers.native.pdf import native_text

    lines = make_lines(4)
    lines[0].bbox = (value, 0.0, value + 10, 10.0)
    assert merging._overlapping_candidate_pairs([(line, line.bbox) for line in lines]) is None
    calls = []
    original = native_text._inline_script_matches_python

    def reference(*args):
        """The record does retain the original value semantics and does not bypass fallback through silent conversion."""
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(native_text, "_inline_script_matches_python", reference)
    # Explicitly preserve the coordinate type with zero orientation, without requiring a legal floating-point conversion to occur first during the rotation phase.
    lines[0].angle = 0
    native_text._native_inline_script_matches(lines, (100.0, 100.0))
    assert calls == [True]


@pytest.mark.parametrize("count", [2, 18])
def test_inline_merge_releases_consumed_lines_without_gc(count):
    """The recursive closure must release the consumed rows in time and cannot leave the objects of the previous stage to the subsequent GC."""
    import gc
    import weakref
    from docvortex.analyzers.native.pdf import native_text

    class ObservableLine(_LineItem):
        """Use weak references to observe internal consumption objects without changing the merging rules of rows."""

    lines = [
        ObservableLine("body", (0.0, 2.0, 10.0, 12.0), 0, 0, visual_row_id=0, effective_height=10.0),
        ObservableLine("2", (10.0, 0.0, 12.0, 4.0), 0, 1, visual_row_id=1, effective_height=4.0),
    ]
    for i in range(2, count):
        lines.append(
            ObservableLine("far", (100.0 * i, 2.0, 100.0 * i + 10.0, 12.0), 0, i, visual_row_id=i, effective_height=10.0)
        )
    consumed = weakref.ref(lines[1])
    enabled = gc.isenabled()
    gc.disable()
    try:
        output = native_text._merge_native_inline_scripts(lines, (3000.0, 100.0))
        assert output[0].text == "body2"
        del lines
        assert consumed() is None
    finally:
        if enabled:
            gc.enable()
