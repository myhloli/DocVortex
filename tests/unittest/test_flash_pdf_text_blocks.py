from __future__ import annotations

from dataclasses import replace

import pytest
from _flash_pdf_test_utils import (
    _text_line,
)

from docvortex.analyzers.native.pdf import geometry, line_layout, line_merging, models, native_text, pipeline, text_blocks


def _span_text_block(
    content: str,
    bbox: tuple[float, float, float, float],
) -> dict[str, object]:
    """Construct a full-width span text back and test using the smallest inner text block."""

    return {
        "type": "text",
        "bbox": bbox,
        "angle": 0,
        "content": content,
        "_visual_row_ids": set(),
        "_single_run_row_id": None,
        "_local_line_bboxes": [bbox],
        "_line_heights": [4.0],
        "_font_signatures": {("Body", 0)},
        "_inline_math_regions": [],
        "_lane_interval": (0.0, 100.0),
        "_lane_is_span": True,
        "_hard_break_before": False,
        "_protected_hard_break_before": False,
        "_hanging_indent_group": None,
        "_leading_emphasis_start": False,
    }


def test_hanging_indent_groups_neutral_entries_and_ignores_centered_heading() -> None:
    """Verify that repeated hanging indents without serial numbers are grouped item by item, and that centered titles do not participate in entries."""

    body_font = ("Body", 0)
    italic_font = ("BodyItalic", 1)
    lines = [
        _text_line(
            "Centered heading",
            (35.0, 0.0, 85.0, 10.0),
            0,
            font_signature=("Heading", 0),
            font_coverage=1.0,
        ),
        _text_line("Alpha begins", (0.0, 20.0, 100.0, 30.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line(
            "alpha italic continuation",
            (15.0, 30.0, 100.0, 40.0),
            2,
            font_signature=italic_font,
            font_coverage=1.0,
        ),
        _text_line("alpha closes", (15.0, 40.0, 70.0, 50.0), 3, font_signature=body_font, font_coverage=1.0),
        _text_line("Beta begins", (0.0, 50.0, 100.0, 60.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line("beta continues", (15.0, 60.0, 100.0, 70.0), 5, font_signature=body_font, font_coverage=1.0),
        _text_line("Gamma begins", (0.0, 70.0, 100.0, 80.0), 6, font_signature=body_font, font_coverage=1.0),
        _text_line("gamma continues", (15.0, 80.0, 100.0, 90.0), 7, font_signature=body_font, font_coverage=1.0),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])
    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 120.0))

    assert 0 not in group_map
    assert [group_map[index] for index in range(1, 8)] == [0, 0, 0, 1, 1, 2, 2]
    assert [block["content"] for block in blocks] == [
        "Centered heading",
        "Alpha begins alpha italic continuation alpha closes",
        "Beta begins beta continues",
        "Gamma begins gamma continues",
    ]


def test_nested_columns_ignore_intervening_full_width_metadata_band() -> None:
    """Verify that when left and right column rows are interleaved, the middle full-width metadata cluster does not block local double-column inference."""

    lines = [
        _text_line("full width", (0.0, 0.0, 100.0, 10.0), 0),
        *[
            _text_line(
                f"left {index}",
                (0.0, 30.0 + 12.0 * index, 45.0, 40.0 + 12.0 * index),
                1 + 2 * index,
            )
            for index in range(4)
        ],
        *[
            _text_line(
                f"right {index}",
                (55.0, 30.0 + 12.0 * index, 100.0, 40.0 + 12.0 * index),
                2 + 2 * index,
            )
            for index in range(4)
        ],
        _text_line("author one", (20.0, 100.0, 80.0, 110.0), 20),
        _text_line("author two", (20.0, 112.0, 80.0, 122.0), 21),
        _text_line("author three", (20.0, 124.0, 80.0, 134.0), 22),
    ]

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        100.0,
        10.0,
    )
    regular = sorted(
        (
            lane
            for lane in lanes
            if not lane.is_span and len(lane.lines) >= 4 and min(item[1][1] for item in lane.lines) >= 30.0
        ),
        key=lambda lane: lane.left,
    )

    assert [(lane.left, lane.right) for lane in regular[:2]] == [
        (0.0, 45.0),
        (55.0, 100.0),
    ]


def test_paragraph_formula_context_merges_dense_same_lane_text() -> None:
    """Verify that complex inline fractions and the text before and after the same column are restored to a text block."""

    def block(
        content: str,
        bbox: tuple[float, float, float, float],
    ) -> dict[str, object]:
        """Constructs a minimal text block with columns and line boxes."""

        return {
            "type": "text",
            "bbox": bbox,
            "angle": 0,
            "content": content,
            "_local_line_bboxes": [bbox],
            "_line_heights": [10.0],
            "_font_signatures": {("Body", 0)},
            "_lane_interval": (0.0, 100.0),
            "_lane_is_span": False,
            "_hard_break_before": False,
            "_leading_emphasis_start": False,
        }

    blocks = [
        block("prefix", (0.0, 0.0, 30.0, 10.0)),
        block("body:", (0.0, 10.0, 100.0, 20.0)),
        block("I=ΔT/ΔH", (0.0, 21.0, 100.0, 31.0)),
        block("tail", (0.0, 32.0, 100.0, 42.0)),
        {
            **block("other lane", (120.0, 0.0, 220.0, 10.0)),
            "_lane_interval": (120.0, 220.0),
        },
    ]
    blocks[2]["_paragraph_formula_context"] = True

    merged = text_blocks._merge_paragraph_formula_context_blocks(
        blocks,
        (220.0, 100.0),
    )

    assert [item["content"] for item in merged] == [
        "prefix body: I=ΔT/ΔH tail",
        "other lane",
    ]


@pytest.mark.parametrize("angle", [90, 270])
def test_rotated_paragraph_formula_context_uses_upright_gap(
    angle: int,
) -> None:
    """Verify that the rotation formula context only merges text in the same column that are truly adjacent in forward coordinates."""

    page_size = (200.0, 200.0)

    def block(
        content: str,
        local_bbox: tuple[float, float, float, float],
        *,
        formula_context: bool = False,
    ) -> dict[str, object]:
        """Constructs a rotated text block that separates the page frame from the forward line frame."""

        return {
            "type": "text",
            "bbox": geometry._rotate_bbox_from_upright(
                local_bbox,
                page_size,
                angle,
            ),
            "angle": angle,
            "content": content,
            "_local_line_bboxes": [local_bbox],
            "_line_heights": [10.0],
            "_font_signatures": {("Body", 0)},
            "_lane_interval": (0.0, 80.0),
            "_lane_is_span": False,
            "_hard_break_before": False,
            "_paragraph_formula_context": formula_context,
        }

    adjacent = text_blocks._merge_paragraph_formula_context_blocks(
        [
            block("formula", (0.0, 0.0, 80.0, 10.0), formula_context=True),
            block("adjacent body", (0.0, 11.0, 80.0, 21.0)),
        ],
        page_size,
    )
    distant = text_blocks._merge_paragraph_formula_context_blocks(
        [
            block("formula", (0.0, 0.0, 80.0, 10.0), formula_context=True),
            block("distant body", (0.0, 100.0, 80.0, 110.0)),
        ],
        page_size,
    )
    missing_geometry_seed = block(
        "formula",
        (0.0, 0.0, 80.0, 10.0),
        formula_context=True,
    )
    missing_geometry_seed.pop("_local_line_bboxes")
    missing_geometry = text_blocks._merge_paragraph_formula_context_blocks(
        [
            missing_geometry_seed,
            block("adjacent body", (0.0, 11.0, 80.0, 21.0)),
        ],
        page_size,
    )

    assert [item["content"] for item in adjacent] == ["formula adjacent body"]
    assert adjacent[0]["bbox"] == geometry._rotate_bbox_from_upright(
        (0.0, 0.0, 80.0, 21.0),
        page_size,
        angle,
    )
    assert [item["content"] for item in distant] == [
        "formula",
        "distant body",
    ]
    assert [item["content"] for item in missing_geometry] == [
        "formula",
        "adjacent body",
    ]


def test_full_width_span_text_merges_without_terminal_punctuation_dependency() -> None:
    """Verify that the same column is full width span The text can be merged continuously across periods, and English word segmentation can be used to restore it."""

    blocks = [
        _span_text_block(
            "Abstract first sentence.",
            (0.0, 0.0, 90.0, 10.0),
        ),
        _span_text_block(
            "Continuation remains in the same paragraph.",
            (0.0, 16.0, 90.0, 26.0),
        ),
        _span_text_block(
            "Another impor-",
            (0.0, 32.0, 90.0, 42.0),
        ),
        _span_text_block(
            "tant detail",
            (0.0, 48.0, 90.0, 58.0),
        ),
        {
            **_span_text_block(
                "Keywords: separate metadata",
                (0.0, 64.0, 90.0, 74.0),
            ),
            "_lane_is_span": False,
            "_hard_break_before": True,
            "_protected_hard_break_before": True,
        },
    ]

    merged = text_blocks._merge_unterminated_text_components(
        blocks,
    )

    assert [block["content"] for block in merged] == [
        "Abstract first sentence. Continuation remains in the same paragraph. Another important detail",
        "Keywords: separate metadata",
    ]


@pytest.mark.parametrize(
    "barrier",
    [
        "hard_break",
        "protected_break",
        "leading_emphasis",
        "font_conflict",
        "narrow_row",
        "different_lane",
        "different_angle",
        "labelled_metadata",
        "intervening_block",
        "ordinary_lane_terminal",
    ],
)
def test_full_width_span_text_respects_structural_barriers(
    barrier: str,
) -> None:
    """Verify that span returns and is still constrained by hard borders, bands, fonts, widths, and blockers."""

    first = _span_text_block(
        "First sentence.",
        (0.0, 0.0, 90.0, 10.0),
    )
    second = _span_text_block(
        "Second sentence",
        (0.0, 16.0, 90.0, 26.0),
    )
    blocks = [first, second]
    if barrier == "hard_break":
        second["_hard_break_before"] = True
    elif barrier == "protected_break":
        second["_protected_hard_break_before"] = True
    elif barrier == "leading_emphasis":
        second["_leading_emphasis_start"] = True
    elif barrier == "font_conflict":
        second["_font_signatures"] = {("Other", 0)}
    elif barrier == "narrow_row":
        second["bbox"] = (0.0, 16.0, 79.0, 26.0)
        second["_local_line_bboxes"] = [second["bbox"]]
    elif barrier == "different_lane":
        second["_lane_interval"] = (0.0, 120.0)
    elif barrier == "different_angle":
        second["angle"] = 90
    elif barrier == "labelled_metadata":
        second["content"] = "Keywords: separate metadata"
    elif barrier == "intervening_block":
        blocks.insert(
            1,
            {
                **_span_text_block(
                    "visual barrier",
                    (0.0, 11.0, 90.0, 15.0),
                ),
                "type": "caption",
            },
        )
    elif barrier == "ordinary_lane_terminal":
        first["_lane_is_span"] = False
        second["_lane_is_span"] = False

    merged = text_blocks._merge_unterminated_text_components(
        blocks,
    )

    assert not any(
        "First sentence." in str(block.get("content") or "") and "Second sentence" in str(block.get("content") or "")
        for block in merged
    )


