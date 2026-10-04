from __future__ import annotations

from dataclasses import replace

import pytest
from _flash_pdf_test_utils import (
    _text_line,
)

from docvortex.analyzers.native.pdf import formulas, geometry, line_merging, models
from docvortex.document.pdf._document import PDFPathInfo


def _formula_member(
    text: str,
    bbox: tuple[float, float, float, float],
    source_index: int,
) -> tuple[models._LineItem, tuple[float, float, float, float]]:
    """Constructs a line of text and its local geometry used by the formula block serialization test."""

    return (
        models._LineItem(
            text=text,
            bbox=bbox,
            angle=0,
            source_index=source_index,
            effective_height=bbox[3] - bbox[1],
        ),
        bbox,
    )


def test_formula_members_expose_union_of_tight_bboxes_with_one_point_padding() -> None:
    """Verify that the text formula retains the tight+1pt output envelope for all members after aggregation."""

    body, body_bbox = _formula_member(
        "x=1",
        (10.0, 20.0, 50.0, 40.0),
        0,
    )
    number, number_bbox = _formula_member(
        "(1)",
        (80.0, 25.0, 90.0, 35.0),
        1,
    )
    body.ink_bbox = (12.0, 25.0, 48.0, 35.0)
    number.ink_bbox = (82.0, 27.0, 89.0, 33.0)

    block = formulas._formula_members_to_block(
        [(body, body_bbox), (number, number_bbox)],
        (100.0, 100.0),
        0,
        anchor_source_index=1,
    )

    assert block is not None
    assert block["_tight_output_bbox"] == (
        11.0,
        24.0,
        90.0,
        36.0,
    )


@pytest.mark.parametrize(
    "text",
    [
        "（℃/hm），用 I 表示：I=ΔT",
        "温度可表示为T=ΔT",
        "The temperature value T=ΔT",
    ],
    ids=["chinese-with-unit", "chinese-without-punctuation", "english-without-keyword"],
)
def test_formula_component_rejects_left_aligned_prose_prefix(text: str) -> None:
    """Verify that complex inline fractions with a common text prefix on the left margin of the same column are not upgraded to interline formulas."""

    prose = _text_line(
        text,
        (0.0, 20.0, 55.0, 30.0),
        0,
        effective_height=10.0,
    )
    numerator = _text_line(
        "×100%",
        (56.0, 20.0, 78.0, 30.0),
        1,
        effective_height=10.0,
    )
    denominator = _text_line(
        "ΔH",
        (45.0, 30.0, 55.0, 38.0),
        2,
        effective_height=8.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (prose, prose.bbox),
            (numerator, numerator.bbox),
            (denominator, denominator.bbox),
        ],
    )

    assert formulas._formula_component_has_left_prose(
        lane.lines,
        lane,
        10.0,
    )


@pytest.mark.parametrize(
    "text",
    [
        "I=ΔT",
        "x+y=z",
        "Score=MLP(vCLS)",
        "conf(Bm)=1",
        "（摄氏度）=T",
        "(temperature value)=T",
    ],
    ids=[
        "single-variable",
        "symbolic-expression",
        "single-identifier",
        "function-identifier",
        "chinese-bracketed-unit",
        "english-bracketed-unit",
    ],
)
def test_formula_component_keeps_left_aligned_formula_identifiers(text: str) -> None:
    """Validation variables, symbolic expressions, whitespace-free identifiers, and pure bracket units remain as independent formulas."""

    formula = _text_line(
        text,
        (0.0, 20.0, 55.0, 30.0),
        0,
        effective_height=10.0,
    )
    denominator = _text_line(
        "ΔH",
        (45.0, 30.0, 55.0, 38.0),
        1,
        effective_height=8.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (formula, formula.bbox),
            (denominator, denominator.bbox),
        ],
    )

    assert not formulas._formula_component_has_left_prose(
        [(formula, formula.bbox), (denominator, denominator.bbox)],
        lane,
        10.0,
    )


def test_inline_prose_formula_component_returns_to_paragraph_context() -> None:
    """Verify that peer body formulas do not generate formula blocks and mark them as subsequent body aggregation contexts."""

    body_font = ("Body", 0)
    prose_formula = _text_line(
        "温度可表示为T=ΔT",
        (0.0, 40.0, 58.0, 50.0),
        0,
        effective_height=10.0,
        font_signature=("Math", 0),
        font_coverage=0.6,
    )
    denominator = _text_line(
        "ΔH",
        (25.0, 51.0, 55.0, 61.0),
        1,
        effective_height=10.0,
        font_signature=("Math", 0),
        font_coverage=0.6,
    )
    number = _text_line(
        "(5)",
        (91.0, 49.0, 100.0, 59.0),
        2,
        effective_height=10.0,
    )
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            3 + index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((76.0, 88.0, 100.0, 112.0))
    ]

    blocks, remaining = formulas._build_formula_like_blocks(
        [prose_formula, denominator, number, *body_lines],
        [],
        (100.0, 140.0),
    )

    assert blocks == []
    assert prose_formula.paragraph_formula_context
    assert denominator.paragraph_formula_context
    assert number.paragraph_formula_context
    assert {line.source_index for line in remaining} == set(range(7))


