from __future__ import annotations

import inspect

import pytest
from _flash_pdf_test_utils import (
    _text_line,
)

from docvortex.analyzers.native.pdf import graphics, models, pipeline
from docvortex.document.pdf._document import PDFPathInfo


def _drawing_axis_line(
    orientation: str,
    bbox: tuple[float, float, float, float],
) -> models._AxisLine:
    """Constructs the PDF plot line used by the graphics container test."""

    return models._AxisLine(
        bbox=bbox,
        width=0.5,
        orientation=orientation,  # type: ignore[arg-type]
    )


def _path_info(
    bbox: tuple[float, float, float, float],
    source_index: int,
    *,
    segment_count: int = 5,
    fill_visible: bool = True,
    stroke_visible: bool = False,
    form_depth: int = 0,
) -> PDFPathInfo:
    """Construct the root layer PDF Path used for strong graphics core testing."""

    return PDFPathInfo(
        bbox=bbox,
        segment_count=segment_count,
        fill_visible=fill_visible,
        stroke_visible=stroke_visible,
        form_depth=form_depth,
        source_index=source_index,
    )


def test_vertical_raster_tiles_merge_before_area_filter() -> None:
    """Verify that vertical slices of the same width are first merged into a complete image, and then a single image area threshold is applied."""

    tiles = [(20.0, float(top), 180.0, float(top + 4)) for top in range(20, 60, 4)]

    assert graphics._merge_vertical_raster_tiles(
        tiles,
        (200.0, 200.0),
    ) == [(20.0, 20.0, 180.0, 60.0)]


def test_full_page_raster_tiles_stay_separate() -> None:
    """Verify that the whole page scanned image slices are not combined into a single container and the existing slice-by-slice output is retained."""

    tiles = [(0.0, float(top), 200.0, float(top + 10)) for top in range(0, 200, 10)]

    assert (
        graphics._merge_vertical_raster_tiles(
            tiles,
            (200.0, 200.0),
        )
        == tiles
    )


def _parallel_rule_split_fixture(
    *,
    cross_gutter: bool = False,
) -> tuple[
    models._LineItem,
    list[models._AxisLine],
    list[tuple[float, float, float, float]],
]:
    """Construct semantic-free text, paired graphics, independent horizontal lines, and optional bar and groove interference characters."""

    left_chars = [
        {
            "char": "L",
            "bbox": (
                100.0 + 40.0 * index,
                80.0,
                120.0 + 40.0 * index,
                89.0,
            ),
        }
        for index in range(8)
    ]
    if cross_gutter:
        left_chars[-1]["bbox"] = (475.0, 80.0, 505.0, 89.0)
    right_chars = [
        {
            "char": "R",
            "bbox": (
                540.0 + 40.0 * index,
                80.0,
                560.0 + 40.0 * index,
                89.0,
            ),
        }
        for index in range(8)
    ]
    line = _text_line(
        "LLLLLLLL RRRRRRRR",
        (100.0, 80.0, 840.0, 89.0),
        7,
        visual_row_id=12,
        effective_height=10.0,
    )
    line.chars = [*left_chars, {"char": " ", "bbox": (400.0, 80.0, 540.0, 89.0)}, *right_chars]
    rules = [
        _drawing_axis_line("horizontal", (80.0, 90.0, 480.0, 90.5)),
        _drawing_axis_line("horizontal", (520.0, 90.0, 920.0, 90.5)),
    ]
    images = [
        (100.0, 100.0, 470.0, 300.0),
        (530.0, 100.0, 900.0, 300.0),
    ]
    return line, rules, images


