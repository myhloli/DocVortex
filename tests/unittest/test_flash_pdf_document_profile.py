from __future__ import annotations

from _flash_pdf_test_utils import _prepared_text_page, _text_line

from docvortex.analyzers.native.pdf import models, titles


def test_document_body_profile_prefers_cross_page_body_and_regular_fonts() -> None:
    """Verify that full text portraits are prioritized across page body line heights, and repeated and obvious bold fonts are excluded."""

    body_font = ("BodyRegular", 0)
    mono_font = ("MonoRegular", 0)
    italic_font = ("BodyItalic", 1 << 6)
    bold_font = ("HeadingBold", 1)
    pages = []
    for page_index in range(4):
        lines = [
            _text_line(
                "body",
                (0.0, 10.0, 80.0, 20.0),
                page_index * 10,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
                dominant_font_weight=400.0,
            )
        ]
        if page_index < 3:
            lines.extend(
                [
                    _text_line(
                        "mono",
                        (0.0, 30.0, 80.0, 38.0),
                        page_index * 10 + 1,
                        effective_height=8.0,
                        font_signature=mono_font,
                        font_coverage=1.0,
                        dominant_font_weight=400.0,
                    ),
                    _text_line(
                        "bold",
                        (0.0, 50.0, 80.0, 60.0),
                        page_index * 10 + 2,
                        effective_height=10.0,
                        font_signature=bold_font,
                        font_coverage=1.0,
                        dominant_font_weight=800.0,
                    ),
                    _text_line(
                        "italic",
                        (0.0, 70.0, 80.0, 78.0),
                        page_index * 10 + 3,
                        effective_height=8.0,
                        font_signature=italic_font,
                        font_coverage=1.0,
                        dominant_font_weight=400.0,
                    ),
                ]
            )
        pages.append(_prepared_text_page(*lines))

    profile = titles._infer_document_body_profile(pages)

    assert profile is not None
    assert profile.body_height == 10.0
    assert profile.body_weight == 400.0
    # The text font portrait only counts text height bands, and shorter monospaced and italic samples no longer enter regular_fonts.
    assert profile.regular_fonts == frozenset({body_font})


def test_sparse_repeated_font_is_not_document_regular_style() -> None:
    """Verify that fonts that are repeated only in short headings across multiple pages are not merged into the full-text regular font."""

    body_font = ("BodyRegular", 0)
    sparse_font = ("SparseHeading", 1)
    pages = [
        _prepared_text_page(
            _text_line(
                "body",
                (0.0, 10.0, 100.0, 20.0),
                page_index * 2,
                font_signature=body_font,
                font_coverage=1.0,
                dominant_font_weight=0.0,
            ),
            _text_line(
                "short",
                (0.0, 40.0, 50.0, 50.0),
                page_index * 2 + 1,
                font_signature=sparse_font,
                font_coverage=1.0,
                dominant_font_weight=0.0,
            ),
        )
        for page_index in range(5)
    ]

    profile = titles._infer_document_body_profile(pages)

    assert profile is not None
    assert body_font in profile.regular_fonts
    assert sparse_font not in profile.regular_fonts


