"""Scanline pruning of the connected components of the plot line must be consistent with the naive full pairing results (#20 regression).

`_connected_drawing_line_components()` Limit matching range with left edge sorting: starting point of second line
When the horizontal clearance exceeds the tolerance plus the right edge of the first line, the Euclidean distance must be larger and can be safely skipped.
Differential testing compared to the naive implementation before repair, covering stacked, total x span, zero-width, endpoint connected, etc.
"""

from __future__ import annotations

import random

import pytest

from docvortex.analyzers.native.pdf import graphics
from docvortex.analyzers.native.pdf.geometry import _bbox_distance
from docvortex.analyzers.native.pdf.models import _AxisLine, _PageSource
from docvortex.document.pdf.native_contracts import PDFPathInfo

BBoxLike = tuple[float, float, float, float]


def _reference_components(
    drawing_lines: list[_AxisLine],
    tolerance: float,
) -> list[list[_AxisLine]]:
    """Naive implementation before repair: calculate Euclidean distance for all pairs one by one."""
    parents = list(range(len(drawing_lines)))

    def find(index: int) -> int:
        """The path is compressed and the reference is returned and the root node is found."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for first_index, first in enumerate(drawing_lines):
        for second_index in range(first_index + 1, len(drawing_lines)):
            if _bbox_distance(first.bbox, drawing_lines[second_index].bbox) <= tolerance:
                first_root, second_root = find(first_index), find(second_index)
                if first_root != second_root:
                    parents[second_root] = first_root

    components: dict[int, list[_AxisLine]] = {}
    for line_index, line in enumerate(drawing_lines):
        components.setdefault(find(line_index), []).append(line)
    return list(components.values())


def _canonical(components: list[list[_AxisLine]]) -> list[tuple[BBoxLike, ...]]:
    """The components are converted into the canonical form sorted by bbox, and the differences between the tree form and the root identity are eliminated and found."""
    return sorted(tuple(sorted(line.bbox for line in component)) for component in components)


def _random_lines(rng: random.Random, count: int) -> list[_AxisLine]:
    """The grid adds dithering horizontal and vertical short lines, mixed with the same x span stacking and connecting endpoints."""
    lines: list[_AxisLine] = []
    for index in range(count):
        column, row = index % 30, index // 30
        x = 20.0 + column * 37.0 + rng.uniform(-2.0, 2.0)
        y = 20.0 + row * 41.0 + rng.uniform(-2.0, 2.0)
        if index % 2 == 0:
            length = rng.choice((4.0, 37.0, 1110.0))
            bbox = (x, y, x + length, y + rng.choice((0.0, 0.5, 2.0)))
            orientation = "horizontal"
        else:
            length = rng.choice((4.0, 41.0, 780.0))
            bbox = (x, y, x + rng.choice((0.0, 0.5, 2.0)), y + length)
            orientation = "vertical"
        lines.append(_AxisLine(bbox=bbox, width=0.3, orientation=orientation))
    return lines


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("tolerance", [2.0, 5.5, 12.0])
def test_sweep_components_match_naive_pair_loop(seed: int, tolerance: float) -> None:
    """The member division of scan line components in random form is consistent bit by bit with naive implementation."""
    rng = random.Random(seed)
    lines = _random_lines(rng, 420)

    assert _canonical(graphics._connected_drawing_line_components(lines, tolerance)) == _canonical(
        _reference_components(lines, tolerance)
    )


def test_sweep_bounds_evaluated_pairs_on_dispersed_grid() -> None:
    """The number of pairs for the actual calculated distance on the dispersed grid is linearly constrained instead of n² full sweep."""
    lines: list[_AxisLine] = []
    for index in range(1800):
        column, row = index % 60, index // 60
        x, y = 20.0 + column * 33.0, 20.0 + row * 27.0
        if index % 2 == 0:
            lines.append(_AxisLine(bbox=(x, y, x + 5.0, y + 0.4), width=0.3, orientation="horizontal"))
        else:
            lines.append(_AxisLine(bbox=(x, y, x + 0.4, y + 5.0), width=0.3, orientation="vertical"))

    counter = {"calls": 0}
    original = graphics._bbox_distance

    def counting(first, second):
        """Candidate pairs that still need to be accurately calculated after statistical spatial index screening."""
        counter["calls"] += 1
        return original(first, second)

    graphics._bbox_distance = counting
    try:
        components = graphics._connected_drawing_line_components(lines, 2.0)
    finally:
        graphics._bbox_distance = original

    assert all(isinstance(component, list) for component in components)
    # The 60-column line sharing the starting point of x naturally cannot be pruned by the x window (26100 times), but must be well below n²/2=1620000.
    assert counter["calls"] <= 20 * len(lines)


@pytest.mark.parametrize("shape", ["long_horizontal", "overlap_x_separate_y", "dense_cross"])
def test_sweep_components_match_naive_degenerate_shapes(shape: str) -> None:
    """Long horizontal lines, distant lines with the same x and densely crossing lines all maintain their original connected members and order."""
    lines: list[_AxisLine] = []
    for index in range(180):
        if shape == "long_horizontal":
            bbox = (0.0, index * 7.0, 2000.0, index * 7.0 + 0.5)
            orientation = "horizontal"
        elif shape == "overlap_x_separate_y":
            bbox = (10.0, index * 11.0, 11.0, index * 11.0 + 3.0)
            orientation = "vertical"
        elif index % 2:
            bbox = (0.0, index * 0.3, 2000.0, index * 0.3 + 0.5)
            orientation = "horizontal"
        else:
            bbox = (index * 0.3, 0.0, index * 0.3 + 0.5, 2000.0)
            orientation = "vertical"
        lines.append(_AxisLine(bbox=bbox, width=0.3, orientation=orientation))

    assert graphics._connected_drawing_line_components(lines, 2.0) == _reference_components(lines, 2.0)


def test_component_summaries_reuse_only_identical_lines_and_tolerance(monkeypatch) -> None:
    """The same input on the same page is only calculated once and must be recalculated after changing line objects or tolerances."""
    lines = _random_lines(random.Random(4), 80)
    source = _PageSource(page_size=(1200.0, 800.0), lines=[], chars=[], drawing_lines=lines)
    original = graphics._connected_drawing_line_components
    counter = {"calls": 0}

    def counting(drawing_lines, tolerance):
        """Count the actual number of times connected component calculations are entered."""
        counter["calls"] += 1
        return original(drawing_lines, tolerance)

    monkeypatch.setattr(graphics, "_connected_drawing_line_components", counting)
    first = graphics._drawing_component_summaries(source, 2.0)
    assert graphics._drawing_component_summaries(source, 2.0) is first
    assert counter["calls"] == 1
    graphics._drawing_component_summaries(source, 3.0)
    source.drawing_lines = list(lines)
    graphics._drawing_component_summaries(source, 2.0)
    assert counter["calls"] == 3


def test_axis_index_preserves_boundary_hits_and_original_vertical_order() -> None:
    """The vertical axis index hits at the tolerance edge and the intersection figures are output in the order in which they were entered."""

    def path(bbox, segment_count=2):
        """Constructs a true path record containing only the fields required for axis detection."""
        return PDFPathInfo(bbox, segment_count, False, True, 0, 0)

    horizontal = path((100.0, 99.5, 200.0, 100.5))
    right = path((209.5, 90.0, 210.5, 160.0))
    left = path((89.5, 90.0, 90.5, 170.0))
    middle = path((145.0, 90.0, 146.0, 170.0))
    complex_path = path((100.0, 105.0, 200.0, 130.0), 8)
    result = graphics._detect_axis_path_graphics(
        [horizontal, right, left, middle, complex_path], (500.0, 500.0), 10.0
    )
    assert result == [(100.0, 90.0, 210.5, 160.0), (89.5, 90.0, 200.0, 170.0)]
