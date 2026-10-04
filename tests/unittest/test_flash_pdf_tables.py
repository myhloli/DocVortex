from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docvortex.analyzers.native.pdf import geometry, models, table_annotations, table_materialization, table_rules, tables
from docvortex.document.pdf._document import PDFPathInfo


def _axis_line(
    orientation: str,
    bbox: tuple[float, float, float, float],
) -> models._LocalAxisLine:
    """Constructs local horizontal and vertical lines used by table candidate tests."""

    return models._LocalAxisLine(
        bbox=bbox,
        original_bbox=bbox,
        orientation=orientation,  # type: ignore[arg-type]
        width=0.0,
    )


def _path_info(
    bbox: tuple[float, float, float, float],
    source_index: int,
    *,
    form_depth: int = 0,
) -> PDFPathInfo:
    """Constructs the rectangle Path used in the filled grid test."""

    return PDFPathInfo(
        bbox=bbox,
        segment_count=5,
        fill_visible=True,
        stroke_visible=False,
        form_depth=form_depth,
        source_index=source_index,
    )


def test_continuation_marker_is_externalized_above_precise_rule_grid() -> None:
    """Verify that the continuation table is marked as independent caption and that the table body starts from the top line of the grid connected to the vertical rail support."""

    continuation = models._LineItem(
        text="续表",
        bbox=(80.0, 10.0, 95.0, 18.0),
        angle=0,
        source_index=0,
        effective_height=8.0,
    )
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[continuation],
        chars=[],
        drawing_lines=[
            models._AxisLine(
                bbox=(0.0, 30.0, 100.0, 30.5),
                width=0.5,
                orientation="horizontal",
            ),
            models._AxisLine(
                bbox=(0.0, 60.0, 100.0, 60.5),
                width=0.5,
                orientation="horizontal",
            ),
            models._AxisLine(
                bbox=(0.0, 30.0, 0.5, 60.5),
                width=0.5,
                orientation="vertical",
            ),
            models._AxisLine(
                bbox=(99.5, 30.0, 100.0, 60.5),
                width=0.5,
                orientation="vertical",
            ),
        ],
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 0.0, 100.0, 60.5),
        local_bbox=(0.0, 0.0, 100.0, 60.5),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 0.0, 100.0, 60.5),
        line_indices={0},
    )

    tables._externalize_table_continuation_captions(
        source,
        [candidate],
    )

    assert candidate.core_bbox == (0.0, 30.0, 100.0, 60.5)
    assert candidate.line_indices == set()
    assert len(candidate.annotations) == 1
    assert candidate.annotations[0].kind == "caption"
    assert candidate.annotations[0].line_indices == {0}


def _filled_grid_path_fixture(
    *,
    band_count: int = 5,
    cell_gap: float = 0.0,
    right_edge: float = 495.0,
    form_depth: int = 0,
) -> list[PDFPathInfo]:
    """Constructs a five-row, two-column filled grid with repeating Path, half-row shading, and edge bars."""

    output = [
        _path_info((100.0, 100.0, 500.0, 400.0), 0, form_depth=form_depth),
        _path_info((105.0, 101.0, 495.0, 104.0), 1, form_depth=form_depth),
        _path_info((105.0, 396.0, 495.0, 399.0), 2, form_depth=form_depth),
    ]
    row_bounds = [
        (105.0, 160.0),
        (160.0, 230.0),
        (230.0, 280.0),
        (280.0, 335.0),
        (335.0, 395.0),
    ]
    source_index = len(output)
    for row_index, (top, bottom) in enumerate(row_bounds[:band_count]):
        cells = [
            (105.0, top, 300.0, bottom),
            (300.0 + cell_gap, top, right_edge, bottom),
        ]
        for cell in cells:
            output.append(_path_info(cell, source_index, form_depth=form_depth))
            source_index += 1
        if row_index == 0:
            output.append(_path_info(cells[0], source_index, form_depth=form_depth))
            source_index += 1
        if row_index % 2 == 1:
            middle = 0.5 * (top + bottom)
            for left, _top, right, _bottom in cells:
                for half_bbox in (
                    (left, top, right, middle),
                    (left, middle, right, bottom),
                ):
                    output.append(
                        _path_info(
                            half_bbox,
                            source_index,
                            form_depth=form_depth,
                        )
                    )
                    source_index += 1
    return output


def test_filled_grid_geometry_detects_exact_outer_bbox_without_text() -> None:
    """Verify that plain Path grid retains accurate outlines without text and clear nested copies."""

    path_infos = _filled_grid_path_fixture()
    rectangles = [path_info.bbox for path_info in path_infos]
    candidates = tables._detect_filled_grid_table_candidates(
        path_infos,
        (1000.0, 1000.0),
    )
    cells = tables._select_maximal_filled_grid_cells(
        rectangles,
        (100.0, 100.0, 500.0, 400.0),
    )

    assert len(cells) == 10
    assert len(candidates) == 1
    assert candidates[0].bbox == (100.0, 100.0, 500.0, 400.0)
    assert candidates[0].core_bbox == candidates[0].bbox
    assert candidates[0].line_indices == set()

    source = models._PageSource(
        page_size=(1000.0, 1000.0),
        lines=[],
        chars=[],
        drawing_lines=[],
        path_infos=path_infos,
    )
    assert [candidate.bbox for candidate in tables._detect_table_candidates(source)] == [(100.0, 100.0, 500.0, 400.0)]

    source.lines = [
        models._LineItem(
            text="任意替换内容",
            bbox=(120.0, 120.0, 260.0, 140.0),
            angle=0,
            source_index=0,
            effective_height=20.0,
        )
    ]
    assert [candidate.bbox for candidate in tables._detect_table_candidates(source)] == [(100.0, 100.0, 500.0, 400.0)]


def test_filled_grid_geometry_rejects_incomplete_or_interfering_paths() -> None:
    """Verify that insufficient line strips, horizontal damage, non-root layers, and strong graphic overlap cannot generate an outer frame."""

    page_size = (1000.0, 1000.0)
    assert (
        tables._detect_filled_grid_table_candidates(
            _filled_grid_path_fixture(band_count=3),
            page_size,
        )
        == []
    )
    assert (
        tables._detect_filled_grid_table_candidates(
            _filled_grid_path_fixture(right_edge=450.0),
            page_size,
        )
        == []
    )
    assert (
        tables._detect_filled_grid_table_candidates(
            _filled_grid_path_fixture(cell_gap=10.0),
            page_size,
        )
        == []
    )
    assert (
        tables._detect_filled_grid_table_candidates(
            _filled_grid_path_fixture(form_depth=1),
            page_size,
        )
        == []
    )
    assert (
        tables._detect_filled_grid_table_candidates(
            _filled_grid_path_fixture(),
            page_size,
            [(120.0, 120.0, 480.0, 380.0)],
        )
        == []
    )


def test_filled_grid_candidate_prevents_rule_bbox_expansion() -> None:
    """After verifying filled grid priority, overlapping horizontal line candidates cannot expand their Path bounding box."""

    lines: list[models._LineItem] = []
    source_index = 0
    for row_index, top in enumerate((120.0, 190.0, 260.0, 330.0)):
        for left, right in ((120.0, 280.0), (320.0, 480.0)):
            lines.append(
                models._LineItem(
                    text=f"cell-{source_index}",
                    bbox=(left, top, right, top + 20.0),
                    angle=0,
                    source_index=source_index,
                    effective_height=20.0,
                    visual_row_id=row_index,
                )
            )
            source_index += 1
    source = models._PageSource(
        page_size=(1000.0, 1000.0),
        lines=lines,
        chars=[],
        drawing_lines=[
            models._AxisLine(
                bbox=(80.0, 90.0, 520.0, 91.0),
                width=1.0,
                orientation="horizontal",
            ),
            models._AxisLine(
                bbox=(80.0, 410.0, 520.0, 411.0),
                width=1.0,
                orientation="horizontal",
            ),
        ],
        path_infos=_filled_grid_path_fixture(),
    )

    candidates = tables._detect_table_candidates(source)

    assert [candidate.bbox for candidate in candidates] == [(100.0, 100.0, 500.0, 400.0)]


