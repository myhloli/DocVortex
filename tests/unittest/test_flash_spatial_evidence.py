"""空间规则的正反例及平移、缩放验证，文字使用与真实原件无关的占位内容。"""

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
    """构造具有独立字号、字形范围及来源身份的中性测试行。"""
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
    """缩放和平移不改变独立粗体小标题判断，图例、参考与同行强调不能提升。"""
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
    """独立改变栏宽及页面变换后，栏首和近满栏标题可提升，越栏和侧栏中部不能提升。"""
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
    """宽空格分开的段首三个成员先恢复整行，后继正文的缩进支持使用整行左缘。"""
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
    """短首行与正文续行合并时，源成员集合必须与内容和行框同时完整传递。"""
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
    """居中参照来自后继正文的重复左右缘，同行碎片不能变成狭窄栏。"""
    rows = [
        _line("An indented first line.", (62, 100, 280, 110), 0),
        _line("Left part", (40, 113, 115, 123), 1),
        _line("right part", (135, 113, 280, 123), 2),
        _line("A full third row.", (40, 126, 280, 136), 3),
    ]
    bounds = _following_stable_body_bounds(rows, 0, 10)
    assert bounds == (40, 100, 280, 136)


def test_wide_gutter_numeric_cells_do_not_use_prose_row_recovery():
    """重复数值列不能采用段首正文的宽空格恢复，保留其独立单元格成员。"""
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
    """下方三栏编号区的规则在缩放和平移后仍不能侵入上方两栏正文。"""
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
    """作者年代式条目的年份不能被误作新条目编号。"""
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
    """上一页作者年代式参考上下文不能把下一页普通数字列表升级为编号文献。"""
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
    assert not appendix.reference_regions


@pytest.mark.parametrize("marker", ["(B7)", "‹B7›", "⟦B7⟧"])
@pytest.mark.parametrize("scale", [0.8, 1.0, 1.6])
def test_number_role_uses_column_and_isolated_math_band(marker, scale):
    """不同包围符的右侧短编号可归入公式，紧邻的短正文不能吸入。"""
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
    """行内百分比不构成包围式编号，不会导致正文成员被公式恢复删除。"""
    lines = [
        _line("An ordinary sentence with some body words.", (40, y, 280, y + 10), i)
        for i, y in enumerate((40, 55, 70, 200, 215, 230))
    ]
    lines += [_line("u = v + w", (50, 110, 210, 130), 10), _line(percentage, (250, 115, 280, 125), 11)]
    assert _build_spatial_numbered_bands(lines, [], (320, 300), []) == ([], set())


def test_outer_table_rule_does_not_make_notes_into_cells():
    """表体结束后的全宽说明从外框中分离，而双列真实单元行保持在表内。"""
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
    """新编号禁止续接，跨页同条续文即使以数字开头也保留连续关系。"""
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


def test_reference_carry_needs_evidence_in_the_new_column():
    """前页的文献标题不会让后一页的普通正文或图后的段落带上参考硬组。"""
    refs = _PreparedPage(
        (600, 800),
        [
            _line("References", (40, 680, 160, 690), 0),
            _line("Novak, J. (2020). Observations across", (40, 714, 460, 724), 1),
            _line("several independent sites and", (56, 730, 460, 740), 2),
        ],
        [],
        [],
        [],
    )
    tail = _line("their associated field reports.", (56, 62, 318, 72), 0)
    entries = [
        _line("Lopez, M. (2019). Another field study.", (40, 79, 470, 89), 1),
        _line("Journal of Experiments, 10, 101–121.", (56, 94, 457, 104), 2),
        _line("Tan, A. (2022). Independent measurements.", (40, 111, 471, 121), 3),
    ]
    body = [
        _line("Ordinary prose from the next section.", (40 + (i % 2) * 16, 270 + i * 15, 450, 280 + i * 15), 10 + i)
        for i in range(4)
    ]
    next_page = _PreparedPage((600, 800), [tail, *entries, *body], [], [], [{"type": "image", "bbox": (40, 140, 450, 240)}])
    unrelated = _PreparedPage((600, 800), body, [], [], [])
    mark_document_reference_regions([refs, next_page, unrelated])
    assert any(box[1] <= 66 < 118 <= box[3] <= 140 for box in next_page.reference_regions)
    group_reference_lines(next_page.remaining_lines, next_page)
    assert all(line.paragraph_group is None for line in body)
    assert not unrelated.reference_regions


