from __future__ import annotations

import inspect

import pytest
from _flash_pdf_test_utils import (
    _prepared_text_page,
    _text_line,
)

from docvortex.analyzers.native.pdf import line_layout, line_merging, models, text_blocks, titles


@pytest.mark.parametrize("heading_text", ["1. INTRODUCTION", "completely unrelated words"])
def test_paragraph_title_classification_is_independent_of_heading_content(heading_text: str) -> None:
    """Verify that different content with the same layout and font results in the exact same paragraph heading type."""

    body_font = ("Body", 0)
    heading_font = ("Heading", 1)
    lines = [
        _text_line("body one", (0.0, 10.0, 100.0, 20.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 22.0, 100.0, 32.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line(heading_text, (0.0, 45.0, 40.0, 55.0), 2, font_signature=heading_font, font_coverage=1.0),
        _text_line("body three", (0.0, 68.0, 100.0, 78.0), 3, font_signature=body_font, font_coverage=1.0),
        _text_line("body four", (0.0, 80.0, 100.0, 90.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line("body five", (0.0, 92.0, 100.0, 102.0), 5, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(lines, (100.0, 150.0), page_index=1, container_bboxes=[])

    assert lines[2].semantic_type == "paragraph_title"


def test_heading_like_content_with_body_layout_remains_text() -> None:
    """Verify that title-style strings remain normal text with body geometry, body fonts, and regular line spacing."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body {index}",
            (0.0, 10.0 + 12.0 * index, 100.0, 20.0 + 12.0 * index),
            index,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index in range(6)
    ]
    lines[2].text = "1. INTRODUCTION"

    titles._classify_page_titles(lines, (100.0, 120.0), page_index=1, container_bboxes=[])

    assert lines[2].semantic_type is None


def test_explicit_section_number_rejects_century_and_decimal_sentence() -> None:
    """Validates generic section number supplementary titles while excluding century chronology and decimal text."""

    body_font = ("Body", 0)
    lines = [
        _text_line("20 century body", (0.0, 10.0, 80.0, 20.0), 0, font_signature=body_font),
        _text_line("ordinary body one", (0.0, 22.0, 100.0, 32.0), 1, font_signature=body_font),
        _text_line("2 Section heading", (0.0, 45.0, 55.0, 55.0), 2, font_signature=body_font),
        _text_line("ordinary body two", (0.0, 62.0, 100.0, 72.0), 3, font_signature=body_font),
        _text_line("ordinary body three", (0.0, 74.0, 100.0, 84.0), 4, font_signature=body_font),
        _text_line("0.26 value, continues", (0.0, 96.0, 100.0, 106.0), 5, font_signature=body_font),
        _text_line("ordinary body four", (0.0, 108.0, 100.0, 118.0), 6, font_signature=body_font),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    titles._classify_explicit_section_titles(
        lines,
        (120.0, 150.0),
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert lines[0].semantic_type is None
    assert lines[2].semantic_type == "paragraph_title"
    assert lines[5].semantic_type is None


def test_document_regular_fonts_only_use_body_height_band() -> None:
    """Verify that large title fonts that are repeated across pages do not enter the full-text regular body font collection."""

    body_font = ("Body", 0)
    heading_font = ("RepeatedHeading", 0)
    pages = [
        _prepared_text_page(
            _text_line(
                f"heading-{page_index}",
                (10.0, 10.0, 90.0, 28.0),
                0,
                effective_height=18.0,
                font_signature=heading_font,
                font_coverage=1.0,
            ),
            _text_line(
                f"body-{page_index}",
                (0.0, 40.0, 100.0, 50.0),
                1,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
        )
        for page_index in range(3)
    ]

    profile = titles._infer_document_body_profile(pages)

    assert profile is not None
    assert profile.body_height == pytest.approx(10.0)
    assert profile.regular_fonts == frozenset({body_font})


def test_noninitial_document_title_requires_paragraph_title_candidate() -> None:
    """Verify that non-front page document titles can only be upgraded from confirmed paragraph title candidates."""

    title_font = ("Title", 0)
    body_font = ("Body", 0)
    title_first = _text_line(
        "Translated document title first",
        (15.0, 20.0, 85.0, 38.0),
        0,
        effective_height=18.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    title_second = _text_line(
        "translated title second",
        (20.0, 38.0, 80.0, 56.0),
        1,
        effective_height=18.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    title_first.semantic_type = "paragraph_title"
    title_second.semantic_type = "paragraph_title"
    author = _text_line(
        "Author One, Author Two",
        (20.0, 65.0, 80.0, 75.0),
        2,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    affiliation = _text_line(
        "University affiliation",
        (25.0, 77.0, 75.0, 87.0),
        3,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    abstract = _text_line(
        "wide abstract body",
        (5.0, 96.0, 95.0, 106.0),
        4,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    titles._promote_noninitial_document_title_band(
        [title_first, title_second, author, affiliation, abstract],
        (100.0, 150.0),
        page_index=1,
        container_bboxes=[],
        document_body_profile=profile,
        title_candidate_source_indices={0, 1},
    )

    assert title_first.semantic_type == "doc_title"
    assert title_second.semantic_type == "doc_title"
    assert all(line.semantic_type is None for line in (author, affiliation, abstract))

    ordinary_large_line = _text_line(
        "large centered non-title",
        (15.0, 20.0, 85.0, 38.0),
        10,
        effective_height=18.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    titles._promote_noninitial_document_title_band(
        [ordinary_large_line],
        (100.0, 150.0),
        page_index=2,
        container_bboxes=[],
        document_body_profile=profile,
        title_candidate_source_indices=set(),
    )
    assert ordinary_large_line.semantic_type is None


def test_noninitial_document_titles_support_multiple_articles_without_metadata() -> None:
    """Multiple articles in a verified magazine can become doc_title on their respective start pages with just a stable title layout."""

    title_font = ("Title", 0)
    body_font = ("Body", 0)
    first_article_title = _text_line(
        "First article title",
        (15.0, 20.0, 85.0, 36.0),
        0,
        effective_height=16.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    first_article_title_tail = _text_line(
        "continued title",
        (20.0, 37.0, 80.0, 53.0),
        1,
        effective_height=16.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    second_article_title = _text_line(
        "Second article title",
        (18.0, 28.0, 82.0, 44.0),
        0,
        effective_height=16.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    later_section = _text_line(
        "Centered section heading",
        (25.0, 85.0, 75.0, 98.0),
        2,
        effective_height=13.0,
        font_signature=title_font,
        font_coverage=1.0,
    )
    first_article_title.semantic_type = "paragraph_title"
    first_article_title_tail.semantic_type = "paragraph_title"
    second_article_title.semantic_type = "paragraph_title"
    later_section.semantic_type = "paragraph_title"
    pages = [
        _prepared_text_page(
            _text_line(
                "magazine cover",
                (5.0, 20.0, 95.0, 30.0),
                0,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
            page_size=(100.0, 150.0),
        ),
        _prepared_text_page(
            first_article_title,
            first_article_title_tail,
            _text_line(
                "first article body",
                (5.0, 70.0, 95.0, 80.0),
                2,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
            page_size=(100.0, 150.0),
        ),
        _prepared_text_page(
            second_article_title,
            _text_line(
                "second article body",
                (5.0, 55.0, 95.0, 65.0),
                1,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
            later_section,
            page_size=(100.0, 150.0),
        ),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    titles._promote_noninitial_document_title_band(
        pages[1].remaining_lines,
        pages[1].page_size,
        page_index=1,
        container_bboxes=[],
        document_body_profile=profile,
        title_candidate_source_indices={0, 1},
    )
    titles._promote_noninitial_document_title_band(
        pages[2].remaining_lines,
        pages[2].page_size,
        page_index=2,
        container_bboxes=[],
        document_body_profile=profile,
        title_candidate_source_indices={0, 2},
    )

    assert first_article_title.semantic_type == "doc_title"
    assert first_article_title_tail.semantic_type == "doc_title"
    assert second_article_title.semantic_type == "doc_title"
    assert later_section.semantic_type == "paragraph_title"


def test_document_body_profile_prefers_width_support_over_footer_page_count() -> None:
    """When verifying that a narrow footer covers more pages, the body height with a larger cumulative line width still takes precedence."""

    body_font = ("Body", 0)
    footer_font = ("Footer", 0)
    pages = []
    for page_index in range(4):
        lines = [
            _text_line(
                f"footer-{page_index}",
                (0.0, 90.0, 10.0, 98.0),
                100,
                effective_height=8.0,
                font_signature=footer_font,
                font_coverage=1.0,
            )
        ]
        if page_index < 3:
            lines.extend(
                _text_line(
                    f"body-{page_index}-{row_index}",
                    (0.0, 10.0 + 12.0 * row_index, 100.0, 20.0 + 12.0 * row_index),
                    row_index,
                    effective_height=10.0,
                    font_signature=body_font,
                    font_coverage=1.0,
                )
                for row_index in range(4)
            )
        pages.append(_prepared_text_page(*lines))

    profile = titles._infer_document_body_profile(pages)

    assert profile is not None
    assert profile.body_height == pytest.approx(10.0)
    assert profile.regular_fonts == frozenset({body_font})


def test_body_height_section_titles_mark_only_repeated_short_anchors() -> None:
    """Verify that the anchor points of short chapters of the same font size are independently formed into titles, and the details, contact information, and copyright lines remain in the main text."""

    body_font = ("Body", 0)
    lines = [
        _text_line("first section", (0.0, 10.0, 20.0, 20.0), 0),
        _text_line("wide body one", (0.0, 24.0, 100.0, 34.0), 1),
        _text_line("wide body two", (0.0, 36.0, 100.0, 46.0), 2),
        _text_line("wide body three", (0.0, 48.0, 100.0, 58.0), 3),
        _text_line("second section", (0.0, 72.0, 20.0, 82.0), 4),
        _text_line("compact row one", (0.0, 84.0, 60.0, 94.0), 5),
        _text_line("compact row two", (0.0, 96.0, 60.0, 106.0), 6),
        _text_line("compact row three", (0.0, 108.0, 60.0, 118.0), 7),
        _text_line("boxed label", (0.0, 132.0, 20.0, 142.0), 8),
        _text_line("boxed row one", (0.0, 144.0, 100.0, 154.0), 9),
        _text_line("boxed row two", (0.0, 156.0, 100.0, 166.0), 10),
        _text_line("boxed row three", (0.0, 168.0, 100.0, 178.0), 11),
        _text_line("contact label", (0.0, 192.0, 20.0, 202.0), 12),
        _text_line("small address", (0.0, 204.0, 60.0, 212.0), 13, effective_height=8.0),
        _text_line("small phone", (0.0, 214.0, 60.0, 222.0), 14, effective_height=8.0),
        _text_line("small email", (0.0, 224.0, 60.0, 232.0), 15, effective_height=8.0),
        _text_line("copyright", (0.0, 250.0, 20.0, 260.0), 16),
    ]
    for line in lines:
        line.font_signature = body_font
        line.font_coverage = 1.0
        line.dominant_font_weight = 250.0

    titles._classify_body_height_section_titles(
        lines,
        (100.0, 280.0),
        container_bboxes=[(0.0, 128.0, 100.0, 180.0)],
        document_body_profile=models._DocumentBodyProfile(
            body_height=10.0,
            body_weight=250.0,
            regular_fonts=frozenset({body_font}),
        ),
    )

    assert [lines[index].semantic_type for index in (0, 4)] == [
        "paragraph_title",
        "paragraph_title",
    ]
    assert all(line.semantic_type is None for index, line in enumerate(lines) if index not in {0, 4})


def test_physical_title_gap_ignores_disjoint_column_and_keeps_overlapping_row() -> None:
    """Verify that physical adjacent rows only participate in title space when horizontal projections intersect, to avoid compressing the spacing of another column of text."""

    target = _text_line("target", (0.0, 40.0, 40.0, 50.0), 0)
    disjoint = _text_line("other column", (60.0, 39.0, 100.0, 49.0), 1)
    overlapping = _text_line("wide row", (0.0, 25.0, 100.0, 35.0), 2)

    gaps = titles._build_physical_title_gap_map(
        [(target, target.bbox), (disjoint, disjoint.bbox), (overlapping, overlapping.bbox)]
    )

    assert gaps[target.source_index] == (5.0, None)


def test_grid_title_suppression_requires_two_distinct_parallel_bands() -> None:
    """To verify that a single three-column short first-row strip is not enough to suppress the title, repeating two strips forms evidence of an information grid."""

    lanes = []
    source_index = 0
    for lane_index in range(3):
        left = 110.0 * lane_index
        first_opener = _text_line(
            f"opener-{lane_index}-0",
            (left, 10.0, left + 20.0, 20.0),
            source_index,
        )
        source_index += 1
        first_body = _text_line(
            f"body-{lane_index}-0",
            (left, 20.0, left + 80.0, 30.0),
            source_index,
        )
        source_index += 1
        lanes.append(
            models._TextLane(
                left=left,
                right=left + 100.0,
                lines=[
                    (first_opener, first_opener.bbox),
                    (first_body, first_body.bbox),
                ],
            )
        )

    assert titles._find_repeated_grid_title_suppressions(lanes, 10.0) == set()

    second_band_sources = set()
    for lane in lanes[:2]:
        second_opener = _text_line(
            "second opener",
            (lane.left, 40.0, lane.left + 20.0, 50.0),
            source_index,
        )
        second_band_sources.add(source_index)
        source_index += 1
        second_body = _text_line(
            "second body",
            (lane.left, 50.0, lane.left + 80.0, 60.0),
            source_index,
        )
        source_index += 1
        lane.lines.extend([(second_opener, second_opener.bbox), (second_body, second_body.bbox)])

    suppressions = titles._find_repeated_grid_title_suppressions(lanes, 10.0)

    assert second_band_sources <= suppressions
    assert len(suppressions) == 5


def test_centered_smaller_paragraph_title_uses_layout_contrast_only() -> None:
    """Verify that small-sized, centered titles in the same font can be identified by column width and top and bottom margins."""

    body_font = ("Body", 0)
    lines = [
        _text_line("body one", (0.0, 10.0, 100.0, 20.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 22.0, 100.0, 32.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line(
            "neutral label",
            (30.0, 45.0, 70.0, 53.0),
            2,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line("body three", (0.0, 66.0, 100.0, 76.0), 3, font_signature=body_font, font_coverage=1.0),
        _text_line("body four", (0.0, 78.0, 100.0, 88.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line("body five", (0.0, 90.0, 100.0, 100.0), 5, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(lines, (100.0, 150.0), page_index=1, container_bboxes=[])

    assert lines[2].semantic_type == "paragraph_title"


def test_centered_smaller_paragraph_title_accepts_compact_text_section() -> None:
    """Verify that a small, centered title can be supported by a continuous small font body section immediately following it."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body {index}",
            (0.0, 12.0 * index, 100.0, 12.0 * index + 10.0),
            index,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index in range(5)
    ]
    lines.extend(
        [
            _text_line(
                "neutral label",
                (30.0, 70.0, 70.0, 78.0),
                5,
                effective_height=8.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
            _text_line("compact row one", (0.0, 84.0, 100.0, 92.0), 6, effective_height=8.0),
            _text_line("compact row two", (0.0, 94.0, 100.0, 102.0), 7, effective_height=8.0),
            _text_line("compact row three", (0.0, 104.0, 100.0, 112.0), 8, effective_height=8.0),
        ]
    )

    titles._classify_page_titles(lines, (100.0, 140.0), page_index=1, container_bboxes=[])

    assert lines[5].semantic_type == "paragraph_title"


@pytest.mark.parametrize("failure_mode", ["too_few", "narrow", "large_gap"])
def test_centered_smaller_paragraph_title_rejects_incomplete_compact_section(
    failure_mode: str,
) -> None:
    """Validate that small font size sections that are less than three lines long, have insufficient line width, or have broken line spacing cannot relax the title."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body {index}",
            (0.0, 12.0 * index, 100.0, 12.0 * index + 10.0),
            index,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index in range(5)
    ]
    compact_rows = [
        _text_line("compact row one", (0.0, 84.0, 100.0, 92.0), 6, effective_height=8.0),
        _text_line("compact row two", (0.0, 94.0, 100.0, 102.0), 7, effective_height=8.0),
        _text_line("compact row three", (0.0, 104.0, 100.0, 112.0), 8, effective_height=8.0),
    ]
    if failure_mode == "too_few":
        compact_rows.pop()
    elif failure_mode == "narrow":
        compact_rows[-1].bbox = (0.0, 104.0, 40.0, 112.0)
    else:
        compact_rows[-1].bbox = (0.0, 114.0, 100.0, 122.0)
    lines.append(
        _text_line(
            "neutral label",
            (30.0, 70.0, 70.0, 78.0),
            5,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
    )
    lines.extend(compact_rows)

    titles._classify_page_titles(lines, (100.0, 140.0), page_index=1, container_bboxes=[])

    assert lines[5].semantic_type is None


def test_multiline_document_title_does_not_absorb_author_line() -> None:
    """Verify that the two large title lines on the homepage are merged into the document title, and the smaller author line remains as normal text."""

    body_font = ("Body", 0)
    title_font = ("Title", 0)
    lines = [
        _text_line(
            "title line one",
            (15.0, 20.0, 85.0, 34.4),
            0,
            effective_height=14.4,
            font_signature=title_font,
            font_coverage=1.0,
        ),
        _text_line(
            "title line two",
            (10.0, 33.8, 90.0, 48.2),
            1,
            effective_height=14.4,
            font_signature=title_font,
            font_coverage=1.0,
        ),
        _text_line(
            "author names",
            (20.0, 60.0, 80.0, 69.1),
            2,
            effective_height=9.1,
            font_signature=title_font,
            font_coverage=1.0,
        ),
        _text_line("body one", (0.0, 110.0, 100.0, 120.0), 3, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 122.0, 100.0, 132.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line("body three", (0.0, 134.0, 100.0, 144.0), 5, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(lines, (100.0, 200.0), page_index=0, container_bboxes=[])

    assert [line.semantic_type for line in lines[:3]] == ["doc_title", "doc_title", None]


def test_multiline_document_title_accepts_uncertain_mixed_dominant_font() -> None:
    """Verify that when the main font coverage of the mixed title is unstable, it can still be merged according to font size, centering and font weight."""

    lines = [
        _text_line(
            "wide mixed title",
            (5.0, 20.0, 95.0, 35.0),
            0,
            effective_height=15.0,
            font_signature=("MixedDominant", 0),
            font_coverage=0.8,
            dominant_font_weight=500.0,
        ),
        _text_line(
            "wide title continuation",
            (10.0, 36.0, 90.0, 50.0),
            1,
            effective_height=14.0,
            font_signature=("LocalTitle", 0),
            font_coverage=1.0,
            dominant_font_weight=500.0,
        ),
        _text_line(
            "author",
            (35.0, 60.0, 65.0, 69.0),
            2,
            effective_height=9.0,
            font_signature=("LocalTitle", 0),
            font_coverage=1.0,
            dominant_font_weight=500.0,
        ),
        _text_line("body one", (0.0, 110.0, 100.0, 120.0), 3),
        _text_line("body two", (0.0, 122.0, 100.0, 132.0), 4),
        _text_line("body three", (0.0, 134.0, 100.0, 144.0), 5),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 200.0),
        page_index=0,
        container_bboxes=[],
    )

    assert [line.semantic_type for line in lines[:3]] == ["doc_title", "doc_title", None]
    blocks = text_blocks._build_text_blocks(lines, [], (100.0, 200.0))
    title_blocks = [block for block in blocks if block["type"] == "doc_title"]
    assert len(title_blocks) == 1
    assert title_blocks[0]["content"] == "wide mixed title\nwide title continuation"


def test_compact_left_heading_accepts_one_body_height_following_gap() -> None:
    """Verify that the short title and subsequent body text can still be confirmed by local style transitions when they are about one line apart."""

    body_font = ("Body", 0)
    heading = _text_line(
        "neutral heading",
        (0.0, 45.0, 25.0, 56.0),
        3,
        effective_height=11.0,
        font_signature=("Heading", 0),
        font_coverage=0.65,
    )
    lines = [
        _text_line(
            "body one",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body two",
            (0.0, 12.0, 100.0, 22.0),
            1,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body three",
            (0.0, 24.0, 100.0, 34.0),
            2,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        heading,
        _text_line(
            "next body",
            (0.0, 67.0, 100.0, 77.0),
            4,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "next body two",
            (0.0, 79.0, 100.0, 89.0),
            5,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
    )

    assert heading.semantic_type == "paragraph_title"


def test_same_font_body_tail_with_moderate_gap_is_not_title() -> None:
    """Verify that the last line of short text after a full line of the same font will not be upgraded to a title due to medium spacing."""

    body_font = ("Body", 0)
    lines = [
        _text_line("reference body", (0.0, 0.0, 100.0, 10.0), 0, effective_height=8.0),
        _text_line("reference body two", (0.0, 10.0, 100.0, 20.0), 1, effective_height=8.0),
        _text_line(
            "local full body",
            (0.0, 35.0, 100.0, 45.0),
            2,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "local body tail",
            (0.0, 54.0, 60.0, 64.0),
            3,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line("different section", (0.0, 80.0, 100.0, 90.0), 4),
        _text_line("different continuation", (0.0, 92.0, 100.0, 102.0), 5),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
    )

    assert lines[3].semantic_type is None


def test_subset_font_visual_row_continues_into_short_body_tail() -> None:
    """After verifying the normalized subset fonts of the split fragments of the same full line, short tails are still prioritized as text continuation lines."""

    first = _text_line(
        "prefix",
        (0.0, 0.0, 12.0, 10.0),
        0,
        visual_row_id=10,
        split_from_row=True,
        font_signature=("SimSun", 0),
        font_coverage=1.0,
    )
    second = _text_line(
        "full row remainder",
        (15.0, 0.0, 100.0, 10.0),
        1,
        visual_row_id=10,
        split_from_row=True,
        font_signature=("ABCDEF+SimSun", 0),
        font_coverage=1.0,
    )
    tail = _text_line(
        "short tail",
        (0.0, 12.0, 35.0, 22.0),
        2,
        font_signature=("UVWXYZ+SimSun", 0),
        font_coverage=1.0,
    )
    rows = [(line, line.bbox) for line in (first, second, tail)]
    profile = models._LaneBodyProfile(
        body_height=10.0,
        body_font=("ABCDEF+SimSun", 0),
        body_weight=400.0,
        regular_gap=2.0,
        style_support={},
    )

    assert titles._continues_local_body_row(rows, 2, 100.0, profile)


def test_normal_body_font_needs_precise_centering_for_layout_title_fallback() -> None:
    """Verify that the common text style can only use the layout title when it is accurately centered and adjacent to the main text."""

    body_font = ("Body", 0)
    imprecise_lines = [
        _text_line("body one", (0.0, 0.0, 100.0, 10.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 12.0, 100.0, 22.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line("imprecise label", (20.0, 35.0, 65.0, 45.0), 2, font_signature=body_font, font_coverage=1.0),
        _text_line("body three", (0.0, 58.0, 100.0, 68.0), 3, font_signature=body_font, font_coverage=1.0),
        _text_line("body four", (0.0, 70.0, 100.0, 80.0), 4, font_signature=body_font, font_coverage=1.0),
    ]
    precise_lines = [
        _text_line("body one", (0.0, 0.0, 100.0, 10.0), 10, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 12.0, 100.0, 22.0), 11, font_signature=body_font, font_coverage=1.0),
        _text_line("precise label", (30.0, 35.0, 70.0, 45.0), 12, font_signature=body_font, font_coverage=1.0),
        _text_line("body three", (0.0, 58.0, 100.0, 68.0), 13, font_signature=body_font, font_coverage=1.0),
        _text_line("body four", (0.0, 70.0, 100.0, 80.0), 14, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(
        imprecise_lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
    )
    titles._classify_page_titles(
        precise_lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
    )

    assert imprecise_lines[2].semantic_type is None
    assert precise_lines[2].semantic_type == "paragraph_title"


def test_first_page_centered_body_style_metadata_does_not_use_title_fallback() -> None:
    """Verify that the normal font size and centered metadata in the upper part of the home page will not be upgraded to a paragraph title simply by leaving blank space."""

    body_font = ("Body", 0)
    lines = [
        _text_line("metadata", (30.0, 20.0, 70.0, 30.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body one", (0.0, 43.0, 100.0, 53.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 55.0, 100.0, 65.0), 2, font_signature=body_font, font_coverage=1.0),
        _text_line("body three", (0.0, 67.0, 100.0, 77.0), 3, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 150.0),
        page_index=0,
        container_bboxes=[],
    )

    assert lines[0].semantic_type is None


def test_first_page_centered_small_regular_metadata_requires_emphasis() -> None:
    """Verify that the metadata of small fonts in the main text area of the homepage is not improved by centering and white space alone."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "body above one",
            (0.0, 10.0, 100.0, 20.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body above two",
            (0.0, 22.0, 100.0, 32.0),
            1,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "centered metadata",
            (25.0, 48.0, 75.0, 55.0),
            2,
            effective_height=7.0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "compact body one",
            (0.0, 72.0, 100.0, 80.0),
            3,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "compact body two",
            (0.0, 82.0, 100.0, 90.0),
            4,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "compact body three",
            (0.0, 92.0, 100.0, 100.0),
            5,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    titles._classify_page_titles(
        lines,
        (100.0, 140.0),
        page_index=0,
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert lines[2].semantic_type is None


def test_first_page_hanging_title_rows_demote_to_text_geometry() -> None:
    """Verify that the indented first line of a nearly full column and the continuation line from the left of the column lead to a title group that returns to the main text on the homepage."""

    heading_font = ("Heading", 0)
    first = _text_line(
        "first row",
        (10.0, 10.0, 100.0, 20.0),
        0,
        font_signature=heading_font,
        font_coverage=1.0,
        semantic_type="paragraph_title",
    )
    second = _text_line(
        "second row",
        (0.0, 20.2, 55.0, 30.2),
        1,
        font_signature=heading_font,
        font_coverage=1.0,
        semantic_type="paragraph_title",
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (first, first.bbox),
            (second, second.bbox),
        ],
    )
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({("Body", 0)}),
    )

    titles._demote_hanging_multiline_text_titles(
        [lane],
        profile,
        page_index=0,
    )

    assert first.semantic_type is None
    assert second.semantic_type is None
    assert first.title_suppressed
    assert second.title_suppressed


def test_inline_typography_reset_promotes_only_distinct_middle_row() -> None:
    """Verify that short paragraph endings, short lines in different fonts, and three-line structures of indented text only promote the middle line."""

    body_font = ("Body", 0)
    heading_font = ("Heading", 0)
    lines = [
        _text_line(
            "short tail",
            (0.0, 0.0, 25.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "inline heading",
            (0.0, 11.0, 45.0, 21.0),
            1,
            font_signature=heading_font,
            font_coverage=1.0,
        ),
        _text_line(
            "indented body",
            (18.0, 22.0, 100.0, 32.0),
            2,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    titles._classify_inline_typography_reset_titles(
        lines,
        (100.0, 100.0),
        container_bboxes=[],
        document_body_profile=profile,
    )
    control = [
        _text_line(
            line.text,
            line.bbox,
            line.source_index,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for line in lines
    ]
    titles._classify_inline_typography_reset_titles(
        control,
        (100.0, 100.0),
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert [line.semantic_type for line in lines] == [
        None,
        "paragraph_title",
        None,
    ]
    assert all(line.semantic_type is None for line in control)


def test_cross_column_document_title_uses_thirteen_tenths_body_height_fallback() -> None:
    """Verify that the cross-column center title on the home page can be hit when it reaches 1.30 times the text, and the author row maintains the text type."""

    body_font = ("Body", 0)
    title_font = ("Title", 0)
    lines = [
        _text_line(
            "wide title",
            (20.0, 20.0, 180.0, 33.0),
            0,
            effective_height=13.0,
            font_signature=title_font,
            font_coverage=1.0,
        ),
        _text_line(
            "author row",
            (60.0, 42.0, 140.0, 52.0),
            1,
            effective_height=10.0,
            font_signature=title_font,
            font_coverage=1.0,
        ),
    ]
    for row_index, top in enumerate((80.0, 92.0, 104.0, 116.0)):
        lines.extend(
            [
                _text_line(
                    f"left body {row_index}",
                    (10.0, top, 90.0, top + 10.0),
                    2 + row_index * 2,
                    font_signature=body_font,
                    font_coverage=1.0,
                ),
                _text_line(
                    f"right body {row_index}",
                    (110.0, top, 190.0, top + 10.0),
                    3 + row_index * 2,
                    font_signature=body_font,
                    font_coverage=1.0,
                ),
            ]
        )

    titles._classify_page_titles(
        lines,
        (200.0, 200.0),
        page_index=0,
        container_bboxes=[],
    )

    assert lines[0].semantic_type == "doc_title"
    assert lines[1].semantic_type != "paragraph_title"


def test_complete_visual_row_promotes_number_and_demotes_inline_body() -> None:
    """Verify that the entire line with the same font number is promoted. If the title font is in the same row as the main text, the entire line is demoted and re-merged."""

    body_font = ("Body", 0)
    title_font = ("Title", 0)
    lines = [
        _text_line("body one", (0.0, 5.0, 100.0, 15.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 17.0, 100.0, 27.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line(
            "2",
            (0.0, 42.0, 8.0, 52.0),
            2,
            visual_row_id=20,
            split_from_row=True,
            font_signature=title_font,
            font_coverage=1.0,
            dominant_font_weight=700.0,
        ),
        _text_line(
            "neutral heading",
            (15.0, 42.0, 60.0, 52.0),
            3,
            visual_row_id=20,
            split_from_row=True,
            font_signature=title_font,
            font_coverage=1.0,
            dominant_font_weight=700.0,
        ),
        _text_line("body three", (0.0, 65.0, 100.0, 75.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line(
            "inline label",
            (0.0, 82.0, 32.0, 92.0),
            5,
            visual_row_id=30,
            split_from_row=True,
            font_signature=title_font,
            font_coverage=1.0,
            dominant_font_weight=700.0,
        ),
        _text_line(
            "inline body",
            (34.0, 82.0, 100.0, 92.0),
            6,
            visual_row_id=30,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line("body four", (0.0, 94.0, 100.0, 104.0), 7, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 140.0),
        page_index=1,
        container_bboxes=[],
    )
    merged = line_merging._merge_title_resolved_visual_rows(lines, (100.0, 140.0))

    numbered_title = next(line for line in merged if line.visual_row_id == 20)
    inline_row = next(line for line in merged if line.visual_row_id == 30)
    assert numbered_title.semantic_type == "paragraph_title"
    assert numbered_title.text == "2 neutral heading"
    assert inline_row.semantic_type is None
    assert inline_row.text == "inline label inline body"


def test_low_coverage_mixed_weight_visual_row_demotes_inline_title() -> None:
    """Verify that the same visual line with low font coverage is still recognized as inline bold text by weight conflict."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "body one",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body two",
            (0.0, 12.0, 100.0, 22.0),
            1,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body three",
            (0.0, 24.0, 100.0, 34.0),
            2,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "inline label",
            (0.0, 50.0, 35.0, 62.0),
            3,
            visual_row_id=30,
            split_from_row=True,
            font_signature=("Heading", 0),
            font_coverage=0.6,
            dominant_font_weight=700.0,
        ),
        _text_line(
            "inline body",
            (37.0, 50.0, 100.0, 62.0),
            4,
            visual_row_id=30,
            run_index=1,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=0.6,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body continuation",
            (0.0, 64.0, 100.0, 74.0),
            5,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 100.0),
        page_index=1,
        container_bboxes=[],
    )
    merged = line_merging._merge_title_resolved_visual_rows(lines, (100.0, 100.0))

    inline_row = next(line for line in merged if line.visual_row_id == 30)
    assert inline_row.semantic_type is None
    assert inline_row.text == "inline label inline body"


def test_full_width_normal_height_inline_heading_merges_with_body_continuation() -> None:
    """Verify that full column bold lines with normal font sizes are reduced to text and continue only with the regular text below them."""

    body_font = ("Body", 0)
    heading_font = ("Heading", 1)
    lines = [
        _text_line("body one", (0.0, 0.0, 100.0, 10.0), 0, font_signature=body_font, font_coverage=1.0),
        _text_line("body two", (0.0, 12.0, 100.0, 22.0), 1, font_signature=body_font, font_coverage=1.0),
        _text_line(
            "inline heading",
            (0.0, 40.0, 100.0, 50.0),
            2,
            font_signature=heading_font,
            font_coverage=1.0,
            dominant_font_weight=700.0,
        ),
        _text_line(
            "continuation one",
            (0.0, 52.0, 100.0, 62.0),
            3,
            effective_height=8.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line("continuation two", (0.0, 64.0, 100.0, 74.0), 4, font_signature=body_font, font_coverage=1.0),
        _text_line("continuation three", (0.0, 76.0, 100.0, 86.0), 5, font_signature=body_font, font_coverage=1.0),
    ]

    titles._classify_page_titles(
        lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
    )
    blocks = text_blocks._build_text_blocks(lines, [], (100.0, 120.0))

    inline_block = next(block for block in blocks if block["content"].startswith("inline heading"))
    assert lines[2].semantic_type is None
    assert inline_block["type"] == "text"
    assert "continuation one" in inline_block["content"]
    assert "body two" not in inline_block["content"]


def test_dense_same_font_two_run_row_requires_complete_high_occupancy_geometry() -> None:
    """Verify that the double run text is restored only if it has the same font, same baseline, consecutive numbering and sufficient occupancy."""

    body_font = ("Body", 0)
    dense_members = [
        _text_line(
            "left",
            (0.0, 0.0, 48.0, 10.0),
            0,
            visual_row_id=10,
            run_index=0,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "right",
            (52.0, 0.0, 100.0, 10.0),
            1,
            visual_row_id=10,
            run_index=1,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    sparse_members = [
        _text_line(
            "sparse left",
            (0.0, 20.0, 30.0, 30.0),
            2,
            visual_row_id=20,
            run_index=0,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "sparse right",
            (70.0, 20.0, 100.0, 30.0),
            3,
            visual_row_id=20,
            run_index=1,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    partial_formula_row = [
        _text_line(
            "formula body",
            (0.0, 40.0, 48.0, 50.0),
            4,
            visual_row_id=30,
            run_index=0,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "sidecar",
            (52.0, 40.0, 100.0, 50.0),
            5,
            visual_row_id=30,
            run_index=2,
            split_from_row=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    different_font_members = [
        dense_members[0],
        _text_line(
            "different",
            (52.0, 0.0, 100.0, 10.0),
            6,
            visual_row_id=10,
            run_index=1,
            split_from_row=True,
            font_signature=("Math", 0),
            font_coverage=1.0,
        ),
    ]

    assert line_merging._is_dense_same_font_two_run_row(dense_members, (100.0, 100.0))
    assert not line_merging._is_dense_same_font_two_run_row(sparse_members, (100.0, 100.0))
    assert not line_merging._is_dense_same_font_two_run_row(partial_formula_row, (100.0, 100.0))
    assert not line_merging._is_dense_same_font_two_run_row(different_font_members, (100.0, 100.0))

    merged = line_merging._merge_title_resolved_visual_rows(
        dense_members + sparse_members,
        (100.0, 100.0),
    )
    assert [line.text for line in merged if line.visual_row_id == 10] == ["left right"]
    assert len([line for line in merged if line.visual_row_id == 20]) == 2


def test_preserved_split_boundary_blocks_dense_and_title_row_restoration() -> None:
    """Verify space column protection blocks both dense body text recovery and paragraph header peer recovery."""

    body_font = ("Body", 0)
    protected_members = [
        _text_line(
            "left",
            (0.0, 0.0, 48.0, 10.0),
            0,
            visual_row_id=10,
            run_index=0,
            split_from_row=True,
            preserve_split_boundary=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "right",
            (52.0, 0.0, 100.0, 10.0),
            1,
            visual_row_id=10,
            run_index=1,
            split_from_row=True,
            preserve_split_boundary=True,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    assert not line_merging._is_dense_same_font_two_run_row(
        protected_members,
        (100.0, 100.0),
    )
    assert not line_merging._can_restore_dense_split_visual_row(
        protected_members,
        (100.0, 100.0),
        [],
        {0: (0, 0), 1: (0, 0)},
    )

    protected_titles = [
        _text_line(
            member.text,
            member.bbox,
            member.source_index,
            visual_row_id=member.visual_row_id,
            run_index=member.run_index,
            split_from_row=True,
            preserve_split_boundary=True,
            font_signature=body_font,
            font_coverage=1.0,
            semantic_type="paragraph_title",
        )
        for member in protected_members
    ]
    merged = line_merging._merge_title_resolved_visual_rows(
        protected_titles,
        (100.0, 100.0),
    )

    assert [line.text for line in merged] == ["left", "right"]


def test_paragraph_title_detector_does_not_read_line_text() -> None:
    """Guard paragraph title candidates, scoring, and adjacent line expansion do not read the text content."""

    source = "\n".join(
        inspect.getsource(function)
        for function in (
            titles._classify_page_titles,
            titles._classify_inline_typography_reset_titles,
            titles._infer_document_body_profile,
            titles._classify_body_height_section_titles,
            titles._body_height_section_followers,
            titles._infer_document_title_profile,
            titles._title_profile_seed_matches_cluster,
            titles._title_profile_alignment,
            titles._document_font_is_regular,
            titles._build_physical_title_gap_map,
            titles._find_repeated_grid_title_suppressions,
            titles._find_container_visual_row_title_suppressions,
            titles._infer_lane_body_profile,
            titles._classify_document_title,
            titles._document_title_fonts_compatible,
            titles._document_title_uses_page_fallback,
            titles._classify_paragraph_titles_in_lane,
            titles._line_uses_document_regular_font,
            titles._matching_document_title_prototype,
            titles._line_inside_visual_container,
            titles._visual_row_has_body_style_sibling,
            titles._is_near_full_mixed_inline_row,
            titles._continues_local_body_row,
            titles._is_continuous_field_row,
            titles._is_full_width_inline_heading,
            titles._has_following_body_row,
            titles._has_following_compact_text_section,
            titles._unify_visual_row_title_types,
            titles._protect_front_matter_title_types,
            titles._infer_front_matter_boundary,
            titles._normalized_title_gap,
            titles._line_near_visual_container,
            titles._is_wide_leading_title_continuation,
            titles._demote_hanging_multiline_text_titles,
            line_layout._title_fonts_compatible,
            titles._expand_paragraph_title_neighbors,
        )
    )

    assert ".text" not in source


def test_cross_lane_title_expansion_accepts_only_wide_leading_line() -> None:
    """Verify that the wider first line can be merged into the narrow title anchor below, and the reversed text continuation line will not be expanded into a title."""

    heading_font = ("Heading", 0)
    leading = _text_line(
        "wide leading line",
        (72.0, 20.0, 287.0, 30.0),
        0,
        font_signature=heading_font,
        font_coverage=1.0,
    )
    anchor = _text_line(
        "narrow anchor",
        (130.0, 30.6, 230.0, 40.6),
        1,
        semantic_type="paragraph_title",
        font_signature=heading_font,
        font_coverage=1.0,
    )
    following = _text_line(
        "wide following line",
        (72.0, 41.2, 287.0, 51.2),
        2,
        font_signature=heading_font,
        font_coverage=1.0,
    )

    titles._expand_cross_lane_paragraph_title_neighbors(
        [(leading, leading.bbox), (anchor, anchor.bbox), (following, following.bbox)]
    )

    assert leading.semantic_type == "paragraph_title"
    assert following.semantic_type is None