def test_document_regular_font_suppresses_code_page_false_title() -> None:
    """Verify that full-text regular font instructions in code pages remain text and bold step labels are still recognized as titles."""

    mono_font = ("MonoRegular", 0)
    regular_font = ("BodyRegular", 0)
    bold_font = ("HeadingBold", 1)
    lines = [
        _text_line(
            "code one",
            (0.0, 5.0, 100.0, 13.0),
            0,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "code two",
            (0.0, 15.0, 100.0, 23.0),
            1,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "ordinary instruction",
            (0.0, 40.0, 45.0, 50.0),
            2,
            effective_height=10.0,
            font_signature=regular_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "code three",
            (0.0, 62.0, 100.0, 70.0),
            3,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "code four",
            (0.0, 72.0, 100.0, 80.0),
            4,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "bold step",
            (0.0, 100.0, 35.0, 110.0),
            5,
            effective_height=10.0,
            font_signature=bold_font,
            font_coverage=1.0,
            dominant_font_weight=800.0,
        ),
        _text_line(
            "code five",
            (0.0, 122.0, 100.0, 130.0),
            6,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({mono_font, regular_font}),
    )

    titles._classify_page_titles(
        lines,
        (100.0, 150.0),
        page_index=1,
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert lines[2].semantic_type is None
    assert lines[5].semantic_type == "paragraph_title"


def test_document_title_profile_promotes_table_adjacent_style_but_not_container_label() -> None:
    """Verify that the cross-page title prototype can bypass neighbor list suppression, but the same style tag inside the container still maintains the text."""

    heading_font = ("Heading", 0)
    heading = _text_line(
        "heading",
        (0.0, 10.0, 40.0, 24.0),
        0,
        effective_height=14.0,
        font_signature=heading_font,
        font_coverage=1.0,
        dominant_font_weight=600.0,
    )
    container_label = _text_line(
        "container label",
        (0.0, 40.0, 40.0, 54.0),
        1,
        effective_height=14.0,
        font_signature=heading_font,
        font_coverage=1.0,
        dominant_font_weight=600.0,
    )
    body_profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({("Body", 0)}),
    )
    title_profile = models._DocumentTitleProfile(
        (
            models._TitleStylePrototype(
                font_family="heading",
                font_flags=0,
                height_ratio=1.4,
                weight=600.0,
                alignment="left",
                anchor_offset=0.0,
                support_count=4,
                support_pages=3,
            ),
        )
    )

    titles._classify_page_titles(
        [heading, container_label],
        (100.0, 100.0),
        page_index=1,
        container_bboxes=[(0.0, 28.0, 100.0, 90.0)],
        document_body_profile=body_profile,
        document_title_profile=title_profile,
    )

    assert heading.semantic_type == "paragraph_title"
    assert container_label.semantic_type is None


def test_document_title_profile_promotes_body_height_centered_section() -> None:
    """Verify that the centered chapter line with the same font size as the main text can be promoted based on the duplicate title prototype and neighbor list relationship."""

    regular_font = ("Body", 0)
    heading = _text_line(
        "section",
        (30.0, 24.0, 70.0, 34.0),
        0,
        effective_height=10.0,
        font_signature=regular_font,
        font_coverage=1.0,
        dominant_font_weight=400.0,
    )
    body = _text_line(
        "body",
        (0.0, 40.0, 100.0, 50.0),
        1,
        effective_height=10.0,
        font_signature=regular_font,
        font_coverage=1.0,
        dominant_font_weight=400.0,
    )
    body_profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({regular_font}),
    )
    title_profile = models._DocumentTitleProfile(
        (
            models._TitleStylePrototype(
                font_family="body",
                font_flags=0,
                height_ratio=1.0,
                weight=400.0,
                alignment="center",
                anchor_offset=0.0,
                support_count=5,
                support_pages=3,
            ),
        )
    )

    titles._classify_page_titles(
        [heading, body],
        (100.0, 100.0),
        page_index=1,
        container_bboxes=[(0.0, 0.0, 100.0, 20.0)],
        document_body_profile=body_profile,
        document_title_profile=title_profile,
    )

    assert heading.semantic_type == "paragraph_title"
    assert body.semantic_type is None


def test_document_title_profile_infers_repeated_large_left_aligned_style() -> None:
    """Verify that a large left-aligned style repeated on two pages forms a document-level heading prototype."""

    body_font = ("Body", 0)
    heading_font = ("RepeatedHeading", 0)
    pages = [
        _prepared_text_page(
            _text_line(
                "body cover",
                (0.0, 20.0, 100.0, 30.0),
                0,
                effective_height=10.0,
                font_signature=body_font,
                font_coverage=1.0,
            ),
            page_size=(100.0, 100.0),
        )
    ]
    for page_index in range(1, 3):
        pages.append(
            _prepared_text_page(
                _text_line(
                    f"heading {page_index}",
                    (0.0, 10.0, 45.0, 24.0),
                    page_index * 10,
                    effective_height=14.0,
                    font_signature=heading_font,
                    font_coverage=1.0,
                    dominant_font_weight=600.0,
                ),
                _text_line(
                    f"body {page_index}",
                    (0.0, 40.0, 100.0, 50.0),
                    page_index * 10 + 1,
                    effective_height=10.0,
                    font_signature=body_font,
                    font_coverage=1.0,
                    dominant_font_weight=400.0,
                ),
                page_size=(100.0, 100.0),
            )
        )
    body_profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font}),
    )

    profile = titles._infer_document_title_profile(pages, body_profile)

    assert profile is not None
    assert len(profile.prototypes) == 1
    assert profile.prototypes[0].font_family == "repeatedheading"
    assert profile.prototypes[0].support_pages == 2