def test_parallel_graphic_rule_rows_split_without_reading_text_content() -> None:
    """Verification of complete spatial evidence splits the same visual line into two protected left and right run."""

    line, rules, images = _parallel_rule_split_fixture()

    split = graphics._split_parallel_graphic_rule_rows(
        [line],
        rules,
        images,
        [],
        (1000.0, 500.0),
        source_index_start=50,
    )

    assert [(item.text, item.bbox) for item in split] == [
        ("LLLLLLLL", (100.0, 80.0, 400.0, 89.0)),
        ("RRRRRRRR", (540.0, 80.0, 840.0, 89.0)),
    ]
    assert [item.source_index for item in split] == [7, 50]
    assert [item.run_index for item in split] == [0, 1]
    assert {item.visual_row_id for item in split} == {12}
    assert all(item.split_from_row for item in split)
    assert all(item.preserve_split_boundary for item in split)

    source = "\n".join(
        inspect.getsource(function)
        for function in (
            graphics._parallel_graphic_rule_pairs,
            graphics._parallel_graphic_row_split_boundary,
            graphics._split_parallel_graphic_rule_rows,
        )
    )
    assert ".text" not in source


@pytest.mark.parametrize(
    "missing_evidence",
    ["right_rule", "right_image", "independent_rules", "clear_gutter", "outside_table"],
)
def test_parallel_graphic_rule_rows_require_all_spatial_evidence(
    missing_evidence: str,
) -> None:
    """Verify that the horizontal lines, graphics, columns, or tables will not be split if any of the exclusions are not established."""

    line, rules, images = _parallel_rule_split_fixture(cross_gutter=missing_evidence == "clear_gutter")
    table_bboxes: list[tuple[float, float, float, float]] = []
    if missing_evidence == "right_rule":
        rules = rules[:1]
    elif missing_evidence == "right_image":
        images = images[:1]
    elif missing_evidence == "independent_rules":
        rules = [_drawing_axis_line("horizontal", (80.0, 90.0, 920.0, 90.5))]
    elif missing_evidence == "outside_table":
        table_bboxes = [(70.0, 70.0, 930.0, 310.0)]

    output = graphics._split_parallel_graphic_rule_rows(
        [line],
        rules,
        images,
        table_bboxes,
        (1000.0, 500.0),
    )

    assert output == [line]


def _graphic_source_fixture() -> models._PageSource:
    """Construct a double-framed graphic, six labels, split caption and adjacent text."""

    lines = [
        _text_line("p = (x, y)", (378.0, 302.0, 421.0, 312.0), 0, visual_row_id=10),
        _text_line("pbar = (xbar, y)", (445.0, 302.0, 488.0, 312.0), 1, visual_row_id=10),
        _text_line("dp", (382.0, 282.0, 403.0, 292.0), 2, visual_row_id=11),
        _text_line("x", (468.0, 282.0, 475.0, 292.0), 3, visual_row_id=11),
        _text_line("Left camera", (360.0, 339.0, 411.0, 350.0), 4, visual_row_id=12),
        _text_line("Right camera", (458.0, 339.0, 509.0, 350.0), 5, visual_row_id=12),
        _text_line(
            "Figure 1: a deliberately long caption that must remain",
            (312.0, 359.0, 500.0, 371.0),
            6,
            visual_row_id=20,
            split_from_row=True,
        ),
        _text_line(
            "independent.",
            (510.0, 359.0, 563.0, 371.0),
            7,
            visual_row_id=20,
            split_from_row=True,
        ),
        _text_line("Nearby prose", (370.0, 225.0, 430.0, 237.0), 8, visual_row_id=9),
    ]
    drawing_lines = [
        _drawing_axis_line("horizontal", (348.0, 272.0, 430.0, 272.5)),
        _drawing_axis_line("horizontal", (348.0, 331.5, 430.0, 332.0)),
        _drawing_axis_line("vertical", (348.0, 272.0, 348.5, 332.0)),
        _drawing_axis_line("vertical", (429.5, 272.0, 430.0, 332.0)),
        _drawing_axis_line("horizontal", (436.0, 272.0, 526.0, 272.5)),
        _drawing_axis_line("horizontal", (436.0, 331.5, 526.0, 332.0)),
        _drawing_axis_line("vertical", (436.0, 272.0, 436.5, 332.0)),
        _drawing_axis_line("vertical", (525.5, 272.0, 526.0, 332.0)),
    ]
    return models._PageSource(
        page_size=(612.0, 792.0),
        lines=lines,
        chars=[],
        drawing_lines=drawing_lines,
    )


