"""Positive and negative examples of spatial rules and verification of translation and zooming. The text uses placeholder content that has nothing to do with the real original."""

from dataclasses import replace

import pytest

from docvortex.analyzers.native.pdf.models import _LineItem, _PreparedPage, _AxisLine, _PageSource, _TableCandidate
from docvortex.analyzers.native.pdf.text_assembly.continuity import mark_document_reference_regions, group_reference_lines
from docvortex.analyzers.native.pdf.formulas import _build_spatial_numbered_bands
from docvortex.analyzers.native.pdf.table_materialization import _separate_embedded_table_notes
from docvortex.postprocess.paragraphs import merge_para_text_blocks
from docvortex.analyzers.native.pdf.title_analysis.compact_titles import _classify_small_emphasized_titles
from docvortex.analyzers.native.pdf.title_analysis.structural import _following_stable_body_bounds
from docvortex.analyzers.native.pdf.line_merging import _merge_post_semantic_text_runs
from docvortex.analyzers.native.pdf.text_assembly.merging import _merge_spatial_text_components


def _line(text, bbox, index, scale=1.0, offset=0.0):
    """Construct neutral test lines with independent font size, font range, and source identity."""
    bounds = tuple(value * scale + offset for value in bbox)
    return _LineItem(
        text,
        bounds,
        0,
        index,
        ink_bbox=bounds,
        effective_height=10 * scale,
        em_height=10 * scale,
        font_signature=("Neutral", 0),
        font_coverage=1.0,
    )


@pytest.mark.parametrize("scale,offset", [(0.7, 13), (1.0, 0), (1.8, 29)])
@pytest.mark.parametrize("excluded", [None, "regular", "reference", "caption", "container", "runin"])
def test_small_emphasized_heading_requires_independent_body_transition(scale, offset, excluded):
    """Zooming and panning do not change the judgment of independent bold subtitles, and legends, references, and peer emphasis cannot be improved."""
    lines = [
        _line("An ordinary preceding paragraph.", (40, 100, 280, 110), 0, scale, offset),
        _line("A neutral heading", (52, 128, 180, 135), 1, scale, offset),
        _line("An ordinary first sentence in this paragraph.", (52, 152, 280, 162), 2, scale, offset),
        _line("The next physical row returns to its left edge.", (40, 165, 280, 175), 3, scale, offset),
        _line("Another ordinary sentence in this paragraph.", (40, 178, 280, 188), 4, scale, offset),
    ]
    for line in lines:
        line.dominant_font_weight = 400
    heading = lines[1]
    heading.dominant_font_weight = 650 if excluded != "regular" else 400
    heading.effective_height = heading.em_height = 7 * scale
    heading.font_signature = ("Emphasized", 0)
    if excluded == "reference":
        heading.paragraph_group = 1
    if excluded == "caption":
        heading.caption_start = True
    if excluded == "runin":
        heading.bbox = lines[0].bbox
    containers = (
        [{"type": "image", "bbox": (35 * scale + offset, 120 * scale + offset, 290 * scale + offset, 195 * scale + offset)}]
        if excluded == "container"
        else []
    )
    page = _PreparedPage((600 * scale + offset, 800 * scale + offset), lines, [], [], fixed_blocks=containers)
    _classify_small_emphasized_titles(page, 1)
    assert (heading.semantic_type == "paragraph_title") == (excluded is None)


