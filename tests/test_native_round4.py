"""Prefix-by-prefix numerical and full table annotation differences for the fourth round of dense candidate optimization."""

import math
import random
import struct
import sys
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import table_rules as rules


@pytest.fixture
def native():
    """A pure Python installation skips kernel-specific assertions, forcing load errors for Rust not to be swallowed."""
    value = get_native()
    if value is None:
        pytest.skip("native backend is not selected")
    return value


def visual_rows(values):
    """Creates rows containing only the values required for clustering, retaining the type of the incoming coordinates to validate special input fallbacks."""
    return [SimpleNamespace(fragments=[SimpleNamespace(local_bbox=(a, 0.0, b, 1.0)) for a, b in row]) for row in values]


def bits(value):
    """Positive and negative zeros are retained and floating point calculation results are compared bit by bit."""
    return struct.pack("d", value)


@pytest.mark.parametrize("seed", range(24))
def test_stable_columns_every_prefix(native, seed):
    """Prefix-by-prefix checking for first hit, mean, sum, duplicate member rows, and three-category alignment coverage."""
    rng = random.Random(seed)
    values = [
        [
            (x, x + rng.choice([0.0, 1.0, 3.0]))
            for x in [rng.choice([-0.0, 0.0, 3.0, 6.0, rng.uniform(-10, 30)]) for _ in range(rng.randrange(0, 8))]
        ]
        for _ in range(60)
    ]
    rows = visual_rows(values)
    state = native.StableColumnClusters(sys.version_info >= (3, 12))
    reference = rules._new_stable_column_clusters()
    cache = rules._StableColumnCache()
    for i, row in enumerate(rows):
        result = state.extend([values[i]], 3.0)
        rules._extend_stable_column_clusters(reference, [row], start_row_index=i, tolerance=3.0)
        assert result == rules._stable_column_result(reference, i + 1)
        assert rules._count_stable_columns(rows[: i + 1], 4.0, cache, allow_prefix_reuse=True) == result
        assert isinstance(cache.prefixes[(4.0, id(rows[0]))].clusters_by_alignment, native.StableColumnClusters)
        for expected, actual in zip(reference.values(), state.snapshot()):
            assert len(expected) == len(actual)
            for cluster, (mean, total, count, row_count) in zip(expected, actual):
                assert bits(mean) == bits(cluster["mean"])
                assert bits(total) == bits(sum(cluster["values"]))
                assert count == len(cluster["values"])
                assert row_count == len(cluster["rows"])


@pytest.mark.parametrize(
    "values", [[-0.0, -0.0, 0.0], [1e16, 1.0, -1e16, 1.0], [5e-324, -5e-324, 1e-323], [1e200, -1e200, 1e-100]]
)
def test_stable_sum_cancellation(native, values):
    """Huge thresholds force homo-clustering, verify cancellation and subnormality and reads without destroying cumulative compensation."""
    state = native.StableColumnClusters(sys.version_info >= (3, 12))
    for end, value in enumerate(values, 1):
        assert state.extend([[(value, value)]], 1e300) is not None
        mean, total, _, _ = state.snapshot()[0][0]
        assert bits(total) == bits(sum(values[:end]))
        assert bits(mean) == bits(value if end == 1 else sum(values[:end]) / end)


@pytest.mark.parametrize("value", [0, math.inf, math.nan, 1e308])
def test_stable_columns_special_values(native, value):
    """Special input or center overflow must be rolled back as a whole and cannot leave partially updated reusable state."""
    rows = visual_rows([[(value, value)], [(value, value)]])
    cache = rules._StableColumnCache()
    assert rules._count_stable_columns(rows, 4.0, cache, allow_prefix_reuse=True) == rules._count_stable_columns_python(
        rows, 4.0
    )


def test_stable_columns_nonprefix_identity(native):
    """Shortening, swapping, and duplicate row references are recalculated individually without overwriting input members or coordinate objects."""
    rows = visual_rows([[(0.0, 4.0)], [(3.0, 8.0)], [(8.0, 12.0)]])
    coordinates = [r.fragments[0].local_bbox for r in rows]
    cache = rules._StableColumnCache()
    for batch in [rows[:1], rows, rows[:2], rows[::-1], [rows[0], rows[0], rows[2]], rows]:
        assert rules._count_stable_columns(batch, 4.0, cache, allow_prefix_reuse=True) == rules._count_stable_columns_python(
            batch, 4.0
        )
    assert all(r.fragments[0].local_bbox is b for r, b in zip(rows, coordinates))