def test_double_box_graphic_claims_six_labels_but_not_caption_or_body() -> None:
    """Verify that the entire row of double-box graphics aggregates six labels, split caption, and adjacent text are not partially claimed."""

    blocks, claimed = graphics._build_graphic_like_blocks(
        _graphic_source_fixture(),
        [],
        set(),
    )

    assert len(blocks) == 1
    assert claimed == set(range(6))
    assert blocks[0]["type"] == "image"
    for expected_text in ("dp", "x", "p = (x, y)", "pbar = (xbar, y)", "Left camera", "Right camera"):
        assert expected_text in blocks[0]["content"]
    assert blocks[0]["content"].count("Left camera") == 1
    assert blocks[0]["content"].count("Right camera") == 1
    assert "Figure 1" not in blocks[0]["content"]
    assert "Nearby prose" not in blocks[0]["content"]


def test_graphic_label_accepts_only_near_diagonal_corner_text() -> None:
    """Verify that short labels with no axial overlap fall into the graph only within strict corner distances."""

    core_bbox = (20.0, 20.0, 80.0, 80.0)
    near_corner = _text_line("unit", (5.0, 5.0, 15.0, 15.0), 0)
    distant_corner = _text_line("unit", (-5.0, -5.0, 5.0, 5.0), 1)
    long_title = _text_line(
        "deliberately wide title",
        (0.0, 5.0, 80.0, 15.0),
        2,
    )

    assert graphics._is_graphic_label_member(
        near_corner,
        core_bbox,
        10.0,
    )
    assert not graphics._is_graphic_label_member(
        distant_corner,
        core_bbox,
        10.0,
    )
    assert not graphics._is_graphic_label_member(
        long_title,
        core_bbox,
        10.0,
    )


def test_materialized_table_bbox_has_priority_over_graphic_candidate() -> None:
    """Validate drawing component skipping the graphics container and retaining full text identity when overlapping the table box."""

    blocks, claimed = graphics._build_graphic_like_blocks(
        _graphic_source_fixture(),
        [(340.0, 265.0, 535.0, 355.0)],
        set(),
    )

    assert blocks == []
    assert claimed == set()