@pytest.mark.parametrize("scale,offset", [(0.7, 13), (1.0, 0), (1.8, 29)])
@pytest.mark.parametrize("right", [240, 320])
@pytest.mark.parametrize("case", ["column_start", "wide", "cross_edge", "lower_side_column"])
def test_small_heading_uses_body_top_and_actual_column_edge(scale, offset, right, case):
    """After independently changing the column width and changing the page, the header of the column and the nearly full column title can be increased, but the overcolumn and the middle part of the side column cannot be increased."""
    heading = _line(
        "Another neutral heading", (52, 128, right + 12 if case == "cross_edge" else right - 2, 135), 1, scale, offset
    )
    heading.font_signature = ("Different emphasized family", 0)
    heading.dominant_font_weight = 650
    heading.effective_height = heading.em_height = 7 * scale
    lines = [heading] + [
        _line("A normal line from the following body paragraph.", (x, y, right, y + 10), i + 2, scale, offset)
        for i, (x, y) in enumerate([(52, 152), (40, 165), (40, 178)])
    ]
    if case in {"wide", "cross_edge"}:
        lines.insert(0, _line("The previous paragraph ends.", (40, 100, right, 110), 0, scale, offset))
    if case == "lower_side_column":
        lines += [
            _line("The main body has already started above.", (right + 50, y, right * 2, y + 10), 10 + i, scale, offset)
            for i, y in enumerate([50, 63, 76, 89])
        ]
    for line in lines:
        if line is not heading:
            line.dominant_font_weight = 400
    page = _PreparedPage(((right * 2 + 40) * scale + offset, 800 * scale + offset), lines, [], [], [])
    _classify_small_emphasized_titles(page, 1)
    assert (heading.semantic_type == "paragraph_title") == (case in {"column_start", "wide"})


@pytest.mark.parametrize("scale,offset", [(0.7, 13), (1.0, 0), (1.8, 29)])
def test_justified_indented_row_members_are_recovered_before_paragraph_merging(scale, offset):
    """The first three members of a paragraph separated by wide spaces are restored to the entire line first, and subsequent text indentation supports using the entire left margin of the line."""
    rows = [
        _line("A previous sentence ends here.", (40, 90, 210, 100), 0, scale, offset),
        _line("Leading", (52, 103, 100, 113), 1, scale, offset),
        _line("a", (121, 103, 131, 113), 2, scale, offset),
        _line("new paragraph", (152, 103, 270, 113), 3, scale, offset),
        _line("The following physical row occupies this column.", (40, 116, 270, 126), 4, scale, offset),
        _line("Another row of the same ordinary paragraph.", (40, 129, 270, 139), 5, scale, offset),
    ]
    for row in rows[1:4]:
        row.visual_row_id = 1
    result = _merge_post_semantic_text_runs(rows, (600 * scale + offset, 800 * scale + offset), [])
    assert sum(row.text == "Leading a new paragraph" for row in result) == 1
    assert result[0].text == rows[0].text


def test_spatial_component_merge_keeps_member_and_geometry_sets_together():
    """When merging short first lines with text continuation lines, the source member collection must be passed in its entirety along with the content and line boxes."""
    lines = [_line("An opener", (40, 100, 65, 110), 1), _line("followed by a full ordinary body row", (40, 112, 270, 122), 2)]
    blocks = [
        {
            "type": "text",
            "content": line.text,
            "bbox": line.bbox,
            "_text_lines": [line],
            "_local_line_bboxes": [line.bbox],
            "_local_output_line_bboxes": [line.bbox],
            "_line_heights": [10],
            "_lane_interval": (40, 270),
            "_lane_is_span": False,
            "_font_signatures": {line.font_signature},
        }
        for line in lines
    ]
    result = _merge_spatial_text_components(blocks, (600, 800))
    assert len(result) == 1
    assert {line.source_index for line in result[0]["_text_lines"]} == {1, 2}
    assert result[0]["_local_line_bboxes"] == [line.bbox for line in lines]
    assert all(line.text in result[0]["content"] for line in lines)


def test_body_corridor_ignores_first_line_indent_and_handles_inline_fragments():
    """Centered references to repeated left and right margins from subsequent text, peer fragments cannot become narrow columns."""
    rows = [
        _line("An indented first line.", (62, 100, 280, 110), 0),
        _line("Left part", (40, 113, 115, 123), 1),
        _line("right part", (135, 113, 280, 123), 2),
        _line("A full third row.", (40, 126, 280, 136), 3),
    ]
    bounds = _following_stable_body_bounds(rows, 0, 10)
    assert bounds == (40, 100, 280, 136)


