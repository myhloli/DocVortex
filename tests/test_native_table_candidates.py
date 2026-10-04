"""Candidate continuous pipelines are differentiated from the Python reference rule on an interval-by-interval basis, preserving candidate coverage and coordinate sources."""

import math
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules as rules_api
from docvortex.analyzers.native.pdf.models import _Fragment, _LocalAxisLine, _VisualRow


def _native():
    """Requires registered new native objects, old extensions or explicit Python backends to not falsely report native validation."""
    native = get_native()
    if native is None or not hasattr(native, "PreparedRuleCandidates"):
        pytest.skip("native rule candidate extension unavailable")
    return native


def _rows(centers, counts):
    """Construct realistic visual rows with repeated sources and limited geometry for identity and membership comparison."""
    output = []
    for position, (center, count) in enumerate(zip(centers, counts, strict=True)):
        box = (0.0, center - 1.0, 10.0, center + 1.0)
        fragments = [_Fragment(str(index), box, box, position // 2 + index) for index in range(count)]
        output.append(_VisualRow(fragments, center, box, position))
    return output


def _rules(centers):
    """Create strictly ordered horizontal lines and reuse the original box object to check for stable sources of extreme values."""
    return [_LocalAxisLine((0.0, y, 10.0, y), (0.0, y, 10.0, y), "horizontal", 1.0) for y in centers]


def test_all_slices_preserve_boundary_members_and_interval_evidence():
    """Exhausts slicing and horizontal line spans, covering shared boundaries, repeating row heights, and compact header exceptions."""
    _native()
    rows = _rows([-5.0, 0.0, 5.0, 10.0, 10.0, 19.9, 20.0, 25.0, 40.0, 55.0], [1, 1, 1, 2, 3, 0, 1, 2, 1, 2])
    rules = _rules([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    index = rules_api._prepare_rule_band_index(rows, rules)
    assert index.native is not None
    for first in range(len(rules) - 1):
        for last in range(first + 1, len(rules)):
            selected_rules = rules[first : last + 1]
            for start in range(len(rows) + 1):
                for end in range(start, len(rows) + 1):
                    selected_rows = rows[start:end]
                    expected = rules_api._partition_rows_by_rule_intervals(selected_rows, selected_rules)
                    for height in (-1.0, 0.0, 4.0, 10.0):
                        groups = rules_api._partition_rows_by_prepared_bands(
                            selected_rows, selected_rules, first, index, height
                        )
                        assert groups == expected
                        assert all(
                            actual is old
                            for group, reference in zip(groups, expected, strict=True)
                            for actual, old in zip(group, reference, strict=True)
                        )
                        expected_evidence = rules_api._every_rule_interval_has_multi_cell_row(
                            selected_rows, selected_rules, height, expected
                        )
                        assert (
                            rules_api._every_rule_interval_has_multi_cell_row(selected_rows, selected_rules, height, groups)
                            == expected_evidence
                        )
    assert rules_api._partition_rows_by_prepared_bands(rows[::-1], rules, 0, index, 4.0) is None


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_core_expansion_preserves_members_geometry_and_signed_zero(angle):
    """Candidate reuse corridor data has been accepted, and the four-way coordinates, members and equal value sources are consistent."""
    _native()
    rows = _rows([0.0, 5.0, 10.0, 20.0], [2, 3, 1, 2])
    rows[0].bbox = (-0.0, -0.0, 10.0, 1.0)
    rules = _rules([0.0, 10.0, 20.0])
    index = rules_api._prepare_rule_band_index(rows, rules)
    assert index.native is not None
    for start in range(len(rows)):
        for end in range(start + 1, len(rows) + 1):
            selected = rows[start:end]
            query = rules_api._prepared_rule_candidate_query(index, selected)
            args = (rules, selected, rows, [], (100.0, 200.0), angle, 4.0, None)
            expected = rules_api._expand_rule_table_candidate(*args, has_possible_table_notes=False)
            actual = rules_api._expand_rule_table_candidate(*args, has_possible_table_notes=False, prepared_candidate=query)
            assert actual == expected
            assert [math.copysign(1.0, value) for value in actual.local_bbox] == [
                math.copysign(1.0, value) for value in expected.local_bbox
            ]
            members, core = rules_api._prepared_rule_candidate_core(query, rules)
            assert members == {fragment.line_index for row in selected for fragment in row.fragments}
            expected_core = rules_api._bbox_union(
                rules_api._bbox_union_many([rule.bbox for rule in rules]),
                rules_api._bbox_union_many([row.bbox for row in selected]),
            )
            assert all(value is original for value, original in zip(core, expected_core, strict=True))


def test_special_objects_stay_on_reference_before_native_calculation():
    """Special objects, very large sources ID and non-finite values fall back explicitly during the preparation phase and do not catch arithmetic errors."""
    _native()
    rules = _rules([0.0, 10.0, 20.0])
    rows = [SimpleNamespace(center_y=1.0), SimpleNamespace(center_y=10.0)]
    index = rules_api._prepare_rule_band_index(rows, rules)
    assert index.native is None
    assert rules_api._partition_rows_by_prepared_bands(rows, rules, 0, index) == rules_api._partition_rows_by_rule_intervals(
        rows, rules
    )
    rows = _rows([1.0, 10.0], [1, 2])
    rows[0].fragments[0].line_index = 2**100
    assert rules_api._prepare_rule_band_index(rows, rules).native is None
    rows[0].center_y = float("nan")
    assert rules_api._prepare_rule_band_index(rows, rules) is None
    duplicate = [rules[0], rules[0], rules[1]]
    assert rules_api._prepare_rule_band_index([], duplicate) is None


def test_native_candidate_validation_errors_are_not_swallowed():
    """The illegal range of the admitted object must report an error explicitly, and cannot switch back to Python implicitly before continuing."""
    native = _native()
    state = native.PreparedRuleCandidates([0.0, 10.0], [(5.0, 2, (0.0, 0.0, 10.0, 10.0), [1, 2])])
    with pytest.raises(ValueError, match="interval"):
        state.partition(0, 2, 0, 2, 4.0)
    with pytest.raises(ValueError, match="core"):
        state.core(0, 0, [(0.0, 0.0, 10.0, 0.0)])
    with pytest.raises(ValueError, match="geometry"):
        native.PreparedRuleCandidates([10.0, 0.0], [])


def test_full_candidate_enumeration_and_deferred_merge_match_reference(monkeypatch):
    """The real build entry preserves all spans and deferred materialization orders while confirming that shared native objects are actually used."""
    import pickle
    from docvortex import _compute_backend
    from docvortex.analyzers.native.pdf.models import _LineItem

    _native()
    rows, lines = [], []
    for row_index in range(8):
        top = 10.0 + row_index * 10.0
        fragments = []
        for column, (left, right) in enumerate(((10.0, 20.0), (50.0, 60.0), (90.0, 100.0))):
            bbox = (left, top, right, top + 5.0)
            source = row_index * 3 + column
            fragments.append(_Fragment(str(source), bbox, bbox, source, row_index))
            lines.append(_LineItem(str(source), bbox, 0, source, effective_height=5.0, visual_row_id=row_index))
        rows.append(_VisualRow(fragments, top + 2.5, (10.0, top, 100.0, top + 5.0), row_index))
    rules = [
        _LocalAxisLine((0.0, y, 110.0, y + 0.1), (0.0, y, 110.0, y + 0.1), "horizontal", 0.0)
        for y in (8.0, 28.0, 48.0, 68.0, 88.0)
    ]
    args = (rows, lines, (150.0, 120.0), 0, 5.0, rules)
    before = pickle.dumps(args)
    with monkeypatch.context() as reference:
        reference.setattr(_compute_backend, "get_native", lambda: None)
        expected_drafts = rules_api._build_rule_table_candidates(*args, defer_materialization=True)
        expected_scores = [draft.score for draft in expected_drafts]
        expected = rules_api._merge_table_candidates(expected_drafts)
    drafts = rules_api._build_rule_table_candidates(*args, defer_materialization=True)
    assert drafts and any(draft.prepared_candidate is not None for draft in drafts)
    assert [draft.score for draft in drafts] == expected_scores
    assert rules_api._merge_table_candidates(drafts) == expected
    assert pickle.dumps(args) == before


def test_compact_core_members_preserve_sparse_signed_ids_and_first_order():
    """Source compression and cross-bitmap word deduplication preserve the order of first occurrence within a range, supporting negative and full i64 boundaries."""
    native = _native()
    original = [*range(-70, 70), -(2**63), 2**63 - 1]
    sources = [original, [2**63 - 1, -70, 33, -(2**63), 2**63 - 1, 0, -1], [], list(reversed(original))]
    packed = [(float(index), len(row), (0.0, float(index), 10.0, float(index + 1)), row) for index, row in enumerate(sources)]
    state = native.PreparedRuleCandidates([0.0, 10.0], packed)
    boundaries = [(0.0, 0.0, 10.0, 10.0)]
    for _ in range(2):
        for start in range(len(sources)):
            for end in range(start + 1, len(sources) + 1):
                expected = list(dict.fromkeys(source for row in sources[start:end] for source in row))
                assert state.core(start, end, boundaries)[0] == expected


def test_compact_core_member_queries_are_independent_across_threads():
    """Concurrent queries use only their respective local bitmaps and cannot reuse the seen tags or return order of another query."""
    from concurrent.futures import ThreadPoolExecutor

    native = _native()
    sources = [[(index * 43 + offset) % 151 - 75 for offset in range(90)] for index in range(6)]
    packed = [(float(index), len(row), (0.0, float(index), 10.0, float(index + 1)), row) for index, row in enumerate(sources)]
    state = native.PreparedRuleCandidates([0.0, 10.0], packed)
    intervals = [(start, end) for start in range(6) for end in range(start + 1, 7)] * 8

    def query(interval):
        """Check first source order of independent slices in native query that releases GIL."""
        start, end = interval
        expected = list(dict.fromkeys(source for row in sources[start:end] for source in row))
        return state.core(start, end, [(0.0, 0.0, 10.0, 10.0)])[0] == expected

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(query, intervals))


def test_prevalidated_core_interval_reuses_only_identical_corridor_rows(monkeypatch):
    """The second linear slice check is only omitted when the complete corridor row identities of the two indexes are consistent."""
    _native()
    rows = _rows([1.0, 5.0, 9.0], [2, 2, 2])
    index = rules_api._prepare_rule_band_index(rows, _rules([0.0, 10.0, 20.0]))
    assert index.native is not None
    selected = rows[1:]
    core = rules_api._PreparedTableCoreRows(tuple(rows), {}, {}, None)
    expected = (index, 1, 3)
    assert rules_api._prepared_rule_candidate_query(index, selected, (core, 1, 3)) == expected

    def unexpected_scan(self, values):
        """Line-by-line identity scans may not be performed repeatedly in the same authenticated corridor."""
        raise AssertionError("duplicate interval scan")

    with monkeypatch.context() as patched:
        patched.setattr(rules_api._RuleBandIndex, "interval", unexpected_scan)
        assert rules_api._prepared_rule_candidate_query(index, selected, (core, 1, 3)) == expected
    # Same-length tuple field replacement must still invalidate cached matches and cannot reuse old ranges.
    core.rows = tuple(reversed(rows))
    assert rules_api._prepared_rule_candidate_query(index, selected, (core, 0, 2)) == expected
    assert index.shared_core_rows[2] is False
    assert rules_api._prepared_rule_candidate_query(index, rows[::-1], (core, 0, 3)) is None
    # External mutable lists and similar duck objects maintain per-reference verification and cannot build trusted origin caches.
    for foreign in (SimpleNamespace(rows=tuple(rows)), rules_api._PreparedTableCoreRows(list(rows), {}, {}, None)):
        assert rules_api._prepared_rule_candidate_query(index, selected, (foreign, 0, 2)) == expected
        foreign.rows = list(reversed(rows))
        assert rules_api._prepared_rule_candidate_query(index, selected, (foreign, 0, 2)) == expected


def _decode_merged(rows):
    """Restore private outputs to public candidate fields for complete comparison with the original Python rules."""
    from docvortex.analyzers.native.pdf.models import _TableAnnotation, _TableCandidate

    return [
        _TableCandidate(
            tuple(b),
            tuple(local),
            angle,
            score,
            tuple(core) if core is not None else None,
            set(ids),
            [
                _TableAnnotation(kind, tuple(box), set(members), {i: tuple(v) for i, v in boxes})
                for kind, box, members, boxes in annotations
            ],
        )
        for b, local, angle, score, core, ids, annotations in rows
    ]


def test_owned_grid_and_dynamic_merge_match_reference():
    """Random overlaps, different orientations, tied scores, and annotation role conflicts are consistent with the full reference link."""
    import random
    from copy import deepcopy
    from docvortex.analyzers.native.pdf.models import _TableAnnotation, _TableCandidate

    native = _native()
    rng = random.Random(1520)
    for angle in (0, 90, 180, 270, 45):
        for case in range(25):
            size = (100.0, 100.0)
            rows = _rows([float(y) for y in range(5, 96, 10)], [3] * 10)
            grids = [(0.0, 0.0, 10.0, 40.0), (0.0, 35.0, 10.0, 80.0), (1.0, 60.0, 9.0, 100.0)]
            grid = native.NativeTableGrid(
                grids, [(row.center_y, [(f.line_index, f.local_bbox) for f in row.fragments]) for row in rows], size, angle, 4.0
            )
            candidates = []
            for i in range(18):
                x, y = rng.choice((0.0, 1.0, 4.0)), rng.uniform(0.0, 80.0)
                local = (x, y, x + rng.choice((0.0, 5.0, 10.0)), y + rng.uniform(2.0, 25.0))
                box = rules_api._rotate_bbox_from_upright(local, size, angle)
                ids = set(rng.sample(range(-2, 15), 4))
                annotations = [
                    _TableAnnotation(kind, box, set(members), {k: box for k in members})
                    for kind, members in [("caption", rng.sample(range(-3, 15), 3)), ("footnote", rng.sample(range(-3, 15), 2))]
                ][: rng.randrange(3)]
                candidates.append(
                    _TableCandidate(box, local, angle, float(rng.randrange(3)), None if i % 5 == 0 else box, ids, annotations)
                )
            expected = deepcopy(candidates)
            rules_api._expand_candidates_to_connected_rule_grids(
                expected, rows, size, angle, 4.0, [], [], prepared_grid_bboxes=grids, grid_member_cache={}
            )
            expected = rules_api._merge_table_candidates(expected)
            merger = native.NativeTableMerger()
            for c in sorted(candidates, key=lambda c: c.score, reverse=True):
                annotations = [(a.kind, a.bbox, list(a.line_indices), list(a.line_bboxes.items())) for a in c.annotations]
                merger.add(c.bbox, c.local_bbox, c.angle, c.score, c.core_bbox, list(c.line_indices), annotations, grid=grid)
            assert _decode_merged(merger.finish()) == expected, (angle, case)


def test_owned_core_queries_do_not_materialize_members(monkeypatch):
    """Note that local queries and copies do not enumerate cores, and new selections do not change the original core collection."""
    from docvortex.analyzers.native.pdf._native_table_merge import _NativeCoreLineSet

    native = _native()
    core, _ = native.PreparedRuleCandidates([0.0, 10.0], [(5.0, 2, (0.0, 1.0, 10.0, 9.0), [-5, 200])]).owned_core(
        0, 1, [(0.0, 0.0, 10.0, 0.0), (0.0, 10.0, 10.0, 10.0)]
    )
    values = _NativeCoreLineSet(core)

    def reject_iteration(self):
        """Normal annotation queries must not export full core members."""
        raise AssertionError("core materialized")

    monkeypatch.setattr(_NativeCoreLineSet, "__iter__", reject_iteration)
    selected = values.copy()
    selected.update([201, 201, -5])
    assert selected.issuperset([-5, 200, 201])
    assert not values.issuperset([201])
    assert selected.intersection([201, 900]) == {201}
    assert len(selected) == 3 and len(values) == 2


def test_native_merger_validates_before_mutation_and_consumes_once():
    """Invalid subsequent candidates must not change the existing results, and all subsequent operations will clearly report an error."""
    native = _native()
    merger = native.NativeTableMerger()
    box = (0.0, 0.0, 10.0, 10.0)
    merger.add(box, box, 0, 1.0, box, [1], [])
    with pytest.raises(ValueError, match="invalid"):
        merger.add((0.0, 0.0, math.inf, 10.0), box, 0, 2.0, box, [2], [])
    assert len(merger.finish()) == 1
    with pytest.raises(ValueError, match="consumed"):
        merger.finish()
    with pytest.raises(ValueError, match="consumed"):
        merger.add(box, box, 0, 1.0, box, [], [])


def test_owned_merge_rejects_unrepresentable_score_without_mutation():
    """Oversized integer scores retain Python sorting semantics, and admission failures do not modify any candidates."""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf._native_table_merge import merge_owned
    from docvortex.analyzers.native.pdf.models import _TableCandidate

    _native()
    box = (0.0, 0.0, 10.0, 10.0)
    candidates = [_TableCandidate(box, box, 0, 2**2000, line_indices={1}), _TableCandidate(box, box, 0, 1.0, line_indices={2})]
    before = deepcopy(candidates)
    assert merge_owned(candidates) is None
    assert candidates == before
