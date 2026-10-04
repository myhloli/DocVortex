"""Change the verification rule type with neutral content and layout to prevent real papers from becoming a production judgment condition."""

from dataclasses import replace

import pytest

from docvortex.analyzers.native.pdf.formulas import _recover_detached_display_components
from docvortex.analyzers.native.pdf.models import _LineItem, _PageSource
from docvortex.analyzers.native.pdf.table_materialization import _restore_front_matter_text_panel
from docvortex.analyzers.native.pdf.text_assembly.assembly import _restore_caption_wrap_text


def _line(text, bbox, index=0):
    """Construct neutral lines of text that preserve ink and source."""
    return _LineItem(text, bbox, 0, index, ink_bbox=bbox, effective_height=10, em_height=10)


@pytest.mark.parametrize("names", [("VarWin", "VarEnd"), ("FooWin", "FooEnd"), ("WindowStatistic", "TailStatistic")])
def test_custom_math_identifiers_do_not_depend_on_names(names):
    """Isogeometric expressions only replace identifiers, and the restored results and numbered members must be consistent."""
    lines = [_line(f"{names[0]} + {names[1]} + signal = 0", (100, 100, 260, 110)), _line("(1)", (270, 100, 285, 110), 1)]
    blocks, claimed = _recover_detached_display_components(lines, [], (600, 800))
    assert len(blocks) == 1 and claimed == {0, 1}


@pytest.mark.parametrize("right", [False, True])
@pytest.mark.parametrize("tails", [1, 2, 4])
def test_wrapped_prose_is_symmetric_and_accepts_multiple_tail_rows(right, tails):
    """Mirroring and multi-line wide-tailing do not change the area recovery of the same type of surrounding text."""
    boxes = [(50, 180, 280, 190), (50, 195, 280, 205), (50, 210, 280, 220)]
    boxes += [(50, 225 + 15 * i, 500, 235 + 15 * i) for i in range(tails)]
    cb, image = (300, 200, 500, 220), (300, 100, 500, 195)
    if right:
        boxes = [(600 - b[2], b[1], 600 - b[0], b[3]) for b in boxes]
        cb, image = (100, 200, 300, 220), (100, 100, 300, 195)
    lines = [_line(f"continuing words part{i}", b, i) for i, b in enumerate(boxes)]
    body = {
        "type": "text",
        "bbox": (50, 180, 550, boxes[-1][3]),
        "angle": 0,
        "content": "continuing words",
        "_text_lines": lines,
    }
    caption = {"type": "text", "bbox": cb, "angle": 0, "content": "Figure 1: Example."}
    blocks = _restore_caption_wrap_text([body, caption], [image], (600, 800))
    assert all(block is not body for block in blocks)
    rebuilt = [block for block in blocks if block is not caption]
    assert sorted(line.source_index for block in rebuilt for line in block["_text_lines"]) == list(range(len(lines)))
    assert not any(block.get("_explicit_break_before") for block in rebuilt)
    assert any(block.get("_geometry_break_before") for block in rebuilt)


@pytest.mark.parametrize("heading", ["Article Info", "Article Information", "文章信息"])
@pytest.mark.parametrize("right", [False, True])
def test_front_panel_uses_roles_and_not_exact_heading_pair(heading, right):
    """Synonymous titles and left-right interchanges still recognize the roles of metadata and sequential abstracts."""
    items = [(heading, (50, 200, 180, 210)), ("Abstract", (250, 200, 500, 210))]
    items += [
        (value, (50, 220 + i * 15, 190, 230 + i * 15))
        for i, value in enumerate(["Received yesterday", "Accepted today", "Keywords: trees"])
    ]
    items += [("This is a sentence with sufficient words for prose.", (250, 220 + i * 15, 540, 230 + i * 15)) for i in range(3)]
    lines = [_line(t, b, i) for i, (t, b) in enumerate(items)]
    if right:
        lines = [replace(line, bbox=(600 - line.bbox[2], line.bbox[1], 600 - line.bbox[0], line.bbox[3])) for line in lines]
    source = _PageSource((600, 800), lines, [], [], page_index=0)
    assert _restore_front_matter_text_panel(source, (40, 180, 560, 300))