def test_complex_path_container_builds_graphic_without_drawing_lines() -> None:
    """Verification that large Path containers and internal 2D complex contours directly form the graphical core."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line("10", (15.0, 20.0, 25.0, 25.0), 0, visual_row_id=1),
            _text_line("label", (30.0, 55.0, 50.0, 60.0), 1, visual_row_id=2),
        ],
        chars=[],
        drawing_lines=[],
        path_infos=[
            _path_info((10.0, 10.0, 90.0, 70.0), 0),
            _path_info((20.0, 20.0, 75.0, 50.0), 1, segment_count=12),
            _path_info((25.0, 25.0, 35.0, 35.0), 2),
            _path_info((40.0, 25.0, 50.0, 40.0), 3),
            _path_info((55.0, 20.0, 65.0, 45.0), 4),
        ],
    )

    core_bboxes = graphics._detect_strong_graphic_bboxes(source)
    blocks, claimed = graphics._build_graphic_like_blocks(
        source,
        [],
        set(),
        core_bboxes,
    )

    assert core_bboxes == [(10.0, 10.0, 90.0, 70.0)]
    assert claimed == {0, 1}
    assert blocks[0]["bbox"] == (10.0, 10.0, 90.0, 70.0)
    assert blocks[0]["content"] == "10\nlabel"


def test_strong_graphic_core_binds_only_to_unique_containing_lane() -> None:
    """Verify that a strong single-column graphic does not absorb adjacent column text, while the true cross-column core remains cross-column."""

    lanes = [
        models._TextLane(left=50.0, right=290.0),
        models._TextLane(left=305.0, right=545.0),
    ]

    assert (
        graphics._strong_graphic_lane_index(
            (73.0, 516.0, 281.0, 655.0),
            lanes,
            10.0,
        )
        == 0
    )
    assert (
        graphics._strong_graphic_lane_index(
            (73.0, 516.0, 520.0, 655.0),
            lanes,
            10.0,
        )
        == -1
    )


def test_axis_pair_requires_internal_two_dimensional_complex_path() -> None:
    """Verify that the intersecting coordinate axes must be supported by two-dimensional complex curves, and that regular rectangular rows will not falsely report graphics."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line("0", (12.0, 65.0, 17.0, 70.0), 0),
            _text_line("x", (78.0, 72.0, 83.0, 77.0), 1),
        ],
        chars=[],
        drawing_lines=[],
        path_infos=[
            _path_info((20.0, 20.0, 21.0, 70.0), 0, segment_count=2, fill_visible=False, stroke_visible=True),
            _path_info((20.0, 69.0, 80.0, 70.0), 1, segment_count=2, fill_visible=False, stroke_visible=True),
            _path_info((25.0, 30.0, 75.0, 60.0), 2, segment_count=12, fill_visible=False, stroke_visible=True),
        ],
    )

    assert graphics._detect_strong_graphic_bboxes(source) == [(20.0, 20.0, 80.0, 70.0)]

    source.path_infos[2] = _path_info(
        (25.0, 30.0, 75.0, 31.0),
        2,
        segment_count=12,
        fill_visible=False,
        stroke_visible=True,
    )
    assert graphics._detect_strong_graphic_bboxes(source) == []


def test_form_image_claims_internal_text_and_small_table_but_not_caption() -> None:
    """Verify that the valid Form annexes the text and small table candidates in the figure, and the external caption is still retained."""

    form_bbox = (10.0, 10.0, 90.0, 65.0)
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line("inside row one", (15.0, 20.0, 70.0, 30.0), 0, visual_row_id=1),
            _text_line("inside row two", (15.0, 35.0, 80.0, 45.0), 1, visual_row_id=2),
            _text_line("Figure 1: outside", (10.0, 70.0, 80.0, 80.0), 2, visual_row_id=3),
        ],
        chars=[],
        drawing_lines=[
            _drawing_axis_line("horizontal", (20.0, 15.0, 80.0, 15.5)),
            _drawing_axis_line("horizontal", (20.0, 55.0, 80.0, 55.5)),
            _drawing_axis_line("vertical", (20.0, 15.0, 20.5, 55.0)),
            _drawing_axis_line("vertical", (79.5, 15.0, 80.0, 55.0)),
        ],
        form_bboxes=[form_bbox],
    )

    selected = graphics._select_form_image_bboxes(source)
    blocks, claimed = graphics._build_form_image_blocks(source, selected, set())

    assert selected == [form_bbox]
    assert graphics._form_supersedes_nested_bbox(
        form_bbox,
        (20.0, 20.0, 40.0, 35.0),
    )
    assert not graphics._form_supersedes_nested_bbox(
        form_bbox,
        (15.0, 15.0, 85.0, 60.0),
    )
    assert claimed == {0, 1}
    assert blocks == [
        {
            "type": "image",
            "bbox": form_bbox,
            "angle": 0,
            "content": "inside row one\ninside row two",
        }
    ]