def test_hanging_indent_accepts_reference_spacing_and_italic_tail() -> None:
    """Verify that reference spacing of 1.25 line height does not break the previous italicized trailing line."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "Alpha begins",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "alpha continues",
            (15.0, 10.0, 100.0, 20.0),
            1,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "alpha italic tail",
            (15.0, 20.0, 75.0, 30.0),
            2,
            font_signature=("BodyItalic", 1),
            font_coverage=1.0,
        ),
        _text_line(
            "Beta begins",
            (0.0, 42.5, 100.0, 52.5),
            3,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "beta continues",
            (15.0, 52.5, 100.0, 62.5),
            4,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "Gamma begins",
            (0.0, 75.0, 100.0, 85.0),
            5,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "gamma continues",
            (15.0, 85.0, 100.0, 95.0),
            6,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])
    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 120.0))

    assert [group_map[index] for index in range(7)] == [0, 0, 0, 1, 1, 2, 2]
    assert [block["content"] for block in blocks] == [
        "Alpha begins alpha continues alpha italic tail",
        "Beta begins beta continues",
        "Gamma begins gamma continues",
    ]


def test_first_line_indent_and_large_gap_do_not_form_hanging_indent_groups() -> None:
    """Verify that normal first-line indentation and lines spanning large gaps do not accidentally trigger hanging indent mode."""

    lines = [
        _text_line("First paragraph", (15.0, 0.0, 100.0, 10.0), 0),
        _text_line("first continuation", (0.0, 10.0, 100.0, 20.0), 1),
        _text_line("Second paragraph", (15.0, 20.0, 100.0, 30.0), 2),
        _text_line("second continuation", (0.0, 30.0, 100.0, 40.0), 3),
        _text_line("Detached start", (0.0, 70.0, 100.0, 80.0), 4),
        _text_line("detached continuation", (15.0, 80.0, 100.0, 90.0), 5),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    assert text_blocks._build_hanging_indent_group_map(lane, [], []) == {}


def test_hanging_indent_keeps_confirmed_entries_before_plain_trailing_paragraph() -> None:
    """Verify that trailing ordinary left-justified paragraphs do not invalidate previously confirmed dangling indent entries as a whole."""

    lines = [
        _text_line("entry one", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("entry one tail", (15.0, 10.0, 100.0, 20.0), 1),
        _text_line("entry two", (0.0, 20.0, 100.0, 30.0), 2),
        _text_line("entry two tail", (15.0, 30.0, 100.0, 40.0), 3),
        _text_line("entry three", (0.0, 40.0, 100.0, 50.0), 4),
        _text_line("entry three tail", (15.0, 50.0, 100.0, 60.0), 5),
        _text_line("plain trailing paragraph", (0.0, 60.0, 100.0, 70.0), 6),
        _text_line("plain continuation", (0.0, 70.0, 100.0, 80.0), 7),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])

    assert [group_map[index] for index in range(6)] == [0, 0, 1, 1, 2, 2]
    assert 6 not in group_map
    assert 7 not in group_map


def test_full_width_hanging_entries_can_start_after_aligned_prose() -> None:
    """Verify that duplicate hanging entries in nearly full columns can be started after the same left-margin text, with ordinary single lines remaining in the front text."""

    lines = [
        _text_line("plain disclosure", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("plain single row", (0.0, 10.0, 100.0, 20.0), 1),
        _text_line("first entry", (0.0, 20.0, 100.0, 30.0), 2),
        _text_line("first entry tail", (15.0, 30.0, 70.0, 40.0), 3),
        _text_line("second entry", (0.0, 40.0, 100.0, 50.0), 4),
        _text_line("second entry tail", (15.0, 50.0, 80.0, 60.0), 5),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])

    assert 0 not in group_map
    assert 1 not in group_map
    assert [group_map[index] for index in range(2, 6)] == [0, 0, 1, 1]


def test_bullet_rows_remain_independent_text_blocks() -> None:
    """Verify that near-full column bullets are explicit segment boundaries and that consecutive disclosure items are not concatenated in the post-processing stage."""

    lines = [
        _text_line("• first disclosure.", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("• second disclosure.", (0.0, 10.0, 100.0, 20.0), 1),
        _text_line("• third disclosure.", (0.0, 20.0, 100.0, 30.0), 2),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 100.0))

    assert [block["content"] for block in blocks] == [
        "• first disclosure.",
        "• second disclosure.",
        "• third disclosure.",
    ]


def test_compact_bullet_rows_keep_one_list_block() -> None:
    """Validates that short bulleted lists continue to be chunked into compact lists and do not apply bounds on nearly full columns of disclosed items."""

    lines = [
        _text_line("• first tool", (0.0, 0.0, 45.0, 10.0), 0),
        _text_line("• second tool", (0.0, 10.0, 48.0, 20.0), 1),
        _text_line("• third tool", (0.0, 20.0, 42.0, 30.0), 2),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    assert text_blocks._explicit_text_break_sources(lane) == set()


def test_adjacent_compact_label_rows_remain_separate() -> None:
    """Verify that consecutive short key-value metadata rows form independent blocks and are not concatenated due to missing end-of-sentence punctuation."""

    lines = [
        _text_line("递交截止时间：09:30", (0.0, 0.0, 55.0, 10.0), 0),
        _text_line("递交方式：paper", (0.0, 10.0, 60.0, 20.0), 1),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    assert text_blocks._explicit_text_break_sources(lane) == {1}


def test_twelve_point_gutter_keeps_two_text_lanes_and_paragraphs_separate() -> None:
    """Verify that the line height and column grooves of about 12pt are still recognized as double columns, and the left and right text will not be cross-spliced."""

    lines: list[models._LineItem] = []
    for row_index, top in enumerate((100.0, 112.0, 124.0)):
        lines.extend(
            [
                _text_line(f"left-{row_index}", (49.0, top, 300.0, top + 12.0), row_index * 2),
                _text_line(f"right-{row_index}", (312.0, top, 563.0, top + 12.0), row_index * 2 + 1),
            ]
        )

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        612.0,
        12.0,
    )
    blocks = text_blocks._build_text_blocks(lines, [], (612.0, 792.0))

    regular_lanes = [lane for lane in lanes if not lane.is_span]
    assert [(lane.left, lane.right) for lane in regular_lanes] == [
        (49.0, 300.0),
        (312.0, 563.0),
    ]
    assert len(blocks) == 2
    assert all("right-" not in block["content"] for block in blocks if "left-" in block["content"])
    assert all("left-" not in block["content"] for block in blocks if "right-" in block["content"])


def test_cross_column_caption_tail_stays_in_span_lane_and_one_text_block() -> None:
    """Verify that a short caption trailing row that is only a single column wide is still recycled into a continuous span of caption."""

    lines: list[models._LineItem] = [
        _text_line("caption line one", (0.0, 10.0, 200.0, 20.0), 0),
        _text_line("caption line two", (0.0, 22.0, 200.0, 32.0), 1),
        _text_line("caption tail", (0.0, 34.0, 70.0, 44.0), 2),
        _text_line("ordinary left short line", (0.0, 70.0, 70.0, 80.0), 3),
    ]
    for row_index, top in enumerate((100.0, 112.0, 124.0, 136.0, 148.0)):
        lines.extend(
            [
                _text_line(
                    f"left body {row_index}",
                    (0.0, top, 90.0, top + 10.0),
                    4 + 2 * row_index,
                ),
                _text_line(
                    f"right body {row_index}",
                    (110.0, top, 200.0, top + 10.0),
                    5 + 2 * row_index,
                ),
            ]
        )

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        200.0,
        10.0,
    )
    blocks = text_blocks._build_text_blocks(lines, [], (200.0, 180.0))

    span_lane = next(lane for lane in lanes if lane.is_span)
    assert {line.source_index for line, _bbox in span_lane.lines} == {0, 1, 2}
    assert all(line.source_index != 3 for line, _bbox in span_lane.lines)
    caption_blocks = [block for block in blocks if "caption line one" in block["content"]]
    assert len(caption_blocks) == 1
    assert "caption tail" in caption_blocks[0]["content"]


def test_slight_bbox_overlap_contributes_to_gap_estimate_and_separates_caption() -> None:
    """Verify that slight vertical overlap counts as zero headroom and the short legend does not merge with the subsequent long caption."""

    body_lines = [
        _text_line("body-0", (312.0, 0.0, 563.0, 12.0), 0),
        _text_line("body-1", (312.0, 11.95, 563.0, 23.95), 1),
        _text_line("body-2", (312.0, 23.9, 563.0, 35.9), 2),
    ]
    legend = _text_line("Right camera", (458.0, 60.0, 509.0, 72.0), 3)
    caption = _text_line(
        "Figure 1: a long caption spanning the full column",
        (312.0, 79.0, 563.0, 91.0),
        4,
    )
    lane = models._TextLane(
        left=312.0,
        right=563.0,
        lines=[*((line, line.bbox) for line in body_lines), (legend, legend.bbox), (caption, caption.bbox)],
    )

    regular_gap, gap_mad = line_layout._estimate_lane_gap(lane)

    assert (regular_gap, gap_mad) == (0.0, 0.0)
    assert not line_layout._should_connect_text_rows(
        (legend, legend.bbox),
        (caption, caption.bbox),
        lane,
        regular_gap,
        gap_mad,
        [],
        [],
    )


def test_local_previous_left_edge_exposes_first_line_indent() -> None:
    """Verify that after the local layout center is moved to the left, the indentation of the first line of the next line relative to the previous physical line can still be recognized."""

    previous = _text_line(
        "previous paragraph without punctuation",
        (0.0, 0.0, 80.0, 10.0),
        0,
    )
    current = _text_line(
        "Indented new paragraph",
        (10.0, 12.0, 100.0, 22.0),
        1,
    )
    lane = models._TextLane(
        left=20.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert not line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        2.0,
        0.0,
        [],
        [],
    )


def test_terminal_full_lane_row_breaks_after_abnormal_clearance() -> None:
    """Verify that if the headroom after the last line of a full-column sentence exceeds half a line height of the normal spacing, a new paragraph will be forced."""

    previous = _text_line("Figure caption ends.", (0.0, 0.0, 100.0, 10.0), 0)
    current = _text_line("new paragraph fills the lane", (0.0, 17.0, 100.0, 27.0), 1)
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert not line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        1.0,
        0.0,
        [],
        [],
    )


def test_effective_height_connects_body_line_after_tall_math_glyph() -> None:
    """Verify that the high math glyph stretches the original bbox while still concatenating the next text line by a valid line height."""

    previous = _text_line(
        "support window Ωp centered at the pixel",
        (312.0, 100.0, 563.0, 118.82),
        0,
        effective_height=12.0,
    )
    current = _text_line(
        "by",
        (312.0, 112.0, 325.0, 124.0),
        1,
        effective_height=12.0,
    )
    lane = models._TextLane(
        left=312.0,
        right=563.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert current.bbox[1] - previous.bbox[3] == pytest.approx(-6.82)
    assert line_layout._effective_text_row_gap(
        (previous, previous.bbox),
        (current, current.bbox),
    ) == pytest.approx(0.0)
    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_body_row_gap_uses_canonical_baseline_without_changing_semantic_gap() -> None:
    """Verify that body connections use baseline cadence, while other semantic paths still retain bbox headroom."""

    previous = _text_line(
        "previous full body row",
        (0.0, 0.0, 100.0, 24.0),
        0,
        effective_height=24.0,
    )
    current = _text_line(
        "current full body row",
        (0.0, 30.0, 100.0, 54.0),
        1,
        effective_height=24.0,
    )
    for line, baseline in ((previous, 20.0), (current, 36.0)):
        line.em_height = 10.0
        line.style_scale_repaired = True
        line.baseline = baseline
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert (
        line_layout._effective_text_row_gap(
            (previous, previous.bbox),
            (current, current.bbox),
        )
        == 20.0
    )
    assert (
        line_layout._effective_body_text_row_gap(
            (previous, previous.bbox),
            (current, current.bbox),
        )
        == 6.0
    )
    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        6.0,
        0.0,
        [],
        [],
    )


def test_cross_lane_short_tail_returns_to_unique_preceding_lane() -> None:
    """Verify that short tails that fall completely within the only text column do not continue to stay at span lane."""

    previous = _text_line(
        "full preceding row",
        (0.0, 10.0, 100.0, 20.0),
        0,
        visual_row_id=1,
        effective_height=10.0,
    )
    tail = _text_line(
        "short tail",
        (0.0, 20.0, 40.0, 30.0),
        1,
        visual_row_id=2,
        effective_height=10.0,
    )
    previous.baseline = 20.0
    tail.baseline = 30.0
    body_lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox)],
    )
    span_lane = models._TextLane(
        left=0.0,
        right=180.0,
        lines=[(tail, tail.bbox)],
        is_span=True,
    )

    line_layout._reattach_cross_lane_short_tails(
        [body_lane, span_lane],
        10.0,
    )

    assert [line.text for line, _bbox in body_lane.lines] == [
        "full preceding row",
        "short tail",
    ]
    assert span_lane.lines == []


def test_inline_scripts_and_touching_low_coverage_runs_are_recovered() -> None:
    """Verify that tight upper and lower subscripts are recovered with low-coverage peer suffixes while preserving external formula numbering."""

    script_lines = [
        _text_line("O(ω", (0.0, 0.0, 100.0, 18.8), 0, visual_row_id=0, effective_height=12.0),
        _text_line("2", (100.3, 0.8, 104.0, 7.0), 1, visual_row_id=1, effective_height=6.0),
        _text_line("Di", (0.0, 40.0, 100.0, 52.0), 2, visual_row_id=2, effective_height=12.0),
        _text_line("p", (96.9, 46.0, 101.0, 53.0), 3, visual_row_id=3, effective_height=7.0),
    ]

    merged_scripts = native_text._merge_native_inline_scripts(script_lines, (200.0, 100.0))

    assert [line.text for line in merged_scripts] == ["O(ω2", "Dip"]
    assert not any(line.restored_inline_cluster for line in merged_scripts)

    caption_prefix = _text_line(
        "(4",
        (0.0, 70.0, 12.0, 82.0),
        4,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    caption_suffix = _text_line(
        "th row).",
        (12.1, 70.0, 50.0, 82.0),
        5,
        font_signature=("Body", 0),
        font_coverage=0.7,
    )
    formula_body = _text_line(
        "formula",
        (0.0, 90.0, 50.0, 102.0),
        6,
        font_signature=("Math", 0),
        font_coverage=1.0,
    )
    formula_number = _text_line(
        "(4)",
        (51.2, 90.0, 60.0, 102.0),
        7,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )

    assert line_merging._can_merge_same_baseline_pair(
        caption_prefix,
        caption_prefix.bbox,
        caption_suffix,
        caption_suffix.bbox,
        [],
    )
    assert not line_merging._can_merge_same_baseline_pair(
        formula_body,
        formula_body.bbox,
        formula_number,
        formula_number.bbox,
        [],
    )


def test_title_resolved_visual_row_merges_sparse_short_prefix() -> None:
    """Verify that short prefixes of the same visual line as far as wide body text are restored by font family and baseline."""

    lines = [
        _text_line(
            "prefix",
            (0.0, 0.0, 15.0, 10.0),
            0,
            visual_row_id=7,
            run_index=0,
            split_from_row=True,
            font_signature=("SimSun", 0),
            font_coverage=1.0,
        ),
        _text_line(
            "wide body",
            (60.0, 0.0, 140.0, 10.0),
            1,
            visual_row_id=7,
            run_index=1,
            split_from_row=True,
            font_signature=("ABCDEF+SimSun", 0),
            font_coverage=1.0,
        ),
    ]

    merged = line_merging._merge_title_resolved_visual_rows(
        lines,
        (200.0, 100.0),
    )

    assert len(merged) == 1
    assert merged[0].text == "prefix wide body"


@pytest.mark.parametrize(
    "failure_mode",
    [
        "font-conflict",
        "different-row",
        "protected-boundary",
        "two-wide-runs",
    ],
)
def test_title_resolved_visual_row_rejects_weak_sparse_prefix(
    failure_mode: str,
) -> None:
    """Preserve splits when validating font, line identity, guard boundaries, or prefix widths."""

    prefix_bbox = (0.0, 0.0, 40.0, 10.0) if failure_mode == "two-wide-runs" else (0.0, 0.0, 15.0, 10.0)
    lines = [
        _text_line(
            "prefix",
            prefix_bbox,
            0,
            visual_row_id=7,
            run_index=0,
            split_from_row=True,
            preserve_split_boundary=(failure_mode == "protected-boundary"),
            font_signature=("SimSun", 0),
            font_coverage=1.0,
        ),
        _text_line(
            "wide body",
            (85.0, 0.0, 165.0, 10.0) if failure_mode == "two-wide-runs" else (60.0, 0.0, 140.0, 10.0),
            1,
            visual_row_id=(8 if failure_mode == "different-row" else 7),
            run_index=1,
            split_from_row=True,
            font_signature=(("OtherFont", 0) if failure_mode == "font-conflict" else ("ABCDEF+SimSun", 0)),
            font_coverage=1.0,
        ),
    ]

    merged = line_merging._merge_title_resolved_visual_rows(
        lines,
        (200.0, 100.0),
    )

    assert [line.text for line in merged] == [
        "prefix",
        "wide body",
    ]


def test_canonical_reference_scale_merges_bracket_marker_but_not_plain_number() -> None:
    """Verify loose Bracket references of the same height can be merged into hosts by PDF font size, ordinary numbers remain independent."""

    def sized_line(
        text: str,
        bbox: tuple[float, float, float, float],
        source_index: int,
        font_size: float,
    ) -> models._LineItem:
        """Constructs a single-character native line with PDF font size evidence."""

        line = _text_line(
            text,
            bbox,
            source_index,
            visual_row_id=source_index,
            effective_height=12.0,
        )
        line.chars = [
            {
                "char": text[0],
                "bbox": bbox,
                "font": {
                    "name": "Fixture",
                    "flags": 0,
                    "size": font_size,
                    "weight": 400,
                },
            }
        ]  # type: ignore[list-item]
        return line

    base = sized_line(
        "TriviaQA",
        (0.0, 10.0, 50.0, 22.0),
        0,
        10.0,
    )
    reference = sized_line(
        "[38]",
        (50.1, 5.0, 57.1, 14.0),
        1,
        6.0,
    )
    plain_number = sized_line(
        "1",
        (50.1, 5.0, 57.1, 14.0),
        2,
        6.0,
    )

    merged_reference = native_text._merge_native_inline_scripts(
        [base, reference],
        (120.0, 100.0),
    )
    separate_number = native_text._merge_native_inline_scripts(
        [
            sized_line(
                "TriviaQA",
                (0.0, 10.0, 50.0, 22.0),
                0,
                10.0,
            ),
            plain_number,
        ],
        (120.0, 100.0),
    )

    assert [line.text for line in merged_reference] == [
        "TriviaQA[38]",
    ]
    assert [line.text for line in separate_number] == [
        "TriviaQA",
        "1",
    ]


def test_same_repeated_indent_continues_after_terminal_punctuation() -> None:
    """Verify that the immediately trailing line of the same hanging indent anchor is not cut again by a preceding period."""

    previous = _text_line(
        "first indented line.",
        (20.0, 0.0, 100.0, 10.0),
        0,
        effective_height=10.0,
    )
    current = _text_line(
        "second indented line",
        (20.0, 10.0, 80.0, 20.0),
        1,
        effective_height=10.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_sparse_lane_terminal_gap_remains_paragraph_boundary() -> None:
    """Verify that sparse pages do not reversely estimate large spacing after a unique period into regular line spacing."""

    previous = _text_line(
        "first generated paragraph.",
        (0.0, 0.0, 100.0, 10.0),
        0,
        effective_height=10.0,
    )
    current = _text_line(
        "second generated paragraph",
        (0.0, 18.0, 100.0, 28.0),
        1,
        effective_height=10.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert not line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        8.0,
        0.0,
        [],
        [],
    )


def test_list_intro_chain_splits_before_item_and_keeps_intro_together() -> None:
    """Continuous boot segments before the verification list are merged with the colon tail, while numbered entries remain hard bounded."""

    lines = [
        _text_line("paragraph continues", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("paragraph closes.", (0.0, 10.0, 100.0, 20.0), 1),
        _text_line("list introduction", (10.0, 20.0, 100.0, 30.0), 2),
        _text_line("directions:", (0.0, 30.0, 25.0, 40.0), 3),
        _text_line("(1) first item", (10.0, 40.0, 100.0, 50.0), 4),
        _text_line("item continuation", (0.0, 50.0, 100.0, 60.0), 5),
    ]

    blocks = text_blocks._build_text_blocks(
        lines,
        [],
        (120.0, 100.0),
    )

    assert [block["content"] for block in blocks] == [
        "paragraph continues paragraph closes. list introduction directions:",
        "(1) first item item continuation",
    ]


def test_single_numbered_tail_merges_back_into_colon_line() -> None:
    """Verify that single numbered trailing entries without continuation lines are merged into colon lines and are not permanently separated by sparse page boundaries."""

    lines = [
        _text_line("scope includes:", (0.0, 0.0, 90.0, 10.0), 0),
        _text_line("(001) only entry;", (10.0, 23.0, 70.0, 33.0), 1),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 100.0))

    assert [block["content"] for block in blocks] == [
        "scope includes: (001) only entry;",
    ]


def test_multiline_component_absorbs_aligned_short_tail() -> None:
    """Verify that multi-line text absorbs a single-line short tail on the same left edge, and does not require the short tail to reach the full column width of the text."""

    lines = [
        _text_line("body starts", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("body continues", (0.0, 10.0, 100.0, 20.0), 1),
        _text_line("short tail", (0.0, 35.0, 35.0, 45.0), 2),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 100.0))

    assert [block["content"] for block in blocks] == [
        "body starts body continues short tail",
    ]


def test_url_rows_continue_colon_introduction() -> None:
    """Verify that URL lines after the colon lead continue to belong to the previous text block, even if URL itself does not fill the column."""

    lines = [
        _text_line("reference link:", (0.0, 0.0, 80.0, 10.0), 0),
        _text_line("https://example.test/path", (0.0, 24.0, 55.0, 34.0), 1),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 100.0))

    assert [block["content"] for block in blocks] == [
        "reference link: https://example.test/path",
    ]


def test_low_overlap_detached_script_preserves_following_row_gap() -> None:
    """Verify that low-overlapping external glyphs fit geometrically into the main body and that the raised top edge does not break the line."""

    base = _text_line(
        "Alpha",
        (10.0, 10.0, 70.0, 20.8),
        0,
        visual_row_id=0,
        effective_height=10.8,
    )
    detached_script = _text_line(
        "◇",
        (70.0, 3.6, 74.2, 11.1),
        1,
        visual_row_id=1,
        effective_height=7.5,
    )
    following = _text_line(
        "Beta",
        (35.0, 22.0, 80.0, 34.0),
        2,
        visual_row_id=2,
        effective_height=10.8,
    )

    merged = native_text._merge_native_inline_scripts(
        [base, detached_script, following],
        (120.0, 100.0),
    )

    assert [line.text for line in merged] == ["Alpha◇", "Beta"]
    assert merged[0].bbox == (10.0, 3.6, 74.2, 20.8)
    assert merged[0].restored_inline_cluster
    assert line_layout._effective_text_row_gap(
        (merged[0], merged[0].bbox),
        (following, following.bbox),
    ) == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("small_text", "small_bbox"),
    [
        pytest.param("wide", (50.0, 3.5, 62.0, 11.0), id="wide-token"),
        pytest.param("◇", (53.0, 3.5, 57.2, 11.0), id="horizontal-gap"),
        pytest.param("◇", (50.0, 0.0, 54.2, 7.5), id="vertical-gap"),
        pytest.param("◇", (50.0, 12.0, 54.2, 19.5), id="same-baseline-cell"),
    ],
)
def test_detached_script_geometry_rejects_ambiguous_small_runs(
    small_text: str,
    small_bbox: tuple[float, float, float, float],
) -> None:
    """Validate that wide text, distant text, and small cells in the same row cannot be merged into the body based on small font size alone."""

    base = _text_line(
        "Base",
        (0.0, 10.0, 50.0, 22.0),
        0,
        visual_row_id=0,
        effective_height=12.0,
    )
    small = _text_line(
        small_text,
        small_bbox,
        1,
        visual_row_id=1,
        effective_height=7.5,
    )

    merged = native_text._merge_native_inline_scripts([base, small], (120.0, 100.0))

    assert [line.text for line in merged] == ["Base", small_text]
    assert not any(line.restored_inline_cluster for line in merged)


def test_full_lane_large_height_mismatch_only_recovers_aligned_continuation() -> None:
    """Verify that full-column mixed font URL can be continued while short formulas remain separated from explicit font style boundaries."""

    previous = _text_line(
        "video sequences have been made avail-",
        (0.0, 0.0, 100.0, 12.0),
        0,
        effective_height=12.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    full_width_url = _text_line(
        "able at http://example.test/data",
        (0.0, 12.0, 100.0, 24.0),
        1,
        effective_height=7.69,
        font_signature=("Mono", 0),
        font_coverage=1.0,
    )
    short_formula = _text_line(
        "x = 1",
        (0.0, 12.0, 60.0, 24.0),
        2,
        effective_height=7.69,
        font_signature=("Math", 0),
        font_coverage=1.0,
    )
    styled_reference = _text_line(
        "italic bibliography continuation",
        (0.0, 12.0, 100.0, 24.0),
        3,
        effective_height=7.69,
        font_signature=("BodyItalic", 1),
        font_coverage=1.0,
    )
    lane = models._TextLane(left=0.0, right=100.0)

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (full_width_url, full_width_url.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )
    assert not line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (short_formula, short_formula.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )
    assert not line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (styled_reference, styled_reference.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_pdf_subset_font_variants_keep_full_rows_and_short_tail_connected() -> None:
    """Verify that PDF subset signature differences for the same font family do not break full column text and its short tail lines."""

    first = _text_line(
        "first full row",
        (0.0, 0.0, 100.0, 10.0),
        0,
        font_signature=("ABCDEF+TimesNewRomanPSMT", 1),
        font_coverage=1.0,
    )
    second = _text_line(
        "second full row",
        (0.0, 10.0, 100.0, 20.0),
        1,
        font_signature=("TimesNewRomanPSMT", 29),
        font_coverage=1.0,
    )
    tail = _text_line(
        "short tail",
        (0.0, 20.0, 45.0, 30.0),
        2,
        font_signature=("GHIJKL+TimesNewRomanPSMT", 17),
        font_coverage=1.0,
    )
    lane = models._TextLane(left=0.0, right=100.0)

    assert line_layout._should_connect_text_rows(
        (first, first.bbox),
        (second, second.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )
    assert line_layout._should_connect_text_rows(
        (second, second.bbox),
        (tail, tail.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_pdf_subset_font_short_tail_keeps_significant_weight_barrier() -> None:
    """When verifying that the font family is the same but the weight changes significantly, short tail continuation lines remain hard segmented."""

    body = _text_line(
        "full body row",
        (0.0, 0.0, 100.0, 10.0),
        0,
        font_signature=("ABCDEF+Body", 1),
        font_coverage=1.0,
        dominant_font_weight=400.0,
    )
    emphasized_tail = _text_line(
        "bold short row",
        (0.0, 10.0, 45.0, 20.0),
        1,
        font_signature=("GHIJKL+Body", 29),
        font_coverage=1.0,
        dominant_font_weight=700.0,
    )
    lane = models._TextLane(left=0.0, right=100.0)

    assert not line_layout._should_connect_text_rows(
        (body, body.bbox),
        (emphasized_tail, emphasized_tail.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_smaller_footnote_after_abnormal_gap_forces_text_block_break() -> None:
    """When the verification font size is less than 88% of the front line and the headroom is too large, the main text and footnotes are forced to be separated into blocks."""

    body = _text_line(
        "body continuation",
        (0.0, 0.0, 100.0, 10.0),
        0,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    footnote = _text_line(
        "small footnote",
        (0.0, 16.0, 100.0, 24.7),
        1,
        effective_height=8.7,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(body, body.bbox), (footnote, footnote.bbox)],
    )

    assert not line_layout._should_connect_text_rows(
        (body, body.bbox),
        (footnote, footnote.bbox),
        lane,
        3.0,
        0.0,
        [],
        [],
    )


def test_overlapping_fraction_fragments_merge_with_ha_and_nb_body_hosts() -> None:
    """Verify that the upper and lower fraction fragments of Ha and Nb are returned to the text host, and ordinary subsequent lines remain independent."""

    body_font = ("Body", 0)
    math_font = ("Math", 1)
    lines = [
        _text_line(
            "Ha =",
            (0.0, 10.0, 20.0, 20.0),
            0,
            visual_row_id=0,
            split_from_row=True,
            effective_height=10.0,
            font_signature=math_font,
            font_coverage=0.67,
        ),
        _text_line(
            "root",
            (30.0, 0.0, 40.0, 12.0),
            1,
            visual_row_id=0,
            split_from_row=True,
            effective_height=9.0,
            font_signature=math_font,
            font_coverage=0.57,
        ),
        _text_line(
            "sigma",
            (30.0, 9.0, 34.0, 16.0),
            2,
            visual_row_id=1,
            effective_height=7.0,
            font_signature=math_font,
            font_coverage=1.0,
        ),
        _text_line(
            "mu",
            (30.0, 16.0, 34.0, 23.0),
            3,
            visual_row_id=2,
            effective_height=7.0,
            font_signature=math_font,
            font_coverage=1.0,
        ),
        _text_line(
            "denotes the Hartmann number",
            (22.0, 3.0, 100.0, 25.0),
            4,
            visual_row_id=3,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=0.9,
        ),
        _text_line(
            "Nb = numerator",
            (0.0, 40.0, 42.0, 53.0),
            5,
            visual_row_id=4,
            effective_height=8.0,
            font_signature=math_font,
            font_coverage=0.6,
        ),
        _text_line(
            "mu",
            (30.0, 48.0, 34.0, 55.0),
            6,
            visual_row_id=5,
            effective_height=7.0,
            font_signature=math_font,
            font_coverage=1.0,
        ),
        _text_line(
            "denotes the Brownian parameter",
            (46.0, 43.0, 100.0, 53.0),
            7,
            visual_row_id=6,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "ordinary following row",
            (0.0, 70.0, 100.0, 80.0),
            8,
            visual_row_id=7,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    merged = line_merging._merge_overlapping_inline_text_clusters(
        lines,
        (120.0, 100.0),
        [],
    )

    assert [line.source_index for line in merged] == [0, 5, 8]
    assert merged[0].text == "Ha = root sigma mu denotes the Hartmann number"
    assert merged[1].text == "Nb = numerator mu denotes the Brownian parameter"
    assert all(line.restored_inline_cluster for line in merged[:2])
    assert not any(line.text in {"sigma", "mu"} for line in merged)


def test_overlapping_delta_fraction_and_tail_form_one_text_block() -> None:
    """Verify that the numerator and denominator of the same physical row can be combined with the amplitude description of the next row to form a single block after recovery."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "where, delta = numerator",
            (0.0, 0.0, 32.0, 13.0),
            0,
            visual_row_id=0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=0.73,
        ),
        _text_line(
            "denominator displays the amplitude",
            (28.0, 3.0, 100.0, 16.0),
            1,
            visual_row_id=1,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=0.86,
        ),
        _text_line(
            "ratio.",
            (0.0, 18.0, 20.0, 28.0),
            2,
            visual_row_id=2,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    merged = line_merging._merge_overlapping_inline_text_clusters(
        lines,
        (120.0, 100.0),
        [],
    )
    blocks = text_blocks._build_text_blocks(merged, [], (120.0, 100.0))

    assert len(merged) == 2
    assert [block["content"] for block in blocks] == ["where, delta = numerator denominator displays the amplitude ratio."]


def test_overlapping_inline_pair_respects_table_and_physical_row_gap() -> None:
    """Verify that 2D fragment joins do not span tables, nor join ordinary upper and lower adjacent body lines."""

    first = _text_line("left", (0.0, 0.0, 40.0, 10.0), 0, effective_height=10.0)
    same_row = _text_line("right", (60.0, 0.0, 100.0, 10.0), 1, effective_height=10.0)
    next_row = _text_line("next", (0.0, 12.0, 40.0, 22.0), 2, effective_height=10.0)

    assert not line_merging._overlapping_inline_cluster_pair_is_connected(
        (first, first.bbox),
        (same_row, same_row.bbox),
        10.0,
        [(45.0, -5.0, 55.0, 15.0)],
    )
    assert not line_merging._overlapping_inline_cluster_pair_is_connected(
        (first, first.bbox),
        (next_row, next_row.bbox),
        10.0,
        [],
    )


def test_hyphen_continuation_cannot_start_false_hanging_indent_entry() -> None:
    """Verify that line breaks and continuations are given priority in the previous paragraph, and the indented instructions and compact formulas in the subsequent first line are still separated into separate blocks."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "linear equations in the trans-",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "verse direction.",
            (0.0, 12.0, 40.0, 22.0),
            1,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "The expression starts here",
            (12.0, 24.0, 100.0, 34.0),
            2,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "the stream function is given below:",
            (0.0, 36.0, 75.0, 46.0),
            3,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "F = fraction",
            (12.0, 48.0, 45.0, 55.0),
            4,
            effective_height=7.0,
            font_signature=("Math", 1),
            font_coverage=0.5,
        ),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])
    blocks = text_blocks._build_text_blocks(lines, [], (120.0, 100.0))

    assert 1 not in group_map
    assert [block["content"] for block in blocks] == [
        "linear equations in the transverse direction.",
        "The expression starts here the stream function is given below:",
        "F = fraction",
    ]


def test_local_two_column_band_survives_full_width_body_below() -> None:
    """Verify that the partial double columns at the top of the page are not covered by the text of the banner below."""

    lines: list[models._LineItem] = []
    for row_index, top in enumerate((0.0, 12.0, 24.0, 36.0)):
        lines.extend(
            [
                _text_line(f"left-{row_index}", (10.0, top, 90.0, top + 10.0), row_index * 2),
                _text_line(f"right-{row_index}", (110.0, top, 190.0, top + 10.0), row_index * 2 + 1),
            ]
        )
    lines.extend(
        _text_line(f"wide-{row_index}", (10.0, top, 190.0, top + 10.0), 20 + row_index)
        for row_index, top in enumerate((70.0, 82.0, 94.0, 106.0))
    )

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        200.0,
        10.0,
    )

    lane_texts = [{line.text for line, _bbox in lane.lines} for lane in lanes]
    assert {f"left-{index}" for index in range(4)} in lane_texts
    assert {f"right-{index}" for index in range(4)} in lane_texts
    assert {f"wide-{index}" for index in range(4)} in lane_texts


def test_nested_lane_accepts_wider_one_sided_rows_and_expands_interval() -> None:
    """Verify that partial columns of narrow legend inference can still accept wide text lines that do not go into another column."""

    lines: list[models._LineItem] = []
    for row_index, top in enumerate((0.0, 10.0, 20.0, 30.0, 40.0)):
        lines.extend(
            [
                _text_line(
                    f"narrow-left-{row_index}",
                    (10.0, top, 55.0, top + 8.0),
                    row_index * 2,
                ),
                _text_line(
                    f"right-{row_index}",
                    (115.0, top, 190.0, top + 8.0),
                    row_index * 2 + 1,
                ),
            ]
        )
    lines.extend(
        [
            _text_line("wide-left-one", (10.0, 32.0, 105.0, 40.0), 20),
            _text_line("wide-left-two", (10.0, 42.0, 105.0, 50.0), 21),
        ]
    )
    lines.extend(
        _text_line(
            f"outer-{row_index}",
            (10.0, top, 190.0, top + 8.0),
            30 + row_index,
        )
        for row_index, top in enumerate((90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0))
    )

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        200.0,
        8.0,
    )

    left_lane = next(lane for lane in lanes if any(line.text == "wide-left-one" for line, _bbox in lane.lines))
    assert not left_lane.is_span
    assert left_lane.right == pytest.approx(105.0)
    assert {line.text for line, _bbox in left_lane.lines if line.text.startswith("wide-left")} == {
        "wide-left-one",
        "wide-left-two",
    }


def test_regular_lanes_accept_wider_one_sided_rows_but_keep_gutter_crossing_span() -> None:
    """Verify that normal double columns will expand the width of one side column without absorbing the column rows that cross the column gap."""

    lines: list[models._LineItem] = []
    for row_index, top in enumerate((0.0, 10.0, 20.0, 30.0, 40.0)):
        lines.extend(
            [
                _text_line(
                    f"narrow-left-{row_index}",
                    (10.0, top, 55.0, top + 8.0),
                    row_index * 2,
                ),
                _text_line(
                    f"right-{row_index}",
                    (115.0, top, 190.0, top + 8.0),
                    row_index * 2 + 1,
                ),
            ]
        )
    lines.extend(
        [
            _text_line("wide-left-one", (10.0, 52.0, 105.0, 60.0), 20),
            _text_line("wide-left-two", (10.0, 62.0, 105.0, 70.0), 21),
            _text_line("crosses-gutter", (10.0, 74.0, 130.0, 82.0), 22),
        ]
    )

    lanes = line_layout._infer_text_lanes(
        [(line, line.bbox) for line in lines],
        200.0,
        8.0,
    )

    left_lane = next(lane for lane in lanes if any(line.text == "wide-left-one" for line, _bbox in lane.lines))
    span_lane = next(lane for lane in lanes if any(line.text == "crosses-gutter" for line, _bbox in lane.lines))
    assert not left_lane.is_span
    assert left_lane.right == pytest.approx(105.0)
    assert span_lane.is_span


def test_short_span_tail_reattaches_before_later_cross_layout_region() -> None:
    """Verify that the short tail row of the partial banner section will not remain in a single column due to another banner row later on the page."""

    first = _text_line("wide one", (10.0, 0.0, 190.0, 10.0), 0)
    second = _text_line("wide two", (10.0, 10.0, 190.0, 20.0), 1)
    tail = _text_line("tail", (10.0, 20.0, 50.0, 30.0), 2)
    later = _text_line("later wide", (10.0, 80.0, 190.0, 90.0), 3)
    parallel = _text_line("parallel", (110.0, 50.0, 190.0, 60.0), 4)
    left_lane = models._TextLane(left=10.0, right=90.0, lines=[(tail, tail.bbox)])
    right_lane = models._TextLane(left=110.0, right=190.0, lines=[(parallel, parallel.bbox)])
    span_lane = models._TextLane(
        left=10.0,
        right=190.0,
        lines=[(first, first.bbox), (second, second.bbox), (later, later.bbox)],
        is_span=True,
    )

    line_layout._reattach_span_lane_continuations(
        [left_lane, right_lane, span_lane],
        10.0,
    )

    assert tail not in [line for line, _bbox in left_lane.lines]
    assert tail in [line for line, _bbox in span_lane.lines]


def test_repeated_indented_span_tails_reattach_and_form_separate_entries() -> None:
    """Verify that the duplicate cross-column first line and indented last line are still grouped one by one after being moved back to span."""

    body_font = ("Body", 0)
    first = _text_line(
        "first wide",
        (10.0, 0.0, 190.0, 10.0),
        0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    first_tail = _text_line(
        "first tail",
        (25.0, 10.0, 90.0, 20.0),
        1,
        font_signature=body_font,
        font_coverage=1.0,
    )
    second = _text_line(
        "second wide",
        (10.0, 20.0, 190.0, 30.0),
        2,
        font_signature=body_font,
        font_coverage=1.0,
    )
    second_tail = _text_line(
        "second tail",
        (25.0, 30.0, 80.0, 40.0),
        3,
        font_signature=body_font,
        font_coverage=1.0,
    )
    left_lane = models._TextLane(
        left=10.0,
        right=90.0,
        lines=[(first_tail, first_tail.bbox), (second_tail, second_tail.bbox)],
    )
    right_marker = _text_line("peer", (110.0, 70.0, 190.0, 80.0), 4)
    right_lane = models._TextLane(
        left=110.0,
        right=190.0,
        lines=[(right_marker, right_marker.bbox)],
    )
    span_lane = models._TextLane(
        left=10.0,
        right=190.0,
        lines=[(first, first.bbox), (second, second.bbox)],
        is_span=True,
    )

    line_layout._reattach_span_lane_continuations(
        [left_lane, right_lane, span_lane],
        10.0,
    )
    group_map = text_blocks._build_hanging_indent_group_map(span_lane, [], [])

    assert not left_lane.lines
    assert [group_map[index] for index in range(4)] == [0, 0, 1, 1]


def test_single_indented_span_tail_does_not_reattach() -> None:
    """Verify that a single indented line is not guessed as a cross-column continuation in the absence of repeat structure support."""

    first = _text_line("wide", (10.0, 0.0, 190.0, 10.0), 0)
    tail = _text_line("tail", (25.0, 10.0, 80.0, 20.0), 1)
    peer = _text_line("peer", (110.0, 50.0, 190.0, 60.0), 2)
    left_lane = models._TextLane(left=10.0, right=90.0, lines=[(tail, tail.bbox)])
    right_lane = models._TextLane(left=110.0, right=190.0, lines=[(peer, peer.bbox)])
    span_lane = models._TextLane(
        left=10.0,
        right=190.0,
        lines=[(first, first.bbox)],
        is_span=True,
    )

    line_layout._reattach_span_lane_continuations(
        [left_lane, right_lane, span_lane],
        10.0,
    )

    assert tail in [line for line, _bbox in left_lane.lines]


def test_aligned_fallback_fonts_merge_but_style_conflict_stays_separate() -> None:
    """Verify that adjacent addresses and contact information can be continued across font families, while style bit changes still form a boundary."""

    address = _text_line(
        "address",
        (0.0, 0.0, 70.0, 10.0),
        0,
        font_signature=("CJK", 32),
        font_coverage=1.0,
        dominant_font_weight=250.0,
    )
    contact = _text_line(
        "contact",
        (0.0, 12.0, 80.0, 22.0),
        1,
        font_signature=("Latin", 32),
        font_coverage=1.0,
        dominant_font_weight=220.0,
    )
    emphasized = _text_line(
        "emphasized",
        (0.0, 24.0, 80.0, 34.0),
        2,
        font_signature=("LatinBold", 64),
        font_coverage=1.0,
        dominant_font_weight=650.0,
    )

    blocks = text_blocks._build_text_blocks(
        [address, contact, emphasized],
        [],
        (100.0, 100.0),
    )

    assert [block["content"] for block in blocks] == [
        "address contact",
        "emphasized",
    ]


def test_hanging_indent_uses_top_pitch_when_terminal_glyph_height_is_abnormal() -> None:
    """Verify that abnormally high-end characters will not break up the hanging indent of duplicate references."""

    lines = [
        _text_line("entry one", (0.0, 0.0, 100.0, 10.0), 0),
        _text_line("entry one tail", (15.0, 10.0, 90.0, 20.0), 1),
        _text_line("entry two", (0.0, 20.0, 100.0, 30.0), 2),
        _text_line(
            "entry two tall tail",
            (15.0, 30.0, 90.0, 40.0),
            3,
            effective_height=18.6,
        ),
        _text_line("entry three", (0.0, 42.0, 100.0, 52.0), 4),
        _text_line("entry three tail", (15.0, 52.0, 90.0, 62.0), 5),
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    group_map = text_blocks._build_hanging_indent_group_map(lane, [], [])

    assert [group_map[index] for index in range(6)] == [0, 0, 1, 1, 2, 2]


def test_spatial_post_merge_connects_short_opener_wide_body_and_tail() -> None:
    """Verify that short first lines, full-width text, and immediate last lines split across columns are rejoined in spatial relationships only."""

    blocks = [
        {
            "type": "text",
            "bbox": (10.0, 10.0, 40.0, 20.0),
            "angle": 0,
            "content": "section",
            "_visual_row_ids": {0},
            "_local_line_bboxes": [(10.0, 10.0, 40.0, 20.0)],
            "_line_heights": [10.0],
        },
        {
            "type": "text",
            "bbox": (10.0, 22.0, 190.0, 42.0),
            "angle": 0,
            "content": "wide body",
            "_visual_row_ids": {1, 2},
            "_local_line_bboxes": [
                (10.0, 22.0, 190.0, 32.0),
                (10.0, 32.0, 190.0, 42.0),
            ],
            "_line_heights": [10.0, 10.0],
        },
        {
            "type": "text",
            "bbox": (10.0, 44.0, 100.0, 54.0),
            "angle": 0,
            "content": "tail",
            "_visual_row_ids": {3},
            "_local_line_bboxes": [(10.0, 44.0, 100.0, 54.0)],
            "_line_heights": [10.0],
        },
    ]

    merged = text_blocks._merge_spatial_text_components(
        blocks,
        (200.0, 100.0),
    )

    assert len(merged) == 1
    assert merged[0]["bbox"] == (10.0, 10.0, 190.0, 54.0)
    assert merged[0]["content"] == "section wide body tail"
    assert merged[0]["_visual_row_ids"] == {0, 1, 2, 3}


def test_spatial_post_merge_uses_compatible_local_lane_width() -> None:
    """Verify that the short first line in a half-page column connects multiple paragraphs of text and stops at the beginning of the next grouping."""

    lane_metadata = {
        "_lane_interval": (40.0, 370.0),
        "_lane_is_span": False,
    }
    blocks = [
        {
            "type": "text",
            "bbox": (40.0, 10.0, 80.0, 20.0),
            "angle": 0,
            "content": "opener",
            "_visual_row_ids": {0},
            "_local_line_bboxes": [(40.0, 10.0, 80.0, 20.0)],
            "_line_heights": [10.0],
            **lane_metadata,
        },
        {
            "type": "text",
            "bbox": (40.0, 22.0, 365.0, 42.0),
            "angle": 0,
            "content": "body one",
            "_visual_row_ids": {1, 2},
            "_local_line_bboxes": [
                (40.0, 22.0, 365.0, 32.0),
                (40.0, 32.0, 365.0, 42.0),
            ],
            "_line_heights": [10.0, 10.0],
            **lane_metadata,
        },
        {
            "type": "text",
            "bbox": (40.0, 44.0, 365.0, 54.0),
            "angle": 0,
            "content": "body two",
            "_visual_row_ids": {3},
            "_local_line_bboxes": [(40.0, 44.0, 365.0, 54.0)],
            "_line_heights": [10.0],
            **lane_metadata,
        },
        {
            "type": "text",
            "bbox": (40.0, 56.0, 365.0, 76.0),
            "angle": 0,
            "content": "body three",
            "_visual_row_ids": {4, 5},
            "_local_line_bboxes": [
                (40.0, 56.0, 365.0, 66.0),
                (40.0, 66.0, 365.0, 76.0),
            ],
            "_line_heights": [10.0, 10.0],
            **lane_metadata,
        },
        {
            "type": "text",
            "bbox": (40.0, 78.0, 230.0, 88.0),
            "angle": 0,
            "content": "body tail",
            "_visual_row_ids": {6},
            "_local_line_bboxes": [(40.0, 78.0, 230.0, 88.0)],
            "_line_heights": [10.0],
            **lane_metadata,
        },
        {
            "type": "text",
            "bbox": (40.0, 90.0, 365.0, 112.0),
            "angle": 0,
            "content": "next section body",
            "_visual_row_ids": {7, 8},
            "_local_line_bboxes": [
                (40.0, 90.0, 90.0, 100.0),
                (40.0, 102.0, 365.0, 112.0),
            ],
            "_line_heights": [10.0, 10.0],
            **lane_metadata,
        },
    ]

    merged = text_blocks._merge_spatial_text_components(
        blocks,
        (600.0, 200.0),
    )

    assert [block["content"] for block in merged] == [
        "opener body one body two body three body tail",
        "next section body",
    ]
    assert merged[0]["bbox"] == (40.0, 10.0, 365.0, 88.0)


def test_spatial_post_merge_does_not_share_incompatible_lane_width() -> None:
    """When verifying that the left and right column intervals are incompatible, the page width is still used, and the first paragraph merging cannot be relaxed."""

    blocks = [
        {
            "type": "text",
            "bbox": (40.0, 10.0, 80.0, 20.0),
            "angle": 0,
            "content": "left opener",
            "_visual_row_ids": {0},
            "_local_line_bboxes": [(40.0, 10.0, 80.0, 20.0)],
            "_line_heights": [10.0],
            "_lane_interval": (40.0, 370.0),
            "_lane_is_span": False,
        },
        {
            "type": "text",
            "bbox": (270.0, 22.0, 590.0, 42.0),
            "angle": 0,
            "content": "right body",
            "_visual_row_ids": {1, 2},
            "_local_line_bboxes": [
                (270.0, 22.0, 590.0, 32.0),
                (270.0, 32.0, 590.0, 42.0),
            ],
            "_line_heights": [10.0, 10.0],
            "_lane_interval": (270.0, 590.0),
            "_lane_is_span": False,
        },
    ]

    merged = text_blocks._merge_spatial_text_components(
        blocks,
        (600.0, 100.0),
    )

    assert [block["content"] for block in merged] == [
        "left opener",
        "right body",
    ]


def test_spatial_post_merge_does_not_join_span_caption_to_column_body() -> None:
    """Verify that cross-column legends are not merged in the secondary stage even if they are immediately adjacent to single-column text."""

    blocks = [
        {
            "type": "text",
            "bbox": (40.0, 10.0, 560.0, 30.0),
            "angle": 0,
            "content": "wide caption",
            "_visual_row_ids": {0, 1},
            "_local_line_bboxes": [
                (200.0, 10.0, 360.0, 20.0),
                (40.0, 20.0, 560.0, 30.0),
            ],
            "_line_heights": [10.0, 10.0],
            "_lane_interval": (40.0, 560.0),
            "_lane_is_span": True,
        },
        {
            "type": "text",
            "bbox": (40.0, 35.0, 290.0, 65.0),
            "angle": 0,
            "content": "left column body",
            "_visual_row_ids": {2, 3, 4},
            "_local_line_bboxes": [
                (40.0, 35.0, 290.0, 45.0),
                (40.0, 45.0, 290.0, 55.0),
                (40.0, 55.0, 290.0, 65.0),
            ],
            "_line_heights": [10.0, 10.0, 10.0],
            "_lane_interval": (40.0, 290.0),
            "_lane_is_span": False,
        },
    ]

    merged = text_blocks._merge_spatial_text_components(blocks, (600.0, 100.0))

    assert [block["content"] for block in merged] == [
        "wide caption",
        "left column body",
    ]


def test_image_caption_marker_only_attaches_same_font_spatial_tail() -> None:
    """Verification of universal legend markers only confirms image proximity candidates and connects continuation lines in the same font back to the corresponding legend."""

    common = {
        "type": "text",
        "angle": 0,
        "_single_run_row_id": None,
        "_lane_interval": (10.0, 90.0),
        "_lane_is_span": True,
    }
    blocks = [
        {
            **common,
            "bbox": (30.0, 50.0, 70.0, 60.0),
            "content": "图1 neutral caption",
            "_visual_row_ids": {1},
            "_local_line_bboxes": [(30.0, 50.0, 70.0, 60.0)],
            "_line_heights": [10.0],
            "_font_signatures": {("Chinese", 0)},
        },
        {
            **common,
            "bbox": (10.0, 58.0, 90.0, 68.0),
            "content": "Fig. 1 neutral caption;",
            "_visual_row_ids": {2},
            "_local_line_bboxes": [(10.0, 58.0, 90.0, 68.0)],
            "_line_heights": [10.0],
            "_font_signatures": {("English", 0)},
        },
        {
            **common,
            "bbox": (20.0, 67.0, 80.0, 77.0),
            "content": "caption continuation",
            "_visual_row_ids": {3},
            "_local_line_bboxes": [(20.0, 67.0, 80.0, 77.0)],
            "_line_heights": [10.0],
            "_font_signatures": {("English", 0)},
        },
    ]

    merged = text_blocks._merge_image_caption_text_blocks(
        blocks,
        [(20.0, 10.0, 80.0, 50.0)],
    )

    assert len(merged) == 2
    chinese = next(block for block in merged if block["content"].startswith("图1"))
    english = next(block for block in merged if block["content"].startswith("Fig. 1"))
    assert "continuation" not in chinese["content"]
    assert "caption continuation" in english["content"]
    assert english["bbox"] == (10.0, 58.0, 90.0, 77.0)


def test_fragmented_center_header_merges_but_remote_volume_stays_separate() -> None:
    """Verify that isometric narrow header glyphs form a logical block and that far-end volume blocks are not absorbed."""

    blocks = []
    for index, left in enumerate((40.0, 60.0, 80.0, 100.0)):
        blocks.append(
            {
                "type": "header",
                "bbox": (left, 5.0, left + 6.0, 15.0),
                "angle": 0,
                "content": f"h{index}",
                "_visual_row_ids": {0},
                "_single_run_row_id": 0,
                "_local_line_bboxes": [(left, 5.0, left + 6.0, 15.0)],
                "_line_heights": [10.0],
                "_font_signatures": {("Header", 0)},
                "_lane_interval": (0.0, 200.0),
                "_lane_is_span": True,
            }
        )
    blocks.append(
        {
            "type": "header",
            "bbox": (170.0, 5.0, 190.0, 15.0),
            "angle": 0,
            "content": "volume",
            "_visual_row_ids": {0},
            "_single_run_row_id": 0,
            "_local_line_bboxes": [(170.0, 5.0, 190.0, 15.0)],
            "_line_heights": [10.0],
            "_font_signatures": {("Header", 0)},
            "_lane_interval": (0.0, 200.0),
            "_lane_is_span": True,
        }
    )

    merged = text_blocks._merge_fragmented_header_blocks(blocks)

    assert len(merged) == 2
    center = next(block for block in merged if block["content"].startswith("h0"))
    assert center["content"] == "h0 h1 h2 h3"
    assert center["bbox"] == (40.0, 5.0, 106.0, 15.0)
    assert next(block for block in merged if block["content"] == "volume")


def test_spatial_post_merge_cannot_skip_intervening_title_block() -> None:
    """Verify that secondary component merges cannot cross the middle title block in the same horizontal flow."""

    common = {
        "angle": 0,
        "_single_run_row_id": None,
        "_lane_interval": (0.0, 100.0),
        "_lane_is_span": False,
        "_font_signatures": {("Body", 0)},
    }
    blocks = [
        {
            **common,
            "type": "text",
            "bbox": (0.0, 0.0, 25.0, 10.0),
            "content": "short opener",
            "_visual_row_ids": {0},
            "_local_line_bboxes": [(0.0, 0.0, 25.0, 10.0)],
            "_line_heights": [10.0],
        },
        {
            **common,
            "type": "paragraph_title",
            "bbox": (0.0, 9.0, 45.0, 19.0),
            "content": "intervening title",
            "_visual_row_ids": {1},
            "_local_line_bboxes": [(0.0, 9.0, 45.0, 19.0)],
            "_line_heights": [10.0],
        },
        {
            **common,
            "type": "text",
            "bbox": (0.0, 12.0, 100.0, 22.0),
            "content": "wide body",
            "_visual_row_ids": {2},
            "_local_line_bboxes": [(0.0, 12.0, 100.0, 22.0)],
            "_line_heights": [10.0],
        },
    ]

    merged = text_blocks._merge_spatial_text_components(blocks, (100.0, 50.0))

    assert [block["content"] for block in merged] == [
        "short opener",
        "intervening title",
        "wide body",
    ]


def test_exaggerated_bbox_short_tail_stays_with_full_width_previous_row() -> None:
    """Verify that the short trailing line with the same left margin does not fall off the body paragraph due to abnormal font box height."""

    previous = _text_line(
        "full width previous row",
        (0.0, 0.0, 100.0, 10.0),
        0,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    current = _text_line(
        "short tail",
        (0.0, 12.0, 25.0, 30.0),
        1,
        effective_height=18.0,
        font_signature=("Number", 0),
        font_coverage=1.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        regular_gap=2.0,
        gap_mad=0.5,
        table_bboxes=[],
        axis_lines=[],
    )


def test_indented_reference_colon_keeps_safe_short_tail_with_abnormal_height() -> None:
    """Verify that when reference continuation lines are indented relative to the left edge of the number, the unusually tall short tail after the colon remains in the same block."""

    previous = _text_line(
        "full continuation：",
        (25.0, 0.0, 100.0, 10.0),
        0,
        effective_height=9.0,
    )
    current = _text_line(
        "short tall tail",
        (25.0, 11.0, 50.0, 29.0),
        1,
        effective_height=18.6,
    )
    next_entry = _text_line(
        "［10］ next entry",
        (0.0, 24.0, 100.0, 34.0),
        2,
        effective_height=10.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (previous, previous.bbox),
            (current, current.bbox),
            (next_entry, next_entry.bbox),
        ],
    )

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        regular_gap=1.0,
        gap_mad=0.1,
        table_bboxes=[],
        axis_lines=[],
    )
    assert text_blocks._starts_structural_reference_entry(
        (current, current.bbox),
        (next_entry, next_entry.bbox),
    )


def test_indented_list_row_can_return_to_lane_left_for_short_tail() -> None:
    """When the first line of the verification list is slightly indented, the short trailing line of the same font back to the left of the column can still be continued."""

    body_font = ("Body", 0)
    previous = _text_line(
        "indented list row",
        (15.0, 0.0, 100.0, 10.0),
        0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    current = _text_line(
        "short tail",
        (0.0, 10.0, 65.0, 20.0),
        1,
        font_signature=body_font,
        font_coverage=1.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(previous, previous.bbox), (current, current.bbox)],
    )

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        regular_gap=0.0,
        gap_mad=0.0,
        table_bboxes=[],
        axis_lines=[],
    )


def test_outdented_reference_number_starts_new_structural_entry() -> None:
    """Verify that the universal reference number only cuts the previous continuation line if the left-hand geometry holds."""

    previous = _text_line("previous continuation", (25.0, 0.0, 100.0, 10.0), 0)
    current = _text_line("［23］ next reference", (0.0, 10.0, 100.0, 20.0), 1)
    aligned = _text_line("［23］ inline marker", (25.0, 10.0, 100.0, 20.0), 2)

    assert text_blocks._starts_structural_reference_entry(
        (previous, previous.bbox),
        (current, current.bbox),
    )
    assert not text_blocks._starts_structural_reference_entry(
        (previous, previous.bbox),
        (aligned, aligned.bbox),
    )


def test_spatial_post_merge_limits_tapered_tail_to_parallel_information_grid() -> None:
    """Verify that the slightly larger spaced descending trailing row is only aligned back to its left-aligned body in the side-by-side information grid."""

    blocks = [
        {
            "type": "text",
            "bbox": (10.0, 10.0, 90.0, 40.0),
            "angle": 0,
            "content": "left body",
            "_visual_row_ids": {0},
            "_local_line_bboxes": [(10.0, 30.0, 90.0, 40.0)],
            "_line_heights": [10.0],
        },
        {
            "type": "text",
            "bbox": (110.0, 10.0, 190.0, 40.0),
            "angle": 0,
            "content": "right body",
            "_visual_row_ids": {1},
            "_local_line_bboxes": [(110.0, 30.0, 190.0, 40.0)],
            "_line_heights": [10.0],
        },
        {
            "type": "text",
            "bbox": (10.0, 50.0, 60.0, 60.0),
            "angle": 0,
            "content": "left tail",
            "_visual_row_ids": {2},
            "_local_line_bboxes": [(10.0, 50.0, 60.0, 60.0)],
            "_line_heights": [10.0],
        },
    ]

    merged = text_blocks._merge_spatial_text_components(
        blocks,
        (200.0, 100.0),
    )

    assert [block["content"] for block in merged] == [
        "left body left tail",
        "right body",
    ]

    isolated = text_blocks._merge_spatial_text_components(
        [blocks[0], blocks[2]],
        (200.0, 100.0),
    )
    assert [block["content"] for block in isolated] == [
        "left body",
        "left tail",
    ]


def test_repeated_leading_emphasis_and_tail_gap_split_structured_text() -> None:
    """Verify that repeated line-first emphasis only forms a structured body boundary when there is sufficient whitespace in the preceding line."""

    lines = [
        _text_line(
            "Purpose first section opener",
            (0.0, 0.0, 100.0, 10.0),
            0,
            leading_emphasis_width=12.0,
        ),
        _text_line("purpose tail.", (0.0, 10.0, 78.0, 20.0), 1),
        _text_line(
            "Methods second section opener",
            (0.0, 20.0, 100.0, 30.0),
            2,
            leading_emphasis_width=12.0,
        ),
        _text_line("methods tail.", (0.0, 30.0, 60.0, 40.0), 3),
        _text_line(
            "Results third section opener",
            (0.0, 40.0, 100.0, 50.0),
            4,
            leading_emphasis_width=12.0,
        ),
        _text_line("results tail.", (0.0, 50.0, 35.0, 60.0), 5),
        _text_line(
            "Conclusions fourth section opener",
            (0.0, 60.0, 100.0, 70.0),
            6,
            leading_emphasis_width=15.0,
        ),
        _text_line("conclusions tail.", (0.0, 70.0, 70.0, 80.0), 7),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (100.0, 100.0))

    assert [block["content"] for block in blocks] == [
        "Purpose first section opener purpose tail.",
        "Methods second section opener methods tail.",
        "Results third section opener results tail.",
        "Conclusions fourth section opener conclusions tail.",
    ]


def test_native_typography_caches_leading_emphasis_before_chars_are_cleared() -> None:
    """The short bold prefix width is cached before verifying characters are released, and subsequent text chunks can still be read."""

    chars = []
    for index, char in enumerate("Lead"):
        chars.append(
            {
                "char": char,
                "bbox": (float(index * 5), 0.0, float(index * 5 + 5), 10.0),
                "font": {"name": "LeadFont", "flags": 0, "weight": 600},
            }
        )
    for index, char in enumerate("body", start=5):
        chars.append(
            {
                "char": char,
                "bbox": (float(index * 5), 0.0, float(index * 5 + 5), 10.0),
                "font": {"name": "BodyFont", "flags": 0, "weight": 400},
            }
        )
    line = models._LineItem(
        text="Lead body",
        bbox=(0.0, 0.0, 45.0, 10.0),
        angle=0,
        source_index=0,
        chars=chars,
    )

    native_text._fill_native_typography(line, (100.0, 100.0))
    pipeline._compact_prepared_lines([line], (100.0, 100.0))

    assert line.leading_emphasis_width == 20.0
    assert not line.chars


def test_native_typography_caches_leading_font_family_run_without_weight_change() -> None:
    """Verify that standalone line starter font run of the same weight is still cached as the visual typesetting width."""

    chars = []
    for index, char in enumerate("Lead"):
        chars.append(
            {
                "char": char,
                "bbox": (float(index * 5), 0.0, float(index * 5 + 5), 10.0),
                "font": {"name": "LeadFont", "flags": 0, "weight": 400},
            }
        )
    for index, char in enumerate("body", start=5):
        chars.append(
            {
                "char": char,
                "bbox": (float(index * 5), 0.0, float(index * 5 + 5), 10.0),
                "font": {"name": "BodyFont", "flags": 0, "weight": 400},
            }
        )
    line = models._LineItem(
        text="Lead body",
        bbox=(0.0, 0.0, 45.0, 10.0),
        angle=0,
        source_index=0,
        chars=chars,
    )

    native_text._fill_native_typography(line, (100.0, 100.0))
    pipeline._compact_prepared_lines([line], (100.0, 100.0))

    assert line.leading_emphasis_width is None
    assert line.leading_typography_width == 20.0
    assert not line.chars


def test_image_adjacent_centered_short_to_wide_rows_split_without_text_markers() -> None:
    """Verify that the short centered row to the wide centered row below the image form independent blocks in a purely visual relationship."""

    first = _text_line("alpha", (35.0, 62.0, 65.0, 72.0), 0)
    second = _text_line("beta", (10.0, 74.0, 90.0, 84.0), 1)

    blocks = text_blocks._build_text_blocks(
        [first, second],
        [],
        (100.0, 100.0),
        visual_bboxes=[(5.0, 10.0, 95.0, 60.0)],
    )
    control = text_blocks._build_text_blocks(
        [first, second],
        [],
        (100.0, 100.0),
    )

    assert [block["content"] for block in blocks] == ["alpha", "beta"]
    assert [block["content"] for block in control] == ["alpha beta"]


def test_short_tail_and_leading_typography_run_split_structured_text() -> None:
    """Validate that wide lines after a short tail form new blocks only if the independent line start font run is present."""

    previous = _text_line("tail", (0.0, 0.0, 35.0, 10.0), 0)
    opener = _text_line(
        "opener body",
        (0.0, 12.0, 90.0, 22.0),
        1,
        leading_typography_width=12.0,
    )

    blocks = text_blocks._build_text_blocks(
        [previous, opener],
        [],
        (100.0, 100.0),
    )
    control = text_blocks._build_text_blocks(
        [previous, replace(opener, leading_typography_width=None)],
        [],
        (100.0, 100.0),
    )

    assert [block["content"] for block in blocks] == ["tail", "opener body"]
    assert [block["content"] for block in control] == ["tail opener body"]


def test_same_baseline_merge_preserves_paragraph_formula_context() -> None:
    """Verify that formula fallback context is not lost after peer shard merge."""

    fragments = [
        _text_line(
            "left",
            (0.0, 0.0, 40.0, 10.0),
            0,
            font_signature=("Math", 0),
            font_coverage=0.5,
            paragraph_formula_context=True,
        ),
        _text_line(
            "right",
            (40.1, 0.0, 80.0, 10.0),
            1,
            font_signature=("Math", 0),
            font_coverage=0.5,
        ),
    ]

    merged = line_merging._merge_same_baseline_text_lines(
        fragments,
        (100.0, 100.0),
        [],
    )

    assert len(merged) == 1
    assert merged[0].paragraph_formula_context


@pytest.mark.parametrize("row_count", [2, 3, 6])
def test_formula_style_text_rows_split_pairwise_without_count_gate(
    row_count: int,
) -> None:
    """Verifies that independent formula style text of any length starting from two lines is split on adjacent lines."""

    rows = [
        _text_line(
            f"row {index}",
            (20.0 - index, 14.0 * index, 80.0 + index, 14.0 * index + 10.0),
            index,
            font_signature=("Math", 0),
            font_coverage=0.5,
            paragraph_formula_context=True,
        )
        for index in range(row_count)
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(row, row.bbox) for row in rows],
    )

    break_sources = text_blocks._formula_style_text_row_break_sources(lane)

    assert break_sources == set(range(row_count))


@pytest.mark.parametrize(
    "second_bbox",
    [
        (35.0, 11.0, 65.0, 21.0),
        (20.0, 5.0, 80.0, 15.0),
    ],
    ids=["narrow-denominator", "overlapping-script-tier"],
)
def test_formula_style_text_rows_keep_connected_math_fragments(
    second_bbox: tuple[float, float, float, float],
) -> None:
    """Verify that narrow denominators and vertically overlapping hierarchies are not broken into independent text blocks."""

    rows = [
        _text_line(
            "formula body",
            (20.0, 0.0, 80.0, 10.0),
            0,
            paragraph_formula_context=True,
        ),
        _text_line(
            "math fragment",
            second_bbox,
            1,
            paragraph_formula_context=True,
        ),
    ]

    blocks = text_blocks._build_text_blocks(
        rows,
        [],
        (100.0, 100.0),
    )

    assert [block["content"] for block in blocks] == [
        "formula body math fragment",
    ]


def test_native_typography_separates_loose_height_from_canonical_em() -> None:
    """Validation exception loose character height does not cover the canonical scale formed by the PDF font size."""

    chars = [
        {
            "char": character,
            "bbox": (float(index * 6), 0.0, float(index * 6 + 6), 24.0),
            "font": {
                "name": "ABCDEF+Body",
                "flags": 0,
                "weight": 400,
                "size": 10.0,
            },
            "char_idx": index,
        }
        for index, character in enumerate("Body")
    ]
    line = models._LineItem(
        text="Body",
        bbox=(0.0, 0.0, 24.0, 24.0),
        angle=0,
        source_index=0,
        chars=chars,
        em_height=10.0,
        style_scale_repaired=True,
    )

    native_text._fill_native_typography(line, (100.0, 100.0))

    assert line.effective_height == 24.0
    assert line.em_height == 10.0
    assert line_layout._line_effective_height(line, line.bbox) == 10.0
    assert line_layout._line_layout_height(line, line.bbox) == 24.0


def test_canonical_visual_resplit_separates_cross_column_coarse_row() -> None:
    """Verify that repaired/tight character gaps can recut thick columns into unique visual run."""

    positions = (0.0, 6.0, 12.0, 18.0, 62.0, 68.0, 74.0, 80.0)
    chars = [
        {
            "char": character,
            "bbox": (position, 10.0, position + 20.0, 20.0),
            "font": {
                "name": "ABCDEF+Body",
                "flags": 0,
                "weight": 400,
                "size": 10.0,
            },
            "char_idx": index,
        }
        for index, (character, position) in enumerate(
            zip("ABCDEFGH", positions, strict=True),
        )
    ]
    visual_bboxes = {index: (position, 10.0, position + 5.0, 20.0) for index, position in enumerate(positions)}
    line = models._LineItem(
        text="ABCDEFGH",
        bbox=(0.0, 10.0, 100.0, 20.0),
        angle=0,
        source_index=5,
        chars=chars,
        visual_row_id=3,
    )

    output, resplits = native_text._resplit_native_visual_runs(
        [line],
        (100.0, 100.0),
        visual_bboxes,
        source_index_start=10,
    )

    assert [item.text for item in output] == ["ABCD", "EFGH"]
    assert [item.source_index for item in output] == [5, 10]
    assert all(item.visual_row_id == 3 for item in output)
    assert all(item.split_from_row for item in output)
    assert resplits[5].source is line
    assert resplits[5].members == tuple(output)


def test_two_leading_emphasis_rows_do_not_activate_structured_text_split() -> None:
    """Verify that less than three repetitions of the first line will not shred ordinary mixed text."""

    lines = [
        _text_line(
            "first emphasized opener",
            (0.0, 0.0, 100.0, 10.0),
            0,
            leading_emphasis_width=15.0,
        ),
        _text_line("first short tail", (0.0, 10.0, 60.0, 20.0), 1),
        _text_line(
            "second emphasized opener",
            (0.0, 20.0, 100.0, 30.0),
            2,
            leading_emphasis_width=15.0,
        ),
        _text_line("second short tail", (0.0, 30.0, 60.0, 40.0), 3),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (100.0, 100.0))

    assert len(blocks) == 1


def test_leading_emphasis_count_does_not_cross_semantic_title_boundary() -> None:
    """Verify that the number of emphasized first lines of different semantic sections cannot be combined to trigger structured body mode."""

    lines = [
        _text_line("first opener", (0.0, 0.0, 100.0, 10.0), 0, leading_emphasis_width=10.0),
        _text_line("first tail", (0.0, 10.0, 60.0, 20.0), 1),
        _text_line("second opener", (0.0, 20.0, 100.0, 30.0), 2, leading_emphasis_width=10.0),
        _text_line("second tail", (0.0, 30.0, 60.0, 40.0), 3),
        _text_line(
            "semantic title",
            (0.0, 50.0, 40.0, 60.0),
            4,
            semantic_type="paragraph_title",
        ),
        _text_line("third opener", (0.0, 70.0, 100.0, 80.0), 5, leading_emphasis_width=10.0),
        _text_line("third tail", (0.0, 80.0, 60.0, 90.0), 6),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (100.0, 100.0))

    first_region = next(block for block in blocks if "first opener" in block["content"])
    assert "second opener" in first_region["content"]


def test_page_footnote_visual_markers_attach_six_affiliation_entries() -> None:
    """Verify that the narrow number of the same row and the first and continuation lines of the unit on the right form independent footnote entries."""

    lines = [
        _text_line("contact name", (10.0, 0.0, 48.0, 10.0), 0, semantic_type="page_footnote"),
        _text_line("contact email", (10.0, 10.0, 55.0, 20.0), 1, semantic_type="page_footnote"),
    ]
    source_index = 2
    for marker in range(1, 7):
        top = 20.0 + (marker - 1) * 20.0
        visual_row_id = 100 + marker
        lines.extend(
            [
                _text_line(
                    str(marker),
                    (0.0, top, 3.0, top + 8.0),
                    source_index,
                    visual_row_id=visual_row_id,
                    run_index=0,
                    split_from_row=True,
                    median_glyph_width=3.0,
                    semantic_type="page_footnote",
                ),
                _text_line(
                    f"affiliation {marker} first row",
                    (10.0, top, 90.0, top + 10.0),
                    source_index + 1,
                    visual_row_id=visual_row_id,
                    run_index=1,
                    split_from_row=True,
                    median_glyph_width=4.0,
                    semantic_type="page_footnote",
                ),
                _text_line(
                    f"affiliation {marker} continuation",
                    (10.0, top + 10.0, 75.0, top + 20.0),
                    source_index + 2,
                    visual_row_id=visual_row_id + 100,
                    median_glyph_width=4.0,
                    semantic_type="page_footnote",
                ),
            ]
        )
        source_index += 3

    blocks = text_blocks._build_text_blocks(
        lines,
        [],
        (100.0, 160.0),
        page_footnote_groups=[{line.source_index for line in lines}],
    )

    assert [block["content"] for block in blocks[:2]] == [
        "contact name",
        "contact email",
    ]
    assert len(blocks[2:]) == 6
    for marker, block in enumerate(blocks[2:], start=1):
        assert block["content"].startswith(f"{marker} affiliation {marker} first row")
        assert f"affiliation {marker} continuation" in block["content"]


def test_local_aligned_column_replaces_polluted_full_width_lane() -> None:
    """Verify that the cross-column text will not cause subsequent stable single-column text to be split by the first line indentation rule."""

    lines = [
        _text_line("wide row one", (0.0, 0.0, 200.0, 10.0), 0),
        _text_line("wide row two", (0.0, 10.0, 200.0, 20.0), 1),
        _text_line("wide short tail", (0.0, 20.0, 150.0, 30.0), 2),
        _text_line(
            "Introduction",
            (110.0, 40.0, 150.0, 50.0),
            3,
            semantic_type="paragraph_title",
        ),
        _text_line("local first sentence.", (110.0, 55.0, 200.0, 65.0), 4),
        _text_line("local second sentence", (110.0, 65.0, 200.0, 75.0), 5),
        _text_line("local third sentence", (110.0, 75.0, 200.0, 85.0), 6),
        _text_line("local final sentence.", (110.0, 85.0, 195.0, 95.0), 7),
    ]

    blocks = text_blocks._build_text_blocks(lines, [], (200.0, 120.0))
    introduction_body = next(block for block in blocks if "local first sentence" in block["content"])

    assert introduction_body["content"] == (
        "local first sentence. local second sentence local third sentence local final sentence."
    )
    assert introduction_body["_lane_interval"] == (110.0, 200.0)


def test_typographic_gap_splits_caption_body_without_image_geometry() -> None:
    """Verify that unusual headroom and line height changes can create paragraph hard boundaries independent of image position."""

    caption = _text_line(
        "caption ends without punctuation",
        (0.0, 0.0, 100.0, 10.0),
        0,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    body = _text_line(
        "body starts",
        (0.0, 19.0, 100.0, 30.5),
        1,
        effective_height=11.5,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    lane = models._TextLane(left=0.0, right=100.0)

    assert not line_layout._should_connect_text_rows(
        (caption, caption.bbox),
        (body, body.bbox),
        lane,
        regular_gap=0.0,
        gap_mad=0.0,
        table_bboxes=[],
        axis_lines=[],
    )


def test_typographic_gap_requires_both_spacing_and_typography_evidence() -> None:
    """Verify that neither font size change alone nor medium headroom alone triggers the new paragraph barrier."""

    base = _text_line("base", (0.0, 0.0, 100.0, 10.0), 0, effective_height=10.0)
    larger_nearby = _text_line(
        "larger nearby",
        (0.0, 10.0, 100.0, 21.5),
        1,
        effective_height=11.5,
    )
    same_size_spaced = _text_line(
        "same size spaced",
        (0.0, 18.0, 100.0, 28.0),
        2,
        effective_height=10.0,
    )
    lane = models._TextLane(left=0.0, right=100.0)

    assert line_layout._should_connect_text_rows(
        (base, base.bbox),
        (larger_nearby, larger_nearby.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )
    assert line_layout._should_connect_text_rows(
        (base, base.bbox),
        (same_size_spaced, same_size_spaced.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_hyphenated_row_bypasses_typographic_gap_barrier() -> None:
    """Verify that typesetting hyphenation still works across font size and headroom changes within the existing proximity cap."""

    previous = _text_line("hyphen-", (0.0, 0.0, 100.0, 10.0), 0, effective_height=10.0)
    current = _text_line("ated", (0.0, 19.0, 100.0, 30.5), 1, effective_height=11.5)
    lane = models._TextLane(left=0.0, right=100.0)

    assert line_layout._should_connect_text_rows(
        (previous, previous.bbox),
        (current, current.bbox),
        lane,
        0.0,
        0.0,
        [],
        [],
    )


def test_caption_post_merge_respects_typographic_gap_barrier() -> None:
    """Verify that the image adjacency post-processing cannot cross the confirmed legend text layout boundary."""

    common = {
        "type": "text",
        "angle": 0,
        "_single_run_row_id": None,
        "_lane_interval": (0.0, 100.0),
        "_lane_is_span": False,
        "_font_signatures": {("Body", 0)},
    }
    caption = {
        **common,
        "bbox": (0.0, 50.0, 100.0, 60.0),
        "content": "Fig. 1 caption",
        "_visual_row_ids": {1},
        "_local_line_bboxes": [(0.0, 50.0, 100.0, 60.0)],
        "_line_heights": [10.0],
    }
    body = {
        **common,
        "bbox": (0.0, 69.0, 100.0, 80.5),
        "content": "body starts",
        "_visual_row_ids": {2},
        "_local_line_bboxes": [(0.0, 69.0, 100.0, 80.5)],
        "_line_heights": [11.5],
    }

    merged = text_blocks._merge_image_caption_text_blocks(
        [caption, body],
        [(0.0, 10.0, 100.0, 50.0)],
    )

    assert [block["content"] for block in merged] == [
        "Fig. 1 caption",
        "body starts",
    ]


def test_repeated_compact_title_continuations_merge_across_font_switch() -> None:
    """Verify that two repeated lines of weak headings can be merged with the immediately following continuation lines in different fonts and restored to the main text."""

    blocks: list[dict[str, object]] = []
    for offset, label in ((0.0, "first"), (45.0, "second")):
        blocks.extend(
            [
                {
                    "type": "paragraph_title",
                    "bbox": (10.0, 10.0 + offset, 65.0, 30.0 + offset),
                    "angle": 0,
                    "content": f"{label} name {label} address",
                    "_visual_row_ids": {1, 2},
                    "_single_run_row_id": None,
                    "_local_line_bboxes": [
                        (10.0, 10.0 + offset, 50.0, 20.0 + offset),
                        (10.0, 20.0 + offset, 65.0, 30.0 + offset),
                    ],
                    "_line_heights": [10.0, 10.0],
                    "_font_signatures": {("Title", 0)},
                    "_lane_interval": (10.0, 100.0),
                    "_lane_is_span": False,
                },
                {
                    "type": "text",
                    "bbox": (10.0, 31.0 + offset, 80.0, 41.0 + offset),
                    "angle": 0,
                    "content": f"{label} contact",
                    "_visual_row_ids": {3},
                    "_single_run_row_id": None,
                    "_local_line_bboxes": [
                        (10.0, 31.0 + offset, 80.0, 41.0 + offset),
                    ],
                    "_line_heights": [10.0],
                    "_font_signatures": {("Body", 0)},
                    "_lane_interval": (10.0, 100.0),
                    "_lane_is_span": False,
                },
            ]
        )

    merged = text_blocks._merge_repeated_compact_title_continuations(
        blocks,
        (200.0, 200.0),
    )

    assert [block["type"] for block in merged] == ["text", "text"]
    assert [block["content"] for block in merged] == [
        "first name first address first contact",
        "second name second address second contact",
    ]


def test_compact_title_continuation_requires_repetition_and_font_switch() -> None:
    """Verify that a single set of structures or continuation lines in the same font do not cross true heading boundaries."""

    title = {
        "type": "paragraph_title",
        "bbox": (10.0, 10.0, 65.0, 30.0),
        "angle": 0,
        "content": "title",
        "_visual_row_ids": {1, 2},
        "_single_run_row_id": None,
        "_local_line_bboxes": [
            (10.0, 10.0, 50.0, 20.0),
            (10.0, 20.0, 65.0, 30.0),
        ],
        "_line_heights": [10.0, 10.0],
        "_font_signatures": {("Title", 0)},
        "_lane_interval": (10.0, 100.0),
        "_lane_is_span": False,
    }
    continuation = {
        "type": "text",
        "bbox": (10.0, 31.0, 80.0, 41.0),
        "angle": 0,
        "content": "body",
        "_visual_row_ids": {3},
        "_single_run_row_id": None,
        "_local_line_bboxes": [(10.0, 31.0, 80.0, 41.0)],
        "_line_heights": [10.0],
        "_font_signatures": {("Body", 0)},
        "_lane_interval": (10.0, 100.0),
        "_lane_is_span": False,
    }

    unique = text_blocks._merge_repeated_compact_title_continuations(
        [title, continuation],
        (200.0, 200.0),
    )
    same_font = text_blocks._merge_repeated_compact_title_continuations(
        [
            title,
            {**continuation, "_font_signatures": {("Title", 0)}},
            {**title, "bbox": (10.0, 55.0, 65.0, 75.0)},
            {
                **continuation,
                "bbox": (10.0, 76.0, 80.0, 86.0),
                "_font_signatures": {("Title", 0)},
            },
        ],
        (200.0, 200.0),
    )

    assert [block["type"] for block in unique] == ["paragraph_title", "text"]
    assert [block["type"] for block in same_font] == [
        "paragraph_title",
        "text",
        "paragraph_title",
        "text",
    ]


def test_caption_neighbor_uses_union_of_aligned_image_row() -> None:
    """Verification that three side-by-side images additionally form the union bbox for unified caption adjacency use."""

    grouped = text_blocks._caption_image_group_bboxes(
        [
            (10.0, 10.0, 35.0, 40.0),
            (37.0, 10.0, 62.0, 40.0),
            (64.0, 10.0, 90.0, 40.0),
        ],
        median_height=10.0,
    )

    assert (10.0, 10.0, 90.0, 40.0) in grouped


def test_inline_math_fragments_merge_into_wide_split_text_row() -> None:
    """Verify that multiple small math blocks stacked on top of a wide text line converge into one text block."""

    host = {
        "type": "text",
        "bbox": (10.0, 40.0, 90.0, 50.0),
        "angle": 0,
        "content": "body host",
        "_visual_row_ids": {1},
        "_single_run_row_id": 1,
        "_local_line_bboxes": [(10.0, 40.0, 90.0, 50.0)],
        "_line_heights": [10.0],
        "_font_signatures": {("Body", 0)},
    }
    fragments = [
        {
            "type": "text",
            "bbox": bbox,
            "angle": 0,
            "content": content,
            "_visual_row_ids": {index + 2},
            "_single_run_row_id": None,
            "_local_line_bboxes": [bbox],
            "_line_heights": [bbox[3] - bbox[1]],
            "_font_signatures": {("Math", 0)},
        }
        for index, (bbox, content) in enumerate(
            (
                ((30.0, 34.0, 40.0, 43.0), "numerator"),
                ((30.0, 47.0, 40.0, 56.0), "denominator"),
                ((60.0, 35.0, 70.0, 44.0), "power"),
            )
        )
    ]

    merged = text_blocks._merge_inline_math_fragment_text_blocks(
        [host, *fragments],
        (200.0, 100.0),
    )

    assert len(merged) == 1
    assert merged[0]["bbox"] == (10.0, 34.0, 90.0, 56.0)
    assert all(token in merged[0]["content"] for token in ("body host", "numerator", "denominator", "power"))
    assert merged[0][text_blocks._INLINE_MATH_RECOVERY_MARKER] is True


def test_residual_narrow_math_fragment_merges_into_unique_wide_host() -> None:
    """Verify that a single narrow math fragment retracts a unique host when it overlaps a wide text line."""

    host = {
        "type": "text",
        "bbox": (10.0, 40.0, 90.0, 52.0),
        "angle": 0,
        "content": "body host",
        "_local_line_bboxes": [(10.0, 40.0, 90.0, 52.0)],
        "_line_heights": [12.0],
    }
    fragment = {
        "type": "text",
        "bbox": (50.0, 48.0, 54.0, 57.0),
        "angle": 0,
        "content": "gamma",
        "_local_line_bboxes": [(50.0, 48.0, 54.0, 57.0)],
        "_line_heights": [9.0],
    }

    merged = text_blocks._merge_residual_narrow_math_text_blocks(
        [host, fragment],
        (200.0, 100.0),
    )

    assert len(merged) == 1
    assert merged[0]["bbox"] == (10.0, 40.0, 90.0, 57.0)
    assert "body host" in merged[0]["content"]
    assert "gamma" in merged[0]["content"]
    assert merged[0][text_blocks._INLINE_MATH_RECOVERY_MARKER] is True


def test_hostless_inline_math_fragments_keep_recovery_marker() -> None:
    """Verify that math shards for non-wide hosts also retain internal recovery markers after merging."""

    fragments = [
        {
            "type": "text",
            "bbox": bbox,
            "angle": 0,
            "content": f"fragment {index}",
            "_local_line_bboxes": [bbox],
            "_line_heights": [10.0],
            "_font_signatures": {("Body" if index < 2 else "Math", 0)},
        }
        for index, bbox in enumerate(
            (
                (20.0, 40.0, 60.0, 50.0),
                (62.0, 40.0, 100.0, 50.0),
                (20.0, 52.0, 55.0, 62.0),
                (57.0, 52.0, 100.0, 62.0),
            )
        )
    ]

    merged = text_blocks._merge_hostless_inline_math_fragment_blocks(
        fragments,
        (200.0, 100.0),
    )

    assert len(merged) == 1
    assert merged[0][text_blocks._INLINE_MATH_RECOVERY_MARKER] is True


def _inline_math_paragraph_test_blocks(
    marker_indices: tuple[int, ...] = (0, 2),
) -> list[dict[str, object]]:
    """Construct five consecutive wide text blocks in the same column for reuse in the mathematics paragraph closure test."""

    bboxes = (
        (20.0, 40.0, 172.0, 70.0),
        (20.0, 74.0, 172.0, 94.0),
        (20.0, 99.0, 172.0, 129.0),
        (20.0, 122.0, 172.0, 145.0),
        (20.0, 145.0, 172.0, 175.0),
    )
    blocks: list[dict[str, object]] = []
    for index, bbox in enumerate(bboxes):
        block: dict[str, object] = {
            "type": "text",
            "bbox": bbox,
            "angle": 0,
            "content": f"paragraph part {index}",
            "_local_line_bboxes": [bbox],
            "_line_heights": [10.0],
            "_font_signatures": {("Body", 0)},
            "_lane_interval": (20.0, 180.0),
            "_lane_is_span": False,
        }
        if index in marker_indices:
            block[text_blocks._INLINE_MATH_RECOVERY_MARKER] = True
        blocks.append(block)
    return blocks


def test_inline_math_paragraph_continuations_merge_five_wide_blocks() -> None:
    """Verify that at least two mathematical recovery blocks can drive five paragraphs of text in the same column to be closed into one block."""

    merged = text_blocks._merge_inline_math_paragraph_continuations(
        _inline_math_paragraph_test_blocks(),
        (300.0, 300.0),
    )

    assert len(merged) == 1
    assert merged[0]["bbox"] == (20.0, 40.0, 172.0, 175.0)
    assert all(f"paragraph part {index}" in str(merged[0]["content"]) for index in range(5))
    assert merged[0][text_blocks._INLINE_MATH_RECOVERY_MARKER] is True


def test_inline_math_paragraph_does_not_merge_ordinary_paragraphs() -> None:
    """Verify that ordinary continuous paragraphs are not merged when two math recovery anchors are missing."""

    merged = text_blocks._merge_inline_math_paragraph_continuations(
        _inline_math_paragraph_test_blocks(marker_indices=(0,)),
        (300.0, 300.0),
    )

    assert len(merged) == 5


@pytest.mark.parametrize("variant", ["different_lane", "large_gap", "font_conflict"])
def test_inline_math_paragraph_rejects_geometry_or_font_conflict(
    variant: str,
) -> None:
    """Verify that crossing columns, excessive spacing, or font conflicts can break math body paragraph chains."""

    blocks = _inline_math_paragraph_test_blocks()
    if variant == "different_lane":
        for block in blocks[2:]:
            left, top, right, bottom = block["bbox"]
            shifted = (left + 170.0, top, right + 170.0, bottom)
            block["bbox"] = shifted
            block["_local_line_bboxes"] = [shifted]
            block["_lane_interval"] = (190.0, 350.0)
    elif variant == "large_gap":
        for block in blocks[2:]:
            left, top, right, bottom = block["bbox"]
            shifted = (left, top + 20.0, right, bottom + 20.0)
            block["bbox"] = shifted
            block["_local_line_bboxes"] = [shifted]
    else:
        for block in blocks[2:]:
            block["_font_signatures"] = {("Other", 0)}

    merged = text_blocks._merge_inline_math_paragraph_continuations(
        blocks,
        (400.0, 300.0),
    )

    assert len(merged) == 5


def test_inline_math_paragraph_does_not_cross_footer_boundary() -> None:
    """Verification footer located between adjacent text blocks blocks math paragraph closure."""

    blocks = _inline_math_paragraph_test_blocks()
    footer_bbox = (20.0, 95.0, 172.0, 98.0)
    footer = {
        "type": "footer",
        "bbox": footer_bbox,
        "angle": 0,
        "content": "footer",
        "_local_line_bboxes": [footer_bbox],
        "_line_heights": [3.0],
        "_font_signatures": {("Body", 0)},
        "_lane_interval": (20.0, 180.0),
        "_lane_is_span": False,
    }

    merged = text_blocks._merge_inline_math_paragraph_continuations(
        [*blocks[:2], footer, *blocks[2:]],
        (300.0, 300.0),
    )

    assert len(merged) == 6
    assert sum(block["type"] == "footer" for block in merged) == 1


def test_overlapping_wide_text_rows_merge_without_broad_page_line_merge() -> None:
    """Verify that overlapping lines with left-edge wide text can be merged at the block level and do not rely on full-page line merging."""

    first = {
        "type": "text",
        "bbox": (20.0, 40.0, 140.0, 52.0),
        "angle": 0,
        "content": "first row",
        "_local_line_bboxes": [(20.0, 40.0, 140.0, 52.0)],
        "_line_heights": [12.0],
        "_font_signatures": {("Body", 0)},
    }
    second = {
        "type": "text",
        "bbox": (20.0, 42.0, 180.0, 65.0),
        "angle": 0,
        "content": "second row continuation",
        "_local_line_bboxes": [
            (100.0, 42.0, 180.0, 53.0),
            (20.0, 54.0, 180.0, 65.0),
        ],
        "_line_heights": [11.0, 11.0],
        "_font_signatures": {("Body", 0)},
    }

    merged = text_blocks._merge_overlapping_same_line_text_blocks(
        [first, second],
        (300.0, 200.0),
    )

    assert len(merged) == 1
    assert merged[0]["bbox"] == (20.0, 40.0, 180.0, 65.0)
    assert "first row" in merged[0]["content"]
    assert "second row continuation" in merged[0]["content"]


def test_boundary_visual_row_merges_two_and_three_row_text_blocks() -> None:
    """Verify that two-line blocks and three-line blocks can be merged when the last line of the previous block and the first line of the following block are spliced."""

    first = {
        "type": "text",
        "bbox": (20.0, 40.0, 280.0, 64.0),
        "angle": 0,
        "content": "first block ending",
        "_local_line_bboxes": [
            (20.0, 40.0, 280.0, 51.0),
            (20.0, 52.0, 200.0, 64.0),
        ],
        "_line_heights": [11.0, 12.0],
        "_font_signatures": {("Body", 0)},
    }
    second = {
        "type": "text",
        "bbox": (20.0, 54.0, 280.0, 90.0),
        "angle": 0,
        "content": "continued on the same visual row",
        "_local_line_bboxes": [
            (204.0, 54.0, 280.0, 64.0),
            (20.0, 66.0, 280.0, 77.0),
            (20.0, 79.0, 280.0, 90.0),
        ],
        "_line_heights": [10.0, 11.0, 11.0],
        "_font_signatures": {("Body", 0)},
    }

    merged = text_blocks._merge_overlapping_same_line_text_blocks(
        [first, second],
        (300.0, 200.0),
    )

    assert len(merged) == 1
    assert "first block ending" in merged[0]["content"]
    assert "continued on the same visual row" in merged[0]["content"]


@pytest.mark.parametrize(
    ("boundary_left", "second_font"),
    [(220.0, ("Body", 0)), (204.0, ("Other", 0))],
)
def test_boundary_visual_row_rejects_large_gap_or_font_conflict(
    boundary_left: float,
    second_font: tuple[str, int],
) -> None:
    """Text blocks are not merged when the horizontal spacing of verification boundary lines is too large or the font collection conflicts."""

    first = {
        "type": "text",
        "bbox": (20.0, 40.0, 280.0, 64.0),
        "angle": 0,
        "content": "first",
        "_local_line_bboxes": [
            (20.0, 40.0, 280.0, 51.0),
            (20.0, 52.0, 200.0, 64.0),
        ],
        "_line_heights": [11.0, 12.0],
        "_font_signatures": {("Body", 0)},
    }
    second = {
        "type": "text",
        "bbox": (20.0, 54.0, 280.0, 90.0),
        "angle": 0,
        "content": "second",
        "_local_line_bboxes": [
            (boundary_left, 54.0, 280.0, 64.0),
            (20.0, 66.0, 280.0, 77.0),
            (20.0, 79.0, 280.0, 90.0),
        ],
        "_line_heights": [10.0, 11.0, 11.0],
        "_font_signatures": {second_font},
    }

    merged = text_blocks._merge_overlapping_same_line_text_blocks(
        [first, second],
        (300.0, 200.0),
    )

    assert len(merged) == 2


def test_front_matter_grid_merges_each_author_column_independently() -> None:
    """The four-column rule preamble information after the verification title is merged by columns and not serialized horizontally."""

    title = {
        "type": "doc_title",
        "bbox": (200.0, 100.0, 800.0, 130.0),
        "angle": 0,
        "content": "document title",
        "_visual_row_ids": {0},
        "_single_run_row_id": None,
        "_local_line_bboxes": [(200.0, 100.0, 800.0, 130.0)],
        "_line_heights": [30.0],
        "_font_signatures": {("Title", 0)},
    }
    author_parts = []
    for column_index, left in enumerate((75.0, 325.0, 575.0, 825.0)):
        for row_index, top in enumerate((150.0, 165.0, 180.0)):
            bbox = (left, top, left + 100.0, top + 10.0)
            author_parts.append(
                {
                    "type": "text",
                    "bbox": bbox,
                    "angle": 0,
                    "content": f"author-{column_index}-row-{row_index}",
                    "_visual_row_ids": {10 + row_index},
                    "_single_run_row_id": None,
                    "_local_line_bboxes": [bbox],
                    "_line_heights": [10.0],
                    "_font_signatures": {("Body", 0)},
                }
            )

    merged = text_blocks._merge_front_matter_column_blocks(
        [title, *author_parts],
        (1000.0, 1000.0),
        page_index=0,
    )
    merged_authors = [block for block in merged if block["type"] == "text"]

    assert len(merged_authors) == 4
    for column_index, block in enumerate(merged_authors):
        assert all(f"author-{column_index}-row-{row_index}" in block["content"] for row_index in range(3))