def test_front_panel_does_not_relabel_later_pages():
    """Candidates and members are not modified when homepage role evidence is missing."""
    line = _line("Article Information", (50, 200, 180, 210))
    source = _PageSource((600, 800), [line], [], [], page_index=1)
    assert not _restore_front_matter_text_panel(source, (40, 180, 550, 300))
    assert line.semantic_type is None


@pytest.mark.parametrize(
    "text", ["The temperature value T=Delta", "温度可以表示为T=Delta", "These are ordinary words", "where Window(x)=Tail(y)"]
)
def test_natural_language_remains_prose(text):
    """The text outside the mathematical structure still provides host evidence, and the function call cannot swallow the leading natural language."""
    from docvortex.analyzers.native.pdf.formulas import _has_sentence_words

    assert _has_sentence_words(text)


@pytest.mark.parametrize("name", ["VarWin", "FooWin", "WindowStatistic"])
@pytest.mark.parametrize("scale", [0.75, 1.0, 1.5])
def test_pdf_entry_preserves_named_formula_under_scaling(name, scale):
    """After character extraction and Flash editing of the real PDF, renaming and proportional scaling still only generate a complete formula."""
    from io import BytesIO
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.document.pdf import PDFDocument
    from docvortex.analyzers.native import PdfModel

    stream = BytesIO()
    painter = Canvas(stream, pagesize=(600 * scale, 800 * scale))
    painter.scale(scale, scale)
    painter.setFont("Helvetica", 10)
    for y in [700, 680, 660]:
        painter.drawString(50, y, "This neutral paragraph supplies a stable body column for analysis.")
    painter.drawRightString(300, 580, f"{name}(x) + Tail(y) = 0")
    painter.drawString(235, 565, "Total(x)")
    painter.line(190, 577, 300, 577)
    painter.drawString(310, 580, "(1)")
    painter.save()
    with PDFDocument(stream.getvalue()) as pdf:
        page = PdfModel().predict(pdf)[0]
    equations = [block for block in page if block["type"] == "equation"]
    assert len(equations) == 1
    assert equations[0]["bbox"][0] <= 235 / 600 and equations[0]["bbox"][2] >= 320 / 600


@pytest.mark.parametrize("intervals", [[(40, 500)], [(40, 210), (260, 550)], [(20, 170), (210, 370), (410, 570)]])
def test_layout_evidence_uses_actual_asymmetric_columns(intervals):
    """Single column, asymmetric double column, and triple column are all defined by repeated text edges and do not rely on the center line of the page."""
    from docvortex.analyzers.native.pdf.layout_evidence import build_layout_evidence

    lines = [
        _line("Neutral paragraph with several words", (left, 100 + j * 15, right, 110 + j * 15), i * 10 + j)
        for i, (left, right) in enumerate(intervals)
        for j in range(4)
    ]
    layout = build_layout_evidence(lines, (600, 800))
    assert len(layout.lanes) == len(intervals)
    for a, b in zip(intervals, intervals[1:]):
        assert layout.separated((a[0], 100, a[1], 110), (b[0], 100, b[1], 110))
    if len(intervals) > 1:
        assert layout.corridor((intervals[0][0], 80, intervals[-1][1], 90)) == (0, 600)


@pytest.mark.parametrize("count", [1, 2, 4])
def test_front_matter_handles_any_number_of_affiliations(count):
    """The number of institutions is not a classification condition; the number and institutional role constitute evidence of grouping."""
    from docvortex.analyzers.native.pdf.text_assembly.continuity import group_front_matter_lines

    lines = [
        _line(f"{i + 1} Department of Science, Example University", (70, 150 + i * 15, 430, 160 + i * 15), i)
        for i in range(count)
    ]
    lines.append(_line("Abstract", (70, 300, 150, 310), 100))
    group_front_matter_lines(lines, (600, 800))
    assert [line.paragraph_group for line in lines[:-1]] == list(range(-1, -count - 1, -1))