def test_filled_grid_materialization_uses_existing_spatial_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validating empty member candidates by core bbox collects text without establishing cell ownership."""

    lines = [
        models._LineItem(
            text="inside-left",
            bbox=(120.0, 120.0, 240.0, 140.0),
            angle=0,
            source_index=0,
            effective_height=20.0,
        ),
        models._LineItem(
            text="inside-right",
            bbox=(330.0, 120.0, 480.0, 140.0),
            angle=0,
            source_index=1,
            effective_height=20.0,
        ),
        models._LineItem(
            text="outside",
            bbox=(120.0, 430.0, 240.0, 450.0),
            angle=0,
            source_index=2,
            effective_height=20.0,
        ),
    ]
    source = models._PageSource(
        page_size=(1000.0, 1000.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
        path_infos=_filled_grid_path_fixture(),
    )
    candidate = tables._detect_filled_grid_table_candidates(
        source.path_infos,
        source.page_size,
    )[0]
    projection = MagicMock(return_value="inside-left   inside-right")
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert claimed == {0, 1}
    assert annotation_blocks == []
    assert blocks == [
        {
            "type": "table",
            "bbox": (100.0, 100.0, 500.0, 400.0),
            "angle": 0,
            "content": "inside-left   inside-right",
        }
    ]
    assert projection.call_args.args[0] is source.chars


def test_table_materialization_preserves_raw_page_line_breaks() -> None:
    """Use original line breaks when verifying that the short last column overlaps slightly with the next row, and disable cross-row splicing."""

    def build_chars(
        text: str,
        left: float,
        top: float,
        start_index: int,
    ) -> list[dict[str, object]]:
        """Construct spatially projected test characters with stable character numbers and compact character boxes."""

        return [
            {
                "char": char,
                "bbox": (
                    left + 2.0 * offset,
                    top,
                    left + 2.0 * (offset + 1),
                    top + 10.0,
                ),
                "char_idx": start_index + offset,
            }
            for offset, char in enumerate(text)
        ]

    error_chars = build_chars("6.20", 80.0, 10.0, 0)
    method_chars = build_chars("CostFilter [10]", 10.0, 19.8, 6)
    source = models._PageSource(
        page_size=(100.0, 40.0),
        lines=[
            models._LineItem(
                text="6.20",
                bbox=(80.0, 10.0, 88.0, 20.0),
                angle=0,
                source_index=0,
                chars=error_chars,
                effective_height=10.0,
                visual_row_id=0,
            ),
            models._LineItem(
                text="CostFilter [10]",
                bbox=(10.0, 19.8, 40.0, 29.8),
                angle=0,
                source_index=1,
                chars=method_chars,
                effective_height=10.0,
                visual_row_id=1,
            ),
        ],
        chars=[
            *error_chars,
            {"char": "\r", "bbox": (88.0, 18.0, 88.0, 18.0), "char_idx": 4},
            {"char": "\n", "bbox": (88.0, 18.0, 88.0, 18.0), "char_idx": 5},
            *method_chars,
        ],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 0.0, 100.0, 40.0),
        local_bbox=(0.0, 0.0, 100.0, 40.0),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 0.0, 100.0, 40.0),
        line_indices={0, 1},
    )

    blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert annotation_blocks == []
    assert claimed == {0, 1}
    assert len(blocks) == 1
    assert blocks[0]["content"].splitlines() == [
        "                                   6.20",
        "CostFilter [10]",
    ]
    assert "6.20CostFilter" not in blocks[0]["content"]


def test_table_core_reclaims_semantic_line_without_touching_outer_marginals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the pre-marked footer within core can be claimed by the table, while the footer and page number outside the table frame remain unclaimed."""

    lines = [
        models._LineItem(
            text="cell",
            bbox=(10.0, 10.0, 40.0, 20.0),
            angle=0,
            source_index=0,
            effective_height=10.0,
        ),
        models._LineItem(
            text="table tail",
            bbox=(10.0, 40.0, 40.0, 50.0),
            angle=0,
            source_index=1,
            effective_height=10.0,
            semantic_type="footer",
        ),
        models._LineItem(
            text="disclaimer",
            bbox=(10.0, 70.0, 60.0, 80.0),
            angle=0,
            source_index=2,
            effective_height=10.0,
            semantic_type="footer",
        ),
        models._LineItem(
            text="1",
            bbox=(90.0, 70.0, 95.0, 80.0),
            angle=0,
            source_index=3,
            effective_height=10.0,
            semantic_type="page_number",
        ),
    ]
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 0.0, 80.0, 60.0),
        local_bbox=(0.0, 0.0, 80.0, 60.0),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 0.0, 80.0, 60.0),
        line_indices={0},
    )
    projection = MagicMock(return_value="cell\ntable tail")
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert len(blocks) == 1
    assert annotation_blocks == []
    assert claimed == {0, 1}
    assert projection.call_args.args[0] is source.chars
    assert {line.source_index for line in lines if line.source_index not in claimed} == {
        2,
        3,
    }


@pytest.mark.parametrize(
    ("median_height", "rejected_length", "accepted_length"),
    [
        (3.0, 39.99, 40.0),
        (5.0, 49.99, 50.0),
    ],
)
def test_long_rule_group_uses_40pt_or_ten_times_height_threshold(
    median_height: float,
    rejected_length: float,
    accepted_length: float,
) -> None:
    """Verify that long horizontal lines follow the threshold of 40pt or ten times the line height, and vertical lines do not participate in grouping."""

    rejected_lines = [
        _axis_line("horizontal", (0.0, 10.0, rejected_length, 10.1)),
        _axis_line("horizontal", (0.0, 20.0, accepted_length, 20.1)),
        _axis_line("horizontal", (0.0, 30.0, accepted_length, 30.1)),
        _axis_line("vertical", (10.0, 0.0, 10.1, 40.0)),
    ]
    accepted_lines = [_axis_line("horizontal", (0.0, top, accepted_length, top + 0.1)) for top in (10.0, 20.0, 30.0)]

    grouped = tables._group_long_horizontal_rules(rejected_lines, median_height)
    assert len(grouped) == 1
    assert [line.bbox for line in grouped[0]] == [
        (0.0, 20.0, accepted_length, 20.1),
        (0.0, 30.0, accepted_length, 30.1),
    ]
    assert len(tables._group_long_horizontal_rules(accepted_lines, median_height)) == 1


def test_connected_rule_grid_expands_core_and_reclaims_sparse_bottom_row() -> None:
    """Verifying that the continuous physical grid completes candidate bases and reclaims sparse last lines of text."""

    bottom_bbox = (10.0, 66.0, 35.0, 72.0)
    bottom_line = models._LineItem(
        text="sparse bottom",
        bbox=bottom_bbox,
        angle=0,
        source_index=9,
        effective_height=6.0,
        visual_row_id=3,
    )
    bottom_row = models._VisualRow(
        fragments=[
            models._Fragment(
                text=bottom_line.text,
                bbox=bottom_bbox,
                local_bbox=bottom_bbox,
                line_index=9,
                visual_row_id=3,
            )
        ],
        center_y=69.0,
        bbox=bottom_bbox,
        visual_row_id=3,
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 8.0, 110.0, 58.1),
        local_bbox=(0.0, 8.0, 110.0, 58.1),
        angle=0,
        score=8.0,
        core_bbox=(0.0, 8.0, 110.0, 58.1),
        line_indices=set(range(9)),
    )
    axis_lines = [
        *[_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in (8.0, 58.0, 80.0)],
        *[_axis_line("vertical", (left, 8.0, left + 0.1, 80.1)) for left in (0.0, 55.0, 109.9)],
    ]

    expanded = tables._expand_candidates_to_connected_rule_grids(
        [candidate],
        [bottom_row],
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        [],
    )

    assert expanded[0].core_bbox == pytest.approx((0.0, 8.0, 110.0, 80.1))
    assert expanded[0].bbox == pytest.approx((0.0, 8.0, 110.0, 80.1))
    assert 9 in expanded[0].line_indices