def test_isolated_numbered_fraction_overrides_left_prose_shape() -> None:
    """Verify that numbered multi-level formulas with fractional lines and upper and lower margins are not misjudged as text by variable names."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "preceding body",
            (0.0, 0.0, 100.0, 10.0),
            0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "FZ SSEs KSSEc=SSEx",
            (0.0, 25.0, 60.0, 35.0),
            1,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
        _text_line(
            "SSEc=dfc",
            (25.0, 36.0, 45.0, 46.0),
            2,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
        _text_line(
            "(7)",
            (91.0, 31.0, 100.0, 41.0),
            3,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "following body",
            (0.0, 60.0, 100.0, 70.0),
            4,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lines],
    )

    assert formulas._formula_component_has_left_prose(
        [(line, line.bbox) for line in lines[1:4]],
        lane,
        10.0,
    )
    assert formulas._formula_component_has_isolated_numbered_fraction(
        [(line, line.bbox) for line in lines[1:4]],
        lane,
        10.0,
        [(10.0, 34.5, 60.0, 35.0)],
    )


def _vector_path(
    bbox: tuple[float, float, float, float],
    source_index: int,
    *,
    segment_count: int = 16,
    fill_visible: bool = True,
    stroke_visible: bool = False,
    form_depth: int = 0,
) -> PDFPathInfo:
    """Path information used to construct vector formula detection tests."""

    return PDFPathInfo(
        bbox=bbox,
        segment_count=segment_count,
        fill_visible=fill_visible,
        stroke_visible=stroke_visible,
        form_depth=form_depth,
        source_index=source_index,
    )


def _vector_formula_body_paths(
    *,
    left: float = 20.0,
    top: float = 50.0,
    source_start: int = 0,
) -> list[PDFPathInfo]:
    """Construct a six-shaped vector formula body that satisfies complexity and size constraints."""

    return [
        _vector_path(
            (left + index * 6.0, top, left + index * 6.0 + 4.0, top + 12.0),
            source_start + index,
        )
        for index in range(6)
    ]


def _vector_formula_source(
    *path_infos: PDFPathInfo,
    page_size: tuple[float, float] = (100.0, 120.0),
    extra_lines: list[models._LineItem] | None = None,
) -> models._PageSource:
    """Construct a page source with a stable text column and high white space where formulas are located."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 20.0, 80.0, 100.0))
    ]
    lines.extend(extra_lines or [])
    return models._PageSource(
        page_size=page_size,
        lines=lines,
        chars=[],
        drawing_lines=[],
        path_infos=list(path_infos),
    )


def test_single_line_formula_requires_numeric_trailing_marker() -> None:
    """Verify that the peer formula only recognizes the numeric number as tag, and does not treat the function parameters as numbers."""

    numbered = _text_line(
        "score=f(x) (3)",
        (15.0, 30.0, 90.0, 40.0),
        0,
        effective_height=10.0,
    )
    parenthesized_argument = _text_line(
        "Score=MLP(vCLS)",
        (15.0, 50.0, 90.0, 60.0),
        1,
        effective_height=10.0,
    )
    numbered.style_scale_repaired = True
    parenthesized_argument.style_scale_repaired = True
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (numbered, numbered.bbox),
            (parenthesized_argument, parenthesized_argument.bbox),
        ],
    )

    assert formulas._is_single_line_numbered_formula(
        (numbered, numbered.bbox),
        lane,
        10.0,
    )
    assert not formulas._is_single_line_numbered_formula(
        (parenthesized_argument, parenthesized_argument.bbox),
        lane,
        10.0,
    )


def test_single_line_numbered_formula_absorbs_connected_math_sidecars() -> None:
    """Verify that the numbered formula core absorbs the equal sign prefix and the narrow numerator, but not the subsequent body text."""

    core = _text_line(
        "|Bm| sum(yi) (1)",
        (40.0, 20.0, 95.0, 35.0),
        0,
        effective_height=10.0,
    )
    prefix = _text_line(
        "acc(Bm)=",
        (10.0, 24.0, 39.5, 34.0),
        1,
        effective_height=10.0,
    )
    numerator = _text_line(
        "1",
        (55.0, 15.0, 60.0, 23.0),
        2,
        effective_height=8.0,
    )
    body = _text_line(
        "ordinary following prose",
        (0.0, 40.0, 100.0, 50.0),
        3,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    core.style_scale_repaired = True
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (core, core.bbox),
            (prefix, prefix.bbox),
            (numerator, numerator.bbox),
            (body, body.bbox),
        ],
    )

    members = formulas._expand_single_line_numbered_formula_members(
        (core, core.bbox),
        lane,
        set(),
        [],
        ("Body", 0),
        10.0,
    )
    block = formulas._formula_members_to_block(
        members,
        (100.0, 100.0),
        0,
        anchor_source_index=core.source_index,
    )

    assert {line.source_index for line, _bbox in members} == {0, 1, 2}
    assert block is not None
    assert "acc(Bm)=" in block["content"]
    assert block["content"].count("\\tag{1}") == 1


def test_vector_formula_paths_and_detached_path_number_form_one_empty_equation() -> None:
    """Verify that the vector body and the distance column right edge path number form an empty content formula."""

    body_paths = _vector_formula_body_paths()
    number_paths = [_vector_path((91.0 + index * 3.0, 51.0, 93.0 + index * 3.0, 60.0), 10 + index) for index in range(3)]
    blocks, claimed = formulas._build_vector_formula_blocks(
        _vector_formula_source(*body_paths, *number_paths),
        [],
        set(),
    )

    assert claimed == set()
    assert blocks == [
        {
            "type": "equation",
            "bbox": (19.0, 49.0, 100.0, 63.0),
            "angle": 0,
            "content": "",
        }
    ]


