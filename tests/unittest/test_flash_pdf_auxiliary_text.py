from __future__ import annotations

import inspect

import pytest
from _flash_pdf_test_utils import (
    _prepared_text_page,
    _text_line,
)

from docvortex.analyzers.native.pdf import auxiliary_text, models, pipeline, text_blocks


def test_prepared_table_lines_reuse_by_angle_and_input_identity(monkeypatch) -> None:
    """Axis lines are only converted once on the same page and in the same direction, and will be recalculated when the direction or line set is changed."""

    page = _prepared_text_page(page_size=(1000.0, 1000.0))
    page.drawing_lines = [models._AxisLine(bbox=(10.0, 20.0, 90.0, 21.0), width=1.0, orientation="horizontal")]
    page.table_bboxes = [(5.0, 15.0, 95.0, 25.0)]
    original = auxiliary_text._transform_axis_lines
    calls = []

    def counting(drawing_lines, page_size, angle):
        """Record the number of real axis transformations while retaining the transformation results."""
        calls.append(angle)
        return original(drawing_lines, page_size, angle)

    monkeypatch.setattr(auxiliary_text, "_transform_axis_lines", counting)
    first = auxiliary_text._prepared_local_axis_table_lines(page, 0)
    assert auxiliary_text._prepared_local_axis_table_lines(page, 0) is first
    assert len(first[1]) == 1
    auxiliary_text._prepared_local_axis_table_lines(page, 90)
    page.drawing_lines = list(page.drawing_lines)
    auxiliary_text._prepared_local_axis_table_lines(page, 0)
    assert calls == [0, 90, 0]