def test_unfinished_author_year_entry_can_end_on_the_next_page():
    """仅剩顶端续行时，依据上页未完条目的挂缩进保留它，并在左缘正文处终止。"""
    refs = _PreparedPage(
        (600, 800),
        [
            _line("Bibliography", (40, 650, 160, 660), 0),
            _line("Young, M. (2012). Proceedings from", (40, 700, 440, 710), 1),
            _line("the international conference on", (55, 715, 455, 725), 2),
        ],
        [],
        [],
        [],
    )
    finish = _line("materials, pp. 15–27.", (55, 60, 210, 70), 0)
    prose = _line("This chapter introduces the next experiment.", (40, 114, 420, 124), 1)
    page = _PreparedPage((600, 800), [finish, prose], [], [], [])
    mark_document_reference_regions([refs, page])
    assert len(page.reference_regions) == 1
    assert page.reference_regions[0][1] <= 60 and 70 <= page.reference_regions[0][3] < 114


@pytest.mark.parametrize("indent", [0, 14])
def test_carried_bibliography_with_publication_years_inside_wrapped_names(indent):
    """跨页作者名单的年代可在续行才出现；无悬挂缩进的书目也应保留整栏范围。"""
    first = _PreparedPage(
        (600, 800),
        [
            _line("References", (40, 600, 210, 610), 0),
            _line("Paul Nguyen and Alex Martin. 2018. Collected essays.", (40, 623, 420, 633), 1),
            _line("Series in History.", (54, 637, 311, 647), 2),
        ],
        [],
        [],
        [],
    )
    rows = [
        "Eva Martin and Thomas Young. A line of authors,",
        "and Marie Schmidt. 2020. Measurement of dispersions.",
        "Society Proceedings, vol. 3.",
        "Luis Pinto, Anna Brown and researchers",
        "from two institutes. 2016. Experiments in the field.",
        "Collected papers, pp. 19–23.",
    ]
    lines = [_line(s, (40 + (indent if i % 3 else 0), 62 + i * 16, 460, 72 + i * 16), i) for i, s in enumerate(rows)]
    second = _PreparedPage((600, 800), lines, [], [], [])
    mark_document_reference_regions([first, second])
    assert any(r[1] <= 62 and r[3] >= 152 for r in second.reference_regions)


def test_linked_citation_tail_and_date_on_its_own_line_protect_reference_items():
    """跨页 DOI 续行、作者较多的编号条目和独占一行的年份均能建立栏内书目证据。"""
    for prefix in (
        ["Ortiz Moore and Claire Jones. Research with long authors", "Data about the surveys", "2018."],
        ["vances in statistical models.", "M. (2018), https://doi.org/10.1000/example"],
    ):
        first = _PreparedPage(
            (600, 800),
            [
                _line("References", (50, 520, 180, 531), 0),
                _line("Anders, R. (2022). Earlier research.", (50, 545, 353, 556), 1),
                _line("Collected papers.", (63, 560, 214, 571), 2),
            ],
            [],
            [],
            [],
        )
        data = prefix + [
            "2. Peters, K. and Dixon, R.",
            "Research on classification.",
            "2019.",
            "3. Smith, L. (2020). Other sources.",
        ]
        lines = [
            _line(t, (50 if i == 0 or t[:1] in {"2", "3"} and ". " in t[:3] else 63, 65 + i * 14, 408, 75 + i * 14), i)
            for i, t in enumerate(data)
        ]
        page = _PreparedPage((600, 800), lines, [], [], [])
        mark_document_reference_regions([first, page])
        assert any(r[1] <= 65 and r[3] >= lines[-1].bbox[3] for r in page.reference_regions)