def test_wide_gutter_numeric_cells_do_not_use_prose_row_recovery():
    """Repeated numeric columns cannot be recovered by using wide spaces in the paragraph header text, retaining their independent cell members."""
    rows = [
        _line("Neutral label", (40, 100, 130, 110), 0),
        _line("32.68%", (151, 100, 205, 110), 1),
        _line("Another label 67.32%", (40, 113, 205, 123), 2),
    ]
    rows[0].visual_row_id = rows[1].visual_row_id = 1
    result = _merge_post_semantic_text_runs(rows, (600, 800), [])
    assert len(result) == 3


@pytest.mark.parametrize("scale,offset", [(1.0, 0.0), (0.7, 13.0), (1.8, 29.0)])
def test_numbered_reference_band_does_not_claim_upper_body(scale, offset):
    """The rules in the numbered area in the lower three columns still cannot invade the main text in the upper two columns after zooming and panning."""
    body = [
        _line("Ordinary body paragraph with neutral words.", (x, y, x + 240, y + 10), i, scale, offset)
        for i, (x, y) in enumerate((x, y) for x in (40, 320) for y in (70, 85, 100, 115))
    ]
    refs = [
        _line(
            f"[{column * 3 + item + 1}] A neutral reference.",
            (x, 410 + item * 30, x + 140, 420 + item * 30),
            20 + column * 3 + item,
            scale,
            offset,
        )
        for column, x in enumerate((40, 230, 420))
        for item in range(3)
    ]
    page = _PreparedPage((600 * scale + offset, 800 * scale + offset), body + refs, [], [], [])
    mark_document_reference_regions([page])
    group_reference_lines(page.remaining_lines, page)
    assert len(page.reference_regions) == 3
    assert all(line.paragraph_group is None for line in body)
    assert len({line.paragraph_group for line in refs}) == 9
    assert all(line.reference_start for line in refs)


def test_publication_years_are_not_new_numbered_entries():
    """The year of an author's chronological entry cannot be mistaken for a new entry number."""
    lines = [_line("References", (40, 100, 150, 110), 0)]
    for i in range(3):
        lines.extend(
            [
                _line(f"Author {chr(65 + i)} and collaborators.", (40, 140 + i * 45, 350, 150 + i * 45), i * 2 + 1),
                _line("2020. An unrelated publication title.", (55, 155 + i * 45, 350, 165 + i * 45), i * 2 + 2),
            ]
        )
    page = _PreparedPage((600, 800), lines, [], [], [])
    mark_document_reference_regions([page])
    group_reference_lines(lines, page)
    assert not page.numbered_references
    assert len({line.paragraph_group for line in lines[1:]}) == 3


def test_author_year_references_do_not_enable_appendix_numbered_lists():
    """The author chronological reference context on the previous page cannot promote the ordinary number list on the next page to a numbered document."""
    references = _PreparedPage(
        (600, 800),
        [_line("References", (40, 100, 150, 110), 0)]
        + [_line("An author and an unrelated publication.", (40, 140 + i * 20, 350, 150 + i * 20), i + 1) for i in range(4)],
        [],
        [],
        [],
    )
    appendix = _PreparedPage(
        (600, 800),
        [_line(f"{i + 1}. A neutral numbered instruction.", (60, 140 + i * 40, 350, 150 + i * 40), i) for i in range(4)],
        [],
        [],
        [],
    )
    mark_document_reference_regions([references, appendix])
    assert not appendix.numbered_references


