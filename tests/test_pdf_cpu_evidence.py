"""Verify conservative equivalence of page primitive reuse, source data read-only, and row pair filtering."""

from __future__ import annotations

import json
import pickle
import random
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex.analyzers import pdf as analysis
from docvortex.analyzers.native.pdf import line_merging as merging
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.analyzers.native.pdf.native_text import _build_native_line_items
from docvortex.document.pdf import Bbox, PDFDocument, PDFPageVectorGeometry, get_lines_from_chars
from docvortex.document.pdf import _document


def _pdf() -> bytes:
    """Generates a standalone PDF with links, rotated text, superscripts and subscripts, and vector lines."""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(400, 600))
    for index in range(25):
        canvas.setFont("Helvetica", 12)
        canvas.drawString(30, 570 - index * 20, f"Row {index}: x")
        canvas.setFont("Helvetica", 7)
        canvas.drawString(130, 574 - index * 20, "2")
        canvas.line(20, 566 - index * 20, 180, 566 - index * 20)
    canvas.linkURL("https://example.com", (30, 550, 90, 565), relative=0)
    canvas.saveState()
    canvas.translate(300, 200)
    canvas.rotate(90)
    canvas.drawString(0, 0, "Rotated text")
    canvas.restoreState()
    canvas.rect(20, 50, 170, 540)
    canvas.save()
    return buffer.getvalue()


def test_vector_snapshot_matches_independent_queries_and_outlives_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """A combined pass is value-for-value consistent with the old query and remains safe to use after the document is closed."""
    with PDFDocument(_pdf()) as document:
        page = document[0]
        drawings, paths = page.get_drawing_lines(), page.get_path_infos()
        extract = MagicMock(wraps=_document._extract_page_paths_and_lines)
        monkeypatch.setattr(_document, "_extract_page_paths_and_lines", extract)
        vectors = page.get_vector_geometry()
        assert vectors == PDFPageVectorGeometry(tuple(drawings), tuple(paths))
        extract.assert_called_once()
    assert pickle.loads(pickle.dumps(vectors)) == vectors


def test_shared_primitives_preserve_characters_and_text_evidence() -> None:
    """Sharing character and vector geometry leaves the source object unchanged and preserves the final evidence of area dependence."""
    with PDFDocument(_pdf()) as document:
        page = document[0]
        geometry = page.get_chars_with_geometry()
        vectors = page.get_vector_geometry()
        before = pickle.dumps(geometry)
        expected = analysis.prepare_text_evidence(page, geometry=geometry, table_regions=[(20, 30, 190, 300)])
        materialized_page = MagicMock(size=page.size, rotation=page.rotation)
        materialized_page.get_link_annotations.return_value = page.get_link_annotations()
        actual = analysis.prepare_text_evidence(
            materialized_page, geometry=geometry, vector_geometry=vectors, table_regions=[(20, 30, 190, 300)]
        )
        assert (actual.styles, actual.links, actual.scripts) == (expected.styles, expected.links, expected.scripts)
        assert actual.geometry is geometry
        analysis.prepare_table_page(materialized_page, geometry=geometry, vector_geometry=vectors)
        materialized_page.get_chars_with_geometry.assert_not_called()
        materialized_page.get_vector_geometry.assert_not_called()
        materialized_page.get_drawing_lines.assert_not_called()
        materialized_page.get_path_infos.assert_not_called()
        assert pickle.dumps(geometry) == before


def _groups(lines: list[_LineItem]) -> dict[tuple[int, bool, str | None], list[int]]:
    """Create an index based on the necessary classification conditions of the original merge rule."""
    groups: dict[tuple[int, bool, str | None], list[int]] = {}
    for index, line in enumerate(lines):
        groups.setdefault((line.angle, line.formula_candidate_only, line.semantic_type), []).append(index)
    return groups