def test_disconnected_stacked_rule_grids_remain_separate() -> None:
    """When verifying that the vertical rails do not span the empty space, the upper and lower grids of the same span remain as two tables."""

    axis_lines = [
        *[_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in (8.0, 58.0, 90.0, 140.0)],
        *[_axis_line("vertical", (left, 8.0, left + 0.1, 58.1)) for left in (0.0, 55.0, 109.9)],
        *[_axis_line("vertical", (left, 90.0, left + 0.1, 140.1)) for left in (0.0, 55.0, 109.9)],
    ]

    grid_bboxes = tables._connected_rule_grid_bboxes(axis_lines, 5.0)

    assert grid_bboxes == pytest.approx(
        [
            (0.0, 8.0, 110.0, 58.1),
            (0.0, 90.0, 110.0, 140.1),
        ]
    )


def _closed_sparse_grid_fixture(
    *,
    horizontal_count: int = 3,
) -> tuple[
    list[models._VisualRow],
    list[models._LineItem],
    list[models._LocalAxisLine],
]:
    """Construct a physical mesh with only a small amount of text but completely closed outer rails."""

    fragment_bboxes = (
        (10.0, 10.0, 30.0, 15.0),
        (75.0, 10.0, 95.0, 15.0),
    )
    fragments = [
        models._Fragment(
            text=f"cell-{index}",
            bbox=bbox,
            local_bbox=bbox,
            line_index=index,
            visual_row_id=0,
        )
        for index, bbox in enumerate(fragment_bboxes)
    ]
    rows = [
        models._VisualRow(
            fragments=fragments,
            center_y=12.5,
            bbox=geometry._bbox_union_many(fragment_bboxes),
            visual_row_id=0,
        )
    ]
    lines = [
        models._LineItem(
            text=fragment.text,
            bbox=fragment.bbox,
            angle=0,
            source_index=fragment.line_index,
            effective_height=5.0,
            visual_row_id=0,
        )
        for fragment in fragments
    ]
    horizontal_tops = (0.0, 20.0, 40.0) if horizontal_count == 3 else (0.0, 40.0)
    axis_lines = [
        *[_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in horizontal_tops],
        *[_axis_line("vertical", (left, 0.0, left + 0.1, 40.1)) for left in (0.0, 55.0, 109.9)],
    ]
    return rows, lines, axis_lines


def test_closed_grid_accepts_sparse_single_column_form_with_empty_row() -> None:
    """Verify that three horizontal borders and a complete outer rail can accommodate a single-column form with empty data rows."""

    rows, lines, axis_lines = _closed_sparse_grid_fixture()
    rows[0].fragments = rows[0].fragments[:1]
    rows[0].bbox = rows[0].fragments[0].bbox
    lines = lines[:1]

    candidates = tables._build_closed_rule_grid_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        [],
    )

    assert len(candidates) == 1
    assert candidates[0].core_bbox == pytest.approx((0.0, 0.0, 110.0, 40.1))
    assert candidates[0].line_indices == {0}


def test_closed_two_boundary_grid_requires_text_in_two_physical_columns() -> None:
    """Verify that two horizontal borders are only accepted if three cross the vertical rails and the text occupies two columns."""

    rows, lines, axis_lines = _closed_sparse_grid_fixture(
        horizontal_count=2,
    )

    candidates = tables._build_closed_rule_grid_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        [],
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == {0, 1}


@pytest.mark.parametrize(
    "failure_mode",
    [
        "no_text",
        "short_outer_tracks",
        "only_two_vertical_tracks",
        "one_occupied_column",
        "excluded_graphic",
    ],
)
def test_closed_sparse_grid_rejects_incomplete_or_excluded_geometry(
    failure_mode: str,
) -> None:
    """Verify that empty boxes, short outer rails, single-frame boxes, single-column text, and strong graphic areas do not falsely report tables."""

    rows, lines, axis_lines = _closed_sparse_grid_fixture(
        horizontal_count=2,
    )
    excluded_bboxes: list[tuple[float, float, float, float]] = []
    if failure_mode == "no_text":
        rows = []
        lines = []
    elif failure_mode == "short_outer_tracks":
        axis_lines = [
            _axis_line("vertical", (line.bbox[0], 5.0, line.bbox[2], 35.0)) if line.orientation == "vertical" else line
            for line in axis_lines
        ]
    elif failure_mode == "only_two_vertical_tracks":
        axis_lines = [line for line in axis_lines if line.orientation != "vertical" or line.bbox[0] != 55.0]
    elif failure_mode == "one_occupied_column":
        rows[0].fragments[1].bbox = (32.0, 10.0, 48.0, 15.0)
        rows[0].fragments[1].local_bbox = rows[0].fragments[1].bbox
        rows[0].bbox = geometry._bbox_union_many([fragment.bbox for fragment in rows[0].fragments])
    else:
        excluded_bboxes = [(0.0, 0.0, 110.0, 40.1)]

    assert (
        tables._build_closed_rule_grid_candidates(
            rows,
            lines,
            (150.0, 100.0),
            0,
            5.0,
            axis_lines,
            excluded_bboxes,
        )
        == []
    )


def test_closed_grid_inside_form_bbox_is_not_a_table_candidate() -> None:
    """Verify that closed wireframes within Form containers continue to be processed by the image branch without generating a table."""

    rows, lines, axis_lines = _closed_sparse_grid_fixture()
    source = models._PageSource(
        page_size=(150.0, 100.0),
        lines=lines,
        chars=[],
        drawing_lines=[
            models._AxisLine(
                bbox=line.bbox,
                width=line.width,
                orientation=line.orientation,
            )
            for line in axis_lines
        ],
        form_bboxes=[(0.0, 0.0, 110.0, 40.1)],
    )

    assert tables._detect_table_candidates(source) == []


def _rule_table_fixture(
    rule_count: int = 3,
    *,
    centered_columns: bool = False,
) -> tuple[
    list[models._VisualRow],
    list[models._LineItem],
    list[models._LocalAxisLine],
]:
    """Constructs a regular table without caption and switchable to a centered column with varying widths."""

    rows: list[models._VisualRow] = []
    lines: list[models._LineItem] = []
    source_index = 0
    for row_index, top in enumerate((10.0, 30.0, 50.0)):
        fragments: list[models._Fragment] = []
        if centered_columns:
            half_width = (2.0, 8.0, 14.0)[row_index]
            column_bounds = (
                (30.0 - half_width, 30.0 + half_width),
                (90.0 - half_width, 90.0 + half_width),
            )
        else:
            column_bounds = ((10.0, 20.0), (50.0, 60.0), (90.0, 100.0))
        for column_index, (left, right) in enumerate(column_bounds):
            bbox = (left, top, right, top + 5.0)
            text = f"r{row_index}c{column_index}"
            fragments.append(
                models._Fragment(
                    text=text,
                    bbox=bbox,
                    local_bbox=bbox,
                    line_index=source_index,
                    visual_row_id=row_index,
                )
            )
            lines.append(
                models._LineItem(
                    text=text,
                    bbox=bbox,
                    angle=0,
                    source_index=source_index,
                    effective_height=5.0,
                    visual_row_id=row_index,
                )
            )
            source_index += 1
        rows.append(
            models._VisualRow(
                fragments=fragments,
                center_y=top + 2.5,
                bbox=(10.0, top, 100.0, top + 5.0),
                visual_row_id=row_index,
            )
        )
    rule_tops = (8.0, 58.0) if rule_count == 2 else (8.0, 33.0, 58.0)
    axis_lines = [_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in rule_tops]
    return rows, lines, axis_lines


def _compact_fully_ruled_table_fixture() -> tuple[
    list[models._VisualRow],
    list[models._LineItem],
    list[models._LocalAxisLine],
]:
    """Construct a compact fully enclosed grid with two rows and four columns, three horizontal and five vertical."""

    rows: list[models._VisualRow] = []
    lines: list[models._LineItem] = []
    column_bounds = (
        (5.0, 20.0),
        (32.0, 48.0),
        (60.0, 75.0),
        (87.0, 105.0),
    )
    source_index = 0
    for row_index, top in enumerate((10.0, 22.0)):
        fragments: list[models._Fragment] = []
        for column_index, (left, right) in enumerate(column_bounds):
            bbox = (left, top, right, top + 5.0)
            text = f"cell-{row_index}-{column_index}"
            fragments.append(
                models._Fragment(
                    text=text,
                    bbox=bbox,
                    local_bbox=bbox,
                    line_index=source_index,
                    visual_row_id=row_index,
                )
            )
            lines.append(
                models._LineItem(
                    text=text,
                    bbox=bbox,
                    angle=0,
                    source_index=source_index,
                    effective_height=5.0,
                    visual_row_id=row_index,
                )
            )
            source_index += 1
        rows.append(
            models._VisualRow(
                fragments=fragments,
                center_y=top + 2.5,
                bbox=geometry._bbox_union_many([fragment.bbox for fragment in fragments]),
                visual_row_id=row_index,
            )
        )

    axis_lines = [
        *[_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in (8.0, 20.0, 32.0)],
        *[_axis_line("vertical", (left, 8.1, left + 0.1, 32.0)) for left in (0.0, 27.5, 55.0, 82.5, 109.9)],
    ]
    return rows, lines, axis_lines