@pytest.mark.parametrize("seed", range(24))
def test_note_rank_tree_and_member_ranges(native, seed):
    """Compare rank trees and exact member fallback, covering duplicate sources, bounded equality, undersampling, and odd and even quantiles."""
    import statistics

    rng = random.Random(seed)
    items = [
        (rng.randrange(30), rng.choice([0.0, 10.0, 20.0, rng.uniform(-50, 50)]), rng.choice([0.0, -0.0, 0.1, 5.0, 10.0]))
        for _ in range(100)
    ]
    metrics = native.TableNoteMetrics(items)
    members = [[rng.randrange(30) for _ in range(3)] for _ in range(20)]
    indexed = metrics.prepare_rows(members)
    empty = metrics.prepare_rows([[]])
    for _ in range(60):
        top, bottom = sorted(rng.choices([-100.0, 0.0, 10.0, 20.0, 100.0], k=2))
        start, end = sorted(rng.choices(range(21), k=2))
        core = {value for row in members[start:end] for value in row}
        for context, a, b, excluded in [(indexed, start, end, core), (empty, 0, 1, set())]:
            heights = sorted(height for index, center, height in items if index not in excluded and not top <= center <= bottom)
            expected = 6.25 if len(heights) < 4 else statistics.median(heights[-((len(heights) + 3) // 4) :])
            assert bits(metrics.height_for_rows(context, a, b, top, bottom, 6.25)) == bits(expected)
    assert metrics.height_for_rows(indexed, 10, 9, 0.0, 10.0, 6.25) is None
    assert metrics.height_for_rows(indexed, 0, 21, 0.0, 10.0, 6.25) is None
    assert native.TableNoteMetrics(items).height_for_rows(indexed, 0, 20, 0.0, 10.0, 6.25) is None


def test_core_interval_reference_and_marker_index(native):
    """Continuous references can be queried, and swapped rows must not be misused; the universal tag index is judged the same as the original core row."""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem

    lines = [
        _LineItem(text, (0.0, float(i), 10.0, float(i + 1)), 0, i, chars=[])
        for i, text in enumerate(["1", "body text long", "a", "b", "a"])
    ]
    rows = [
        SimpleNamespace(fragments=[SimpleNamespace(line_index=i)], bbox=(0.0, float(i), 10.0, float(i + 1))) for i in range(5)
    ]
    metrics = notes._prepare_table_note_body_metrics(lines, (100.0, 100.0), 0)
    context = notes._prepare_table_core_rows(rows, lines, metrics)
    assert context.marker_safe
    assert context.interval(rows[1:4]) == (1, 4)
    assert context.interval(rows[::-1]) is None
    for marker in ["a", "1", "b", "missing"]:
        cache = {}
        for start in range(5):
            for end in range(start + 1, 6):
                expected = notes._table_core_references_marker(marker, lines[start:end], (100.0, 100.0), 0)
                assert context.references(marker, start, end, lines, (100.0, 100.0), 0, cache) == expected
        assert not cache, "全走廊查询不得建立来源与标记的稠密 False 矩阵"


def test_marker_index_duplicate_sources_and_existing_cache(native):
    """When the source is repeated, the first row cache semantics are followed, and the existing cache cannot be overwritten by precomputation."""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem

    lines = [_LineItem(text, (0.0, float(i), 10.0, float(i + 1)), 0, 1, chars=[]) for i, text in enumerate(["body", "a"])]
    rows = [SimpleNamespace(fragments=[SimpleNamespace(line_index=1)], bbox=(0.0, 0.0, 10.0, 2.0))]
    metrics = notes._prepare_table_note_body_metrics(lines, (100.0, 100.0), 0)
    for cache in [None, {}, {(1, "a"): True}, {(1, "a"): False}]:
        context = notes._prepare_table_core_rows(rows, lines, metrics)
        before = None if cache is None else dict(cache)
        expected = notes._table_core_references_marker("a", lines, (100.0, 100.0), 0, None if cache is None else dict(cache))
        assert context.references("a", 0, 1, lines, (100.0, 100.0), 0, cache) == expected
        assert cache == before


def test_marker_line_context_is_reused_across_corridors(native, monkeypatch):
    """The same candidate context only performs marker input verification once, and multiple corridors reuse the source row index."""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem

    lines = [
        _LineItem(
            text,
            (0.0, float(index), 10.0, float(index + 1)),
            0,
            index,
            chars=[{"char": text, "char_idx": index, "bbox": (0.0, 0.0, 1.0, 1.0)}],
        )
        for index, text in enumerate(("body", "a1", "body"))
    ]
    calls = 0
    original = notes._prepare_marker_line_context

    def counted(values):
        """Only the number of context preparation times is counted and ordinary input judgment is not changed."""
        nonlocal calls
        calls += 1
        return original(values)

    monkeypatch.setattr(notes, "_prepare_marker_line_context", counted)
    context = rules._RuleCandidateContext(
        [], lines, (100.0, 100.0), 0, 8.0, [], [], {}, notes._prepare_table_note_body_metrics(lines, (100.0, 100.0), 0)
    )
    prepared = []
    for corridor in range(2):
        if context.marker_line_context is None:
            context.marker_line_context = notes._prepare_marker_line_context(lines)
        marker_safe, source_lines = context.marker_line_context
        row = SimpleNamespace(
            fragments=[SimpleNamespace(line_index=1)],
            bbox=(0.0, float(corridor), 10.0, float(corridor + 1)),
        )
        prepared.append(
            notes._prepare_table_core_rows(
                [row], lines, context.body_metrics, marker_safe=marker_safe, source_lines=source_lines
            )
        )
    assert calls == 1
    assert marker_safe is True
    assert source_lines == {index: [line] for index, line in enumerate(lines)}
    assert all(item.marker_safe and item.source_lines is source_lines for item in prepared)
    unsafe = [{"char": "x", "char_idx": 0, "bbox": (0.0, 0.0, math.nan, 1.0)}]
    lines[0].chars.append(unsafe)
    assert notes._prepare_marker_line_context(lines) == (False, {})


def test_marker_queries_are_lazy_and_bounded(native, monkeypatch):
    """Small candidates only check the sources they need, repeatedly query the reused bitmap, and mark elimination does not affect the decision."""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem

    ids = [10, 10**12, -5]
    lines = [
        _LineItem(text, (0.0, float(i), 10.0, float(i + 1)), 0, index, chars=[])
        for i, (index, text) in enumerate(zip(ids, ["a", "body", "a"]))
    ]
    rows = [SimpleNamespace(fragments=[SimpleNamespace(line_index=index)], bbox=line.bbox) for index, line in zip(ids, lines)]
    metrics = notes._prepare_table_note_body_metrics(lines, (100.0, 100.0), 0)
    context = notes._prepare_table_core_rows(rows, lines, metrics)
    original = notes._table_core_references_marker
    calls = []

    def record(marker, selected, *args, **kwargs):
        """Record the source of actual execution without replacing decision semantics, avoiding full-corridor precomputation regression."""
        calls.extend(line.source_index for line in selected)
        return original(marker, selected, *args, **kwargs)

    monkeypatch.setattr(notes, "_table_core_references_marker", record)
    assert context.references("a", 0, 1, lines, (100.0, 100.0), 0, {})
    assert calls == [10]
    assert context.references("a", 0, 3, lines, (100.0, 100.0), 0, {})
    assert calls == [10]
    assert not context.references("a", 1, 2, lines, (100.0, 100.0), 0, {})
    assert calls == [10, 10**12]
    for index in range(200):
        assert not context.references(chr(0x4E00 + index), 1, 2, lines, (100.0, 100.0), 0, {})
    assert len(context.marker_states) == 128
    assert all(known.bit_length() <= 3 and hits.bit_length() <= 3 for known, hits in context.marker_states.values())
    assert not context.references("a", 1, 2, lines, (100.0, 100.0), 0, {})
    assert context.references("a", 2, 3, lines, (100.0, 100.0), 0, {})


@pytest.mark.parametrize("seed", range(12))
def test_row_geometry_order_and_coordinate_identity(native, seed):
    """Exhaustive interval extrema and non-monotone lower bounds, negative zero and coordinate source references must be consistent with left folding."""
    from docvortex.analyzers.native.pdf.geometry import _bbox_union_many
    from docvortex.analyzers.native.pdf import table_annotations as notes

    rng = random.Random(seed)
    boxes = [tuple(rng.choice([0.0, -0.0, 1.0, 5.0, rng.uniform(-20, 20)]) for _ in range(4)) for _ in range(32)]
    index = native.TableRowGeometry(boxes)
    rows = [SimpleNamespace(bbox=b) for b in boxes]
    context = notes._PreparedTableCoreRows(rows, {}, {}, None, geometry=index)
    for start in range(len(boxes)):
        for end in range(start + 1, len(boxes) + 1):
            expected = _bbox_union_many(boxes[start:end])
            actual = context.bbox(start, end)
            assert list(map(bits, actual)) == list(map(bits, expected))
            assert all(a is b for a, b in zip(actual, expected))
    for bottom in [-100.0, -0.0, 0.0, 1.0, 5.0, 100.0]:
        expected = next((i for i, b in enumerate(boxes) if b[3] > bottom), len(boxes))
        assert index.first_after(bottom) == expected
    assert index.first_after(math.nan) is None
    assert index.union_indices(0, 0) is None
    with pytest.raises(ValueError):
        native.TableRowGeometry([(0.0, 0.0, math.nan, 1.0)])


def test_note_prefix_skip_preserves_chain(native):
    """Only prefixes that are determined not to participate can be skipped, and the spacing and font stopping rules of subsequent table notes remain unchanged."""
    from docvortex.analyzers.native.pdf import table_annotations as notes
    from docvortex.analyzers.native.pdf.models import _LineItem, _VisualRow, _Fragment

    lines = [
        _LineItem(text, (0.0, y, 80.0, y + 4.0), 0, i, chars=[], effective_height=4.0)
        for i, (y, text) in enumerate(
            [(5.0, "body"), (0.0, "body"), (12.0, "Note: source"), (16.5, "continued"), (30.0, "far")]
        )
    ]
    rows = [
        _VisualRow(
            [_Fragment(line.text, line.bbox, line.bbox, line.source_index)], (line.bbox[1] + line.bbox[3]) / 2, line.bbox
        )
        for line in lines
    ]
    box = (0.0, 0.0, 80.0, 10.0)
    prepared = notes._prepare_table_note_rows(rows, lines, box, 5.0, (100.0, 100.0), 0)
    args = (rows, lines, box, 5.0, {0, 1}, (100.0, 100.0), 0)
    expected = notes._collect_footnote_rows(*args, prepared_rows=list(prepared))
    actual = notes._collect_footnote_rows(*args, prepared_rows=prepared)
    assert actual == expected
    assert all(a is b for a, b in zip(actual, expected))


@pytest.mark.parametrize("fail", [False, True])
def test_visual_worker_releases_recent_cycles(monkeypatch, fail):
    """Both successful and abnormal tasks recycle recent closed-loop payloads without relying on automatic GC counting or changing encoding results."""
    import gc
    import weakref
    from docvortex.document.pdf import images, visuals

    references = []
    closed = []
    generations = []
    collect = gc.collect

    def record_collection(generation=2):
        """Confirm that the exception cleanup has also reached the recycling boundary, and the successful path also verifies that the payload has indeed been released."""
        generations.append(generation)
        return collect(generation)

    class Payload:
        """Simulation turned off PDF The wrapper object still holds a circular reference to the input data."""

        def __init__(self):
            """Creates a loop-only GC recyclable payload, without retaining test-side strong references."""
            self.self_ref = self
            self.data = b"x" * 100_000

    def load(*args):
        """Recent rings generated during simulation rendering must not be accumulated in worker when abnormal."""
        payload = Payload()
        references.append(weakref.ref(payload))
        if fail:
            raise ValueError("render failure")
        return []

    monkeypatch.setattr(images, "load_images_from_pdf_core", load)
    monkeypatch.setattr(images.gc, "collect", record_collection)
    monkeypatch.setattr(images, "_close_image_dicts", lambda value: closed.append(value))
    monkeypatch.setattr(visuals, "_attach_prepared_visual_block_images", lambda *args: None)
    enabled = gc.isenabled()
    gc.disable()
    try:
        if fail:
            with pytest.raises(ValueError, match="render failure"):
                images._load_visual_crops_worker(b"pdf", 200, 0, 0, [])
        else:
            assert images._load_visual_crops_worker(b"pdf", 200, 0, 0, []) == []
            assert references[0]() is None
        assert len(closed) == 1
        assert generations == [0]
    finally:
        if enabled:
            gc.enable()
        collect()