def test_vector_formula_claims_text_number_but_keeps_content_empty() -> None:
    """Verify that the extractable independent number is incorporated into the path formula and uniquely claimed, but does not serve as the body of the formula."""

    number = _text_line("(12)", (91.0, 51.0, 99.0, 60.0), 20, effective_height=9.0)
    blocks, claimed = formulas._build_vector_formula_blocks(
        _vector_formula_source(*_vector_formula_body_paths(), extra_lines=[number]),
        [],
        set(),
    )

    assert claimed == {20}
    assert blocks[0]["type"] == "equation"
    assert blocks[0]["content"] == ""
    assert blocks[0]["bbox"] == pytest.approx((19.0, 49.0, 100.0, 63.0))


def test_vector_formula_rejects_unmatched_number_rules_strokes_forms_and_inline_paths() -> None:
    """Verify that no subject number, thin rules, strokes, Form icon and text peer path will not cause false positives."""

    unmatched_number = [_vector_path((91.0 + index * 3.0, 51.0, 93.0 + index * 3.0, 60.0), index) for index in range(3)]
    rules = [
        _vector_path((10.0 + index * 12.0, 70.0, 20.0 + index * 12.0, 70.5), 10 + index, segment_count=5) for index in range(6)
    ]
    excluded = [
        _vector_path((20.0, 50.0, 24.0, 62.0), 30, stroke_visible=True),
        _vector_path((26.0, 50.0, 30.0, 62.0), 31, form_depth=1),
    ]
    inline_paths = _vector_formula_body_paths(top=20.0, source_start=40)
    blocks, claimed = formulas._build_vector_formula_blocks(
        _vector_formula_source(*unmatched_number, *rules, *excluded, *inline_paths),
        [],
        set(),
    )

    assert blocks == []
    assert claimed == set()


def test_vector_formula_respects_columns_and_existing_containers() -> None:
    """Verify that double-column subjects with the same height are not interconnected, and subjects covered by high-priority containers are excluded."""

    left_lines = [
        _text_line(f"left-{index}", (0.0, top, 100.0, top + 10.0), index, effective_height=10.0)
        for index, top in enumerate((0.0, 20.0, 80.0, 100.0))
    ]
    right_lines = [
        _text_line(
            f"right-{index}",
            (120.0, top, 220.0, top + 10.0),
            10 + index,
            effective_height=10.0,
        )
        for index, top in enumerate((0.0, 20.0, 80.0, 100.0))
    ]
    source = models._PageSource(
        page_size=(220.0, 120.0),
        lines=[*left_lines, *right_lines],
        chars=[],
        drawing_lines=[],
        path_infos=[
            *_vector_formula_body_paths(left=20.0, source_start=0),
            *_vector_formula_body_paths(left=140.0, source_start=20),
        ],
    )

    column_blocks, column_claimed = formulas._build_vector_formula_blocks(
        source,
        [],
        set(),
    )
    blocks, claimed = formulas._build_vector_formula_blocks(
        source,
        [{"type": "image", "bbox": (130.0, 45.0, 180.0, 67.0), "content": ""}],
        set(),
    )

    assert column_claimed == set()
    assert len(column_blocks) == 2
    assert column_blocks[0]["bbox"] == pytest.approx((19.0, 49.0, 55.0, 63.0))
    assert column_blocks[1]["bbox"] == pytest.approx((139.0, 49.0, 175.0, 63.0))
    assert claimed == set()
    assert len(blocks) == 1
    assert blocks[0]["bbox"] == pytest.approx((19.0, 49.0, 55.0, 63.0))


def test_detached_formula_sidecar_sharing_middle_row_moves_to_trailing_line() -> None:
    """Verify that the pure bbox rule will post the long distance narrow sidecar that shares the middle visual line with the main text."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 60.0, 10.0), 0),
        _formula_member("body", (10.0, 10.0, 40.0, 20.0), 1),
        _formula_member("marker", (100.0, 10.0, 110.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (130.0, 60.0),
        0,
        anchor_source_index=2,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 110.0, 30.0),
        "angle": 0,
        "content": "numerator\nbody\ndenominator\nmarker",
    }


@pytest.mark.parametrize("marker", ["(4)", "（4）", "﹙4﹚", "(4）"])
def test_adjacent_parenthesized_formula_number_serializes_as_tag(marker: str) -> None:
    """Verify that the various parentheses sequence numbers close to the body of the formula are converted to tag, and the leading commas remain in the text."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 90.0, 10.0), 0),
        _formula_member(f", {marker}", (91.0, 0.0, 110.0, 10.0), 1),
        _formula_member("body", (10.0, 10.0, 40.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (130.0, 60.0),
        0,
        anchor_source_index=1,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 110.0, 30.0),
        "angle": 0,
        "content": "numerator,\nbody\ndenominator\\tag{4}",
    }