def test_rule_table_candidate_accepts_captionless_regular_text_distribution() -> None:
    """Verify that three horizontal lines and continuous stable columns are sufficient to identify tables without explicit headers."""

    rows, lines, axis_lines = _rule_table_fixture()

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == set(range(9))


@pytest.mark.parametrize("centered", (False, True))
def test_deferred_rule_candidates_match_eager_merge(monkeypatch, centered):
    """Confirm that there are no materialization candidates before sorting, and the final merged boxes, members, scores, and annotations are exactly the same."""
    import pickle

    rows, lines, rules = _rule_table_fixture(centered_columns=centered)
    before = pickle.dumps((rows, lines, rules))
    args = (rows, lines, (150.0, 100.0), 0, 5.0, rules)
    expected = table_rules._merge_table_candidates(table_rules._build_rule_table_candidates(*args))
    original = table_rules._expand_rule_table_candidate
    calls = []

    def materialize(*args, **kwargs):
        """Only the real candidate materialization timing is recorded, and the construction parameters are not modified."""
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(table_rules, "_expand_rule_table_candidate", materialize)
    drafts = table_rules._build_rule_table_candidates(*args, defer_materialization=True)
    assert drafts and not calls
    assert table_rules._merge_table_candidates(drafts) == expected
    assert len(calls) == len(drafts)
    assert pickle.dumps((rows, lines, rules)) == before


def test_deferred_rule_candidates_share_frozen_grid_components(monkeypatch):
    """Confirm that delayed candidate frozen grid components can be multiplexed by closed grid detection without re-scanning."""
    rows, lines, axis_lines = _rule_table_fixture()
    original = table_rules._connected_rule_grid_components
    calls = []

    def counted(*args, **kwargs):
        """It only counts the number of reconstructions of connected components and does not change any geometric results."""
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(table_rules, "_connected_rule_grid_components", counted)
    drafts = table_rules._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        defer_materialization=True,
    )
    assert drafts
    assert len(calls) == 1
    shared = drafts[0].context.grid_components
    assert shared is not None
    closed = table_rules._build_closed_rule_grid_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        [],
        None,
        grid_components=shared,
    )
    assert len(calls) == 1
    assert closed == table_rules._build_closed_rule_grid_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
        [],
        None,
    )


def test_rule_table_candidate_accepts_center_aligned_columns_with_varying_widths() -> None:
    """Verify that a two-column table with changing left and right boundaries but stable center can still form a candidate."""

    rows, lines, axis_lines = _rule_table_fixture(centered_columns=True)

    assert tables._count_stable_columns(rows, 5.0) == (2, 1.0)
    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == set(range(6))


def test_compact_fully_ruled_two_row_table_is_accepted() -> None:
    """Verify that the two-row table can pass strict candidate admission when three horizontal and five vertical lines form a complete grid."""

    rows, lines, axis_lines = _compact_fully_ruled_table_fixture()

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == set(range(8))
    assert candidates[0].score == 8.0


def test_caption_anchored_two_row_three_line_table_is_accepted() -> None:
    """Verification of strong table questions can be combined with three horizontal lines, two rows and multiple columns to confirm the table without vertical lines."""

    rows, lines, axis_lines = _compact_fully_ruled_table_fixture()
    axis_lines = [line for line in axis_lines if line.orientation == "horizontal"]
    lines.append(
        models._LineItem(
            text="Table 1 Results",
            bbox=(20.0, 0.0, 90.0, 6.0),
            angle=0,
            source_index=100,
            effective_height=5.0,
        )
    )
    rows.append(
        models._VisualRow(
            fragments=[
                models._Fragment(
                    text="Table 1 Results",
                    bbox=(20.0, 0.0, 90.0, 6.0),
                    local_bbox=(20.0, 0.0, 90.0, 6.0),
                    line_index=100,
                    visual_row_id=100,
                )
            ],
            center_y=3.0,
            bbox=(20.0, 0.0, 90.0, 6.0),
            visual_row_id=100,
        )
    )
    rows.sort(key=lambda row: row.center_y)

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 200.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == set(range(8))
    assert [annotation.kind for annotation in candidates[0].annotations] == ["caption"]


def test_compact_grid_deduplicates_repeated_vertical_paths() -> None:
    """Verify that repeating a vertical line path at the same location does not expand the physical column count and score of a compact table."""

    rows, lines, axis_lines = _compact_fully_ruled_table_fixture()
    axis_lines.extend(_axis_line("vertical", (left + 0.3, 8.1, left + 0.4, 32.0)) for left in (0.0, 27.5, 55.0, 82.5, 109.9))

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].score == 8.0


@pytest.mark.parametrize(
    "failure_mode",
    [
        "horizontal_only",
        "short_verticals",
        "missing_outer",
        "same_cell",
        "center_on_boundary",
    ],
)
def test_compact_two_row_layout_requires_complete_grid(
    failure_mode: str,
) -> None:
    """Verify that two lines of text remain non-table when missing a full vertical grid or unique cell mapping."""

    rows, lines, axis_lines = _compact_fully_ruled_table_fixture()
    if failure_mode == "horizontal_only":
        axis_lines = [line for line in axis_lines if line.orientation == "horizontal"]
    elif failure_mode == "short_verticals":
        axis_lines = [
            _axis_line("vertical", (line.bbox[0], 13.0, line.bbox[2], 27.0)) if line.orientation == "vertical" else line
            for line in axis_lines
        ]
    elif failure_mode == "missing_outer":
        axis_lines = [line for line in axis_lines if line.orientation != "vertical" or line.bbox[0] > 1.0]
    elif failure_mode == "same_cell":
        rows[0].fragments[1].bbox = (8.0, 10.0, 18.0, 15.0)
        rows[0].fragments[1].local_bbox = rows[0].fragments[1].bbox
    else:
        rows[0].fragments[1].bbox = (26.5, 10.0, 28.5, 15.0)
        rows[0].fragments[1].local_bbox = rows[0].fragments[1].bbox

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert candidates == []


def test_compact_admission_does_not_accept_two_row_column_prose() -> None:
    """Verify that two lines of normal two-column text does not lower the barrier to entry due to compact table branches."""

    rows, lines, axis_lines = _compact_fully_ruled_table_fixture()
    for row in rows:
        row.fragments = [row.fragments[0], row.fragments[-1]]
        row.bbox = geometry._bbox_union_many([fragment.bbox for fragment in row.fragments])
    retained_indices = {fragment.line_index for row in rows for fragment in row.fragments}
    lines = [line for line in lines if line.source_index in retained_indices]
    axis_lines = [line for line in axis_lines if line.orientation == "horizontal"]

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert candidates == []


def test_two_horizontal_rule_grid_is_accepted_by_spatial_distribution() -> None:
    """Verify that two long horizontal lines combined with dense stable columns form a table candidate."""

    rows, lines, axis_lines = _rule_table_fixture(rule_count=2)
    axis_lines.extend(
        [
            _axis_line("vertical", (0.0, 8.0, 0.1, 58.0)),
            _axis_line("vertical", (55.0, 8.0, 55.1, 58.0)),
            _axis_line("vertical", (109.9, 8.0, 110.0, 58.0)),
        ]
    )

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].line_indices == set(range(9))


def test_duplicate_horizontal_paths_count_as_one_boundary() -> None:
    """Verify that duplicate PDF path at the same y location counts as only one boundary."""

    axis_lines = [
        _axis_line("horizontal", (0.0, 10.0, 100.0, 10.1)),
        _axis_line("horizontal", (0.0, 10.5, 100.0, 10.6)),
        _axis_line("horizontal", (0.0, 30.0, 100.0, 30.1)),
    ]

    groups = tables._group_long_horizontal_rules(axis_lines, 5.0)

    assert len(groups) == 1
    assert len(groups[0]) == 2
    assert [line.bbox[1] for line in groups[0]] == [10.0, 30.0]
    assert tables._group_long_horizontal_rules(axis_lines[:2], 5.0) == []