def test_form_image_bbox_tightens_to_supported_internal_evidence() -> None:
    """Verify that Form with fully nested Path removes whitespace edges, text slightly out of bounds is still included in the evidence and clipped to the page."""

    source = models._PageSource(
        page_size=(120.0, 120.0),
        lines=[
            _text_line("top label", (20.0, 25.0, 70.0, 34.0), 0),
            _text_line("bottom label", (25.0, 68.0, 80.0, 81.0), 1),
        ],
        chars=[],
        drawing_lines=[
            _drawing_axis_line("horizontal", (20.0, 25.0, 85.0, 25.5)),
            _drawing_axis_line("horizontal", (20.0, 75.0, 85.0, 75.5)),
            _drawing_axis_line("vertical", (20.0, 25.0, 20.5, 75.0)),
            _drawing_axis_line("vertical", (84.5, 25.0, 85.0, 75.0)),
        ],
        form_bboxes=[(10.0, 10.0, 100.0, 80.0)],
        path_infos=[
            _path_info((20.0, 25.0, 80.0, 70.0), 0, form_depth=1),
            _path_info((25.0, 30.0, 85.0, 75.0), 1, form_depth=1),
        ],
    )

    assert graphics._select_form_image_bboxes(source) == [(20.0, 25.0, 85.0, 81.0)]


def test_graphic_label_absorbs_short_axis_title_but_rejects_long_caption() -> None:
    """Verified that the upper and lower axis titles can be expanded to eight times the line height, and long legends are still rejected by the graph container."""

    core_bbox = (20.0, 30.0, 140.0, 80.0)
    top_axis_title = _text_line(
        "axis title",
        (20.0, 17.0, 70.0, 25.0),
        0,
        effective_height=8.0,
    )
    bottom_axis_title = _text_line(
        "axis title",
        (80.0, 85.0, 115.0, 91.0),
        1,
        effective_height=6.0,
    )
    long_caption = _text_line(
        "a deliberately long figure caption",
        (20.0, 85.0, 140.0, 93.0),
        2,
        effective_height=8.0,
    )

    assert graphics._is_graphic_label_member(
        top_axis_title,
        core_bbox,
        8.0,
        margin_scale=1.0,
    )
    assert graphics._is_graphic_label_member(
        bottom_axis_title,
        core_bbox,
        8.0,
        margin_scale=1.0,
    )
    assert not graphics._is_graphic_label_member(
        long_caption,
        core_bbox,
        8.0,
        margin_scale=1.0,
    )


def _inline_raster_sequence_source() -> models._PageSource:
    """Construct four bitmaps, three line spacers and three lines of text on the right."""

    return models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line(
                "、",
                (20.0, 22.0, 22.0, 25.0),
                0,
                visual_row_id=7,
                run_index=0,
                split_from_row=True,
            ),
            _text_line(
                "、",
                (32.0, 22.0, 34.0, 25.0),
                1,
                visual_row_id=7,
                run_index=1,
                split_from_row=True,
            ),
            _text_line(
                "、",
                (48.0, 22.0, 50.0, 25.0),
                2,
                visual_row_id=7,
                run_index=2,
                split_from_row=True,
            ),
            _text_line(
                "尾随文字",
                (60.0, 22.0, 90.0, 25.0),
                3,
                visual_row_id=7,
                run_index=3,
                split_from_row=True,
            ),
            _text_line("正文第一行", (10.0, 27.0, 90.0, 30.0), 4, visual_row_id=8),
            _text_line("正文第二行", (10.0, 32.0, 50.0, 35.0), 5, visual_row_id=9),
        ],
        chars=[],
        drawing_lines=[],
        image_bboxes=[
            (10.0, 20.0, 20.0, 24.0),
            (22.0, 20.0, 32.0, 24.0),
            (34.0, 20.0, 48.0, 24.0),
            (50.0, 20.0, 60.0, 24.0),
        ],
    )