def test_adjacent_square_bracket_formula_sidecar_keeps_visual_order() -> None:
    """Verify that the content of square brackets does not trigger the round bracket formula sequence number rule."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 90.0, 10.0), 0),
        _formula_member(", [4]", (91.0, 0.0, 110.0, 10.0), 1),
        _formula_member("body", (10.0, 10.0, 40.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (130.0, 60.0),
        0,
        anchor_source_index=1,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 110.0, 30.0),
        "angle": 0,
        "content": "numerator, [4]\nbody\ndenominator",
    }


def test_detached_formula_sidecar_on_middle_row_moves_after_denominator() -> None:
    """Verify that the far narrow range sidecar that exclusives the middle visual line lines up after the denominator and leaves no empty lines."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 70.0, 10.0), 0),
        _formula_member("sidecar", (100.0, 10.0, 110.0, 20.0), 1),
        _formula_member("denominator", (30.0, 20.0, 65.0, 30.0), 2),
    ]

    block = formulas._formula_members_to_block(
        members,
        (130.0, 60.0),
        0,
        anchor_source_index=1,
    )

    assert block == {
        "type": "equation",
        "bbox": (20.0, 0.0, 110.0, 30.0),
        "angle": 0,
        "content": "numerator\ndenominator\nsidecar",
    }


def test_detached_formula_sidecar_already_at_end_keeps_visual_row() -> None:
    """Verify that discrete sidecar already at the end of content maintains the original visual line format."""

    members = [
        _formula_member("formula", (10.0, 0.0, 40.0, 10.0), 0),
        _formula_member("terminal", (100.0, 0.0, 110.0, 10.0), 1),
    ]

    block = formulas._formula_members_to_block(
        members,
        (130.0, 60.0),
        0,
        anchor_source_index=1,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 110.0, 10.0),
        "angle": 0,
        "content": "formula        terminal",
    }


def test_attached_formula_sidecar_keeps_visual_order() -> None:
    """Verify that the right anchor point with insufficient headroom on the body of the formula maintains the original visual order."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 60.0, 10.0), 0),
        _formula_member("body", (10.0, 10.0, 70.0, 20.0), 1),
        _formula_member("sidecar", (85.0, 10.0, 95.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (120.0, 60.0),
        0,
        anchor_source_index=2,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 95.0, 30.0),
        "angle": 0,
        "content": "numerator\nbody   sidecar\ndenominator",
    }


def test_wide_formula_sidecar_keeps_visual_order() -> None:
    """Verify that distant anchors whose width exceeds the median row height limit are not pushed behind."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 60.0, 10.0), 0),
        _formula_member("body", (10.0, 10.0, 70.0, 20.0), 1),
        _formula_member("wide", (100.0, 10.0, 126.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (140.0, 60.0),
        0,
        anchor_source_index=2,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 126.0, 30.0),
        "angle": 0,
        "content": "numerator\nbody      wide\ndenominator",
    }


def test_non_rightmost_formula_sidecar_keeps_visual_order() -> None:
    """Verify that anchor points that are not on the rightmost side of a formula component are not postfixed."""

    members = [
        _formula_member("numerator", (20.0, 0.0, 120.0, 10.0), 0),
        _formula_member("body", (10.0, 10.0, 40.0, 20.0), 1),
        _formula_member("sidecar", (90.0, 10.0, 100.0, 20.0), 2),
        _formula_member("denominator", (30.0, 20.0, 70.0, 30.0), 3),
    ]

    block = formulas._formula_members_to_block(
        members,
        (140.0, 60.0),
        0,
        anchor_source_index=2,
    )

    assert block == {
        "type": "equation",
        "bbox": (10.0, 0.0, 120.0, 30.0),
        "angle": 0,
        "content": "numerator\nbody        sidecar\ndenominator",
    }


def test_detached_formula_anchor_collects_multiline_formula_but_not_body_prefix() -> None:
    """Verify that the low right edge anchor point traces back to the multi-line formula, and excludes left-aligned text and right-sided periods."""

    body_font = ("Body", 0)
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 12.0, 24.0))
    ]
    body_prefix = _text_line(
        "regular prose before formula",
        (0.0, 60.0, 60.0, 70.0),
        3,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    formula_lines = [
        _text_line("numerator", (20.0, 42.0, 50.0, 52.0), 4, effective_height=10.0),
        _text_line("Fp =", (15.0, 52.0, 45.0, 62.0), 5, effective_height=10.0),
        _text_line("otherwise", (20.0, 65.0, 65.0, 75.0), 6, effective_height=10.0),
        _text_line("0,", (68.0, 72.0, 78.0, 82.0), 7, effective_height=10.0),
    ]
    punctuation = _text_line(".", (93.0, 45.0, 96.0, 55.0), 8, effective_height=10.0)
    number = _text_line("(7)", (91.0, 75.0, 100.0, 85.0), 9, effective_height=10.0)
    lane_lines = [*body_lines, body_prefix, *formula_lines, punctuation, number]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[(line, line.bbox) for line in lane_lines],
    )

    anchors = formulas._find_formula_spatial_anchors(lane, 10.0)

    assert len(anchors) == 1
    assert anchors[0].line is number
    assert anchors[0].detached_below_body

    anchor_center = geometry._bbox_center_y(anchors[0].bbox)
    dominant_font = formulas._infer_formula_body_font(lane, 10.0)
    members = formulas._grow_formula_spatial_component(
        lane,
        anchors[0],
        anchor_center - 4.75 * 10.0,
        anchor_center + 2.25 * 10.0,
        set(),
        [],
        dominant_font,
        10.0,
    )
    member_texts = {line.text for line, _bbox in members}

    assert member_texts == {"numerator", "Fp =", "otherwise", "0,", "(7)"}
    assert body_prefix.text not in member_texts
    assert punctuation.text not in member_texts