def test_regular_pitch_style_change_does_not_become_title() -> None:
    """Verify that text continuations with only font changes but no extra paragraph headroom do not misinterpret titles."""

    body_font = ("Body", 0)
    alternate_font = ("Alternate", 0)
    lines = [
        _text_line(
            "body one",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "continuation",
            (0.0, 16.0, 80.0, 26.0),
            1,
            font_signature=alternate_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body two",
            (0.0, 32.0, 100.0, 42.0),
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

    titles._classify_page_titles(
        lines,
        (100.0, 100.0),
        page_index=1,
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert lines[1].semantic_type is None


def test_title_font_at_body_size_does_not_override_document_size_profile() -> None:
    """Verify that when the title font falls into the text size band, it will not be misjudged as a title simply by large white space."""

    heading_font = ("Heading", 0)
    lines = [
        _text_line(
            "body one",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=("Body", 0),
            font_coverage=1.0,
        ),
        _text_line(
            "weak heading",
            (0.0, 35.0, 45.0, 46.0),
            1,
            effective_height=11.0,
            font_signature=heading_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body two",
            (0.0, 60.0, 100.0, 70.0),
            2,
            font_signature=("Body", 0),
            font_coverage=1.0,
        ),
    ]
    body_profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({("Body", 0)}),
    )
    title_profile = models._DocumentTitleProfile(
        (
            models._TitleStylePrototype(
                font_family="heading",
                font_flags=0,
                height_ratio=1.4,
                weight=400.0,
                alignment="left",
                anchor_offset=0.0,
                support_count=4,
                support_pages=3,
            ),
        )
    )

    titles._classify_page_titles(
        lines,
        (100.0, 100.0),
        page_index=1,
        container_bboxes=[],
        document_body_profile=body_profile,
        document_title_profile=title_profile,
    )

    assert lines[1].semantic_type is None


def test_smaller_recurrent_regular_font_does_not_gain_title_style_signal() -> None:
    """Verify that the smaller cross-page regular code font will not be promoted to the title simply by font switching, even with large margins."""

    body_font = ("BodyRegular", 0)
    mono_font = ("MonoRegular", 0)
    lines = [
        _text_line(
            "body one",
            (0.0, 5.0, 100.0, 15.0),
            0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body two",
            (0.0, 17.0, 100.0, 27.0),
            1,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "small regular command",
            (2.0, 45.0, 98.0, 53.0),
            2,
            effective_height=8.0,
            font_signature=mono_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body three",
            (0.0, 70.0, 100.0, 80.0),
            3,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
        _text_line(
            "body four",
            (0.0, 82.0, 100.0, 92.0),
            4,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
            dominant_font_weight=400.0,
        ),
    ]
    profile = models._DocumentBodyProfile(
        body_height=10.0,
        body_weight=400.0,
        regular_fonts=frozenset({body_font, mono_font}),
    )

    titles._classify_page_titles(
        lines,
        (100.0, 120.0),
        page_index=1,
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert lines[2].semantic_type is None


def test_document_profile_finds_cover_title_without_promoting_bottom_metadata() -> None:
    """Verify that the full-text text benchmark recognizes plain cover titles, with bottom version metadata remaining as plain text."""

    title_font = ("TitleBold", 1)
    metadata_font = ("MetadataBold", 1)
    lines = [
        _text_line(
            "cover title one",
            (15.0, 55.0, 85.0, 80.0),
            0,
            effective_height=25.0,
            font_signature=title_font,
            font_coverage=1.0,
            dominant_font_weight=800.0,
        ),
        _text_line(
            "cover title two",
            (25.0, 85.0, 75.0, 110.0),
            1,
            effective_height=25.0,
            font_signature=title_font,
            font_coverage=1.0,
            dominant_font_weight=800.0,
        ),
        _text_line(
            "version metadata",
            (35.0, 168.0, 65.0, 178.0),
            2,
            effective_height=10.0,
            font_signature=metadata_font,
            font_coverage=1.0,
            dominant_font_weight=800.0,
        ),
        _text_line(
            "date metadata",
            (30.0, 180.0, 70.0, 190.0),
            3,
            effective_height=10.0,
            font_signature=metadata_font,
            font_coverage=1.0,
            dominant_font_weight=800.0,
        ),
    ]
    profile = models._DocumentBodyProfile(10.0, 400.0, frozenset())

    titles._classify_page_titles(
        lines,
        (100.0, 200.0),
        page_index=0,
        container_bboxes=[],
        document_body_profile=profile,
    )

    assert [line.semantic_type for line in lines] == [
        "doc_title",
        "doc_title",
        None,
        None,
    ]