def test_raster_image_threshold_accepts_point_38_percent_only() -> None:
    """Admission is allowed when the verification page area reaches 0.38%, and isolated images slightly below the threshold are still filtered."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[],
        chars=[],
        drawing_lines=[],
        image_bboxes=[
            (0.0, 0.0, 9.5, 4.0),
            (20.0, 0.0, 29.49, 4.0),
        ],
    )

    blocks, claimed = graphics._build_raster_image_blocks(source, [], set())

    assert [block["bbox"] for block in blocks] == [(0.0, 0.0, 9.5, 4.0)]
    assert claimed == set()


def test_signature_images_bypass_raster_threshold_and_deduplicate_same_geometry() -> None:
    """Verify that small signatures are still output, and candidates in the same frame between signatures and between signatures and bitmaps are only retained once."""

    source = models._PageSource(
        page_size=(1000.0, 1000.0),
        lines=[],
        chars=[],
        drawing_lines=[],
        image_bboxes=[(100.0, 100.0, 200.0, 200.0)],
        signature_bboxes=[
            (10.0, 10.0, 30.0, 30.0),
            (10.2, 10.1, 30.1, 30.2),
            (100.0, 100.0, 200.0, 200.0),
        ],
    )

    blocks, claimed = graphics._build_raster_image_blocks(source, [], set())

    assert [block["bbox"] for block in blocks] == [
        (10.0, 10.0, 30.0, 30.0),
        (100.0, 100.0, 200.0, 200.0),
    ]
    assert claimed == set()


def test_inline_raster_images_and_gap_runs_form_one_composite_image() -> None:
    """Verify that the four admitted pictures and the three peer intervals fit into a composite image."""

    source = _inline_raster_sequence_source()

    blocks, claimed = graphics._build_raster_image_blocks(source, [], set())

    assert len(blocks) == 1
    assert blocks[0]["bbox"] == (10.0, 20.0, 60.0, 25.0)
    assert blocks[0]["content"].replace(" ", "") == "、、、"
    assert blocks[0]["_inline_visual_row_id"] == 7
    assert claimed == {0, 1, 2}
    assert 3 not in claimed


@pytest.mark.parametrize(
    "failure_mode",
    [
        "misaligned",
        "wide_gap",
        "missing_gap",
        "inconsistent_row",
        "too_few_images",
        "container_bridge",
    ],
)
def test_inline_raster_composite_requires_complete_spatial_sequence(
    failure_mode: str,
) -> None:
    """Preserves independent images without generating composite containers when the validation image or spacer structure is incomplete."""

    source = _inline_raster_sequence_source()
    container_blocks: list[dict[str, object]] = []
    if failure_mode == "misaligned":
        source.image_bboxes[1] = (22.0, 10.0, 32.0, 14.0)
    elif failure_mode == "wide_gap":
        source.image_bboxes[2] = (42.0, 20.0, 56.0, 24.0)
        source.image_bboxes[3] = (58.0, 20.0, 68.0, 24.0)
        source.lines[2].bbox = (56.0, 22.0, 58.0, 25.0)
    elif failure_mode == "missing_gap":
        source.lines = [line for line in source.lines if line.source_index != 1]
    elif failure_mode == "inconsistent_row":
        source.lines[1].visual_row_id = 8
    elif failure_mode == "too_few_images":
        source.image_bboxes = source.image_bboxes[:2]
    else:
        container_blocks = [
            {
                "type": "table",
                "bbox": (32.5, 20.0, 33.5, 25.0),
                "angle": 0,
                "content": "",
            }
        ]

    blocks, claimed = graphics._build_raster_image_blocks(
        source,
        container_blocks,
        set(),
    )

    assert len(blocks) == len(source.image_bboxes)
    assert not [block for block in blocks if "_inline_visual_row_id" in block]
    assert claimed == set()


def test_inline_composite_sorts_before_overlapping_multiline_text() -> None:
    """Verify that when a composite image is bound to a multi-line body containing the first line of the same line, the image is always output first."""

    blocks = [
        {
            "type": "image",
            "bbox": (10.0, 20.0, 60.0, 25.0),
            "angle": 0,
            "content": "、、、",
            "_inline_visual_row_id": 7,
        },
        {
            "type": "text",
            "bbox": (10.0, 22.0, 90.0, 35.0),
            "angle": 0,
            "content": "尾随文字正文第一行正文第二行",
            "_visual_row_ids": {7, 8, 9},
            "_local_line_bboxes": [
                (60.0, 22.0, 90.0, 25.0),
                (10.0, 27.0, 90.0, 30.0),
                (10.0, 32.0, 50.0, 35.0),
            ],
        },
        {
            "type": "paragraph_title",
            "bbox": (10.0, 40.0, 30.0, 44.0),
            "angle": 0,
            "content": "下一节",
        },
    ]

    sorted_blocks = pipeline._sort_blocks_with_visual_row_groups(
        blocks,
        (100.0, 100.0),
    )

    assert [block["type"] for block in sorted_blocks] == [
        "image",
        "text",
        "paragraph_title",
    ]


def test_raster_images_filter_small_objects_avoid_containers_and_claim_text_once() -> None:
    """Validates unique text attribution for bitmap filtering, container precedence, empty content, and overlapping objects."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line("inside", (25.0, 25.0, 35.0, 35.0), 0, visual_row_id=1),
            _text_line("outside caption", (10.0, 52.0, 50.0, 60.0), 1, visual_row_id=2),
            _text_line("covered container text", (20.0, 70.0, 40.0, 80.0), 2, visual_row_id=3),
        ],
        chars=[],
        drawing_lines=[],
        image_bboxes=[
            (10.0, 10.0, 50.0, 50.0),
            (20.0, 20.0, 40.0, 40.0),
            (60.0, 10.0, 90.0, 40.0),
            (0.0, 90.0, 9.0, 94.0),
            (10.0, 60.0, 50.0, 90.0),
        ],
    )

    blocks, claimed = graphics._build_raster_image_blocks(
        source,
        [{"type": "table", "bbox": (10.0, 60.0, 50.0, 90.0), "angle": 0, "content": "table"}],
        set(),
    )

    assert len(blocks) == 3
    assert all(block["type"] == "image" and block["angle"] == 0 for block in blocks)
    assert [block["bbox"] for block in blocks] == [
        (10.0, 10.0, 50.0, 50.0),
        (60.0, 10.0, 90.0, 40.0),
        (20.0, 20.0, 40.0, 40.0),
    ]
    assert [block["content"] for block in blocks] == ["", "", "inside"]
    assert claimed == {0}