@pytest.mark.parametrize("heading", ["Affiliations", "作者单位"])
def test_affiliation_role_does_not_merge_independent_numbered_notes(heading):
    """Even if there is an institution title, explicitly numbered entries are still output separately."""
    from docvortex.analyzers.native.pdf.text_assembly.footnotes import _split_page_footnote_entries

    lines = [
        _line(heading, (40, 600, 160, 610)),
        _line("1. Department of Science.", (40, 620, 560, 630), 1),
        _line("2. University of Example.", (40, 645, 560, 655), 2),
    ]
    groups = _split_page_footnote_entries(lines, (600, 800))
    assert not any(lines[1] in group and lines[2] in group for group in groups)


@pytest.mark.parametrize("heading", ["Affiliations", "作者单位", ""])
def test_inline_affiliation_markers_stay_in_continuous_note(heading):
    """Inline numbering in coherent institutional text is not broken up by short headings or language changes."""
    from docvortex.analyzers.native.pdf.text_assembly.footnotes import _split_page_footnote_entries

    lines = [_line(heading, (40, 600, 160, 610), 0)] if heading else []
    lines += [
        _line("1 Department of Science; 2 University of Example;", (40, 615, 560, 625), 1),
        _line("3 Institute of Computing, Example City", (40, 630, 550, 640), 2),
    ]
    assert len(_split_page_footnote_entries(lines, (600, 800))) == 1


@pytest.mark.parametrize("color", [(0, 0, 0, 255), (0, 80, 200, 255)])
def test_vector_formula_color_does_not_change_semantics(color):
    """Ordinary vector formulas are still formulas after changing colors. Decoration judgment cannot rely solely on color."""
    from test_flash_pdf_formulas import _vector_formula_source, _vector_formula_body_paths
    from docvortex.analyzers.native.pdf.formulas import _build_vector_formula_blocks

    paths = [replace(path, fill_rgba=color) for path in _vector_formula_body_paths()]
    blocks, _ = _build_vector_formula_blocks(_vector_formula_source(*paths), [], set())
    assert len(blocks) == 1 and blocks[0]["type"] == "equation"


def test_repeated_decoration_requires_shape_and_geometry():
    """Only decoration candidates whose structure and relative position are repeated at the same time are remarked, and the same position is not sufficient evidence."""
    from docvortex.analyzers.native.pdf.formulas import classify_repeated_vector_decorations
    from docvortex.analyzers.native.pdf.models import _PreparedPage

    blocks = [
        {"type": "equation", "bbox": (450, 730, 540, 770), "_vector_edge_decoration": True, "_vector_shape": shape}
        for shape in [(12, 24), (12, 24), (13, 24)]
    ]
    pages = [_PreparedPage((600, 800), [], [], [], [block]) for block in blocks]
    classify_repeated_vector_decorations(pages)
    assert [block["type"] for block in blocks] == ["image", "image", "equation"]


@pytest.mark.parametrize("language", ["en", "zh"])
def test_front_panel_without_exact_titles_and_with_short_abstract(language):
    """Metadata fields and abstract roles are sufficient to validate short abstracts without relying on article information titles or a fixed number of text lines."""
    english = [
        "Received yesterday",
        "Keywords: example",
        "Summary",
        "This short abstract explains a neutral scientific result clearly.",
    ]
    chinese = ["收稿日期：2026年1月1日", "关键词：示例", "摘要", "这里使用中性示例说明研究目标及主要结论。"]
    values = english if language == "en" else chinese
    boxes = [(40, 200, 190, 210), (40, 215, 190, 225), (250, 180, 350, 190), (250, 200, 550, 210)]
    source = _PageSource((600, 800), [_line(t, b, i) for i, (t, b) in enumerate(zip(values, boxes))], [], [], page_index=0)
    assert _restore_front_matter_text_panel(source, (30, 170, 570, 250))