def test_page_footnote_uses_separator_and_stops_before_distant_footer_text() -> None:
    """Verify that single-column footers are triggered by a horizontal line at the bottom of the page and stop expanding before a larger line gap."""

    lines = [
        _text_line("body one", (100.0, 100.0, 900.0, 110.0), 0),
        _text_line("body two", (100.0, 130.0, 900.0, 140.0), 1),
        _text_line("body three", (100.0, 160.0, 900.0, 170.0), 2),
        _text_line("note one", (100.0, 770.0, 500.0, 780.0), 3),
        _text_line("note two", (100.0, 788.0, 500.0, 798.0), 4),
        _text_line("note three", (100.0, 806.0, 500.0, 816.0), 5),
        _text_line("footer text", (100.0, 850.0, 600.0, 860.0), 6),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [
        models._AxisLine(
            bbox=(100.0, 750.0, 260.0, 752.0),
            width=1.0,
            orientation="horizontal",
        ),
        models._AxisLine(
            bbox=(100.0, 750.5, 260.0, 752.5),
            width=1.0,
            orientation="horizontal",
        ),
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert [line.semantic_type for line in lines] == [
        None,
        None,
        None,
        "page_footnote",
        "page_footnote",
        "page_footnote",
        None,
    ]
    assert page.page_footnote_groups == [{3, 4, 5}]


@pytest.mark.parametrize(
    ("page_index", "page_count"),
    [(0, 1), (1, 3)],
)
def test_page_footnote_trailing_footer_applies_on_any_page(
    page_index: int,
    page_count: int,
) -> None:
    """Verify that a compact tail segment within the footer projection of a single page or non-home page that crosses the line continuation threshold can be marked as a footer."""

    body_lines = [
        _text_line(
            f"body {index}",
            (100.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(3)
    ]
    footnote_lines = [
        _text_line(
            "note one",
            (100.0, 770.0, 800.0, 778.0),
            3,
            effective_height=8.0,
            semantic_type="page_footnote",
        ),
        _text_line(
            "note two",
            (100.0, 790.0, 500.0, 798.0),
            4,
            effective_height=8.0,
            semantic_type="page_footnote",
        ),
        _text_line(
            "note three",
            (100.0, 810.0, 450.0, 818.0),
            5,
            effective_height=8.0,
            semantic_type="page_footnote",
        ),
    ]
    footer_lines = [
        _text_line(
            "footer first",
            (100.0, 834.0, 600.0, 842.0),
            6,
            effective_height=8.0,
        ),
        _text_line(
            "footer second",
            (100.0, 846.0, 400.0, 854.0),
            7,
            effective_height=8.0,
        ),
    ]
    page = _prepared_text_page(
        *body_lines,
        *footnote_lines,
        *footer_lines,
        page_size=(1000.0, 1000.0),
    )
    page.page_footnote_groups = [{3, 4, 5}]

    pages = [_prepared_text_page() for _index in range(page_count)]
    pages[page_index] = page
    auxiliary_text._classify_page_footnote_trailing_footers(pages)

    assert [line.semantic_type for line in footnote_lines] == [
        "page_footnote",
    ] * 3
    assert [line.semantic_type for line in footer_lines] == ["footer"] * 2


@pytest.mark.parametrize(
    "case",
    [
        "other_lane",
        "within_continuation",
        "body_sized",
        "split_tail",
        "container_overlap",
        "missing_anchor",
    ],
)
def test_page_footnote_trailing_footer_rejects_weak_geometry(case: str) -> None:
    """Verify that cross-column text, weak separation, text scale, multiple tail paragraphs and container overlap cannot be guessed as footers."""

    body_lines = [
        _text_line(
            f"body {index}",
            (100.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(3)
    ]
    footnote_lines = [
        _text_line(
            f"note {index}",
            (100.0, 770.0 + 20.0 * index, 500.0, 778.0 + 20.0 * index),
            3 + index,
            effective_height=8.0,
            semantic_type="page_footnote",
        )
        for index in range(3)
    ]
    first_top, second_top = (828.0, 840.0) if case == "within_continuation" else (834.0, 846.0)
    if case == "split_tail":
        second_top = 870.0
    tail_height = 10.0 if case == "body_sized" else 8.0
    tail_lines = [
        _text_line(
            "tail first",
            (100.0, first_top, 600.0, first_top + tail_height),
            6,
            effective_height=tail_height,
        ),
        _text_line(
            "tail second",
            (100.0, second_top, 400.0, second_top + tail_height),
            7,
            effective_height=tail_height,
        ),
    ]
    extra_lines = (
        [
            _text_line(
                "right column body",
                (700.0, 835.0, 950.0, 845.0),
                8,
                effective_height=10.0,
            )
        ]
        if case == "other_lane"
        else []
    )
    page = _prepared_text_page(
        *body_lines,
        *footnote_lines,
        *tail_lines,
        *extra_lines,
        page_size=(1000.0, 1000.0),
    )
    if case != "missing_anchor":
        page.page_footnote_groups = [{3, 4, 5}]
    if case == "container_overlap":
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": (80.0, 825.0, 620.0, 860.0),
                "angle": 0,
                "content": "",
            }
        ]

    auxiliary_text._classify_page_footnote_trailing_footers([page])

    assert all(line.semantic_type is None for line in tail_lines)
    assert all(line.semantic_type is None for line in extra_lines)


def test_image_footnote_requires_image_rule_and_smaller_text() -> None:
    """Verification of chart footnotes must include pictures, long horizontal lines at the bottom, and evidence of font size shrinkage."""

    lines = [
        _text_line(
            f"body {index}",
            (50.0, 40.0 + 20.0 * index, 450.0, 50.0 + 20.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    note = _text_line(
        "chart source",
        (50.0, 325.0, 180.0, 333.0),
        4,
        effective_height=8.0,
    )
    lines.append(note)
    page = _prepared_text_page(*lines, page_size=(500.0, 500.0))
    page.fixed_blocks = [
        {
            "type": "image",
            "bbox": (50.0, 140.0, 300.0, 315.0),
            "angle": 0,
            "content": "",
        }
    ]
    page.drawing_lines = [models._AxisLine((50.0, 318.0, 300.0, 319.0), 1.0, "horizontal")]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert note.semantic_type == "footnote"
    assert page.page_footnote_groups == []


def test_sparse_image_footnotes_retry_with_document_body_height() -> None:
    """Verify that sparse image pages can recover two figure footnotes contaminated by their own font sizes using full text text size."""

    caption = _text_line(
        "chart caption",
        (50.0, 40.0, 300.0, 48.0),
        0,
        effective_height=8.0,
    )
    notes = [
        _text_line(
            "left source",
            (50.0, 255.0, 300.0, 262.0),
            1,
            effective_height=7.0,
        ),
        _text_line(
            "right source",
            (550.0, 255.0, 800.0, 262.0),
            2,
            effective_height=7.0,
        ),
    ]
    page = _prepared_text_page(caption, *notes, page_size=(1000.0, 500.0))
    page.fixed_blocks = [
        {"type": "image", "bbox": bbox, "angle": 0, "content": ""}
        for bbox in [
            (50.0, 80.0, 450.0, 250.0),
            (550.0, 80.0, 950.0, 250.0),
        ]
    ]
    page.drawing_lines = [
        models._AxisLine((50.0, 252.0, 450.0, 253.0), 1.0, "horizontal"),
        models._AxisLine((550.0, 252.0, 950.0, 253.0), 1.0, "horizontal"),
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert [line.semantic_type for line in notes] == [None, None]

    auxiliary_text._classify_deferred_image_footnotes([page], body_height=10.0)

    assert [line.semantic_type for line in notes] == ["footnote", "footnote"]
    assert page.page_footnote_groups == []


@pytest.mark.parametrize(
    "missing_evidence",
    ["image", "rule", "small_text", "table_rule"],
)
def test_image_footnote_rejects_incomplete_visual_evidence(
    missing_evidence: str,
) -> None:
    """When verifying that any joint evidence is missing, ordinary text below the figure will not be promoted to figure footnotes."""

    body = [
        _text_line(
            f"body {index}",
            (50.0, 40.0 + 20.0 * index, 450.0, 50.0 + 20.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    note = _text_line(
        "candidate",
        (50.0, 325.0, 180.0, 333.0),
        4,
        effective_height=10.0 if missing_evidence == "small_text" else 8.0,
    )
    page = _prepared_text_page(*body, note, page_size=(500.0, 500.0))
    if missing_evidence != "image":
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": (50.0, 140.0, 300.0, 315.0),
                "angle": 0,
                "content": "",
            }
        ]
    if missing_evidence != "rule":
        page.drawing_lines = [models._AxisLine((50.0, 318.0, 300.0, 319.0), 1.0, "horizontal")]
    if missing_evidence == "table_rule":
        page.table_bboxes = [(40.0, 300.0, 310.0, 340.0)]

    auxiliary_text._classify_page_auxiliary_text(page)
    auxiliary_text._classify_deferred_image_footnotes([page], body_height=10.0)

    assert note.semantic_type is None


def test_page_footnote_supports_independent_column_rules() -> None:
    """Verify that the respective dashes in the left and right columns only claim consecutive footnotes in this column."""

    lines = [
        *[
            _text_line(f"left body {index}", (80.0, 100.0 + 30.0 * index, 460.0, 110.0 + 30.0 * index), index)
            for index in range(3)
        ],
        *[
            _text_line(
                f"right body {index}",
                (540.0, 100.0 + 30.0 * index, 920.0, 110.0 + 30.0 * index),
                index + 3,
            )
            for index in range(3)
        ],
        _text_line("left note one", (80.0, 820.0, 400.0, 830.0), 6),
        _text_line("left note two", (80.0, 838.0, 400.0, 848.0), 7),
        _text_line("right note one", (540.0, 820.0, 860.0, 830.0), 8),
        _text_line("right note two", (540.0, 838.0, 860.0, 848.0), 9),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [
        models._AxisLine((80.0, 800.0, 220.0, 802.0), 1.0, "horizontal"),
        models._AxisLine((540.0, 800.0, 680.0, 802.0), 1.0, "horizontal"),
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in lines[:6])
    assert all(line.semantic_type == "page_footnote" for line in lines[6:])
    assert page.page_footnote_groups == [{6, 7}, {8, 9}]


def test_page_footnote_accepts_slightly_left_shifted_rule_with_two_small_rows() -> None:
    """Verify that a dash slightly earlier than the left edge of the column confirms footnotes by high coverage and continuous lines of small font size."""

    body = [
        _text_line(
            f"body {index}",
            (122.0, 180.0 + 90.0 * index, 622.0, 190.0 + 90.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    notes = [
        _text_line(
            "note one",
            (122.0, 862.0, 550.0, 870.0),
            4,
            effective_height=8.0,
        ),
        _text_line(
            "note two",
            (122.0, 874.0, 520.0, 882.0),
            5,
            effective_height=8.0,
        ),
    ]
    page = _prepared_text_page(
        *body,
        *notes,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (100.0, 850.0, 300.0, 852.0),
            1.0,
            "horizontal",
        )
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in body)
    assert all(line.semantic_type == "page_footnote" for line in notes)
    assert page.page_footnote_groups == [{4, 5}]


def test_page_footnote_rejects_left_shifted_rule_with_single_or_body_size_row() -> None:
    """Verify that footnotes are not triggered when a slightly left-skewed horizontal line is missing two lines or there is evidence of font size shrinkage."""

    for note_rows, note_height in ((1, 8.0), (2, 10.0)):
        lines = [
            *[
                _text_line(
                    f"body {index}",
                    (122.0, 180.0 + 90.0 * index, 622.0, 190.0 + 90.0 * index),
                    index,
                    effective_height=10.0,
                )
                for index in range(4)
            ],
            *[
                _text_line(
                    f"note {index}",
                    (122.0, 862.0 + 12.0 * index, 550.0, 862.0 + 12.0 * index + note_height),
                    4 + index,
                    effective_height=note_height,
                )
                for index in range(note_rows)
            ],
        ]
        page = _prepared_text_page(
            *lines,
            page_size=(1000.0, 1000.0),
        )
        page.drawing_lines = [
            models._AxisLine(
                (100.0, 850.0, 300.0, 852.0),
                1.0,
                "horizontal",
            )
        ]

        auxiliary_text._classify_page_auxiliary_text(page)

        assert page.page_footnote_groups == []
        assert all(line.semantic_type is None for line in lines)


def test_page_footnote_accepts_centered_short_rule_with_small_rows() -> None:
    """A short horizontal line in the center of the verification column can confirm the footnote based on the page position and small font size in continuous lines."""

    body = [
        _text_line(
            f"body {index}",
            (100.0, 180.0 + 80.0 * index, 500.0, 190.0 + 80.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    notes = [
        _text_line(
            "note one",
            (100.0, 862.0, 490.0, 870.0),
            4,
            effective_height=8.0,
        ),
        _text_line(
            "note two",
            (100.0, 874.0, 460.0, 882.0),
            5,
            effective_height=8.0,
        ),
    ]
    page = _prepared_text_page(
        *body,
        *notes,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (200.0, 850.0, 400.0, 852.0),
            1.0,
            "horizontal",
        )
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in body)
    assert all(line.semantic_type == "page_footnote" for line in notes)
    assert page.page_footnote_groups == [{4, 5}]


def test_page_footnote_rejects_centered_fraction_rule_without_upper_clearance() -> None:
    """Verify that centered horizontal lines sandwiched between upper and lower formula levels do not trigger footers."""

    body = [
        _text_line(
            f"body {index}",
            (100.0, 180.0 + 80.0 * index, 500.0, 190.0 + 80.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    formula_rows = [
        _text_line(
            "formula numerator",
            (100.0, 842.0, 500.0, 852.0),
            4,
            effective_height=10.0,
        ),
        _text_line(
            "formula denominator",
            (100.0, 854.0, 490.0, 862.0),
            5,
            effective_height=8.0,
        ),
        _text_line(
            "formula tail",
            (100.0, 866.0, 460.0, 874.0),
            6,
            effective_height=8.0,
        ),
    ]
    page = _prepared_text_page(
        *body,
        *formula_rows,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (200.0, 850.0, 400.0, 852.0),
            1.0,
            "horizontal",
        )
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert page.page_footnote_groups == []
    assert all(line.semantic_type is None for line in formula_rows)


def test_page_footnote_accepts_lower_half_column_width_rule_with_smaller_text() -> None:
    """Verify that the column width horizontal lines in the lower half of the page can be used to identify single-column footnotes by shrinking the font size."""

    lines = [
        _text_line(
            f"body {index}",
            (100.0, 180.0 + 24.0 * index, 900.0, 190.0 + 24.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    lines.extend(
        [
            _text_line(
                "note one",
                (100.0, 620.0, 650.0, 628.0),
                4,
                effective_height=8.0,
            ),
            _text_line(
                "note two",
                (100.0, 632.0, 620.0, 640.0),
                5,
                effective_height=8.0,
            ),
        ]
    )
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [models._AxisLine((100.0, 600.0, 900.0, 602.0), 1.0, "horizontal")]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in lines[:4])
    assert [line.semantic_type for line in lines[4:]] == [
        "page_footnote",
        "page_footnote",
    ]
    assert page.page_footnote_groups == [{4, 5}]


def test_page_footnote_stops_at_next_aligned_column_rule() -> None:
    """Verify that the second dividing line in the same column will split the contribution statement and authorship into two footnote groups."""

    lines = [
        *[
            _text_line(
                f"body {index}",
                (100.0, 150.0 + 30.0 * index, 500.0, 160.0 + 30.0 * index),
                index,
                effective_height=10.0,
            )
            for index in range(4)
        ],
        _text_line(
            "contribution one",
            (100.0, 590.0, 430.0, 598.0),
            4,
            effective_height=8.0,
        ),
        _text_line(
            "contribution two",
            (100.0, 602.0, 240.0, 610.0),
            5,
            effective_height=8.0,
        ),
        _text_line(
            "affiliation one",
            (120.0, 640.0, 460.0, 648.0),
            6,
            effective_height=8.0,
        ),
        _text_line(
            "affiliation two",
            (120.0, 652.0, 400.0, 660.0),
            7,
            effective_height=8.0,
        ),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [
        models._AxisLine((100.0, 570.0, 500.0, 572.0), 1.0, "horizontal"),
        models._AxisLine((100.0, 620.0, 500.0, 622.0), 1.0, "horizontal"),
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert page.page_footnote_groups == [{4, 5}, {6, 7}]
    assert all(line.semantic_type == "page_footnote" for line in lines[4:])


def test_page_footnote_rejects_column_width_rule_without_size_contraction() -> None:
    """Verify that the normal font size text below the column width horizontal line does not become a footnote simply by virtue of its position on the bottom half of the page."""

    lines = [
        _text_line(
            f"body {index}",
            (100.0, 180.0 + 24.0 * index, 900.0, 190.0 + 24.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    lines.append(
        _text_line(
            "continued body",
            (100.0, 620.0, 700.0, 630.0),
            4,
            effective_height=10.0,
        )
    )
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [models._AxisLine((100.0, 600.0, 900.0, 602.0), 1.0, "horizontal")]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in lines)


def test_page_footnote_unions_aligned_regular_and_span_lanes_only() -> None:
    """Verify that the regular/span column on the same left edge can be claimed jointly, but it will not swallow up the right text column."""

    lines = [
        *[
            _text_line(f"left body {index}", (100.0, 100.0 + 30.0 * index, 480.0, 110.0 + 30.0 * index), index)
            for index in range(3)
        ],
        *[
            _text_line(
                f"right body {index}",
                (520.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index),
                index + 3,
            )
            for index in range(3)
        ],
        _text_line("span note one", (100.0, 810.0, 900.0, 820.0), 6),
        _text_line("span note two", (100.0, 830.0, 900.0, 840.0), 7),
        _text_line("span note three", (100.0, 850.0, 900.0, 860.0), 8),
        _text_line("left note one", (100.0, 812.0, 480.0, 822.0), 9),
        _text_line("left note two", (100.0, 832.0, 480.0, 842.0), 10),
        _text_line("right body below rule", (520.0, 812.0, 900.0, 822.0), 11),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.drawing_lines = [models._AxisLine((100.0, 800.0, 305.0, 802.0), 1.0, "horizontal")]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert {line.source_index for line in lines if line.semantic_type == "page_footnote"} == {
        6,
        7,
        8,
        9,
        10,
    }
    assert page.page_footnote_groups == [{6, 7, 8, 9, 10}]
    assert lines[11].semantic_type is None


def test_page_footnote_entries_split_first_line_indent_without_text() -> None:
    """Verify that the first line indentation pattern separates the two footnote starting lines and absorbs left-justified continuation lines."""

    lines = [
        _text_line("first", (87.0, 732.0, 245.0, 742.0), 0, effective_height=8.0, median_glyph_width=4.5),
        _text_line("second", (83.0, 743.0, 293.0, 753.0), 1, effective_height=8.0, median_glyph_width=4.5),
        _text_line("continuation", (70.0, 755.0, 276.0, 763.0), 2, effective_height=7.0, median_glyph_width=5.0),
        _text_line("tail", (70.0, 766.0, 146.0, 773.0), 3, effective_height=7.0, median_glyph_width=5.0),
    ]

    entries = text_blocks._split_page_footnote_entries(
        lines,
        (595.0, 842.0),
    )

    assert [[line.source_index for line in entry] for entry in entries] == [
        [0],
        [1, 2, 3],
    ]


def test_page_footnote_entries_keep_same_left_compact_continuation() -> None:
    """Verify that the sub-full first line and the immediately following line on the same left edge remain in the same footnote block."""

    lines = [
        _text_line(
            "first",
            (70.0, 732.0, 220.0, 740.0),
            0,
            effective_height=8.0,
            median_glyph_width=4.0,
            font_signature=("NoteFont", 0),
            font_coverage=1.0,
        ),
        _text_line(
            "continuation",
            (70.0, 743.0, 270.0, 751.0),
            1,
            effective_height=8.0,
            median_glyph_width=4.0,
            font_signature=("NoteFont", 0),
            font_coverage=1.0,
        ),
    ]

    entries = text_blocks._split_page_footnote_entries(
        lines,
        (595.0, 842.0),
    )

    assert [[line.source_index for line in entry] for entry in entries] == [
        [0, 1],
    ]


def test_page_footnote_single_geometric_marker_keeps_group_together() -> None:
    """Verification row id A missing single narrowly numbered first line still aggregates the entire set of consecutive footnotes."""

    note_font = ("NoteFont", 0)
    lines = [
        _text_line(
            "1",
            (10.0, 80.0, 12.0, 88.0),
            0,
            visual_row_id=10,
            effective_height=8.0,
            font_signature=note_font,
            font_coverage=1.0,
            median_glyph_width=2.0,
            semantic_type="page_footnote",
        ),
        _text_line(
            "first source",
            (14.0, 80.5, 60.0, 88.5),
            1,
            visual_row_id=11,
            effective_height=8.0,
            font_signature=note_font,
            font_coverage=1.0,
            median_glyph_width=2.0,
            semantic_type="page_footnote",
        ),
        _text_line(
            "second source",
            (10.0, 92.0, 70.0, 100.0),
            2,
            effective_height=8.0,
            font_signature=note_font,
            font_coverage=1.0,
            median_glyph_width=2.0,
            semantic_type="page_footnote",
        ),
        _text_line(
            "third source",
            (10.0, 104.0, 65.0, 112.0),
            3,
            effective_height=8.0,
            font_signature=note_font,
            font_coverage=1.0,
            median_glyph_width=2.0,
            semantic_type="page_footnote",
        ),
    ]

    entries = text_blocks._split_page_footnote_entries(
        lines,
        (100.0, 140.0),
    )

    assert [[line.source_index for line in entry] for entry in entries] == [
        [0, 1, 2, 3],
    ]


def test_page_footnote_entries_split_hanging_indent_and_tighten_boxes() -> None:
    """Verify that Boosting type continuation indentation is split into four footnotes and that abnormally tall character boxes no longer cover each other."""

    page_size = (612.2833862304688, 858.8975830078125)
    lines = [
        _text_line(
            "receipt",
            (81.2767, 740.0383, 238.2958, 758.6347),
            66,
            effective_height=18.5964,
            median_glyph_width=3.92,
            semantic_type="page_footnote",
        ),
        _text_line(
            "fund start",
            (81.2767, 750.5583, 546.9993, 769.1547),
            67,
            effective_height=9.0,
            median_glyph_width=7.84,
            semantic_type="page_footnote",
        ),
        _text_line(
            "fund continuation",
            (119.6767, 761.0783, 364.8372, 779.6747),
            68,
            effective_height=10.38,
            median_glyph_width=4.78,
            semantic_type="page_footnote",
        ),
        _text_line(
            "author",
            (81.2767, 771.5983, 385.9195, 790.1947),
            69,
            effective_height=9.0,
            median_glyph_width=7.84,
            semantic_type="page_footnote",
        ),
        _text_line(
            "corresponding",
            (81.2767, 782.1183, 439.2661, 800.7147),
            70,
            effective_height=9.0,
            median_glyph_width=7.84,
            semantic_type="page_footnote",
        ),
    ]
    page = _prepared_text_page(*lines, page_size=page_size)
    page.page_footnote_groups = [{line.source_index for line in lines}]

    blocks = [block for block in pipeline._finalize_prepared_page(page, page_index=0) if block["type"] == "page_footnote"]

    assert [block["content"] for block in blocks] == [
        "receipt",
        "fund start fund continuation",
        "author",
        "corresponding",
    ]
    assert [block["bbox"] for block in blocks] == [
        [0.133, 0.867, 0.389, 0.878],
        [0.133, 0.879, 0.893, 0.902],
        [0.133, 0.904, 0.63, 0.914],
        [0.133, 0.916, 0.717, 0.927],
    ]
    assert all(previous["bbox"][3] < current["bbox"][1] for previous, current in zip(blocks, blocks[1:]))


@pytest.mark.parametrize(
    ("rule_bbox", "table_bboxes"),
    [
        ((100.0, 750.0, 125.0, 752.0), []),
        ((400.0, 750.0, 550.0, 752.0), []),
        ((100.0, 750.0, 260.0, 752.0), [(90.0, 730.0, 500.0, 850.0)]),
        (None, []),
    ],
)
def test_page_footnote_rejects_decorative_formula_table_and_missing_rules(
    rule_bbox: tuple[float, float, float, float] | None,
    table_bboxes: list[tuple[float, float, float, float]],
) -> None:
    """Verify that decorative short lines, centered formula lines, table lines, and bottom text without horizontal lines do not trigger footnotes."""

    lines = [
        _text_line("body one", (100.0, 100.0, 900.0, 110.0), 0),
        _text_line("body two", (100.0, 130.0, 900.0, 140.0), 1),
        _text_line("body three", (100.0, 160.0, 900.0, 170.0), 2),
        _text_line("bottom body", (100.0, 770.0, 500.0, 780.0), 3),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.table_bboxes = table_bboxes
    if rule_bbox is not None:
        page.drawing_lines = [models._AxisLine(rule_bbox, 1.0, "horizontal")]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in lines)


def test_page_footnote_rejects_collinear_rule_segment_next_to_table() -> None:
    """Verify that adjacent broken lines of the same height outside the table box do not trigger footers independently."""

    lines = [
        _text_line("body one", (100.0, 100.0, 900.0, 110.0), 0),
        _text_line("body two", (100.0, 130.0, 900.0, 140.0), 1),
        _text_line("body three", (100.0, 160.0, 900.0, 170.0), 2),
        _text_line("bottom table row", (100.0, 770.0, 500.0, 780.0), 3),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))
    page.table_bboxes = [(280.0, 730.0, 900.0, 850.0)]
    page.drawing_lines = [
        models._AxisLine((100.0, 750.0, 260.0, 752.0), 1.0, "horizontal"),
        models._AxisLine((280.0, 750.0, 900.0, 752.0), 1.0, "horizontal"),
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert all(line.semantic_type is None for line in lines)
    assert page.page_footnote_groups == []


@pytest.mark.parametrize(
    ("angle", "bbox"),
    [
        (270, (20.0, 250.0, 50.0, 650.0)),
        (90, (930.0, 250.0, 960.0, 650.0)),
    ],
)
def test_aside_text_accepts_tall_vertical_text_in_either_edge_band(
    angle: int,
    bbox: tuple[float, float, float, float],
) -> None:
    """Verify that when horizontal text dominates, the high proportion of vertical text on the left and right edges are marked as sidebars."""

    lines = [
        *[_text_line(f"body {index}", (100.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index), index) for index in range(6)],
        _text_line("aside", bbox, 6, angle=angle, effective_height=20.0),
    ]
    page = _prepared_text_page(*lines, page_size=(1000.0, 1000.0))

    auxiliary_text._classify_page_auxiliary_text(page)

    assert lines[-1].semantic_type == "aside_text"
    assert all(line.semantic_type is None for line in lines[:-1])


def test_aside_text_rejects_short_internal_wide_and_non_dominant_rotated_text() -> None:
    """Verify that short rotated lines, in-page rotated lines, excessively wide margin lines, and rotated text do not falsely report sidebars."""

    upright_lines = [
        _text_line(f"body {index}", (100.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index), index) for index in range(10)
    ]
    rejected = [
        _text_line("short", (20.0, 250.0, 50.0, 350.0), 10, angle=270, effective_height=20.0),
        _text_line("internal", (400.0, 200.0, 430.0, 600.0), 11, angle=270, effective_height=20.0),
        _text_line("wide", (0.0, 200.0, 100.0, 600.0), 12, angle=270, effective_height=20.0),
    ]
    page = _prepared_text_page(*upright_lines, *rejected, page_size=(1000.0, 1000.0))
    auxiliary_text._classify_page_auxiliary_text(page)
    assert all(line.semantic_type is None for line in rejected)

    rotated_body = [
        _text_line(f"upright {index}", (100.0, 100.0 + 20.0 * index, 300.0, 110.0 + 20.0 * index), index) for index in range(4)
    ]
    rotated_body.append(_text_line("edge but not aside", (20.0, 200.0, 50.0, 700.0), 4, angle=270, effective_height=20.0))
    rotated_page = _prepared_text_page(*rotated_body, page_size=(1000.0, 1000.0))
    auxiliary_text._classify_page_auxiliary_text(rotated_page)
    assert rotated_body[-1].semantic_type is None


def test_auxiliary_text_classification_is_content_independent() -> None:
    """Verify that replacing an entire line of text does not change the footer space classification results."""

    geometries = [
        (100.0, 100.0, 900.0, 110.0),
        (100.0, 130.0, 900.0, 140.0),
        (100.0, 160.0, 900.0, 170.0),
        (100.0, 770.0, 500.0, 780.0),
        (100.0, 788.0, 500.0, 798.0),
    ]
    first_lines = [_text_line(f"alpha {index}", bbox, index) for index, bbox in enumerate(geometries)]
    second_lines = [_text_line(f"完全不同 {index}", bbox, index) for index, bbox in enumerate(geometries)]
    first_page = _prepared_text_page(*first_lines, page_size=(1000.0, 1000.0))
    second_page = _prepared_text_page(*second_lines, page_size=(1000.0, 1000.0))
    for page in (first_page, second_page):
        page.drawing_lines = [models._AxisLine((100.0, 750.0, 260.0, 752.0), 1.0, "horizontal")]
        auxiliary_text._classify_page_auxiliary_text(page)

    assert [line.semantic_type for line in first_lines] == [line.semantic_type for line in second_lines]
    assert first_page.page_footnote_groups == second_page.page_footnote_groups


def test_auxiliary_text_classifiers_do_not_read_line_text() -> None:
    """Static guard sidebar and footer classification functions do not access text content."""

    source = "\n".join(
        inspect.getsource(function)
        for function in (
            auxiliary_text._classify_page_auxiliary_text,
            auxiliary_text._classify_aside_text,
            auxiliary_text._geometric_text_support_by_angle,
            auxiliary_text._classify_image_footnotes,
            auxiliary_text._classify_deferred_image_footnotes,
            auxiliary_text._image_footnote_members,
            auxiliary_text._classify_page_footnotes,
            auxiliary_text._classify_page_footnote_trailing_footers,
            auxiliary_text._page_footnote_trailing_footer_members,
            auxiliary_text._page_footnote_continuation_gap_limit,
            auxiliary_text._augment_footnote_groups_with_edge_markers,
            auxiliary_text._classify_rule_delimited_headers,
            auxiliary_text._classify_page_number_outer_companions,
            auxiliary_text._rule_belongs_to_confirmed_table,
            auxiliary_text._merge_overlapping_source_groups,
            auxiliary_text._footnote_lane_members,
            text_blocks._tight_page_footnote_bboxes,
        )
    )

    assert ".text" not in source


def test_finalize_preserves_preclassified_auxiliary_text_types() -> None:
    """Verify that marked side columns and footers will not be lost during the single-page finalization phase."""

    page = _prepared_text_page(
        _text_line("note", (10.0, 80.0, 40.0, 90.0), 0, semantic_type="page_footnote"),
        _text_line("chart note", (10.0, 65.0, 40.0, 75.0), 2, semantic_type="footnote"),
        _text_line(
            "aside",
            (2.0, 20.0, 5.0, 60.0),
            1,
            angle=270,
            effective_height=3.0,
            semantic_type="aside_text",
        ),
    )

    blocks = pipeline._finalize_prepared_page(page, page_index=0)

    assert {block["type"] for block in blocks} == {
        "footnote",
        "page_footnote",
        "aside_text",
    }


def test_repeated_marginals_require_cross_page_evidence_and_separate_page_numbers() -> None:
    """Verify that duplicate headers and footers with incremental mirror page numbers are marked, and isolated edge lines and body text remain unchanged."""

    margin_font = ("Margin", 0)
    pages = [
        _prepared_text_page(
            _text_line("Quarterly report 2024 - 1", (20.0, 5.0, 80.0, 10.0), 0, font_signature=margin_font, font_coverage=1.0),
            _text_line("Only on first page", (20.0, 11.0, 80.0, 16.0), 1, font_signature=margin_font, font_coverage=1.0),
            _text_line("Repeated body", (10.0, 30.0, 90.0, 40.0), 2, font_signature=margin_font, font_coverage=1.0),
            _text_line("1", (4.0, 48.0, 6.0, 53.0), 3, font_signature=margin_font, font_coverage=1.0),
            _text_line("10", (5.0, 89.0, 10.0, 94.0), 4, font_signature=margin_font, font_coverage=1.0),
            _text_line("Confidential", (30.0, 95.0, 70.0, 99.0), 5, font_signature=margin_font, font_coverage=1.0),
        ),
        _prepared_text_page(
            _text_line("Quarterly report 2024 - 2", (20.0, 5.0, 80.0, 10.0), 0, font_signature=margin_font, font_coverage=1.0),
            _text_line("Repeated body", (10.0, 30.0, 90.0, 40.0), 1, font_signature=margin_font, font_coverage=1.0),
            _text_line("2", (4.0, 48.0, 6.0, 53.0), 2, font_signature=margin_font, font_coverage=1.0),
            _text_line("11", (90.0, 89.0, 95.0, 94.0), 3, font_signature=margin_font, font_coverage=1.0),
            _text_line("Confidential", (30.0, 95.0, 70.0, 99.0), 4, font_signature=margin_font, font_coverage=1.0),
        ),
        _prepared_text_page(
            _text_line("Quarterly report 2024 - 3", (20.0, 5.0, 80.0, 10.0), 0, font_signature=margin_font, font_coverage=1.0),
            _text_line("Repeated body", (10.0, 30.0, 90.0, 40.0), 1, font_signature=margin_font, font_coverage=1.0),
            _text_line("3", (4.0, 48.0, 6.0, 53.0), 2, font_signature=margin_font, font_coverage=1.0),
            _text_line("12", (5.0, 89.0, 10.0, 94.0), 3, font_signature=margin_font, font_coverage=1.0),
            _text_line("Confidential", (30.0, 95.0, 70.0, 99.0), 4, font_signature=margin_font, font_coverage=1.0),
        ),
    ]

    auxiliary_text._classify_repeated_page_marginals(pages)

    assert [[line.semantic_type for line in page.remaining_lines] for page in pages] == [
        ["header", None, None, None, "page_number", "footer"],
        ["header", None, None, "page_number", "footer"],
        ["header", None, None, "page_number", "footer"],
    ]


def test_top_rule_marks_only_unclassified_text_above_it_as_header() -> None:
    """Verify that the page number is retained first, and the remaining text above the long horizontal line is supplemented with the header and the offline text remains unchanged."""

    page_number = _text_line(
        "7",
        (900.0, 30.0, 920.0, 40.0),
        0,
        semantic_type="page_number",
    )
    journal = _text_line("journal", (100.0, 30.0, 600.0, 40.0), 1)
    doi = _text_line("doi", (100.0, 44.0, 500.0, 54.0), 2)
    body = _text_line("body", (100.0, 80.0, 700.0, 90.0), 3)
    page = _prepared_text_page(
        page_number,
        journal,
        doi,
        body,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [models._AxisLine((80.0, 60.0, 920.0, 62.0), 1.0, "horizontal")]

    auxiliary_text._classify_rule_delimited_headers([page])

    assert [line.semantic_type for line in page.remaining_lines] == [
        "page_number",
        "header",
        "header",
        None,
    ]


def test_top_rule_uses_first_separator_before_later_section_rule() -> None:
    """Verify that multiple long horizontal lines at the top of the page only use the top valid header separator line."""

    header = _text_line("header", (100.0, 30.0, 300.0, 40.0), 0)
    section = _text_line("section", (100.0, 90.0, 300.0, 110.0), 1)
    body = _text_line("body", (100.0, 150.0, 700.0, 160.0), 2)
    page = _prepared_text_page(
        header,
        section,
        body,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (80.0, 60.0, 920.0, 62.0),
            1.0,
            "horizontal",
        ),
        models._AxisLine(
            (80.0, 120.0, 920.0, 122.0),
            1.0,
            "horizontal",
        ),
    ]

    auxiliary_text._classify_rule_delimited_headers([page])

    assert header.semantic_type == "header"
    assert section.semantic_type is None
    assert body.semantic_type is None


def test_top_decorative_rule_does_not_precede_real_header_separator() -> None:
    """Verify that the decorative line at the top without text above does not preempt the real divider line below the masthead text."""

    header = _text_line("letterhead", (100.0, 30.0, 400.0, 40.0), 0)
    body = _text_line("body", (100.0, 80.0, 700.0, 90.0), 1)
    page = _prepared_text_page(
        header,
        body,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (80.0, 8.0, 920.0, 10.0),
            1.0,
            "horizontal",
        ),
        models._AxisLine(
            (80.0, 60.0, 920.0, 62.0),
            1.0,
            "horizontal",
        ),
    ]

    auxiliary_text._classify_rule_delimited_headers([page])

    assert header.semantic_type == "header"
    assert body.semantic_type is None


def test_top_rule_uses_ink_bbox_when_loose_bbox_crosses_separator() -> None:
    """Verify that when the loose box crosses the top horizontal line, it still distinguishes the online date and offline DOI according to the true font."""

    date = _text_line(
        "2026 年 4 月",
        (100.0, 30.0, 240.0, 64.0),
        0,
        effective_height=10.0,
    )
    date.ink_bbox = (100.0, 34.0, 240.0, 45.0)
    doi = _text_line(
        "DOI:10.1000/example",
        (100.0, 58.0, 500.0, 76.0),
        1,
        effective_height=10.0,
    )
    doi.ink_bbox = (100.0, 64.0, 500.0, 74.0)
    body = _text_line(
        "body",
        (100.0, 90.0, 700.0, 100.0),
        2,
        effective_height=10.0,
    )
    page = _prepared_text_page(
        date,
        doi,
        body,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine(
            (80.0, 60.0, 920.0, 62.0),
            1.0,
            "horizontal",
        )
    ]

    auxiliary_text._classify_rule_delimited_headers([page])

    assert date.semantic_type == "header"
    assert doi.semantic_type is None
    assert body.semantic_type is None


def test_top_rule_inside_graphic_does_not_mark_header() -> None:
    """Verify that the long horizontal line in the header of the graphics container does not mistakenly mark the normal text above it as the header."""

    upper_text = _text_line("upper text", (100.0, 30.0, 600.0, 40.0), 0)
    body = _text_line("body", (100.0, 80.0, 700.0, 90.0), 1)
    page = _prepared_text_page(
        upper_text,
        body,
        page_size=(1000.0, 1000.0),
    )
    page.fixed_blocks = [
        {
            "type": "image",
            "bbox": (50.0, 50.0, 950.0, 70.0),
            "angle": 0,
            "content": "",
        }
    ]
    page.drawing_lines = [models._AxisLine((80.0, 60.0, 920.0, 62.0), 1.0, "horizontal")]

    auxiliary_text._classify_rule_delimited_headers([page])

    assert upper_text.semantic_type is None
    assert body.semantic_type is None


def test_page_number_outer_companions_classify_top_and_bottom_text_and_images() -> None:
    """Verify that the text and empty content images outside the upper and lower page numbers are symmetrically converted into headers and footers."""

    top_page = _prepared_text_page(
        _text_line("top visual", (20.0, 1.0, 80.0, 5.0), 0),
        _text_line(
            "1",
            (90.0, 8.0, 95.0, 13.0),
            1,
            semantic_type="page_number",
        ),
        _text_line("top body", (20.0, 18.0, 80.0, 28.0), 2),
    )
    top_page.fixed_blocks = [
        {
            "type": "image",
            "bbox": (40.0, 0.0, 60.0, 5.0),
            "angle": 0,
            "content": "",
        }
    ]
    bottom_page = _prepared_text_page(
        _text_line("bottom body", (20.0, 70.0, 80.0, 80.0), 0),
        _text_line(
            "2",
            (90.0, 82.0, 95.0, 87.0),
            1,
            semantic_type="page_number",
        ),
        _text_line("bottom visual", (20.0, 91.0, 80.0, 96.0), 2),
    )
    bottom_page.fixed_blocks = [
        {
            "type": "image",
            "bbox": (40.0, 90.0, 60.0, 99.0),
            "angle": 0,
            "content": "",
        }
    ]

    auxiliary_text._classify_page_number_outer_companions([top_page, bottom_page])

    assert [line.semantic_type for line in top_page.remaining_lines] == [
        "header",
        "page_number",
        None,
    ]
    assert top_page.fixed_blocks[0]["type"] == "header"
    assert [line.semantic_type for line in bottom_page.remaining_lines] == [
        None,
        "page_number",
        "footer",
    ]
    assert bottom_page.fixed_blocks[0]["type"] == "footer"

    blocks = pipeline._finalize_prepared_page(bottom_page, page_index=1)
    assert any(block["type"] == "footer" and block["content"] == "" for block in blocks)


def test_page_number_sequence_survives_portrait_to_landscape_edge_change() -> None:
    """Verify that the consecutive page numbers can be moved from the bottom to the side when switching between horizontal and vertical versions, and that subsequent side sequences continue to hit."""

    pages = [
        _prepared_text_page(
            _text_line("2", (45.0, 126.0, 55.0, 131.0), 0),
            page_size=(100.0, 140.0),
        ),
        _prepared_text_page(
            _text_line("3", (126.0, 75.0, 131.0, 80.0), 0),
            page_size=(140.0, 100.0),
        ),
        _prepared_text_page(
            _text_line("4", (126.0, 75.0, 131.0, 80.0), 0),
            page_size=(140.0, 100.0),
        ),
    ]

    auxiliary_text._classify_repeated_page_marginals(pages)

    assert [page.remaining_lines[0].semantic_type for page in pages] == ["page_number"] * 3


def test_single_page_marginal_content_remains_text() -> None:
    """Verify that text at the top and bottom of a single page cannot be guessed as a header, footer, or page number based solely on its position."""

    page = _prepared_text_page(
        _text_line("Page 7", (45.0, 3.0, 55.0, 8.0), 0),
        _text_line("Copyright notice", (20.0, 92.0, 80.0, 98.0), 1),
    )

    auxiliary_text._classify_repeated_page_marginals([page])

    assert [line.semantic_type for line in page.remaining_lines] == [None, None]


def test_extreme_page_footnotes_can_be_overridden_by_repeated_marginals() -> None:
    """Verify that only bottom footnotes can be changed to footers and page numbers based on evidence of cross-page repetition and increment."""

    pages = []
    for page_index in range(3):
        pages.append(
            _prepared_text_page(
                _text_line(
                    "stable footer",
                    (20.0, 95.0, 70.0, 99.0),
                    0,
                    effective_height=4.0,
                    font_signature=("Margin", 0),
                    font_coverage=1.0,
                    semantic_type="page_footnote",
                ),
                _text_line(
                    str(page_index + 10),
                    (90.0, 95.0, 95.0, 99.0),
                    1,
                    effective_height=4.0,
                    font_signature=("Margin", 0),
                    font_coverage=1.0,
                    semantic_type="page_footnote",
                ),
                _text_line(
                    "local note",
                    (20.0, 86.0, 60.0, 90.0),
                    2,
                    semantic_type="page_footnote",
                ),
            )
        )

    auxiliary_text._classify_repeated_page_marginals(pages)

    assert [[line.semantic_type for line in page.remaining_lines] for page in pages] == [
        ["footer", "page_number", "page_footnote"]
    ] * 3


def test_single_page_compound_header_requires_small_split_row_and_body_edge() -> None:
    """Verify that the small font split line at the top of the single page can be identified as the header by the right edge of the text column."""

    header_name = _text_line(
        "journal",
        (60.0, 2.0, 85.0, 4.0),
        0,
        visual_row_id=7,
        run_index=0,
        split_from_row=True,
        effective_height=2.0,
    )
    header_number = _text_line(
        "5",
        (96.0, 2.0, 98.0, 4.0),
        1,
        visual_row_id=7,
        run_index=1,
        split_from_row=True,
        effective_height=2.0,
    )
    page = _prepared_text_page(
        header_name,
        header_number,
        *[
            _text_line(
                f"body-{index}",
                (50.0, 10.0 + 12.0 * index, 98.0, 20.0 + 12.0 * index),
                2 + index,
                effective_height=10.0,
            )
            for index in range(4)
        ],
    )

    auxiliary_text._classify_single_page_compound_headers([page])

    assert header_name.semantic_type == "header"
    assert header_number.semantic_type == "header"


def test_isolated_first_page_footer_uses_multi_page_geometry_only() -> None:
    """Verify that the only short line at the bottom of the multi-page home page can be used to add the footer, while the single page has the same layout as the main text."""

    def build_page() -> tuple[models._PreparedPage, models._LineItem]:
        """Construct a page with four lines of descending text and an isolated bottom candidate."""

        footer = _text_line(
            "neutral notice",
            (37.5, 98.0, 62.5, 100.0),
            4,
            effective_height=2.0,
        )
        return (
            _prepared_text_page(
                *[
                    _text_line(
                        f"body-{index}",
                        (0.0, 55.0 + 10.0 * index, 100.0, 60.0 + 10.0 * index),
                        index,
                        effective_height=5.0,
                    )
                    for index in range(4)
                ],
                footer,
            ),
            footer,
        )

    multi_page, multi_footer = build_page()
    single_page, single_footer = build_page()

    auxiliary_text._classify_isolated_first_page_footer([multi_page, _prepared_text_page()])
    auxiliary_text._classify_isolated_first_page_footer([single_page])

    assert multi_footer.semantic_type == "footer"
    assert single_footer.semantic_type is None


def test_repeated_visual_headers_use_geometry_and_skip_first_page() -> None:
    """Verify that duplicate header images are relabeled by geometry only, and that an empty content can also be left as header."""

    pages = [_prepared_text_page(page_size=(100.0, 100.0)) for _ in range(5)]
    contents = ["cover", "", "beta", "", "delta"]
    for page, content in zip(pages, contents, strict=True):
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": (10.0, 0.0, 90.0, 10.0),
                "angle": 0,
                "content": content,
            }
        ]

    auxiliary_text._classify_repeated_visual_headers(pages)

    assert pages[0].fixed_blocks[0]["type"] == "image"
    assert [page.fixed_blocks[0]["type"] for page in pages[1:]] == ["header"] * 4


def test_repeated_visual_headers_require_three_top_geometry_matches() -> None:
    """Verify that two-page duplicates, non-top-of-page images, and significantly drifting bbox do not form a visual header."""

    pages = [_prepared_text_page(page_size=(100.0, 100.0)) for _ in range(6)]
    bboxes = [
        (10.0, 0.0, 90.0, 10.0),
        (10.0, 0.0, 90.0, 10.0),
        (10.0, 0.0, 90.0, 10.0),
        (10.0, 20.0, 90.0, 30.0),
        (30.0, 0.0, 90.0, 10.0),
        (10.0, 0.0, 90.0, 10.0),
    ]
    for page, bbox in zip(pages, bboxes, strict=True):
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": bbox,
                "angle": 0,
                "content": "value",
            }
        ]

    auxiliary_text._classify_repeated_visual_headers(pages)

    assert [page.fixed_blocks[0]["type"] for page in pages] == ["image"] * 6


def test_repeated_visual_headers_support_alternating_pages() -> None:
    """Verify that duplicate images spaced two from the same odd-even page can still form visual header clusters."""

    pages = [_prepared_text_page(page_size=(100.0, 100.0)) for _ in range(7)]
    for page_index, page in enumerate(pages):
        if page_index in {2, 4, 6}:
            page.fixed_blocks = [
                {
                    "type": "image",
                    "bbox": (10.0, 0.0, 90.0, 10.0),
                    "angle": 0,
                    "content": f"page-{page_index}",
                }
            ]

    auxiliary_text._classify_repeated_visual_headers(pages)

    assert [pages[index].fixed_blocks[0]["type"] for index in (2, 4, 6)] == ["header"] * 3


def test_repeated_visual_headers_require_matching_orientation() -> None:
    """Verification bbox Three pages of identical images with inconsistent orientations do not form the same visual header cluster."""

    pages = [_prepared_text_page(page_size=(100.0, 100.0)) for _ in range(4)]
    for page_index, page in enumerate(pages[1:], start=1):
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": (10.0, 0.0, 90.0, 10.0),
                "angle": 90 if page_index == 3 else 0,
                "content": f"page-{page_index}",
            }
        ]

    auxiliary_text._classify_repeated_visual_headers(pages)

    assert [page.fixed_blocks[0]["type"] for page in pages[1:]] == ["image"] * 3


def test_image_bottom_border_does_not_create_image_or_page_footnote() -> None:
    """Verify that the horizontal line along its own coordinate axis under the picture does not judge the text next to the picture as a footnote."""

    body = [
        _text_line(
            f"body {index}",
            (50.0, 40.0 + 20.0 * index, 450.0, 50.0 + 20.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    axis_label = _text_line(
        "T -1/4",
        (150.0, 317.0, 230.0, 325.0),
        4,
        effective_height=8.0,
    )
    page = _prepared_text_page(
        *body,
        axis_label,
        page_size=(500.0, 500.0),
    )
    page.fixed_blocks = [
        {
            "type": "image",
            "bbox": (50.0, 140.0, 300.0, 315.0),
            "angle": 0,
            "content": "",
        }
    ]
    page.drawing_lines = [
        models._AxisLine(
            (50.0, 314.0, 300.0, 315.0),
            1.0,
            "horizontal",
        )
    ]

    auxiliary_text._classify_page_auxiliary_text(page)

    assert axis_label.semantic_type is None
    assert page.page_footnote_groups == []


def test_two_bottom_rules_classify_centered_url_as_footer() -> None:
    """Verify that the two horizontal lines with the same span at the bottom of the page mark the URL in the middle as the footer."""

    body = [
        _text_line(
            f"body {index}",
            (100.0, 100.0 + 30.0 * index, 900.0, 110.0 + 30.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(4)
    ]
    url = _text_line(
        "https://example.test/journal",
        (350.0, 920.0, 650.0, 930.0),
        10,
        effective_height=10.0,
    )
    page = _prepared_text_page(
        *body,
        url,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [
        models._AxisLine((300.0, 900.0, 700.0, 901.0), 1.0, "horizontal"),
        models._AxisLine((300.0, 950.0, 700.0, 951.0), 1.0, "horizontal"),
    ]

    auxiliary_text._classify_rule_delimited_footers([page])

    assert url.semantic_type == "footer"


def test_single_bottom_rule_classifies_small_right_lane_rows_as_footer() -> None:
    """Verify that the continuous small text in the right column below the single horizontal line at the bottom is marked as the footer."""

    body = [
        _text_line(
            f"body {column}-{row}",
            (
                80.0 + 500.0 * column,
                100.0 + 25.0 * row,
                420.0 + 500.0 * column,
                110.0 + 25.0 * row,
            ),
            10 * column + row,
            effective_height=10.0,
        )
        for column in range(2)
        for row in range(5)
    ]
    footer_rows = [
        _text_line(
            f"footer {index}",
            (600.0, 858.0 + 10.0 * index, 880.0, 866.0 + 10.0 * index),
            20 + index,
            effective_height=8.0,
        )
        for index in range(3)
    ]
    page = _prepared_text_page(
        *body,
        *footer_rows,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [models._AxisLine((580.0, 850.0, 900.0, 851.0), 1.0, "horizontal")]

    auxiliary_text._classify_rule_delimited_footers([page])

    assert [line.semantic_type for line in footer_rows] == ["footer"] * 3
    assert all(line.semantic_type is None for line in body)


@pytest.mark.parametrize("inside_image", [False, True])
def test_single_bottom_rule_rejects_body_sized_or_container_rows(
    inside_image: bool,
) -> None:
    """Verify that neither text-size lines nor horizontal lines within images can trigger single-line footers."""

    body = [
        _text_line(
            f"body {index}",
            (100.0, 100.0 + 25.0 * index, 900.0, 110.0 + 25.0 * index),
            index,
            effective_height=10.0,
        )
        for index in range(6)
    ]
    bottom_rows = [
        _text_line(
            f"bottom {index}",
            (150.0, 858.0 + 12.0 * index, 850.0, 868.0 + 12.0 * index),
            10 + index,
            effective_height=8.0 if inside_image else 10.0,
        )
        for index in range(2)
    ]
    page = _prepared_text_page(
        *body,
        *bottom_rows,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [models._AxisLine((100.0, 850.0, 900.0, 851.0), 1.0, "horizontal")]
    if inside_image:
        page.fixed_blocks = [
            {
                "type": "image",
                "bbox": (80.0, 800.0, 920.0, 900.0),
                "angle": 0,
                "content": "",
            }
        ]

    auxiliary_text._classify_rule_delimited_footers([page])

    assert all(line.semantic_type is None for line in bottom_rows)


def test_single_bottom_rule_rejects_formula_fragments_with_unstable_left_edges() -> None:
    """Verify that formula fragments with discrete horizontal starting points under the score line cannot be misjudged as footers."""

    body = [
        _text_line(
            f"body {column}-{row}",
            (
                80.0 + 500.0 * column,
                100.0 + 25.0 * row,
                420.0 + 500.0 * column,
                110.0 + 25.0 * row,
            ),
            10 * column + row,
            effective_height=10.0,
        )
        for column in range(2)
        for row in range(5)
    ]
    formula_fragments = [
        _text_line(
            f"fragment {index}",
            (left, 858.0 + 9.0 * index, left + 80.0, 866.0 + 9.0 * index),
            20 + index,
            effective_height=8.0,
        )
        for index, left in enumerate((590.0, 710.0, 620.0))
    ]
    page = _prepared_text_page(
        *body,
        *formula_fragments,
        page_size=(1000.0, 1000.0),
    )
    page.drawing_lines = [models._AxisLine((600.0, 850.0, 850.0, 851.0), 1.0, "horizontal")]

    auxiliary_text._classify_rule_delimited_footers([page])

    assert all(line.semantic_type is None for line in formula_fragments)


def test_split_footer_row_fragments_inherit_stable_anchor_type() -> None:
    """Verify that left and right fragments of the same footer visual row inherit type footer from stable anchors."""

    fragments = [
        _text_line(
            "journal",
            (100.0, 930.0, 300.0, 940.0),
            0,
            visual_row_id=8,
            split_from_row=True,
        ),
        _text_line(
            "Vol. 1",
            (420.0, 930.0, 500.0, 940.0),
            1,
            visual_row_id=8,
            split_from_row=True,
            semantic_type="footer",
        ),
        _text_line(
            "No. 2",
            (650.0, 930.0, 750.0, 940.0),
            2,
            visual_row_id=8,
            split_from_row=True,
        ),
    ]
    page = _prepared_text_page(
        *fragments,
        page_size=(1000.0, 1000.0),
    )

    auxiliary_text._classify_split_marginal_row_companions([page])

    assert [line.semantic_type for line in fragments] == ["footer"] * 3


def test_distant_same_row_text_inherits_top_page_number_header_type() -> None:
    """Verify that the page number can be marked as a header without relying on horizontal distance when it is the same baseline as the running title."""

    page_number = _text_line(
        "8",
        (10.0, 10.0, 15.0, 15.0),
        0,
        semantic_type="page_number",
    )
    running_title = _text_line(
        "running title",
        (60.0, 10.0, 90.0, 15.0),
        1,
    )
    page = _prepared_text_page(
        page_number,
        running_title,
        page_size=(100.0, 100.0),
    )

    auxiliary_text._classify_page_number_outer_companions([page])

    assert page_number.semantic_type == "page_number"
    assert running_title.semantic_type == "header"


def test_near_page_sized_image_overlapping_page_number_stays_image() -> None:
    """Verify that nearly full-page images that slightly overlap the bottom page number line are not converted into footers."""

    page = _prepared_text_page(
        _text_line(
            "3",
            (90.0, 82.0, 95.0, 87.0),
            0,
            semantic_type="page_number",
        ),
        page_size=(100.0, 100.0),
    )
    full_page_image = {
        "type": "image",
        "bbox": (10.0, 8.0, 90.0, 85.0),
        "angle": 0,
        "content": "",
    }
    marginal_banner = {
        "type": "image",
        "bbox": (7.0, 92.0, 39.0, 97.0),
        "angle": 0,
        "content": "",
    }
    page.fixed_blocks = [full_page_image, marginal_banner]

    auxiliary_text._classify_page_number_outer_companions([page])

    assert full_page_image["type"] == "image"
    assert marginal_banner["type"] == "footer"