def test_raster_image_content_is_removed_from_text_and_empty_image_page_is_kept() -> None:
    """Verify that the text in the picture only enters image, the independent caption is correctly marked, and the pure picture page still outputs empty content."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            _text_line("inside row one", (20.0, 20.0, 50.0, 30.0), 0, visual_row_id=1),
            _text_line("inside row two", (20.0, 35.0, 50.0, 45.0), 1, visual_row_id=2),
            _text_line("Figure 1: outside caption", (10.0, 70.0, 80.0, 80.0), 2, visual_row_id=3),
        ],
        chars=[],
        drawing_lines=[],
        image_bboxes=[(10.0, 10.0, 60.0, 60.0)],
    )

    blocks = pipeline._analyze_page_source(source)
    image_block = next(block for block in blocks if block["type"] == "image")
    caption_block = next(block for block in blocks if block["type"] == "caption")

    assert image_block["content"] == "inside row one\ninside row two"
    assert caption_block["content"] == "Figure 1: outside caption"
    assert sum("inside row" in block["content"] for block in blocks) == 1

    empty_page_blocks = pipeline._analyze_page_source(
        models._PageSource(
            page_size=(100.0, 100.0),
            lines=[],
            chars=[],
            drawing_lines=[],
            image_bboxes=[(10.0, 10.0, 60.0, 60.0)],
        )
    )
    assert empty_page_blocks == [
        {
            "type": "image",
            "bbox": [0.1, 0.1, 0.6, 0.6],
            "angle": 0,
            "content": "",
        }
    ]