def test_formula_above_dense_body_collects_math_but_stops_at_title_barrier() -> None:
    """Verify that formulas above text-heavy areas aggregate and do not absorb immediately adjacent section headings."""

    body_font = ("Body", 0)
    formula_lines = [
        _text_line(
            "formula numerator",
            (20.0, 8.0, 58.0, 18.0),
            0,
            effective_height=10.0,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
        _text_line(
            "formula denominator",
            (25.0, 19.0, 55.0, 29.0),
            1,
            effective_height=10.0,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
    ]
    number = _text_line("(5)", (91.0, 17.0, 100.0, 27.0), 2, effective_height=10.0)
    heading = _text_line(
        "neutral section heading",
        (0.0, 28.0, 40.0, 42.0),
        3,
        effective_height=14.0,
        font_signature=("Heading", 0),
        font_coverage=1.0,
    )
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            4 + index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((44.0, 56.0, 68.0, 80.0))
    ]
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            *((line, line.bbox) for line in formula_lines),
            (number, number.bbox),
            (heading, heading.bbox),
            *((line, line.bbox) for line in body_lines),
        ],
    )

    anchors = formulas._find_formula_spatial_anchors(lane, 10.0, body_font)

    assert len(anchors) == 1
    assert anchors[0].detached_above_body
    members = formulas._grow_formula_spatial_component(
        lane,
        anchors[0],
        0.0,
        100.0,
        set(),
        [],
        body_font,
        10.0,
    )

    assert {line.text for line, _bbox in members} == {
        "formula numerator",
        "formula denominator",
        "(5)",
    }