def test_front_panel_preserves_actual_grid_and_insufficient_fields():
    """The table will not be revoked when the same text falls in the trusted cell grid, and duplicate fields of the same type will not constitute evidence of multiple roles."""
    from docvortex.analyzers.native.pdf.models import _AxisLine

    values = [
        ("Received yesterday", (40, 200, 190, 210)),
        ("Keywords: example", (40, 215, 190, 225)),
        ("Summary", (250, 180, 350, 190)),
        ("This short abstract explains a neutral scientific result clearly.", (250, 200, 550, 210)),
    ]
    lines = [_line(t, b, i) for i, (t, b) in enumerate(values)]
    rules = [
        _AxisLine((220, 175, 221, 245), 1, "vertical"),
        _AxisLine((35, 195, 560, 196), 1, "horizontal"),
        _AxisLine((35, 220, 560, 221), 1, "horizontal"),
    ]
    source = _PageSource((600, 800), lines, [], rules, page_index=0)
    assert not _restore_front_matter_text_panel(source, (30, 170, 570, 250))
    source.drawing_lines = []
    source.lines[1] = replace(lines[1], text="Received revised copy")
    assert not _restore_front_matter_text_panel(source, (30, 170, 570, 250))


@pytest.mark.parametrize("stop", ["Appendix", "Acknowledgements"])
def test_reference_context_stops_inside_the_same_page(stop):
    """An independent section after a reference on the same page terminates the context, and the following paragraphs and the next page are not grouped by reference entries."""
    from docvortex.analyzers.native.pdf.models import _PreparedPage
    from docvortex.analyzers.native.pdf.text_assembly.continuity import mark_document_reference_regions

    lines = [_line("References", (40, 100, 120, 110), 0)]
    lines += [_line(f"[{i}] Neutral Author. A reference entry.", (40, 120 + i * 15, 400, 130 + i * 15), i) for i in range(1, 5)]
    lines += [replace(_line(stop, (40, 230, 180, 240), 10), semantic_type="paragraph_title", structural_title=True)]
    lines += [
        _line("Independent section prose with several words.", (40, 260 + i * 15, 400, 270 + i * 15), 20 + i) for i in range(4)
    ]
    first = _PreparedPage((600, 800), lines, [], [], [])
    second = _PreparedPage((600, 800), lines[-4:], [], [], [])
    mark_document_reference_regions([first, second])
    assert first.reference_regions and max(b[3] for b in first.reference_regions) == 230
    assert not second.reference_regions


def test_local_caption_column_is_not_hidden_by_full_width_body():
    """The upper banner and the short tail of the figure do not erase the partial column next to the figure, and it is clear that the column groove does not need to be located in the center line of the page."""
    from docvortex.analyzers.native.pdf.layout_evidence import build_layout_evidence

    lines = [_line("Wide prose occupies the page.", (40, 100 + i * 15, 560, 110 + i * 15), i) for i in range(4)]
    lines += [_line("Narrow body prose.", (40, 200 + i * 12, 240, 210 + i * 12), 10 + i) for i in range(5)]
    lines += [
        replace(_line(text, (255, 210 + i * 12, right, 220 + i * 12), 20 + i), caption_start=i == 0)
        for i, (text, right) in enumerate(
            [("Figure 1: Neutral caption", 540), ("A continuation of the caption", 540), ("Short end.", 320)]
        )
    ]
    layout = build_layout_evidence(lines, (600, 800))
    assert layout.separated((40, 224, 240, 234), (255, 222, 540, 232))


def test_math_fragment_merge_does_not_cross_explicit_geometry_boundary():
    """Internal geometry breakpoints prevent further expansion of the frame without the need to fake semantic segment boundaries."""
    from docvortex.analyzers.native.pdf.text_assembly.continuity import merge_overlapping_member_blocks

    first = _line("a neutral unfinished sentence-", (40, 200, 240, 210), 0)
    second = _line("continues beside a figure", (40, 211, 480, 221), 1)
    blocks = [{"type": "text", "bbox": line.bbox, "content": line.text, "_text_lines": [line]} for line in [first, second]]
    blocks[1]["_geometry_break_before"] = True
    assert len(merge_overlapping_member_blocks(blocks, (600, 800))) == 2