def test_rule_spans_keep_historical_exhaustive_order() -> None:
    """Verify that horizontal line candidates retain the legacy first/bottom enumeration order intact and do not delete internal subranges."""

    assert list(table_rules._iter_rule_spans(5)) == [
        (0, 1),
        (0, 2),
        (0, 3),
        (0, 4),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 3),
        (2, 4),
        (3, 4),
    ]


def test_shared_line_index_set_matches_builtin_set_mutations() -> None:
    """Verify that the shared basis set is exactly the same as the ordinary set when adding, deleting, and merging candidates with the same basis."""

    base = frozenset({1, 2, 3})
    shared = models._SharedLineIndexSet(base, {4})
    expected = {1, 2, 3, 4}

    shared.difference_update({2, 4})
    expected.difference_update({2, 4})
    shared.add(4)
    expected.add(4)
    shared.discard(1)
    expected.discard(1)
    assert set(shared) == expected
    assert len(shared) == len(expected)

    other = models._SharedLineIndexSet(base, {5})
    other.difference_update({3})
    expected_other = ({1, 2, 3} | {5}) - {3}
    shared.update(other)
    expected.update(expected_other)

    assert set(shared) == expected
    assert shared == expected


def test_shared_line_index_set_exact_conversion_preserves_plain_target_union() -> None:
    """Verify that after ordinary candidates are transferred to the shared base, the merge result will not incorrectly replace the deleted base members of the candidates."""

    base = frozenset({1, 2, 3})
    shared = models._SharedLineIndexSet(base, {5})
    shared.discard(2)
    plain = models._TableCandidate(
        bbox=(0.0, 0.0, 10.0, 10.0),
        local_bbox=(0.0, 0.0, 10.0, 10.0),
        angle=0,
        score=10.0,
        line_indices={1, 4},
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 0.0, 10.0, 10.0),
        local_bbox=(0.0, 0.0, 10.0, 10.0),
        angle=0,
        score=1.0,
        line_indices=shared,
    )

    merged = table_rules._merge_table_candidates([plain, candidate])

    assert len(merged) == 1
    assert isinstance(merged[0].line_indices, models._SharedLineIndexSet)
    assert set(merged[0].line_indices) == {1, 3, 4, 5}


def test_rule_interval_partition_matches_historical_closed_interval_scan() -> None:
    """Verify that primary bucketing is completely consistent with the old version of closed interval segment-by-segment scanning, and the boundary rows belong to two adjacent segments at the same time."""

    rules = [_axis_line("horizontal", (0.0, y, 100.0, y + 0.1)) for y in (10.0, 20.0, 30.0)]
    rows = [
        models._VisualRow(
            fragments=[
                models._Fragment(
                    str(index),
                    (10.0, center_y - 1.0, 20.0, center_y + 1.0),
                    (10.0, center_y - 1.0, 20.0, center_y + 1.0),
                    index,
                    index,
                )
            ],
            center_y=center_y,
            bbox=(10.0, center_y - 1.0, 20.0, center_y + 1.0),
            visual_row_id=index,
        )
        for index, center_y in enumerate((10.05, 15.0, 20.05, 25.0, 30.05))
    ]

    actual = table_rules._partition_rows_by_rule_intervals(rows, rules)
    expected = [
        [
            row
            for row in rows
            if geometry._bbox_center_y(top_rule.bbox) <= row.center_y <= geometry._bbox_center_y(bottom_rule.bbox)
        ]
        for top_rule, bottom_rule in zip(rules, rules[1:])
    ]

    assert actual == expected
    assert rows[2] in actual[0]
    assert rows[2] in actual[1]


def test_rule_corridor_cache_matches_uncached_interval_projection() -> None:
    """Verify that the exact corridor cache only reuses the horizontal cropping and does not change the vertical admission and output visual lines."""

    rows, _lines, _axis_lines = _rule_table_fixture()
    cache: dict[tuple[float, float], list[table_rules._RuleCorridorRow]] = {}
    row_index = table_rules._build_row_interval_index(rows)
    intervals = [
        (0.0, 8.0, 110.0, 20.1),
        (0.0, 8.0, 110.0, 32.1),
        (0.0, 20.0, 110.0, 32.1),
    ]

    for rule_bbox in intervals:
        expected = table_rules._rows_inside_rule_interval(
            rows,
            rule_bbox,
            [],
            row_index,
        )
        actual = table_rules._rows_inside_rule_interval(
            rows,
            rule_bbox,
            [],
            corridor_cache=cache,
        )
        assert actual == expected

    assert len(cache) == 1


def test_stable_column_prefix_reuse_matches_full_recomputation() -> None:
    """Verify that strict prefix continuation is fully consistent with de novo clustering at each growth stage."""

    rows, _lines, _axis_lines = _rule_table_fixture()
    cache = table_rules._StableColumnCache()

    for end_index in range(1, len(rows) + 1):
        current_rows = rows[:end_index]
        expected = table_rules._count_stable_columns(current_rows, 5.0)
        actual = table_rules._count_stable_columns(
            current_rows,
            5.0,
            cache,
            allow_prefix_reuse=True,
        )
        assert actual == expected

    assert len(cache.prefixes) == 1
    assert next(iter(cache.prefixes.values())).row_ids == tuple(id(row) for row in rows)


def test_nearest_rule_pair_excludes_header_line_from_table_bbox() -> None:
    """When verifying that there are no multi-cell rows between the header horizontal line and the upper boundary of the table, the table is not expanded bbox."""

    rows, lines, axis_lines = _rule_table_fixture(rule_count=2)
    axis_lines.insert(0, _axis_line("horizontal", (0.0, 0.0, 110.0, 0.1)))

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 100.0),
        0,
        5.0,
        axis_lines,
    )

    assert len(candidates) == 1
    assert candidates[0].core_bbox is not None
    assert candidates[0].core_bbox[1] == pytest.approx(8.0)


def test_chart_tick_rows_fail_dense_multi_cell_distribution() -> None:
    """Verify that multiple chart scale rows cannot impersonate regular tables due to vertical discontinuity."""

    rows: list[models._VisualRow] = []
    lines: list[models._LineItem] = []
    source_index = 0
    for row_index, (top, anchors) in enumerate(
        (
            (10.0, (10.0, 30.0, 50.0, 70.0, 90.0)),
            (20.0, (50.0,)),
            (30.0, (45.0,)),
            (75.0, (10.0, 30.0, 50.0, 70.0, 90.0)),
            (85.0, (50.0,)),
            (95.0, (45.0,)),
        )
    ):
        fragments: list[models._Fragment] = []
        for anchor in anchors:
            bbox = (anchor, top, anchor + 5.0, top + 5.0)
            text = f"tick-{source_index}"
            fragments.append(
                models._Fragment(
                    text=text,
                    bbox=bbox,
                    local_bbox=bbox,
                    line_index=source_index,
                    visual_row_id=row_index,
                )
            )
            lines.append(
                models._LineItem(
                    text=text,
                    bbox=bbox,
                    angle=0,
                    source_index=source_index,
                    effective_height=5.0,
                    visual_row_id=row_index,
                )
            )
            source_index += 1
        rows.append(
            models._VisualRow(
                fragments=fragments,
                center_y=top + 2.5,
                bbox=geometry._bbox_union_many([fragment.bbox for fragment in fragments]),
                visual_row_id=row_index,
            )
        )
    axis_lines = [_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in (0.0, 55.0, 110.0)]

    candidates = tables._build_rule_table_candidates(
        rows,
        lines,
        (150.0, 120.0),
        0,
        5.0,
        axis_lines,
    )

    assert candidates == []