@pytest.mark.parametrize("marker", ["(B7)", "‹B7›", "⟦B7⟧"])
@pytest.mark.parametrize("scale", [0.8, 1.0, 1.6])
def test_number_role_uses_column_and_isolated_math_band(marker, scale):
    """The short numbers on the right side of different brackets can be included in the formula, and the short text immediately adjacent to them cannot be inhaled."""
    lines = [
        _line("A neutral body sentence with enough words.", (40, y, 280, y + 10), i, scale)
        for i, y in enumerate((50, 65, 80, 200, 215, 230))
    ]
    lines += [
        _line("is given by", (40, 95, 105, 105), 10, scale),
        _line("u = v + w", (80, 130, 210, 140), 11, scale),
        _line("q", (160, 144, 170, 154), 12, scale),
        _line(marker, (255, 132, 280, 142), 13, scale),
    ]
    bands, claimed = _build_spatial_numbered_bands(lines, [], (320 * scale, 300 * scale), [])
    assert len(bands) == 1
    assert claimed == {11, 12, 13}


@pytest.mark.parametrize("percentage", ["100%", "１００％"])
def test_percentage_at_line_end_is_not_an_equation_number(percentage):
    """Inline percentages do not constitute bracketed numbers and will not cause body members to be deleted by formula recovery."""
    lines = [
        _line("An ordinary sentence with some body words.", (40, y, 280, y + 10), i)
        for i, y in enumerate((40, 55, 70, 200, 215, 230))
    ]
    lines += [_line("u = v + w", (50, 110, 210, 130), 10), _line(percentage, (250, 115, 280, 125), 11)]
    assert _build_spatial_numbered_bands(lines, [], (320, 300), []) == ([], set())


def test_outer_table_rule_does_not_make_notes_into_cells():
    """The full-width description after the table body ends is separated from the outer box, while the double-column real cell row remains within the table."""
    notes = [
        _line("* A neutral note spanning the table width.", (40, 152, 275, 162), 1),
        _line("Continuation of the same explanatory note.", (40, 166, 275, 176), 2),
    ]
    rules = [_AxisLine((40, y, 280, y), 1, "horizontal") for y in (80, 100, 120, 145, 185)]
    source = _PageSource((320, 400), notes, [], rules)
    candidate = _TableCandidate((40, 80, 280, 185), (40, 80, 280, 185), 0, 1.0, core_bbox=(40, 80, 280, 185))
    _separate_embedded_table_notes(source, candidate)
    assert candidate.core_bbox[3] == 145
    assert candidate.annotations[0].line_indices == {1, 2}
    second = replace(candidate, core_bbox=(40, 80, 280, 185), annotations=[])
    source.lines.append(_line("Independent cell", (200, 168, 275, 178), 3))
    _separate_embedded_table_notes(source, second)
    assert not second.annotations


def test_reference_boundaries_survive_public_page_finalization():
    """Continuations with new numbers are prohibited, and continuations of the same article across two pages will be maintained even if they begin with a number."""
    pages = [
        {
            "page_idx": 0,
            "blocks": [
                {
                    "type": "text",
                    "index": 0,
                    "bbox": [0.1, 0.8, 0.45, 0.9],
                    "content": "[1] An unfinished reference",
                    "lines": [{"bbox": [0.1, 0.8, 0.45, 0.82]}],
                    "_reference_start": True,
                },
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {
                    "type": "text",
                    "index": 0,
                    "bbox": [0.1, 0.1, 0.45, 0.12],
                    "content": "2024;10:12-13.",
                    "lines": [{"bbox": [0.1, 0.1, 0.45, 0.12]}],
                    "_reference_start": False,
                },
                {
                    "type": "text",
                    "index": 1,
                    "bbox": [0.1, 0.14, 0.45, 0.16],
                    "content": "[2] A different reference",
                    "lines": [{"bbox": [0.1, 0.14, 0.45, 0.16]}],
                    "_reference_start": True,
                },
            ],
        },
    ]
    merge_para_text_blocks(pages)
    assert pages[1]["blocks"][0]["continues_prev"]
    assert not pages[1]["blocks"][1].get("continues_prev")