@pytest.mark.parametrize("color", [(0, 0, 0, 255), (0, 90, 160, 255)])
@pytest.mark.parametrize("numbered", [False, True])
def test_publication_wordmark_needs_context_and_math_wins(color, numbered):
    """Multi-line wordmarks in the margin rely on publishing role recognition, grayscale behavior is the same, and independent formula numbers take precedence."""
    from docvortex.document.pdf.native_contracts import PDFPathInfo
    from docvortex.analyzers.native.pdf.formulas import _build_vector_formula_blocks

    paths = [
        PDFPathInfo((450 + 6 * i, 730 + 12 * j, 454 + 6 * i, 740 + 12 * j), 16, True, False, 0, j * 6 + i, color)
        for j in range(2)
        for i in range(6)
    ]
    body = [
        _line("Neutral full width paragraph supplies body context", (40, 500 + i * 15, 560, 510 + i * 15), i) for i in range(4)
    ]
    if numbered:
        body.append(_line("(1)", (546, 735, 558, 745), 50))
    source = _PageSource((600, 800), body, [], [], path_infos=paths, publication_bboxes=[(40, 735, 200, 745)])
    blocks, _ = _build_vector_formula_blocks(source, [], set())
    assert len(blocks) == 1 and blocks[0]["type"] == ("equation" if numbered else "image")
    source.publication_bboxes = []
    blocks, _ = _build_vector_formula_blocks(source, [], set())
    assert len(blocks) == 1 and blocks[0]["type"] == "equation"


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("scale,offset", [(0.75, 10), (1.0, 0), (1.5, 20)])
def test_layout_evidence_and_math_recovery_use_upright_coordinates(angle, scale, offset):
    """Column ownership and two-dimensional formula members remain unchanged under zooming, panning, and four page orientations."""
    from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_from_upright
    from docvortex.analyzers.native.pdf.layout_evidence import build_layout_evidence

    page = (600 * scale + 2 * offset, 800 * scale + 2 * offset)
    if angle in {90, 270}:
        page = page[::-1]

    def transformed(box):
        """Construct the real box in the page with the same affine transformation to avoid modifying only the angle label."""
        return _rotate_bbox_from_upright(tuple(v * scale + offset for v in box), page, angle)

    lines = [
        replace(
            _line("Neutral paragraph with enough words", transformed((left, 100 + j * 15, right, 110 + j * 15)), i * 10 + j),
            angle=angle,
            effective_height=10 * scale,
            em_height=10 * scale,
        )
        for i, (left, right) in enumerate([(40, 210), (260, 550)])
        for j in range(4)
    ]
    layout = build_layout_evidence(lines, page, angle=angle)
    assert len(layout.lanes) == 2
    members = [
        replace(_line(text, transformed(box), i), angle=angle, effective_height=10 * scale, em_height=10 * scale)
        for i, (text, box) in enumerate(
            [("Window(x)+Tail(y)=0", (100, 300, 240, 310)), ("Total(x)", (140, 315, 190, 325)), ("(1)", (250, 300, 265, 310))]
        )
    ]
    blocks, claimed = _recover_detached_display_components(members, [], page)
    assert len(blocks) == 1 and blocks[0]["angle"] == angle and claimed == {0, 1, 2}


def test_actual_full_width_runin_prompts_and_numbered_start():
    """Correct errors in the verification rules of the real general paper, bold prompts should be returned to the main text, and independent line numbers should be maintained to retain paragraph boundaries."""
    from test_flash_manual_annotations import _model
    from tools.review_flash_annotations import visible

    pages = _model("frames_v1")
    for prefix in ["Human annotation.", "Dataset Statistics."]:
        found = [b for b in pages[3] if visible(b["content"]).startswith(prefix)]
        assert len(found) == 1 and found[0]["type"] == "text"
        assert any(prefix in visible(span) and "bold" in span.get("styles", []) for span in found[0]["content"])
    numbered = [b for b in pages[5] if visible(b["content"]).startswith("(2) BM25-Retrieved Prompt")]
    assert len(numbered) == 1 and numbered[0]["type"] == "text"
    assert not any(
        "In this set of experiments" in visible(b["content"]) and "(2) BM25" in visible(b["content"]) for b in pages[5]
    )