def test_long_sparse_rule_interval_cannot_bridge_two_low_column_tables() -> None:
    """Verify that low-column tables cannot be merged across long ranges when there is only one row of sparse text between them."""

    rows: list[models._VisualRow] = []
    source_index = 0
    for row_index, (top, anchors) in enumerate(
        (
            (10.0, (10.0, 50.0, 90.0)),
            (20.0, (10.0, 50.0, 90.0)),
            (30.0, (10.0, 50.0, 90.0)),
            (80.0, (10.0, 90.0)),
            (130.0, (10.0, 50.0, 90.0)),
            (140.0, (10.0, 50.0, 90.0)),
        )
    ):
        fragments = []
        for anchor in anchors:
            bbox = (anchor, top, anchor + 5.0, top + 5.0)
            fragments.append(
                models._Fragment(
                    text=f"cell-{source_index}",
                    bbox=bbox,
                    local_bbox=bbox,
                    line_index=source_index,
                    visual_row_id=row_index,
                )
            )
            source_index += 1
        rows.append(
            models._VisualRow(
                fragments=fragments,
                center_y=top + 2.5,
                bbox=geometry._bbox_union_many([fragment.bbox for fragment in fragments]),
                visual_row_id=row_index,
            )
        )
    rules = [_axis_line("horizontal", (0.0, top, 110.0, top + 0.1)) for top in (0.0, 40.0, 120.0, 150.0)]

    assert not tables._rule_intervals_are_column_compatible(rows, rules, 5.0)


def test_split_table_footnote_marker_is_joined_before_matching() -> None:
    """Verify that the rotated table of unpacked For with asterisk footnotes can be recognized after visual row splicing."""

    row = models._VisualRow(
        fragments=[
            models._Fragment("For", (0.0, 0.0, 10.0, 5.0), (0.0, 0.0, 10.0, 5.0), 0),
            models._Fragment("*rainfall", (12.0, 0.0, 35.0, 5.0), (12.0, 0.0, 35.0, 5.0), 1),
        ],
        center_y=2.5,
        bbox=(0.0, 0.0, 35.0, 5.0),
    )

    assert tables._is_table_note_text(tables._visual_row_text(row))
    assert not tables._is_table_note_text("1 Numeric table footnote")


def test_table_candidate_merge_keeps_body_and_annotation_roles_disjoint() -> None:
    """Verify table body identity takes precedence when merging duplicate candidates, and tighten caption boundaries according to line-by-line boxes."""

    caption_bbox = (10.0, 10.0, 50.0, 20.0)
    header_bbox = (10.0, 25.0, 90.0, 35.0)
    primary = models._TableCandidate(
        bbox=(0.0, 0.0, 100.0, 80.0),
        local_bbox=(0.0, 0.0, 100.0, 80.0),
        angle=0,
        score=2.0,
        core_bbox=(0.0, 25.0, 100.0, 80.0),
        line_indices={2},
        annotations=[
            models._TableAnnotation(
                kind="caption",
                bbox=geometry._bbox_union(caption_bbox, header_bbox),
                line_indices={0, 1},
                line_bboxes={0: caption_bbox, 1: header_bbox},
            )
        ],
    )
    secondary = models._TableCandidate(
        bbox=(0.0, 0.0, 100.0, 80.0),
        local_bbox=(0.0, 0.0, 100.0, 80.0),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 25.0, 100.0, 80.0),
        line_indices={1, 2},
        annotations=[
            models._TableAnnotation(
                kind="caption",
                bbox=caption_bbox,
                line_indices={0},
                line_bboxes={0: caption_bbox},
            )
        ],
    )

    merged = tables._merge_table_candidates([primary, secondary])

    assert len(merged) == 1
    assert merged[0].line_indices == {1, 2}
    assert len(merged[0].annotations) == 1
    assert merged[0].annotations[0].line_indices == {0}
    assert merged[0].annotations[0].bbox == caption_bbox


def test_materialize_table_externalizes_multiline_annotations_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification split number caption, multi-line numeric footnotes and table bodies are each materialized and claimed once per source line."""

    line_specs = (
        ("Table", (10.0, 10.0, 35.0, 20.0), 0),
        ("2: Metrics", (38.0, 10.0, 80.0, 20.0), 1),
        ("Header Body", (10.0, 30.0, 90.0, 60.0), 2),
        ("1 Numeric note", (10.0, 70.0, 70.0, 80.0), 3),
        ("* Symbol note", (10.0, 80.0, 70.0, 90.0), 4),
    )
    lines = [
        models._LineItem(
            text=text,
            bbox=bbox,
            angle=0,
            source_index=source_index,
            effective_height=10.0,
            font_signature=("Test", 0),
            font_coverage=1.0,
        )
        for text, bbox, source_index in line_specs
    ]
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(10.0, 10.0, 90.0, 90.0),
        local_bbox=(10.0, 10.0, 90.0, 90.0),
        angle=0,
        score=1.0,
        core_bbox=(10.0, 30.0, 90.0, 60.0),
        line_indices={2},
        annotations=[
            models._TableAnnotation(
                kind="caption",
                bbox=(10.0, 10.0, 80.0, 20.0),
                line_indices={0, 1},
            ),
            models._TableAnnotation(
                kind="footnote",
                bbox=(10.0, 70.0, 70.0, 90.0),
                line_indices={3, 4},
            ),
        ],
    )
    projection = MagicMock(return_value="Header Body")
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    table_blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert table_blocks == [
        {
            "type": "table",
            "bbox": (10.0, 30.0, 90.0, 60.0),
            "angle": 0,
            "content": "Header Body",
        }
    ]
    assert [block["type"] for block in annotation_blocks] == [
        "caption",
        "footnote",
    ]
    assert [block["content"] for block in annotation_blocks] == [
        "Table 2: Metrics",
        "1 Numeric note * Symbol note",
    ]
    assert all(
        block["_table_annotation_complete"] is True
        and block["_font_signatures"] == {("Test", 0)}
        and len(block["_line_heights"]) == 2
        for block in annotation_blocks
    )
    assert projection.call_args.args[0] is source.chars
    assert projection.call_args.args[1] == (10.0, 30.0, 90.0, 60.0)
    assert claimed == {0, 1, 2, 3, 4}


def test_table_annotation_splits_short_tail_before_wide_font_family_reset() -> None:
    """Verify that the wideline font family restarts after the short tail of the table to form two independent comment blocks."""

    lines = [
        models._LineItem(
            "first body",
            (10.0, 70.0, 90.0, 80.0),
            0,
            0,
            effective_height=10.0,
            em_height=10.0,
            font_signature=("FirstFont", 0),
            font_coverage=1.0,
        ),
        models._LineItem(
            "tail",
            (10.0, 82.0, 30.0, 92.0),
            0,
            1,
            effective_height=10.0,
            em_height=10.0,
            font_signature=("FirstFont", 0),
            font_coverage=1.0,
        ),
        models._LineItem(
            "second body",
            (12.0, 94.0, 88.0, 104.0),
            0,
            2,
            effective_height=10.0,
            em_height=10.0,
            font_signature=("SecondFont", 0),
            font_coverage=1.0,
        ),
    ]
    source = models._PageSource(
        page_size=(120.0, 120.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(5.0, 10.0, 110.0, 110.0),
        local_bbox=(5.0, 10.0, 110.0, 110.0),
        angle=0,
        score=1.0,
        core_bbox=(5.0, 10.0, 110.0, 60.0),
        line_indices=set(),
    )
    annotation = models._TableAnnotation(
        kind="footnote",
        bbox=(10.0, 70.0, 90.0, 104.0),
        line_indices={0, 1, 2},
        line_bboxes={line.source_index: line.bbox for line in lines},
    )

    blocks = tables._build_table_annotation_blocks(
        source,
        candidate,
        annotation,
    )
    lines[2].font_signature = ("FirstFont", 0)
    control = tables._build_table_annotation_blocks(
        source,
        candidate,
        annotation,
    )

    assert [block["content"] for block in blocks] == [
        "first body tail",
        "second body",
    ]
    assert [block["type"] for block in blocks] == [
        "footnote",
        "footnote",
    ]
    assert [block["content"] for block in control] == [
        "first body tail second body",
    ]


def test_invalid_table_annotation_falls_back_to_full_table_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the comment lines that cannot form content remain in the table body projection, and the table frame and claims do not shrink."""

    lines = [
        models._LineItem("body", (10.0, 30.0, 90.0, 60.0), 0, 0),
        models._LineItem("", (10.0, 10.0, 90.0, 20.0), 0, 1),
    ]
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(10.0, 10.0, 90.0, 60.0),
        local_bbox=(10.0, 10.0, 90.0, 60.0),
        angle=0,
        score=1.0,
        core_bbox=(10.0, 30.0, 90.0, 60.0),
        line_indices={0},
        annotations=[
            models._TableAnnotation(
                kind="caption",
                bbox=(10.0, 10.0, 90.0, 20.0),
                line_indices={1},
            )
        ],
    )
    projection = MagicMock(return_value="body")
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    table_blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert annotation_blocks == []
    assert table_blocks[0]["bbox"] == candidate.bbox
    assert projection.call_args.args[0] is source.chars
    assert projection.call_args.args[1] == candidate.bbox
    assert claimed == {0, 1}