def test_candidate_screening_never_drops_original_acceptance() -> None:
    """Covering the situation where the source box offset and font scale are larger than the visible box, the accepted superset of the original decision is verified pair by pair."""
    rng = random.Random(918)
    accepted = 0
    for _ in range(30):
        lines = []
        for index in range(80):
            x, y = rng.uniform(0, 200), rng.uniform(0, 200)
            width, height = rng.uniform(0.1, 30), rng.uniform(0.1, 25)
            box = (x, y, x + width, y + height)
            lines.append(
                _LineItem(
                    "x",
                    box,
                    0,
                    index,
                    source_bbox=(x - 5, y - 10, x + width + 5, y + height + 10),
                    baseline=y + height,
                    chars=[{"char_idx": index, "source_indices": (index,), "bbox": Bbox(list(box)), "char": "x"}],
                    visual_row_id=index,
                    effective_height=rng.uniform(0.1, 80),
                    font_signature=("test", 0),
                    font_coverage=1,
                )
            )
        candidates = merging._same_baseline_candidate_pairs(lines, [line.bbox for line in lines], _groups(lines))
        assert candidates is not None
        for left, first in enumerate(lines):
            assert candidates[left] == sorted(set(candidates[left]))
            for right in range(left + 1, len(lines)):
                second = lines[right]
                if merging._can_merge_same_baseline_pair(first, first.bbox, second, second.bbox, []):
                    accepted += 1
                    assert right in candidates[left]
    assert accepted > 100


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_invalid_geometry_uses_exhaustive_fallback(invalid: float) -> None:
    """The abnormal source box cannot quietly narrow down the candidates and is returned to the existing algorithm for processing."""
    lines = [_LineItem("a", (0, 0, 20, 10), 0, 0), _LineItem("b", (21, 0, 40, 10), 0, 1)]
    lines[1].source_bbox = (0, invalid, 40, 10)
    assert merging._same_baseline_candidate_pairs(lines, [line.bbox for line in lines], _groups(lines)) is None


def test_dense_page_bounds_candidate_storage() -> None:
    """Range queries do not truncate members; additional geometric prefiltering only allows deletion of row pairs that are rejected by the original rule."""
    from docvortex.analyzers.native.pdf._interval_candidates import IntervalCandidates

    lines = [_LineItem("a", (0, 0, 20, 10), 0, index) for index in range(128)]
    intervals = IntervalCandidates([(0, 10)] * len(lines), _groups(lines))
    assert intervals[0] == list(range(1, 128))
    assert intervals[127] == []
    candidates = merging._same_baseline_candidate_pairs(lines, [line.bbox for line in lines], _groups(lines))
    assert candidates is not None
    assert not isinstance(candidates, list)
    for first, line in enumerate(lines):
        partners = candidates[first]
        assert partners == sorted(set(partners))
        assert all(second > first for second in partners)
        assert [
            second
            for second in partners
            if merging._can_merge_same_baseline_pair(line, line.bbox, lines[second], lines[second].bbox, [])
        ] == [
            second
            for second in range(first + 1, len(lines))
            if merging._can_merge_same_baseline_pair(line, line.bbox, lines[second], lines[second].bbox, [])
        ]
    assert candidates[127] == []


def test_bottom_edge_tolerance_is_included() -> None:
    """The original rule candidate remains when the visible boxes do not overlap but the effective font scale allows for bottom edge alignment."""
    lines = [
        _LineItem("a", (0, 0, 10, 1), 0, 0, effective_height=40, font_signature=("font", 0), font_coverage=1),
        _LineItem("b", (11, 10, 21, 11), 0, 1, effective_height=40, font_signature=("font", 0), font_coverage=1),
    ]
    assert merging._can_merge_same_baseline_pair(lines[0], lines[0].bbox, lines[1], lines[1].bbox, [])
    assert merging._same_baseline_candidate_pairs(lines, [line.bbox for line in lines], _groups(lines)) == [[1], []]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("tables", [[], [(0, 100, 400, 180)]])
def test_merged_output_matches_exhaustive_order(monkeypatch: pytest.MonkeyPatch, angle: int, tables: list) -> None:
    """Compares complete row objects, confirms that filtering does not change the set of claims, characters, or result ordering."""
    with PDFDocument(_pdf()) as document:
        page = document[0]
        lines = _build_native_line_items(get_lines_from_chars(page.get_chars_with_geometry().chars), page.size)
        for line in lines:
            line.angle = angle
        actual = merging.merge_text_line_clusters(deepcopy(lines), page.size, tables)
        monkeypatch.setattr(merging, "_same_baseline_candidate_pairs", lambda *_args: None)
        expected = merging.merge_text_line_clusters(deepcopy(lines), page.size, tables)
        assert json.dumps([asdict(line) for line in actual], default=repr, sort_keys=True) == json.dumps(
            [asdict(line) for line in expected], default=repr, sort_keys=True
        )