def test_formula_number_cannot_upgrade_ordinary_body_row() -> None:
    """When the verification bracket number lacks an independent formula body, ordinary text cannot be upgraded to a formula."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 12.0, 48.0, 60.0))
    ]
    ordinary = _text_line(
        "ordinary body row",
        (0.0, 30.0, 70.0, 40.0),
        4,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    number = _text_line("(9)", (91.0, 30.0, 100.0, 40.0), 5, effective_height=10.0)

    blocks, remaining = formulas._build_formula_like_blocks(
        [*lines, ordinary, number],
        [],
        (100.0, 80.0),
    )

    assert blocks == []
    assert ordinary in remaining
    assert number in remaining


def test_overlapping_denominator_cannot_become_short_formula_anchor() -> None:
    """Verify that right-edge denominator characters that overlap laterally with the text do not become non-numbered formula anchors."""

    body_font = ("Body", 0)
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 92.0, top + 10.0),
            index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 12.0, 24.0))
    ]
    formula_body = _text_line(
        "Pr = cp mu",
        (25.0, 36.0, 90.0, 46.0),
        3,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=0.8,
    )
    denominator = _text_line(
        "k",
        (88.0, 38.0, 92.0, 45.0),
        4,
        effective_height=7.0,
        font_signature=("Math", 1),
        font_coverage=1.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=92.0,
        lines=[
            *((line, line.bbox) for line in body_lines),
            (formula_body, formula_body.bbox),
            (denominator, denominator.bbox),
        ],
    )

    assert formulas._find_formula_spatial_anchors(lane, 10.0, body_font) == []


def test_compact_multiline_cluster_becomes_one_isolated_equation() -> None:
    """Verify that extractable compact F/G multi-line clusters form a single formula block and do not enter the surrounding text."""

    body_font = ("Body", 0)
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            visual_row_id=index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 12.0, 24.0))
    ]
    fragments = [
        _text_line(
            "F = numerator",
            (10.0, 36.0, 25.0, 49.0),
            3,
            visual_row_id=3,
            effective_height=8.0,
            font_signature=("Math", 1),
            font_coverage=0.5,
        ),
        _text_line(
            "denominator, G =",
            (20.0, 39.0, 40.0, 52.0),
            4,
            visual_row_id=4,
            split_from_row=True,
            effective_height=9.5,
            font_signature=("Math", 1),
            font_coverage=0.4,
        ),
        _text_line(
            "numerator",
            (46.0, 36.0, 52.0, 43.0),
            5,
            visual_row_id=4,
            split_from_row=True,
            effective_height=7.0,
            font_signature=("Math", 1),
            font_coverage=0.5,
        ),
        _text_line(
            "denominator",
            (46.0, 39.0, 53.0, 52.0),
            6,
            visual_row_id=5,
            effective_height=7.0,
            font_signature=("Math", 1),
            font_coverage=0.33,
        ),
    ]
    following = _text_line(
        "following body",
        (0.0, 54.0, 100.0, 64.0),
        7,
        visual_row_id=6,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )

    merged = line_merging._merge_overlapping_inline_text_clusters(
        [*body_lines, *fragments, following],
        (120.0, 100.0),
        [],
    )
    compact_cluster = next(line for line in merged if line.source_index == 3)
    blocks, remaining = formulas._build_formula_like_blocks(
        merged,
        [],
        (120.0, 100.0),
    )

    assert compact_cluster.compact_formula_cluster
    assert blocks == [
        {
            "type": "equation",
            "bbox": (10.0, 36.0, 53.0, 52.0),
            "angle": 0,
            "content": "F = numerator denominator, G = numerator denominator",
        }
    ]
    assert compact_cluster not in remaining


@pytest.mark.parametrize(
    ("candidate_bbox", "expected_equation_count"),
    [
        ((5.0, 36.0, 38.0, 46.0), 1),
        ((0.0, 36.0, 33.0, 46.0), 0),
    ],
    ids=["deliberate-indent", "flush-left"],
)
def test_compact_formula_accepts_deliberate_indent_but_rejects_flush_left(
    candidate_bbox: tuple[float, float, float, float],
    expected_equation_count: int,
) -> None:
    """Verify that compact formulas in the main text can be upgraded by clear left indentation, and the short text in the column remains as the main text."""

    body_font = ("Body", 0)
    candidate = replace(
        _text_line(
            "F = dY, G = dX",
            candidate_bbox,
            2,
            effective_height=10.0,
            font_signature=("Math", 1),
            font_coverage=0.5,
        ),
        compact_formula_cluster=True,
    )
    lines = [
        _text_line(
            "body above",
            (0.0, 20.0, 100.0, 30.0),
            0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        candidate,
        _text_line(
            "body below",
            (0.0, 52.0, 100.0, 62.0),
            1,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    blocks, remaining = formulas._build_formula_like_blocks(
        lines,
        [],
        (100.0, 100.0),
    )

    assert len(blocks) == expected_equation_count
    assert (candidate in remaining) is (expected_equation_count == 0)


def test_hanging_indent_reference_tail_is_not_unnumbered_equation() -> None:
    """Verify that a short-tailed line with a continuation line in the same font followed by a new entry with a left burst will not be accidentally centered and upgraded to a formula."""

    body_font = ("Body", 0)
    reference_font = ("Reference", 0)
    candidate = _text_line(
        "short reference tail",
        (15.0, 36.0, 85.0, 46.0),
        2,
        effective_height=10.0,
        font_signature=reference_font,
        font_coverage=0.5,
    )
    lines = [
        _text_line(
            "body above",
            (0.0, 0.0, 100.0, 10.0),
            0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "long reference continuation",
            (15.0, 24.0, 95.0, 34.0),
            1,
            effective_height=10.0,
            font_signature=reference_font,
            font_coverage=1.0,
        ),
        candidate,
        _text_line(
            "next reference entry",
            (0.0, 48.0, 100.0, 58.0),
            3,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body below",
            (0.0, 60.0, 100.0, 70.0),
            4,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    blocks, remaining = formulas._build_formula_like_blocks(
        lines,
        [],
        (100.0, 100.0),
    )

    assert blocks == []
    assert candidate in remaining


def _build_compact_margin_case(
    candidate_bbox: tuple[float, float, float, float],
    above_bbox: tuple[float, float, float, float],
    below_bbox: tuple[float, float, float, float],
) -> tuple[
    models._LineItem,
    list[dict[str, object]],
    list[models._LineItem],
]:
    """Construct a compact formula margin use case with stable text lines above and below."""

    body_font = ("Body", 0)
    above = _text_line(
        "body above",
        above_bbox,
        0,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    candidate = replace(
        _text_line(
            "formula cluster",
            candidate_bbox,
            1,
            effective_height=10.0,
            font_signature=("Math", 1),
            font_coverage=0.5,
        ),
        compact_formula_cluster=True,
    )
    below = _text_line(
        "body below",
        below_bbox,
        2,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )
    blocks, remaining = formulas._build_formula_like_blocks(
        [above, candidate, below],
        [],
        (100.0, 1000.0),
    )
    return candidate, blocks, remaining


def _build_text_formula_margin_case(
    formula_bboxes: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ],
    body_tops: tuple[float, float, float, float],
) -> tuple[list[dict[str, object]], list[models._LineItem]]:
    """Construct text formula margin use case with right edge numbering and stable body column band."""

    body_font = ("Body", 0)
    formula_lines = [
        _text_line(
            "formula numerator",
            formula_bboxes[0],
            0,
            effective_height=10.0,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
        _text_line(
            "formula denominator",
            formula_bboxes[1],
            1,
            effective_height=10.0,
            font_signature=("Math", 0),
            font_coverage=0.6,
        ),
        _text_line("(5)", formula_bboxes[2], 2, effective_height=10.0),
    ]
    body_lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            3 + index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate(body_tops)
    ]
    return formulas._build_formula_like_blocks(
        [*formula_lines, *body_lines],
        [],
        (100.0, 1000.0),
    )


@pytest.mark.parametrize(
    ("candidate_bbox", "above_bbox", "below_bbox"),
    [
        ((25.0, 20.0, 65.0, 30.0), (0.0, 0.0, 100.0, 10.0), (0.0, 40.0, 100.0, 50.0)),
        (
            (25.0, 970.0, 65.0, 980.0),
            (0.0, 940.0, 100.0, 950.0),
            (0.0, 990.0, 100.0, 1000.0),
        ),
    ],
    ids=["top", "bottom"],
)
def test_compact_formula_fully_in_page_margin_remains_text(
    candidate_bbox: tuple[float, float, float, float],
    above_bbox: tuple[float, float, float, float],
    below_bbox: tuple[float, float, float, float],
) -> None:
    """Verify that clusters of compact formulas are not promoted to formulas when they fall overall into the top or bottom 5%."""

    candidate, blocks, remaining = _build_compact_margin_case(
        candidate_bbox,
        above_bbox,
        below_bbox,
    )

    assert blocks == []
    assert candidate in remaining


def test_compact_formula_crossing_page_margin_boundary_remains_equation() -> None:
    """Verify that compact true formulas that cross the top 5% boundary still output formula blocks."""

    candidate, blocks, remaining = _build_compact_margin_case(
        (25.0, 45.0, 65.0, 55.0),
        (0.0, 30.0, 100.0, 40.0),
        (0.0, 60.0, 100.0, 70.0),
    )

    assert blocks == [
        {
            "type": "equation",
            "bbox": candidate.bbox,
            "angle": 0,
            "content": "formula cluster",
        }
    ]
    assert candidate not in remaining


@pytest.mark.parametrize(
    ("formula_bboxes", "body_tops"),
    [
        (
            (
                (20.0, 8.0, 58.0, 18.0),
                (25.0, 19.0, 55.0, 29.0),
                (91.0, 17.0, 100.0, 27.0),
            ),
            (44.0, 56.0, 68.0, 80.0),
        ),
        (
            (
                (20.0, 955.0, 58.0, 965.0),
                (25.0, 966.0, 55.0, 976.0),
                (91.0, 973.0, 100.0, 983.0),
            ),
            (900.0, 912.0, 924.0, 936.0),
        ),
    ],
    ids=["top", "bottom"],
)
def test_text_formula_fully_in_page_margin_remains_text(
    formula_bboxes: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ],
    body_tops: tuple[float, float, float, float],
) -> None:
    """Verify that the original text line is not claimed when the entire space component of the text formula falls within 5% of the page margin."""

    blocks, remaining = _build_text_formula_margin_case(
        formula_bboxes,
        body_tops,
    )

    assert blocks == []
    assert {line.source_index for line in remaining} == set(range(7))


@pytest.mark.parametrize(
    ("formula_bboxes", "body_tops", "expected_bbox"),
    [
        (
            (
                (20.0, 40.0, 58.0, 50.0),
                (25.0, 51.0, 55.0, 61.0),
                (91.0, 49.0, 100.0, 59.0),
            ),
            (76.0, 88.0, 100.0, 112.0),
            (20.0, 40.0, 100.0, 61.0),
        ),
        (
            (
                (20.0, 940.0, 58.0, 950.0),
                (25.0, 951.0, 55.0, 961.0),
                (91.0, 958.0, 100.0, 968.0),
            ),
            (885.0, 897.0, 909.0, 921.0),
            (20.0, 940.0, 100.0, 968.0),
        ),
    ],
    ids=["top", "bottom"],
)
def test_text_formula_crossing_page_margin_boundary_remains_equation(
    formula_bboxes: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ],
    body_tops: tuple[float, float, float, float],
    expected_bbox: tuple[float, float, float, float],
) -> None:
    """Verify that textual true formulas that cross the top or bottom 5% boundaries are still output."""

    blocks, remaining = _build_text_formula_margin_case(
        formula_bboxes,
        body_tops,
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "equation"
    assert blocks[0]["bbox"] == expected_bbox
    assert {line.source_index for line in remaining} == {3, 4, 5, 6}


def test_justified_mixed_font_visual_row_before_body_is_not_formula_anchor() -> None:
    """Verify that full-column counterparts with bold phrases mixed with regular fonts will not misjudge formulas due to short words on the right edge."""

    body_font = ("Body", 0)
    heading_font = ("Heading", 1)
    lines = [
        _text_line(
            f"body-{index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            visual_row_id=index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 12.0, 24.0))
    ]
    fragments = [
        _text_line(
            "label",
            (0.0, 36.0, 20.0, 46.0),
            10,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=heading_font,
            font_coverage=1.0,
        ),
        _text_line(
            "one",
            (24.0, 36.0, 38.0, 46.0),
            11,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "two",
            (43.0, 36.0, 57.0, 46.0),
            12,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "three",
            (63.0, 36.0, 80.0, 46.0),
            13,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "contains",
            (86.0, 36.0, 100.0, 46.0),
            14,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]
    continuation = _text_line(
        "continuation body row",
        (0.0, 46.0, 100.0, 56.0),
        15,
        visual_row_id=11,
        effective_height=10.0,
        font_signature=body_font,
        font_coverage=1.0,
    )

    blocks, remaining = formulas._build_formula_like_blocks(
        [*lines, *fragments, continuation],
        [],
        (100.0, 100.0),
    )

    assert blocks == []
    assert {line.source_index for line in remaining} == {
        0,
        1,
        2,
        10,
        11,
        12,
        13,
        14,
        15,
    }


def test_split_visual_row_with_right_number_forms_one_equation() -> None:
    """Verify that multi-font peer formulas and their right-hand numbers are combined into a formula block before column inference."""

    body_font = ("Body", 0)
    math_font = ("Math", 0)
    lines = [
        _text_line(
            "body above",
            (0.0, 10.0, 100.0, 20.0),
            0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "E =",
            (20.0, 35.0, 40.0, 45.0),
            1,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=math_font,
            font_coverage=0.6,
        ),
        _text_line(
            "mc2",
            (42.0, 35.0, 65.0, 45.0),
            2,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=("MathItalic", 0),
            font_coverage=0.6,
        ),
        _text_line(
            "(8)",
            (90.0, 35.0, 100.0, 45.0),
            3,
            visual_row_id=10,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body below",
            (0.0, 60.0, 100.0, 70.0),
            4,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    blocks, remaining = formulas._build_formula_like_blocks(
        lines,
        [],
        (100.0, 100.0),
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "equation"
    assert blocks[0]["content"].endswith(r"\tag{8}")
    assert "(8)" not in blocks[0]["content"]
    assert {line.source_index for line in remaining} == {0, 4}


def test_split_visual_row_formula_tail_defers_to_spatial_growth() -> None:
    """Verify that the tail of the fraction is claimed after a delay, and the complete formula is collected by the spatial anchor point."""

    body_font = ("Body", 0)
    math_font = ("Math", 0)
    italic_font = ("MathItalic", 0)
    bracket_font = ("Bracket", 0)
    lines = [
        _text_line(
            "body above 1",
            (0.0, 0.0, 100.0, 10.0),
            0,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body above 2",
            (0.0, 12.0, 100.0, 22.0),
            1,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "formula intro",
            (0.0, 36.0, 100.0, 46.0),
            2,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "at = 1",
            (10.0, 63.0, 28.0, 73.0),
            3,
            effective_height=10.0,
            font_signature=italic_font,
            font_coverage=0.5,
        ),
        _text_line(
            "2 ln",
            (30.0, 67.0, 42.0, 77.0),
            4,
            effective_height=10.0,
            font_signature=math_font,
            font_coverage=1.0,
        ),
        _text_line(
            "( 1 - εt",
            (45.0, 52.0, 65.0, 82.0),
            5,
            effective_height=10.0,
            font_signature=bracket_font,
            font_coverage=0.2,
        ),
        _text_line(
            "εt",
            (58.0, 72.0, 62.0, 80.0),
            6,
            visual_row_id=10,
            run_index=0,
            split_from_row=True,
            effective_height=8.0,
            font_signature=italic_font,
            font_coverage=0.5,
        ),
        _text_line(
            ")",
            (67.0, 52.0, 70.0, 82.0),
            7,
            visual_row_id=10,
            run_index=1,
            split_from_row=True,
            effective_height=30.0,
            font_signature=bracket_font,
            font_coverage=1.0,
        ),
        _text_line(
            "（3）",
            (90.0, 67.0, 100.0, 77.0),
            8,
            visual_row_id=10,
            run_index=2,
            split_from_row=True,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=0.67,
        ),
        _text_line(
            "body below 1",
            (0.0, 86.0, 100.0, 96.0),
            9,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
        _text_line(
            "body below 2",
            (0.0, 98.0, 100.0, 108.0),
            10,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        ),
    ]

    early_blocks, early_claimed = formulas._build_split_visual_row_formula_blocks(
        lines,
        [],
        (100.0, 120.0),
    )
    blocks, remaining = formulas._build_formula_like_blocks(
        lines,
        [],
        (100.0, 120.0),
    )

    assert early_blocks == []
    assert early_claimed == set()
    assert blocks == [
        {
            "type": "equation",
            "bbox": (10.0, 52.0, 100.0, 82.0),
            "angle": 0,
            "content": "at = 1   ( 1 - εt)\n2 ln\nεt\\tag{3}",
        }
    ]
    assert {line.source_index for line in remaining} == {0, 1, 2, 9, 10}


def test_centered_low_body_font_line_forms_unnumbered_equation() -> None:
    """Verify that lines of math font centered between upper and lower body text and covered by lower body text form unnumbered equations."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            f"body {index}",
            (0.0, top, 100.0, top + 10.0),
            index,
            effective_height=10.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index, top in enumerate((0.0, 14.0, 58.0, 72.0))
    ]
    lines.append(
        _text_line(
            "|E(X) - M| < n-1",
            (25.0, 36.0, 75.0, 46.0),
            10,
            effective_height=10.0,
            font_signature=("Math", 0),
            font_coverage=0.5,
        )
    )

    blocks, remaining = formulas._build_formula_like_blocks(
        lines,
        [],
        (100.0, 100.0),
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "equation"
    assert blocks[0]["content"] == "|E(X) - M| < n-1"
    assert {line.source_index for line in remaining} == {0, 1, 2, 3}


def test_punctuated_number_anchor_protects_stacked_formula_member() -> None:
    """Validating short punctuation numbers on the right prevents fraction members from being recognized prematurely as unnumbered formulas."""

    candidate = _text_line(
        "fraction numerator",
        (20.0, 30.0, 65.0, 40.0),
        1,
        effective_height=10.0,
        font_signature=("Math", 0),
        font_coverage=0.5,
    )
    number_anchor = _text_line(
        ", (4)",
        (66.0, 32.0, 78.0, 42.0),
        2,
        effective_height=10.0,
        font_signature=("Body", 0),
        font_coverage=1.0,
    )
    lane = models._TextLane(
        left=0.0,
        right=100.0,
        lines=[
            (candidate, candidate.bbox),
            (number_anchor, number_anchor.bbox),
        ],
    )

    assert formulas._has_nearby_punctuated_formula_number_anchor(
        (candidate, candidate.bbox),
        lane,
        median_height=10.0,
    )