def _table_note_reference_fixture(
    marker: str,
    reference_mode: str,
    *,
    angle: int = 0,
    note_height: float = 8.0,
) -> tuple[
    list[models._VisualRow],
    list[models._LineItem],
    tuple[float, float, float, float],
    set[int],
    tuple[float, float],
]:
    """Construct a local coordinate fixture with neutral in-table references, table note first row, and text height samples."""

    page_size = (200.0, 200.0)
    rule_bbox = (0.0, 20.0, 100.0, 50.0)
    core_local_bbox = (10.0, 25.0, 70.0, 35.0)
    core_chars: list[dict[str, object]] = []
    if reference_mode == "superscript":
        core_text = f"cell{marker}"
        x_position = 10.0
        for raw_char in "cell":
            local_bbox = (x_position, 25.0, x_position + 7.0, 35.0)
            core_chars.append(
                {
                    "char": raw_char,
                    "bbox": geometry._rotate_bbox_from_upright(local_bbox, page_size, angle),
                }
            )
            x_position += 8.0
        for raw_char in marker:
            local_bbox = (x_position, 22.0, x_position + 5.0, 28.0)
            core_chars.append(
                {
                    "char": raw_char,
                    "bbox": geometry._rotate_bbox_from_upright(local_bbox, page_size, angle),
                }
            )
            x_position += 5.5
    elif reference_mode == "compact":
        core_text = marker
    else:
        core_text = "neutral value"

    core_line = models._LineItem(
        text=core_text,
        bbox=geometry._rotate_bbox_from_upright(core_local_bbox, page_size, angle),
        angle=angle,
        source_index=0,
        chars=core_chars,  # type: ignore[arg-type]
        effective_height=10.0,
        font_signature=("Table", 0),
        font_coverage=1.0,
    )
    note_local_bbox = (0.0, 51.0, 80.0, 51.0 + note_height)
    note_line = models._LineItem(
        text=f"{marker} neutral explanation",
        bbox=geometry._rotate_bbox_from_upright(note_local_bbox, page_size, angle),
        angle=angle,
        source_index=1,
        effective_height=note_height,
        font_signature=("Note", 0),
        font_coverage=1.0,
    )
    body_lines = [
        models._LineItem(
            text=f"body-{source_index}",
            bbox=geometry._rotate_bbox_from_upright(
                (110.0, top, 190.0, top + 10.0),
                page_size,
                angle,
            ),
            angle=angle,
            source_index=source_index,
            effective_height=10.0,
            font_signature=("Body", 0),
            font_coverage=1.0,
        )
        for source_index, top in enumerate((140.0, 152.0, 164.0, 176.0), start=2)
    ]
    rows = [
        models._VisualRow(
            fragments=[models._Fragment(core_text, core_local_bbox, core_local_bbox, 0, 0)],
            center_y=geometry._bbox_center_y(core_local_bbox),
            bbox=core_local_bbox,
            visual_row_id=0,
        ),
        models._VisualRow(
            fragments=[models._Fragment(note_line.text, note_local_bbox, note_local_bbox, 1, 1)],
            center_y=geometry._bbox_center_y(note_local_bbox),
            bbox=note_local_bbox,
            visual_row_id=1,
        ),
    ]
    return rows, [core_line, note_line, *body_lines], rule_bbox, {0}, page_size


@pytest.mark.parametrize(
    ("text", "expected"),
    (("7 neutral", "7"), ("(q) neutral", "q"), ("xy: neutral", "xy")),
)
def test_auxiliary_table_note_marker_is_unicode_generic(text: str, expected: str) -> None:
    """Verification auxiliary tags are only subject to the general Unicode morphology, and specific character changes do not affect extraction."""

    assert tables._extract_auxiliary_table_note_marker(text) == expected


@pytest.mark.parametrize(
    ("marker", "reference_mode"),
    (("7", "superscript"), ("q", "compact"), ("xy", "compact")),
)
def test_auxiliary_table_note_requires_neutral_core_reference(
    marker: str,
    reference_mode: str,
) -> None:
    """Verification that neutral markers are confirmed by superscript or compact cells can initiate the table annotation chain."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        marker,
        reference_mode,
    )

    selected = tables._collect_footnote_rows(
        rows,
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        0,
    )

    assert [tables._visual_row_text(row) for row in selected] == [f"{marker} neutral explanation"]


def test_prepared_table_note_context_matches_direct_calculation() -> None:
    """Verify that the precalculation of corridor rows and text heights and the successive calculations yield exactly the same table annotation results."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "q",
        "compact",
    )
    prepared_rows = table_annotations._prepare_table_note_rows(
        rows,
        lines,
        rule_bbox,
        8.0,
        page_size,
        0,
    )
    prepared_body_metrics = table_annotations._prepare_table_note_body_metrics(
        lines,
        page_size,
        0,
    )

    direct_height = table_annotations._table_note_body_reference_height(
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        0,
    )
    prepared_height = table_annotations._table_note_body_reference_height(
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        0,
        prepared_body_metrics,
    )
    direct_rows = tables._collect_footnote_rows(
        rows,
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        0,
    )
    prepared_result = tables._collect_footnote_rows(
        rows,
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        0,
        prepared_rows=prepared_rows,
        prepared_body_metrics=prepared_body_metrics,
    )

    assert prepared_height == direct_height
    assert prepared_result == direct_rows


def test_auxiliary_table_note_rejects_marker_without_core_reference() -> None:
    """Validates that short markup text immediately adjacent to a table cannot start a table annotation chain when an in-table reference is missing."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "7",
        "none",
    )

    assert (
        tables._collect_footnote_rows(
            rows,
            lines,
            rule_bbox,
            8.0,
            core_indices,
            page_size,
            0,
        )
        == []
    )


def test_superscript_reference_requires_smaller_raised_glyph() -> None:
    """Verify that the same character on the common baseline cannot be referenced as an intra-table superscript."""

    _rows, lines, _rule_bbox, _core_indices, page_size = _table_note_reference_fixture(
        "7",
        "superscript",
    )
    core_line = lines[0]
    for char in core_line.chars:
        if str(char.get("char")) == "7":
            char["bbox"] = (42.0, 25.0, 49.0, 35.0)

    assert not tables._line_has_superscript_marker(core_line, "7", page_size, 0)


@pytest.mark.parametrize("note_height", [10.0, 14.0])
def test_auxiliary_table_note_rejects_body_or_title_sized_first_row(
    note_height: float,
) -> None:
    """Verifying that a normal body or title line with an in-table reference still does not start the table note chain."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "q",
        "compact",
        note_height=note_height,
    )

    assert (
        tables._collect_footnote_rows(
            rows,
            lines,
            rule_bbox,
            8.0,
            core_indices,
            page_size,
            0,
        )
        == []
    )


def test_auxiliary_table_note_rejects_loose_first_gap() -> None:
    """Verify that the expansion of the first line of the auxiliary mark exceeds three-quarters of the local line height."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "q",
        "compact",
    )
    late_bbox = (0.0, 57.0, 80.0, 65.0)
    lines[1].bbox = late_bbox
    rows[1] = models._VisualRow(
        fragments=[models._Fragment(lines[1].text, late_bbox, late_bbox, 1, 1)],
        center_y=geometry._bbox_center_y(late_bbox),
        bbox=late_bbox,
        visual_row_id=1,
    )

    assert (
        tables._collect_footnote_rows(
            rows,
            lines,
            rule_bbox,
            8.0,
            core_indices,
            page_size,
            0,
        )
        == []
    )


def test_auxiliary_table_note_requires_smaller_than_body_reference() -> None:
    """The first line of verification auxiliary marks must also be significantly smaller than the reference height of the text in the same direction."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "q",
        "compact",
    )
    for line in lines[2:]:
        line.effective_height = 8.5

    assert (
        tables._collect_footnote_rows(
            rows,
            lines,
            rule_bbox,
            8.0,
            core_indices,
            page_size,
            0,
        )
        == []
    )


def test_table_note_precheck_uses_clipped_corridor_projection() -> None:
    """Verify that another column of normal text does not obscure the real Note start row in the table corridor."""

    outside_bbox = (140.0, 51.0, 190.0, 59.0)
    inside_bbox = (10.0, 51.0, 70.0, 59.0)
    row = models._VisualRow(
        fragments=[
            models._Fragment("ordinary prose", outside_bbox, outside_bbox, 1, 1),
            models._Fragment("Note: values are adjusted", inside_bbox, inside_bbox, 2, 1),
        ],
        center_y=55.0,
        bbox=geometry._bbox_union(outside_bbox, inside_bbox),
        visual_row_id=1,
    )

    assert table_annotations._has_possible_table_note_rows([row]) is False
    assert (
        table_annotations._has_possible_table_note_rows_in_corridor(
            [row],
            (0.0, 40.0, 100.0, 60.0),
            8.0,
        )
        is True
    )


def test_auxiliary_table_note_uses_clipped_corridor_projection() -> None:
    """Verify that the short mark in another column cannot use fragments in the table column to create false evidence of the projection of the entire row."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "q",
        "compact",
    )
    outside_bbox = (140.0, 51.0, 190.0, 59.0)
    inside_bbox = (10.0, 51.0, 70.0, 59.0)
    rows[1] = models._VisualRow(
        fragments=[
            models._Fragment("q outside", outside_bbox, outside_bbox, 1, 1),
            models._Fragment("inside continuation", inside_bbox, inside_bbox, 6, 1),
        ],
        center_y=55.0,
        bbox=geometry._bbox_union(outside_bbox, inside_bbox),
        visual_row_id=1,
    )
    lines.append(
        models._LineItem(
            text="inside continuation",
            bbox=inside_bbox,
            angle=0,
            source_index=6,
            effective_height=8.0,
            font_signature=("Note", 0),
            font_coverage=1.0,
        )
    )

    assert (
        tables._collect_footnote_rows(
            rows,
            lines,
            rule_bbox,
            8.0,
            core_indices,
            page_size,
            0,
        )
        == []
    )


def test_rotated_auxiliary_table_note_uses_local_superscript_geometry() -> None:
    """Verify that superscript references can still be confirmed after rotating the table first into local forward coordinates."""

    rows, lines, rule_bbox, core_indices, page_size = _table_note_reference_fixture(
        "7",
        "superscript",
        angle=90,
    )

    selected = tables._collect_footnote_rows(
        rows,
        lines,
        rule_bbox,
        8.0,
        core_indices,
        page_size,
        90,
    )

    assert [tables._visual_row_text(row) for row in selected] == ["7 neutral explanation"]


def test_table_note_chain_cannot_expand_beyond_ten_line_heights() -> None:
    """Verify that table and annotation continuation links cannot expand infinitely toward the bottom of the page even if the font and line spacing are stable."""

    lines: list[models._LineItem] = []
    rows: list[models._VisualRow] = []
    specs = [("Note: neutral", (0.0, 51.0, 80.0, 59.0))]
    specs.extend((f"continuation-{index}", (0.0, 59.5 + 8.5 * index, 80.0, 67.5 + 8.5 * index)) for index in range(12))
    for source_index, (text, bbox) in enumerate(specs):
        lines.append(
            models._LineItem(
                text=text,
                bbox=bbox,
                angle=0,
                source_index=source_index,
                effective_height=8.0,
                font_signature=("Note", 0),
                font_coverage=1.0,
            )
        )
        rows.append(
            models._VisualRow(
                fragments=[models._Fragment(text, bbox, bbox, source_index, source_index)],
                center_y=geometry._bbox_center_y(bbox),
                bbox=bbox,
                visual_row_id=source_index,
            )
        )

    selected = tables._collect_footnote_rows(
        rows,
        lines,
        (0.0, 0.0, 100.0, 50.0),
        8.0,
        set(),
        (200.0, 200.0),
        0,
    )

    assert selected
    assert len(selected) < len(rows)
    assert selected[-1].bbox[3] <= 130.0


def test_table_note_chain_stops_at_font_and_size_transition() -> None:
    """Verify that the continuation line of a table note cannot span the title or subsequent body text where the font size changes."""

    specs = [
        ("Note: neutral marker", (0.0, 52.0, 60.0, 60.0), ("Note", 0), 8.0),
        ("neutral continuation", (0.0, 61.0, 65.0, 69.0), ("Note", 0), 8.0),
        ("section barrier", (0.0, 70.0, 45.0, 84.0), ("Heading", 0), 14.0),
        ("ordinary body", (10.0, 85.0, 100.0, 95.0), ("Body", 0), 10.0),
    ]
    lines: list[models._LineItem] = []
    rows: list[models._VisualRow] = []
    for source_index, (text, bbox, font, height) in enumerate(specs):
        lines.append(
            models._LineItem(
                text=text,
                bbox=bbox,
                angle=0,
                source_index=source_index,
                effective_height=height,
                font_signature=font,
                font_coverage=1.0,
            )
        )
        fragment = models._Fragment(
            text=text,
            bbox=bbox,
            local_bbox=bbox,
            line_index=source_index,
            visual_row_id=source_index,
        )
        rows.append(
            models._VisualRow(
                fragments=[fragment],
                center_y=geometry._bbox_center_y(bbox),
                bbox=bbox,
                visual_row_id=source_index,
            )
        )

    selected = tables._collect_footnote_rows(
        rows,
        lines,
        (0.0, 0.0, 100.0, 50.0),
        10.0,
        set(),
        (100.0, 100.0),
        0,
    )

    assert [tables._visual_row_text(row) for row in selected] == [
        "Note: neutral marker",
        "neutral continuation",
    ]


def test_numeric_body_row_cannot_start_table_note_chain() -> None:
    """Verify that text starting with a number cannot independently initiate table annotation expansion even if it is immediately adjacent to the lower boundary of the table."""

    bbox = (0.0, 52.0, 80.0, 62.0)
    line = models._LineItem(
        text="5 ordinary numbered body",
        bbox=bbox,
        angle=0,
        source_index=0,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    row = models._VisualRow(
        fragments=[models._Fragment(line.text, bbox, bbox, 0, 0)],
        center_y=geometry._bbox_center_y(bbox),
        bbox=bbox,
        visual_row_id=0,
    )

    assert (
        tables._collect_footnote_rows(
            [row],
            [line],
            (0.0, 0.0, 100.0, 50.0),
            10.0,
            set(),
            (100.0, 100.0),
            0,
        )
        == []
    )


@pytest.mark.parametrize("projection_mode", ["empty", "error"])
def test_failed_table_projection_does_not_claim_text(
    monkeypatch: pytest.MonkeyPatch,
    projection_mode: str,
) -> None:
    """Complete rollback candidate when verification projection is empty or throws an error, text lines can still enter the text path."""

    lines = [
        models._LineItem(
            text="cell",
            bbox=(10.0, 20.0, 30.0, 30.0),
            angle=0,
            source_index=0,
            effective_height=10.0,
        ),
        models._LineItem(
            text="Table 1",
            bbox=(10.0, 5.0, 30.0, 15.0),
            angle=0,
            source_index=1,
            effective_height=10.0,
        ),
    ]
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=lines,
        chars=[],
        drawing_lines=[],
    )
    candidate = models._TableCandidate(
        bbox=(0.0, 0.0, 50.0, 50.0),
        local_bbox=(0.0, 0.0, 50.0, 50.0),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 20.0, 50.0, 50.0),
        line_indices={0},
        annotations=[
            models._TableAnnotation(
                kind="caption",
                bbox=(10.0, 5.0, 30.0, 15.0),
                line_indices={1},
            )
        ],
    )
    projection = MagicMock(return_value="")
    if projection_mode == "error":
        projection.side_effect = RuntimeError("projection failed")
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    blocks, annotation_blocks, claimed = tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert blocks == []
    assert annotation_blocks == []
    assert claimed == set()
    assert projection.call_args.args[0] is source.chars
