"""由原页视觉证据冻结本轮 Flash 缺陷，禁止用候选输出生成期望。"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pytest

from _flash_pdf_test_utils import _text_line, _visible_text
from docvortex.analyzers.native.pdf import auxiliary_text, pipeline
from docvortex.analyzers.native.pdf.models import _TextLane
from docvortex.document.pdf import PDFDocument

FIXTURES = Path(__file__).parent / "pdfs" / "flash_review_20261003"


@pytest.mark.parametrize("name,anchor", [("review_121", "Congratulations"), ("review_122", "3. Mix reagents")])
def test_transformed_font_metrics_do_not_promote_plain_instructions_to_document_title(name, anchor):
    """原页普通字号的祝贺句和编号操作仍为正文，异常字体包围框不能制造文档标题。"""
    blocks = _pages(name)[0]
    matches = [b for b in blocks if anchor in _visible_text(b["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "text"
    assert matches[0]["bbox"][3] - matches[0]["bbox"][1] < .035


def test_drying_instruction_keeps_its_second_line_in_one_paragraph():
    """步骤19的两行属于同一操作，不能因失真的原生字体行高而拆开。"""
    blocks = _pages("review_121")[0]
    matches = [b for b in blocks if "19. Allow the tubes" in _visible_text(b["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "text"
    assert "ensure that the tube interior is completely dry." in _visible_text(matches[0]["content"])


def test_incubation_instruction_and_separate_instructor_note_have_distinct_boundaries():
    """原页步骤4与下方留白后的NOTE各自成段，不能合成一段。"""
    blocks = _pages("review_122")[0]
    instruction = next(b for b in blocks if "4. Incubate all" in _visible_text(b["content"]))
    note = next(b for b in blocks if "freeze your completed restriction digests" in _visible_text(b["content"]))
    assert instruction is not note
    assert instruction["type"] == note["type"] == "text"
    assert "NOTE" not in _visible_text(instruction["content"])


@pytest.mark.parametrize("name,anchors", [
    ("review_121", ("Restriction Enzyme Digest Prep", "II. Set Up the Restriction Digests")),
    ("review_122", ("III. Electrophorese Digests", "Load the Gel")),
])
def test_native_bold_instruction_section_headings_survive_local_metric_calibration(name, anchors):
    """原页粗体章节标题各自独立，几何校准不能吞入相邻正文或表格表头。"""
    blocks = _pages(name)[0]
    for anchor in anchors:
        matches = [b for b in blocks if anchor in _visible_text(b["content"])]
        assert len(matches) == 1 and matches[0]["type"] == "paragraph_title"
        assert matches[0]["bbox"][3] - matches[0]["bbox"][1] < .03


def test_metric_calibration_preserves_complete_micropipette_instruction_and_number():
    """几何校准不能把含微升符号的步骤20拆开，也不能裁掉左侧编号。"""
    blocks = _pages("review_121")[0]
    b = next(b for b in blocks if "20. Use a micropipette" in _visible_text(b["content"]))
    assert _visible_text(b["content"]).endswith("follows.")
    assert .15 <= b["bbox"][0] <= .16


def test_metric_calibration_keeps_four_complete_bullet_entries_and_separate_supply_label():
    """原页四个圆点均与自己的正文合并，器材标签不混入前一条。"""
    blocks = _pages("review_122")[0]
    bullets = [b for b in blocks if _visible_text(b["content"]).startswith("•") and b["bbox"][1] < .6]
    assert len(bullets) == 4
    assert all(b["type"] == "text" and len(_visible_text(b["content"]).split()) >= 4 for b in bullets)
    assert all("Supplies and Equipment" not in _visible_text(b["content"]) for b in bullets)
    label = [b for b in blocks if _visible_text(b["content"]) == "Supplies and Equipment"]
    assert len(label) == 1


def test_metric_calibration_separates_loading_step_note_and_bullet_introduction():
    """第二步、独立NOTE及While loading引导各自完整，后续两条圆点保持独立。"""
    blocks = _pages("review_122")[0]
    anchors = ("2. Use a micropipette to load", "NOTE: Be careful not to punch", "While loading,")
    matches = [next(b for b in blocks if anchor in _visible_text(b["content"])) for anchor in anchors]
    assert len({id(b) for b in matches}) == 3
    assert all(b["type"] == "text" for b in matches)
    assert _visible_text(matches[0]["content"]).endswith("loaded.")
    assert _visible_text(matches[2]["content"]) == "While loading,"
    assert len([b for b in blocks if _visible_text(b["content"]).startswith("•") and b["bbox"][1] > .8]) == 2


def test_native_two_column_reagent_table_keeps_physical_cells_and_internal_text_order():
    """原页完整外框和列线限定两行双列表；多行文字按各列顺序保留，不能回退错序投影。"""
    from bs4 import BeautifulSoup
    blocks = _pages("review_121")[0]
    table = next(b for b in blocks if b["type"] == "table")
    soup = BeautifulSoup(table["content"], "html.parser")
    assert len(soup.find_all("tr")) == 2 and len(soup.find_all("td")) == 4
    cells = [cell.get_text(" ", strip=True) for cell in soup.find_all("td")]
    assert cells[0] == "Reagents" and cells[1] == "Supplies and Equipment"
    assert cells[2].startswith("At each student station:") and cells[2].endswith("Sterile distilled or deionized water")
    assert cells[2].index('"Evidence A"') < cells[2].index('"Evidence B"') < cells[2].index("Restriction Buffer")
    assert cells[3].startswith("Microcentrifuge tube rack") and cells[3].endswith("Water bath at 37°C")
    assert "*Store on ice" not in table["content"]
    assert [tag.get_text(strip=True) for tag in soup.find_all("b")] == ["Reagents", "Supplies and Equipment"]
    assert [tag.get_text(strip=True) for tag in soup.find_all("i")] == ["At each student station:", "To be shared by all groups:"]


def test_store_on_ice_is_a_unique_footnote_under_the_native_reagent_table():
    """公开MiddleJson将中部表下注释唯一归属到该两列表格，不能成为页脚或表内文字。"""
    from docvortex import parse
    blocks = parse(FIXTURES / "review_121.pdf", keep_model_json=True).to_dict()["pages"][0]["blocks"]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    notes = [b for b in tables[0]["content"] if b["type"] == "table_footnote" and _visible_text(b["content"]) == "*Store on ice"]
    assert len(notes) == 1
    assert all("*Store on ice" not in _visible_text(b.get("content", "")) for b in blocks if b["type"] != "table")


@pytest.mark.parametrize("scale,left,width", [(.7, 10, 240), (1, 43, 400), (1.8, 110, 620)])
@pytest.mark.parametrize("kind", ["calibrated", "no_map", "unrelated_map"])
def test_table_recovery_consumes_calibrated_character_geometry_without_mutating_source(scale, left, width, kind):
    """移动缩放原生网格与失真字框，明确的字符校准才恢复四个单元格，源记录始终保留。"""
    from bs4 import BeautifulSoup
    from docvortex.analyzers.native.pdf.models import _PageSource, _TableCandidate, _AxisLine
    from docvortex.analyzers.native.pdf.table_materialization import _materialize_table_blocks
    bounds = (left, 100 * scale, left + width, 150 * scale)
    xs = (left, left + .5 * width, left + width)
    ys = (100 * scale, 125 * scale, 150 * scale)
    rules = [_AxisLine(bbox=(left, y - .05 * scale, left + width, y + .05 * scale), orientation="horizontal", width=.1 * scale) for y in ys]
    rules += [_AxisLine(bbox=(x - .05 * scale, ys[0], x + .05 * scale, ys[-1]), orientation="vertical", width=.1 * scale) for x in xs]
    chars = []
    calibrated = {}
    for text, col, row in (("Alpha", 0, 0), ("Beta", 1, 0), ("Upper", 0, 1), ("Lower", 1, 1)):
        x = xs[col] + 12 * scale
        y = ys[row] + 8 * scale
        for index, letter in enumerate(text):
            idx = len(chars)
            xx = x + index * 5 * scale
            chars.append({"char": letter, "bbox": (xx, y - 25 * scale, xx + 5 * scale, y + 30 * scale),
                          "char_idx": idx, "rotation": 0., "font": {"name": "Fixture", "size": scale, "flags": 0, "weight": 400}})
            calibrated[idx] = (xx, y, xx + 5 * scale, y + 10 * scale)
    original = [char["bbox"] for char in chars]
    if kind == "no_map":calibrated = {}
    if kind == "unrelated_map":calibrated = {index + 999: box for index, box in calibrated.items()}
    source = _PageSource((left + width + 20, 250 * scale), [], chars, rules)
    candidate = _TableCandidate(bounds, bounds, 0, 100, core_bbox=bounds)
    tables, _notes, _claimed = _materialize_table_blocks(source, [candidate], calibrated_char_bboxes=calibrated)
    soup = BeautifulSoup(tables[0]["content"], "html.parser") if tables else BeautifulSoup("", "html.parser")
    assert bool(soup.find("table")) == (kind == "calibrated")
    if kind == "calibrated":
        assert len(soup.find_all("tr")) == 2
        assert [cell.get_text() for cell in soup.find_all("td")] == ["Alpha", "Beta", "Upper", "Lower"]
    assert [char["bbox"] for char in chars] == original


def _metric_fixture_line(text, bbox, index, **fields):
    """构造允许显式指定内部几何与语义标记的尺度反例，不修改通用测试替身。"""
    from docvortex.analyzers.native.pdf.models import _LineItem
    return _LineItem(text, bbox, 0, index, **fields)


@pytest.mark.parametrize('scale', [.7, 1, 1.6])
@pytest.mark.parametrize('kind', ['zero_group', 'group', 'none', 'different', 'title'])
@pytest.mark.parametrize('merger', ['baseline', 'overlap', 'dense'])
def test_same_row_mergers_preserve_only_unanimous_native_structural_membership(scale, kind, merger):
    """三种同行合并均保留零值段组和标题带；未知或不一致归属不能被强行传播。"""
    from docvortex.analyzers.native.pdf.line_merging import (
        _merge_same_baseline_group, _merge_overlapping_inline_cluster, _merge_dense_split_visual_row,
    )
    h = 10 * scale
    group = 0 if kind == 'zero_group' else 7 if kind in {'group', 'different'} else None
    members = [
        _metric_fixture_line(text, (x * scale, 30 * scale, (x + 35) * scale, 30 * scale + h), index,
                             effective_height=h, paragraph_group=(8 if kind == 'different' and index else group),
                             title_band_id=(0 if kind == 'title' else None),
                             structural_title=kind == 'title', explicit_section_title=kind == 'title')
        for index, (text, x) in enumerate([('Native first', 15), ('native second', 52)])
    ]
    page_size = (200 * scale, 300 * scale)
    if merger == 'baseline':
        result = _merge_same_baseline_group([0, 1], members, [member.bbox for member in members], page_size)
    elif merger == 'overlap':
        result = _merge_overlapping_inline_cluster([(member, member.bbox) for member in members], page_size,
                                                   h, compact_formula_cluster=False)
    else:
        result = _merge_dense_split_visual_row(members, page_size)
    assert result.paragraph_group == (group if kind != 'different' else None)
    assert result.title_band_id == (0 if kind == 'title' else None)
    assert result.structural_title == result.explicit_section_title == (kind == 'title')
    assert all(text in result.text for text in ['Native first', 'native second'])


@pytest.mark.parametrize("scale,left,width", [(.7, 10, 240), (1, 43, 400), (1.8, 110, 620)])
@pytest.mark.parametrize("kind", ["positive", "ordinary", "no_donor", "five_rows", "large_font", "two_baselines", "other_family", "unreliable_donor"])
def test_extreme_local_font_metric_repair_requires_repetition_and_healthy_family_geometry(scale, left, width, kind):
    """缩放移动字体矩阵异常；健康同族、六行重复与单基线同时成立才校正。"""
    from docvortex.analyzers.native.pdf.char_geometry import DocumentGeometryPlan, _CharSample, _repair_extreme_local_font_metrics
    from docvortex.analyzers.native.pdf.models import _LineItem
    by_line = {}
    count = 5 if kind == "five_rows" else 6
    for index in range(count + (kind != "no_donor")):
        donor = index == count
        baseline = (100 + index * 18) * scale
        h = (14 if donor else 22 if kind == "ordinary" else 55) * scale
        bbox = (left, baseline - .8 * h, left + width, baseline + .2 * h)
        line = _LineItem("ABCDEFGHIJKLMNO", bbox, 0, index, effective_height=h)
        members = []
        for char in range(15):
            origin = (left + char * width / 15, baseline + (5 * scale if kind == "two_baselines" and char > 6 and not donor else 0))
            tight = (origin[0], origin[1] - 7 * scale, origin[0] + 4 * scale, origin[1])
            family = "different" if donor and kind == "other_family" else "fixture"
            font_size = 10 * scale if kind == "large_font" else scale
            members.append(_CharSample(0, line, char, index * 15 + char, "A", bbox, tight, origin,
                                       bbox, tight, origin, (family, font_size, 0, 400, 0, "latin"), font_size,
                                       not (donor and kind == "unreliable_donor" and char > 5)))
        by_line[0, index] = members
    plan = DocumentGeometryPlan()
    _repair_extreme_local_font_metrics(plan, by_line, [(left + width + 20, 500 * scale)])
    assert bool(plan.local_metric_repair_sources) == (kind == "positive")
    if kind == "positive":
        assert plan.local_metric_repair_sources == {(0, index) for index in range(6)}
        assert all(abs(value - 14 * scale) < 1e-5 for value in plan.line_style_scales.values())


@pytest.mark.parametrize("scale,left,width", [(.7, 10, 240), (1, 43, 400), (1.8, 110, 620)])
@pytest.mark.parametrize("kind", ["positive", "unbold", "no_blank", "side_header", "numbered_step", "formula", "no_followers", "uncalibrated"])
def test_bold_section_after_local_metric_repair_requires_whitespace_and_body_followers(scale, left, width, kind):
    """移动缩放章节标题，普通正文、表头、编号操作和数学行不得凭粗体晋升。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_headings_after_extreme_local_metric_repair
    rows = []
    for index in range(6):
        y = (100 + index * 12) * scale
        bbox = (left, y, left + width, y + 10 * scale)
        rows.append(_metric_fixture_line("Ordinary complete body sentence.", bbox, index, effective_height=10 * scale,
                               em_height=10 * scale, source_bbox=(left, y - 20 * scale, left + width, y + 35 * scale),
                               style_scale_repaired=kind != "uncalibrated", dominant_font_weight=400))
    y = (171 if kind == "no_blank" else 185) * scale
    title = _metric_fixture_line("2. Example instruction" if kind == "numbered_step" else "Example section heading",
                       (left, y, left + .6 * width, y + 11 * scale), 6, effective_height=11 * scale,
                       dominant_font_weight=400 if kind == "unbold" else 700, font_coverage=1,
                       formula_candidate_only=kind == "formula")
    rows.append(title)
    for index in range(2 if kind != "no_followers" else 1):
        yy = y + (20 + index * 12) * scale
        rows.append(_metric_fixture_line("Following ordinary body sentence.", (left, yy, left + width, yy + 10 * scale), 7 + index,
                               effective_height=10 * scale, dominant_font_weight=400))
    if kind == "side_header":
        rows.append(_metric_fixture_line("Adjacent field", (left + .7 * width, y, left + width, y + 11 * scale), 10, effective_height=11 * scale))
    _classify_headings_after_extreme_local_metric_repair(rows, (left + width + 30, 600 * scale), [])
    assert (title.semantic_type == "paragraph_title") == (kind == "positive")


@pytest.mark.parametrize("scale,left,width", [(.7, 10, 240), (1, 43, 400), (1.8, 110, 620)])
@pytest.mark.parametrize("kind", ["positive", "no_blank", "unfinished", "not_centered", "caption", "formula", "grouped", "large_type"])
def test_centered_complete_sentence_is_separate_only_after_terminal_blank_transition(scale, left, width, kind):
    """独立居中收句依赖前文收句和留白，普通续行、图注及数学成员保持原段。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _leading_typography_reset_break_sources
    h = 10 * scale
    a = _metric_fixture_line("Unfinished tail," if kind == "unfinished" else "Completed tail.",
                   (left, 100 * scale, left + .5 * width, 110 * scale), 0, effective_height=h)
    y = (112 if kind == "no_blank" else 123) * scale
    x = left + (.02 if kind == "not_centered" else .13) * width
    b = _metric_fixture_line("***A complete centered sentence!***", (x, y, x + .74 * width, y + h), 1,
                   effective_height=2 * h if kind == "large_type" else h, caption_start=kind == "caption",
                   formula_candidate_only=kind == "formula", paragraph_group=1 if kind == "grouped" else None)
    lane = _TextLane(left, left + width, [(a, a.bbox), (b, b.bbox)])
    assert _leading_typography_reset_break_sources(lane, 2 * scale, 0) == ({1} if kind == "positive" else set())


@pytest.mark.parametrize("scale,left,width", [(.7, 10, 240), (1, 43, 400), (1.8, 110, 620)])
@pytest.mark.parametrize("kind", ["positive", "blank", "different_left", "unrepaired", "healthy_source", "different_font", "table_barrier"])
def test_calibrated_indented_instruction_continues_only_with_matching_rows(scale, left, width, kind):
    """校准后的常规缩进续行仍受字体、左缘、净空及表格屏障约束。"""
    from docvortex.analyzers.native.pdf.line_layout import _should_connect_text_rows
    h = 10 * scale
    x = left + 1.6 * h
    y = (138 if kind == "blank" else 112) * scale
    other_x = x + (12 * scale if kind == "different_left" else 0)
    a = _metric_fixture_line("20. A complete first instruction sentence.", (x, 100 * scale, left + width, 110 * scale), 0,
                             effective_height=h, font_signature=("Fixture", 0), font_coverage=1, paragraph_terminal=True,
                             source_bbox=(x, 80 * scale, left + width, (100 if kind == "healthy_source" else 135) * scale),
                             style_scale_repaired=kind != "unrepaired")
    b = _metric_fixture_line("Following words continue the same instruction.", (other_x, y, left + width, y + h), 1,
                             effective_height=h, font_signature=("Different" if kind == "different_font" else "Fixture", 0),
                             font_coverage=1, source_bbox=(other_x, y - 20 * scale, left + width, y + 35 * scale),
                             style_scale_repaired=kind != "unrepaired")
    lane = _TextLane(left, left + width, [(a, a.bbox), (b, b.bbox)])
    tables = [(left, 108 * scale, left + width, 114 * scale)] if kind == "table_barrier" else []
    assert _should_connect_text_rows((a, a.bbox), (b, b.bbox), lane, 2 * scale, 0, tables, []) == (kind == "positive")


@lru_cache(maxsize=None)
def _pages(name):
    """解析完整原件，保留跨页分类上下文且不读取 benchmark GT。"""
    with PDFDocument(str(FIXTURES / f"{name}.pdf")) as document:
        return pipeline._analyze_native_document(document)


def test_short_rule_real_footnote_keeps_body_outside():
    """短线下的编号18和两行小字是完整脚注，不能粘到正文。"""
    blocks = _pages("short_separator")[0]
    matches = [b for b in blocks if "SimultaneityNoisyCriteriaMultistart" in _visible_text(b["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "page_footnote"
    assert _visible_text(matches[0]["content"]).startswith("18")
    assert "toolbox extensions" in _visible_text(matches[0]["content"])
    assert matches[0]["bbox"][1] > 0.8


@pytest.mark.parametrize(
    "number,markers",
    [
        (2, (19,)),
        (3, (22,)),
        (4, (23,)),
        (9, (34, 35, 36)),
        (10, (84, 85)),
        (11, (86, 87, 88)),
        (12, (41,)),
        (13, (24, 25)),
        (14, (49, 50, 51, 52)),
        (15, (72,)),
    ],
)
def test_reviewed_book_notes_have_complete_unique_numbered_entries(number, markers):
    """依据人工原页复核完整编号条目，特别保留短小的同上注释而非只保留编号。"""
    notes = [_visible_text(b["content"]) for b in _pages(f"review_{number}")[0] if b["type"] == "page_footnote"]
    for marker in markers:
        matches = [text for text in notes if text.startswith(str(marker) + " ")]
        assert len(matches) == 1 and len(matches[0]) > len(str(marker)) + 4, (number, marker, notes)
    if number == 11:
        assert next(text for text in notes if text.startswith("88 ")) == "88 Ibid."
        assert "Ibid." not in next(text for text in notes if text.startswith("87 "))
    if number == 9:
        assert len(notes) == 4 and sum(text.startswith("Book.") for text in notes) == 1
        assert next(text for text in notes if text.startswith("35 ")).endswith("Reformata cited above.")


@pytest.mark.parametrize("name,anchor", [("review_2", "Choosing between Observer Models"), ("review_38", "6.2")])
def test_reviewed_book_and_report_headings_are_separate_from_body(name, anchor):
    """原页编号章节标题独立成框，章节号与标题完整且不包含相邻正文段。"""
    matches = [block for block in _pages(name)[0] if anchor in _visible_text(block["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "paragraph_title"
    assert matches[0]["bbox"][3] - matches[0]["bbox"][1] < 0.06


def test_book_caption_excludes_larger_body_paragraph_after_publication_line():
    """原页图注以出版信息结束，随后字号增大的正文不能被图注认领。"""
    blocks = _pages("review_11")[0]
    caption = next(b for b in blocks if "Figure 4.11" in _visible_text(b["content"]))
    assert caption["type"] == "caption" and caption["bbox"][3] < 0.52
    assert "London, 1799" in _visible_text(caption["content"])
    body = next(b for b in blocks if "knowledge about remote civilizations" in _visible_text(b["content"]))
    assert body["type"] == "text" and "Figure 4.11" not in _visible_text(body["content"])


def test_parallel_book_images_keep_distinct_regions_and_centered_caption_tails():
    """两个独立编号图注对应两张原生图片，每段居中斜体续行都属于自己的图注。"""
    blocks = _pages("review_12")[0]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2 and images[0]["bbox"][2] < images[1]["bbox"][0]
    captions = [b for b in blocks if b["type"] == "caption"]
    assert len(captions) == 2
    assert all("The Wonderful Lamp." in _visible_text(b["content"]) for b in captions)
    assert not any(
        b["type"] == "text" and _visible_text(b["content"]) in {"The Wonderful Lamp.", "Aladdin, or The Wonderful Lamp."}
        for b in blocks
    )


@pytest.mark.parametrize("scale,left", [(0.7, 10), (1, 43), (1.8, 110)])
def test_short_numbered_note_keeps_its_small_body_at_each_scale(scale, left):
    """移动编号并改变字号，短注释仍随自己的编号；无编号窄片段不能制造新条目。"""
    from docvortex.analyzers.native.pdf.text_assembly.footnotes import _split_page_footnote_entries

    def row(text, x, y, width, index, row_id):
        """构造有稳定基线与悬挂缩进的脚注片段。"""
        return _text_line(
            text,
            (left + x * scale, y * scale, left + (x + width) * scale, (y + 10) * scale),
            index,
            visual_row_id=row_id,
            split_from_row=True,
            median_glyph_width=4 * scale,
            font_signature=("Synthetic", 0),
        )

    lines = [
        row("17", 0, 100, 6, 0, 0),
        row("A longer bibliographic explanation", 20, 100, 150, 1, 0),
        row("Continued publication details.", 20, 112, 140, 2, 1),
        row("18", 0, 124, 6, 3, 2),
        row("Same.", 20, 124, 18, 4, 2),
    ]
    entries = _split_page_footnote_entries(lines, (400 * scale, 500 * scale))
    assert [[line.source_index for line in entry] for entry in entries] == [[0, 1, 2], [3, 4]]
    lines[-2].text = "x"
    entries = _split_page_footnote_entries(lines, (400 * scale, 500 * scale))
    assert not any({3, 4} == {line.source_index for line in entry} for entry in entries)


@pytest.mark.parametrize("scale,left,width", [(0.7, 15, 260), (1, 45, 350), (1.6, 90, 500)])
@pytest.mark.parametrize("independent", [False, True])
def test_parallel_native_images_follow_caption_regions_without_duplicate_ownership(scale, left, width, independent):
    """改变页面宽度、字号及位置；独立图题分开两图，统一图题仍保留整体图形。"""
    from docvortex.analyzers.native.pdf import graphics, models

    images = [
        (left, 60 * scale, left + 0.45 * width, 170 * scale),
        (left + 0.55 * width, 60 * scale, left + width, 170 * scale),
    ]
    caption_boxes = (
        [(left, 180 * scale, left + 0.45 * width, 190 * scale), (left + 0.55 * width, 180 * scale, left + width, 190 * scale)]
        if independent
        else [(left, 180 * scale, left + width, 190 * scale)]
    )
    lines = [_text_line(f"Figure {index + 1}. A general result", bbox, index) for index, bbox in enumerate(caption_boxes)]
    caption_sources = {line.source_index for line in lines}
    lines.extend(
        _text_line(
            "Ordinary unrelated explanatory words for this synthetic page.",
            (left, y * scale, left + width, (y + 10) * scale),
            10 + index,
            font_coverage=1,
        )
        for index, y in enumerate((240, 254, 268))
    )
    source = models._PageSource((left + width + 40, 400 * scale), lines, [], [], image_bboxes=images)
    blocks, _ = graphics._build_caption_graphic_blocks(
        source, caption_line_indices=caption_sources, table_bboxes=[], code_bboxes=[]
    )
    assert len(blocks) == (2 if independent else 1)
    if independent:
        assert blocks[0]["bbox"][2] < blocks[1]["bbox"][0]


@pytest.mark.parametrize("scale,left", [(0.7, 10), (1, 43), (1.8, 110)])
@pytest.mark.parametrize("body", [False, True])
def test_centered_caption_tail_and_body_font_reset_have_different_boundaries(scale, left, body):
    """同尺度居中斜体续行属于图注，留白后字号增大的正文必须独立。"""
    from docvortex.analyzers.native.pdf.text_assembly.annotations import _caption_tail_matches_seed
    from docvortex.analyzers.native.pdf.text_assembly.rows import _caption_to_body_break_sources

    seed = {
        "bbox": (left, 100 * scale, left + 180 * scale, 110 * scale),
        "_line_heights": [10 * scale],
        "_font_signatures": {("Synthetic", 0)},
    }
    tail = {
        "bbox": (
            left + (0 if body else 50) * scale,
            (118 if body else 112) * scale,
            left + (180 if body else 130) * scale,
            (132 if body else 122) * scale,
        ),
        "_line_heights": [(14 if body else 10) * scale],
        "_font_signatures": {("Synthetic", 64)},
    }
    assert _caption_tail_matches_seed(seed, tail, 10 * scale) is (not body)
    first = _text_line("Figure 2. A general result", seed["bbox"], 0)
    first.caption_start = True
    second = _text_line("An unrelated line", tail["bbox"], 1)
    lane = _TextLane(left, left + 180 * scale, [(first, first.bbox), (second, second.bbox)])
    assert _caption_to_body_break_sources(lane) == ({1} if body else set())


def test_caption_state_ends_at_outdented_body_with_unreliable_first_line_height():
    """居中图注后正文回到栏左缘，即使首行有效字高偏小也不能误切第二行续文。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _caption_to_body_break_sources

    lines = [
        _text_line("Figure 4. A result", (60, 100, 220, 110), 0),
        _text_line("The ordinary paragraph begins", (20, 130, 260, 140), 1, effective_height=7),
        _text_line("with a continued line", (20, 146, 260, 156), 2),
    ]
    lines[0].caption_start = True
    lane = _TextLane(20, 260, [(line, line.bbox) for line in lines])
    assert not _caption_to_body_break_sources(lane)


@pytest.mark.parametrize("page,markers", [(5, ("7We leave", "8E.g.")), (7, ("10", "11")), (13, ("13", "14"))])
def test_native_superscript_footnotes_remain_separate(page, markers):
    """行内小号上标编号提供真实分项证据，不依赖拆 run 或块号。"""
    notes = [_visible_text(b["content"]) for b in _pages("two_column_retrieval")[page] if b["type"] == "page_footnote"]
    assert all(sum(text.startswith(marker) for text in notes) == 1 for marker in markers), notes


def test_bibliographic_notes_keep_all_continuations_and_unique_numbers():
    """不同字体和独立编号 run 的两栏脚注也应逐项完整，不把书目信息拆出去。"""
    notes = [_visible_text(b["content"]) for b in _pages("numbered_bibliographic_notes")[0] if b["type"] == "page_footnote"]
    assert len(notes) == 9
    assert all(sum(text.startswith(str(number) + " ") for text in notes) == 1 for number in range(25, 34))
    assert "(London: Printed for J. Johnson, 1786), 165." in next(text for text in notes if text.startswith("25 "))


def test_two_column_footnotes_follow_column_order():
    """原页左栏25至29读完后再读右栏30至33，不能按跨栏视觉行交错。"""
    notes = [_visible_text(b["content"]) for b in _pages("numbered_bibliographic_notes")[0] if b["type"] == "page_footnote"]
    assert [int(text.split()[0]) for text in notes] == list(range(25, 34))


def test_full_width_same_size_footnote_has_number_indent_and_body_clearance():
    """原页双栏正文下的通栏注5虽同字号，分隔线、独立编号和悬挂续行仍确认脚注。"""
    matches = [block for block in _pages("review_38")[0] if "The question on re-hiring" in _visible_text(block["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "page_footnote"
    assert _visible_text(matches[0]["content"]).startswith("5.")
    assert "first affected by the pandemic." in _visible_text(matches[0]["content"])


def test_single_line_note_uses_native_ink_scale_when_line_metrics_are_wrong():
    """原页注6字形明显缩小，异常行框高度与正文相同也不能丢失编号脚注。"""
    blocks = _pages("native_bars_39")[0]
    matches = [block for block in blocks if "Compared to 38% in July" in _visible_text(block["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "page_footnote"


def test_parallel_chart_groups_own_all_native_labels():
    """一幅图中多个行业分组仍为一个图体，坐标、刻度和图例不能残留成独立正文。"""
    blocks = _pages("review_38")[0]
    images = [block for block in blocks if block["type"] == "image"]
    assert len(images) == 2
    for block in blocks:
        if block["type"] == "text":
            x, y = (block["bbox"][0] + block["bbox"][2]) / 2, (block["bbox"][1] + block["bbox"][3]) / 2
            assert not any(
                image["bbox"][0] < x < image["bbox"][2] and image["bbox"][1] < y < image["bbox"][3] for image in images
            )


def test_wrapped_numbered_heading_keeps_its_unindented_last_line():
    """9.5标题换行后的Business Models仍属于完整标题，不能成为独立text。"""
    matches = [
        block for block in _pages("native_bars_39")[0] if "Adapting to the New Normal" in _visible_text(block["content"])
    ]
    assert len(matches) == 1 and matches[0]["type"] == "paragraph_title"
    assert "Business Models" in _visible_text(matches[0]["content"])


def test_plain_marginal_number_and_word_do_not_form_equation():
    """单页页眉可以是text，但页号和普通字词不能仅凭空间分裂成为公式。"""
    blocks = _pages("short_separator")[0]
    assert not any(b["type"] == "equation" and b["bbox"][1] < 0.1 for b in blocks)
    assert any("314" in _visible_text(b["content"]) for b in blocks)
    assert any("Yarrow" in _visible_text(b["content"]) for b in blocks)


@pytest.mark.parametrize(
    "name,edge",
    [
        ("short_separator", "314"),
        ("review_2", "316"),
        ("review_4", "322"),
        ("numbered_bibliographic_notes", "Circulating Things, Circulating Stereotypes"),
    ],
)
def test_single_page_marginal_text_is_separate_from_body(name, edge):
    """单页允许页边块为text，但页码或小号页眉必须独立，不能合入正文。"""
    matches = [b for b in _pages(name)[0] if edge in _visible_text(b["content"])]
    assert len(matches) == 1
    assert _visible_text(matches[0]["content"]).strip() == edge
    assert matches[0]["type"] == "text" and matches[0]["bbox"][3] < 0.08


@pytest.mark.parametrize("scale,left", [(0.7, 12), (1, 45), (1.8, 95)])
@pytest.mark.parametrize("kind", ["number", "small_label", "body_opener", "middle", "tight"])
def test_marginal_body_boundary_uses_margin_gap_and_repeated_body_scale(scale, left, kind):
    """改变字号和位置，只切开顶边编号或小号文字；正文短首行、紧排与页中编号保持连续。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _top_marginal_text_break_sources

    top = 160 if kind == "middle" else 20
    marginal_height = 9 if kind == "small_label" else 10
    first_text = "Section continuation" if kind == "body_opener" else "52" if kind != "small_label" else "General chapter label"
    gap = 1 if kind == "tight" else 7
    body_y = top + marginal_height + gap
    lines = [
        _text_line(
            first_text,
            (left, top * scale, left + (20 if first_text == "52" else 180) * scale, (top + marginal_height) * scale),
            0,
        )
    ]
    lines.extend(
        _text_line(
            "An ordinary unrelated line of continuous prose",
            (left, (body_y + 12 * i) * scale, left + 240 * scale, (body_y + 12 * i + 10) * scale),
            i + 1,
        )
        for i in range(3)
    )
    lane = _TextLane(left, left + 240 * scale, [(line, line.bbox) for line in lines])
    expected = {1} if kind in {"number", "small_label"} else set()
    assert _top_marginal_text_break_sources(lane, 500 * scale) == expected


def test_explanatory_sentence_stays_outside_display_equation():
    """公式上方解释句的来源文字必须可导出，公式裁图不能吞入它。"""
    blocks = _pages("two_column_retrieval")[4]
    assert any("expressed in Equation 1." in _visible_text(b["content"]) and b["type"] == "text" for b in blocks)


def test_modeling_body_is_complete_and_following_display_formula_contains_its_number():
    """原页建模说明直到denoted as为同一正文段，下一行公式5包含全式及右编号，不产生伪标题。"""
    blocks=_pages('display_math_29')[0]
    body=[block for block in blocks if _visible_text(block.get('content','')).startswith('To construct a miniature multiverse')]
    assert len(body)==1 and body[0]['type']=='text'
    assert _visible_text(body[0]['content']).endswith('so that the moments of time can in this context be denoted as')
    equations=[block for block in blocks if block['type']=='equation' and .81<block['bbox'][1]<.85]
    assert len(equations)==1 and equations[0]['bbox'][0]<.32 and equations[0]['bbox'][2]>.83
    assert not any(block['type']=='paragraph_title' and block['bbox'][1]>.74 for block in blocks)


def test_two_numbered_url_footnotes_remain_complete_unique_entries():
    """原页两条编号URL属于脚注，编号、完整地址及单双精度区别均保留，不能合成正文。"""
    notes=[_visible_text(block['content']) for block in _pages('review_142')[0] if block['type']=='page_footnote']
    assert len(notes)==2
    assert notes==['1http://en.wikipedia.org/wiki/Single-precision_floating-point_format',
                   '2http://en.wikipedia.org/wiki/Double-precision_floating-point_format']


def test_two_difference_and_limit_display_formulas_keep_fraction_extents_outside_prose():
    """原页两条独立差分/极限公式各自聚合分式全高，周围解释句完整保留为正文。"""
    blocks=_pages('review_143')[0]
    equations=[block for block in blocks if block['type']=='equation']
    assert len(equations)==2
    assert equations[0]['bbox'][1]<=.801 and equations[0]['bbox'][3]>=.828
    assert equations[1]['bbox'][1]<=.865 and equations[1]['bbox'][3]>=.898
    assert any(block['type']=='text' and _visible_text(block['content']).startswith('Suppose f is a continuously') for block in blocks)
    assert any(block['type']=='text' and _visible_text(block['content']).endswith('By definition,') for block in blocks)


def test_repeated_soil_layer_labels_start_separate_complete_body_paragraphs():
    """原页七个土层标签有独立空行，末尾括号不能阻止段界；各标签及其完整说明只归入自己的正文段。"""
    blocks=_pages('review_164')[0]
    labels=['3Btg2','3Btg3','3Btg4','3Btg5/E','3Btg6/E','3Btg7/E','3Btg8/E']
    paragraphs=[block for block in blocks if any(_visible_text(block.get('content','')).startswith(label) for label in labels)]
    assert len(paragraphs)==7 and all(block['type']=='text' for block in paragraphs)
    for block,label in zip(paragraphs,labels):
        text=_visible_text(block['content'])
        assert text.startswith(label) and not any(other in text for other in labels if other!=label)
    assert _visible_text(paragraphs[0]['content']).endswith('(0 to 15 in thick)')


@pytest.mark.parametrize('scale,left,width',[(.7,12,350),(1,70,460),(1.8,210,700)])
@pytest.mark.parametrize('kind',['paragraphs','single_font_start','no_blank','far_gap','outdent','caption','formula','grouped','large_type','wide_start'])
def test_repeated_typographic_paragraph_starts_require_blank_same_lane_body_evidence(scale,left,width,kind):
    """无句点段尾也可由重复窄字体标签和空行确认段界；连续行、异栏、图注、公式、分组和大字号均保护。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _leading_typography_reset_break_sources

    lines=[]
    for paragraph in range(3):
        base=(100+paragraph*(24 if kind=='no_blank' else 80 if kind=='far_gap' else 38))*scale
        x=left+(12*scale if kind=='outdent' and paragraph else 0)
        height=(20 if kind=='large_type' and paragraph else 10)*scale
        first=_text_line('Different label with further unrelated native body words',(x,base,x+width,base+height),len(lines),
                         font_signature=('UnrelatedSerif',0),font_coverage=1)
        if kind!='single_font_start' or paragraph==0:first.leading_typography_width=(.4 if kind=='wide_start' else .07)*width
        if kind=='caption':first.caption_start=True
        if kind=='formula':first.compact_formula_cluster=True
        if kind=='grouped':first.paragraph_group=3
        lines.append(first)
        lines.append(_text_line('Continued ordinary observation ends with a unit (some amount)',
                                (left,base+12*scale,left+.85*width,base+22*scale),len(lines),
                                font_signature=('UnrelatedSerif',0),font_coverage=1))
    lane=_TextLane(left,left+width,[(line,line.bbox) for line in lines])
    actual=_leading_typography_reset_break_sources(lane,2*scale,0)
    assert actual==({2,4} if kind=='paragraphs' else set())


@pytest.mark.parametrize(
    "number,regions",
    [
        (142, [(0.442, 0.411, 0.572, 0.459), (0.348, 0.841, 0.665, 0.865)]),
        (
            144,
            [
                (0.354, 0.15, 0.66, 0.162),
                (0.323, 0.223, 0.692, 0.237),
                (0.27, 0.62, 0.744, 0.635),
                (0.355, 0.66, 0.659, 0.674),
                (0.388, 0.874, 0.863, 0.891),
            ],
        ),
        (145, [(0.47, 0.403, 0.544, 0.433), (0.401, 0.598, 0.615, 0.637)]),
    ],
)
def test_textbook_displays_own_complete_math_without_prose_or_overlapping_crops(number, regions):
    """原页独立积分、分式及不等式必须唯一聚合，未编号公式也不能误作标题或正文。"""
    blocks = _pages(f"review_{number}")[0]
    equations = [block for block in blocks if block["type"] == "equation"]
    for bounds in regions:
        matches = [
            block
            for block in equations
            if all(
                block["bbox"][i] <= value + 0.006 if i < 2 else block["bbox"][i] >= value - 0.006
                for i, value in enumerate(bounds)
            )
        ]
        assert len(matches) == 1, (number, bounds, equations)
        box = matches[0]["bbox"]
        assert box[3] - box[1] < 0.065
        assert not any(
            other is not matches[0] and pipeline._bbox_overlap_in_smaller(box, other["bbox"]) > 0.2 for other in equations
        )
    if number == 144:
        intro = next(block for block in blocks if "Multiplying equation" in _visible_text(block["content"]))
        assert intro["type"] == "text" and intro["bbox"][3] < 0.615


@pytest.mark.parametrize("scale,left", [(0.7, 0), (1, 33), (1.8, 90)])
@pytest.mark.parametrize("kind", ["fraction", "adjacent", "prose", "table"])
def test_detached_math_requires_complete_geometry_and_respects_prose_tables(scale, left, kind):
    """改变字号、位置和栏宽，检验分式完整性、相邻等式分离及正文和表格屏障。"""
    from docvortex.analyzers.native.pdf import formulas

    def row(text, x, y, width, index, coverage=0.5):
        """按统一尺度构造独立源行，不使用特定教材原句。"""
        return _text_line(
            text,
            (left + x * scale, y * scale, left + (x + width) * scale, (y + 10) * scale),
            index,
            effective_height=10 * scale,
            font_signature=("Synthetic", 0),
            font_coverage=coverage,
        )

    rows = [row("An ordinary explanation continues on this line.", 0, y, 180, i, 1) for i, y in enumerate((80, 94, 165, 179))]
    if kind == "fraction":
        rows += [row("Uv", 91, 116, 13, 4), row("K =", 60, 123, 23, 5), row("θ", 94, 130, 6, 6)]
        expected = 1
    else:
        rows += [row("a = b + c" if kind != "prose" else "ordinary result = b + c", 55, 118, 75, 4)]
        if kind == "adjacent":
            rows += [row("e = f + g", 55, 134, 75, 5)]
        expected = 2 if kind == "adjacent" else 0
    barriers = [rows[-1].bbox] if kind == "table" else []
    blocks, _ = formulas._recover_detached_display_components(rows, barriers, (left + 220 * scale, 240 * scale))
    assert len(blocks) == expected
    if kind == "fraction":
        assert set(blocks[0]["_formula_members"]) == {4, 5, 6}


@pytest.mark.parametrize("flags,small,expected", [(64, False, True), (0, True, True), (0, False, False)])
def test_short_variable_products_require_native_style_evidence(flags, small, expected):
    """连写短词只有斜体或角标证据才作为数学乘积，普通字体单词继续建立正文屏障。"""
    from docvortex.analyzers.native.pdf import formulas

    line = _text_line("1 + uvw", (20, 40, 80, 50), 0)
    line.chars = [
        {"char": char, "font": {"flags": flags, "size": 7 if small and i == 6 else 10}} for i, char in enumerate(line.text)
    ]
    assert ("uvw" in formulas._native_math_word_fragments(line)) is expected


@pytest.mark.parametrize(
    "name,regions",
    [
        ("display_math_28", [(0.44, 0.56, 0.835, 0.58), (0.37, 0.624, 0.835, 0.643)]),
        ("display_math_29", [(0.35, 0.819, 0.835, 0.844)]),
        ("display_math_30", [(0.219, 0.733, 0.835, 0.76), (0.425, 0.809, 0.835, 0.836), (0.396, 0.869, 0.835, 0.895)]),
        ("display_math_31", [(0.338, 0.073, 0.835, 0.108)]),
        ("display_math_32", [(0.141, 0.628, 0.33, 0.67)]),
        ("display_math_33", [(0.141, 0.296, 0.745, 0.338)]),
        (
            "display_math_34",
            [
                (0.141, 0.173, 0.397, 0.209),
                (0.14, 0.329, 0.434, 0.37),
                (0.141, 0.412, 0.591, 0.454),
                (0.14, 0.539, 0.575, 0.561),
            ],
        ),
    ],
)
def test_complete_native_display_math_members_and_separate_body(name, regions):
    """按原页冻结的区域覆盖完整分式及编号，方程不得与解释正文或题名共享归属。"""
    equations = [block for block in _pages(name)[0] if block["type"] == "equation"]
    for bounds in regions:
        matches = [
            block
            for block in equations
            if all(block["bbox"][i] <= v + 0.006 if i < 2 else block["bbox"][i] >= v - 0.006 for i, v in enumerate(bounds))
        ]
        assert len(matches) == 1, (name, bounds, equations)
        assert matches[0]["bbox"][3] - matches[0]["bbox"][1] < 0.08
    assert not any(block["bbox"][1] < 0.06 for block in equations)


@pytest.mark.parametrize("page,count", [(6, 2), (7, 1), (14, 2), (16, 2)])
def test_grouped_tables_include_headers_without_overlapping_fragments(page, count):
    """同列分组表由表题限定完整范围，两个面板可分别成表但不得碎裂重叠。"""
    blocks = _pages("two_column_retrieval")[page]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == count
    target_tables = tables[1:] if page == 6 else tables
    assert all("Robust" in _visible_text(b["content"]) for b in target_tables)
    if page == 6:
        # 左栏是不同列名的 Recall 参数表，原页中没有 Robust 列。
        assert "k = 2" in _visible_text(tables[0]["content"])
    assert not any(
        b["type"] == "header" and _visible_text(b["content"]).strip() in {"Robust", "Covid", "News", "Touché", "Avg."}
        for b in blocks
    )


def test_sparse_key_value_table_contains_every_group_once():
    """同列分组表的十三条横线和独立表题限定范围，硬件、模型、训练及超参数必须同属一表。"""
    blocks = _pages("two_column_retrieval")[17]
    tables = [block for block in blocks if block["type"] == "table"]
    matched = [block for block in tables if "Elasticsearch" in _visible_text(block["content"])]
    assert len(matched) == 1
    content = _visible_text(matched[0]["content"])
    assert all(text in content for text in ("Models", "Training Settings", "Hyperparameters", "PYTREC", "0.5"))
    assert len(tables) == 3
    assert not any(block is not matched[0] and "Elasticsearch" in _visible_text(block["content"]) for block in blocks)


@pytest.mark.parametrize(
    "name,markers",
    [("display_math_32", ("1The",)), ("display_math_33", ("3That", "4The")), ("display_math_34", ("5An", "6The"))],
)
def test_raster_separator_still_provides_native_footnote_geometry(name, markers):
    """原件用极薄图片画分隔线，原生脚注文字仍按编号独立归属，保持非OCR解析。"""
    notes = [_visible_text(block["content"]) for block in _pages(name)[0] if block["type"] == "page_footnote"]
    assert all(sum(text.startswith(marker) for text in notes) == 1 for marker in markers), notes


@pytest.mark.parametrize("name", ["gradient_slide", "chart_slide", "table_slide"])
def test_background_images_do_not_consume_native_slide_text(name):
    """完整原生标题保留为文字，纯背景不得成为整页内容图认领所有字符。"""
    blocks = _pages(name)[0]
    # 渐变背景样本是本轮复核的目录页：完整原生目录也属于文字内容，不能限定只能是普通text。
    assert any(b["type"] in {"text", "paragraph_title", "doc_title", "index"} and len(_visible_text(b["content"])) > 30 for b in blocks)
    assert not any(
        b["type"] == "image" and b["bbox"][2] - b["bbox"][0] > 0.95 and b["bbox"][3] - b["bbox"][1] > 0.95 for b in blocks
    )


@pytest.mark.parametrize("name,count", [("native_bars_39", 1), ("native_bars_183", 3)])
def test_native_bar_charts_keep_all_series_as_independent_images(name, count):
    """原页的堆叠柱图和三个性能图各自完整保留，重复几何不能形成重叠图块或数据表。"""
    images = [block for block in _pages(name)[0] if block["type"] == "image"]
    assert len(images) == count, images
    if name.endswith("183"):
        assert all(block["bbox"][3] - block["bbox"][1] > 0.2 for block in images)
        assert not any(block["type"] == "equation" for block in _pages(name)[0])
    else:
        assert not any(block["type"] == "table" for block in _pages(name)[0])


def test_bar_chart_labels_are_owned_once_and_caption_is_outside():
    """完整柱图中的原生标签只随图片导出，图1标题作为图注保留在图外。"""
    blocks = _pages("native_bars_39")[0]
    image = next(block for block in blocks if block["type"] == "image")
    for block in blocks:
        if block["type"] == "text":
            box = block["bbox"]
            assert not (
                image["bbox"][0] < (box[0] + box[2]) / 2 < image["bbox"][2]
                and image["bbox"][1] < (box[1] + box[3]) / 2 < image["bbox"][3]
            ), block
    captions = [block for block in blocks if "Figure 9.4.1:" in _visible_text(block["content"])]
    assert len(captions) == 1 and captions[0]["type"] == "caption"


@pytest.mark.parametrize("number", [81, 82, 84])
def test_shaded_table_body_excludes_its_caption_and_note(number):
    """红色表头限定首表范围，上方表题和下方注释不能被新表候选重复认领。"""
    blocks = _pages(f"review_{number}")[0]
    body = min((block for block in blocks if block["type"] == "table"), key=lambda block: block["bbox"][1])
    assert body["bbox"][1] > 0.16
    caption = next(block for block in blocks if block["type"] == "caption")
    assert caption["bbox"][3] < body["bbox"][1]


@pytest.mark.parametrize(
    "number,rows,words",
    [
        (45, 9, ("Name of organization", "Total", "27,926")),
        (46, 12, ("Political party", "Provisional registration", "Kampucheaniyum Party", "+16")),
        (47, 10, ("Political party", "Total", "86,092", "+1,884")),
        (178, 7, ("Communication Channel", "Medium", "Examples", "Goodies", "buttons, etc")),
    ],
)
def test_open_vertical_table_tracks_restore_all_headers_and_terminal_rows(number, rows, words):
    """原页竖向列线限定完整表体，恢复合并表头及末行，不把表题或页眉认领为单元格。"""
    from bs4 import BeautifulSoup

    blocks = _pages(f"review_{number}")[0]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1
    parsed = BeautifulSoup(tables[0]["content"], "html.parser")
    text = parsed.get_text(" ", strip=True)
    assert all(word in text for word in words), text
    assert len(parsed.find_all("tr")) == rows, str(parsed)
    assert "Table:" not in text and "Table 7.1" not in text and "Mission Report" not in text
    assert not any(b["type"] == "text" and any(word == _visible_text(b["content"]).strip() for word in words) for b in blocks)
    if number in {45, 46, 178}:
        captions = [b for b in blocks if b["type"] == "caption" and _visible_text(b["content"]).startswith("Table")]
        assert len(captions) == 1 and captions[0]["bbox"][3] <= tables[0]["bbox"][1]
    if number in {46, 47}:
        assert any(int(cell.get("colspan", 1)) == 2 for cell in parsed.find_all(["td", "th"]))


def test_short_ruled_tables_use_caption_and_complete_rows_including_single_column():
    """三条横线与独立表题支持单列十行任务表及六列两行数据表，不把表注当正文。"""
    from bs4 import BeautifulSoup

    blocks = _pages("review_197")[0]
    tables = sorted([b for b in blocks if b["type"] == "table"], key=lambda b: b["bbox"][1])
    assert len(tables) == 2
    parsed = [BeautifulSoup(b["content"], "html.parser") for b in tables]
    assert [len(table.find_all("tr")) for table in parsed] == [11, 2]
    assert "winogrande:1.1.0" in parsed[0].get_text()
    assert [len(row.find_all(["td", "th"])) for row in parsed[1].find_all("tr")] == [6, 6]
    assert len([b for b in blocks if b["type"] == "caption" and _visible_text(b["content"]).startswith("Table")]) == 2


def test_body_link_underline_does_not_start_page_footnote():
    """正文中同字号链接的下划线不是脚注分隔线，后半正文保持完整。"""
    blocks = _pages("review_178")[0]
    matches = [b for b in blocks if "events, they will still gain recognition" in _visible_text(b["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "text"
    assert "Creating promotional materials" in _visible_text(matches[0]["content"])


def test_body_paragraph_below_chart_is_not_page_footnote():
    """图底横线不是页脚注分隔线，图后的完整正文必须保留正文类型及末行。"""
    matches = [b for b in _pages("review_100")[0] if "On a final note" in _visible_text(b["content"])]
    assert len(matches) == 1 and matches[0]["type"] == "text"
    assert _visible_text(matches[0]["content"]).endswith("stripped of any language")


def test_repeated_appendix_table_captions_separate_each_independent_grid():
    """原页三张相邻表各有编号和表题，表35和36不能合并，表注不得进入表体。"""
    blocks = _pages("review_83")[0]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 3
    captions = [b for b in blocks if b["type"] == "caption"]
    assert all(sum(f"TABLE {number}:" in _visible_text(b["content"]) for b in captions) == 1 for number in (35, 36, 37))
    assert not any(
        "TABLE 35:" in b["content"] or "TABLE 36:" in b["content"] or "These are real data" in b["content"] for b in tables
    )
    assert any(b["type"] == "footnote" and "These are real data" in _visible_text(b["content"]) for b in blocks)


def test_cell_text_reclusters_wrapped_lines_without_other_column_centered_bridge():
    """其他列的垂直居中文字不能让单元格中两条物理行按横坐标交错拼接。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph
    from docvortex.analyzers.native.pdf._table_recovery.text import build_cell_text

    glyphs = []
    for row, word in enumerate(["Alpha", "Beta"]):
        for column, letter in enumerate(word):
            index = len(glyphs)
            glyphs.append(
                NativeTableGlyph(
                    index,
                    index,
                    letter,
                    (column * 6, row * 12, column * 6 + 5, row * 12 + 10),
                    0,
                    explicit_break_before=row == 1 and column == 0,
                )
            )
    assert build_cell_text(glyphs, 10) == "Alpha Beta"


def test_cell_reclustering_keeps_overlapping_slanted_watermark_in_original_visual_row():
    """倾斜水印字框互相覆盖，不具有两条完整物理行的净空，不能重排原视觉行。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph
    from docvortex.analyzers.native.pdf._table_recovery.text import build_cell_text

    glyphs = [
        NativeTableGlyph(i, i, letter, bounds, 0)
        for i, (letter, bounds) in enumerate(
            [
                ("试", (3, 235, 16, 244)),
                ("文", (10, 228, 23, 241)),
                ("档", (17, 221, 30, 234)),
                ("3", (21, 221, 26, 232)),
                (".", (26, 221, 29, 232)),
                ("0", (29, 221, 34, 232)),
            ]
        )
    ]
    assert build_cell_text(glyphs, 10.5) == "试文档3.0"


def test_two_digit_link_notes_are_separate_complete_entries():
    """原页两位编号与链接同行，编号宽度稍大也应按两个独立条目保留。"""
    notes = [_visible_text(b["content"]) for b in _pages("review_46")[0] if b["type"] == "page_footnote"]
    assert len(notes) == 2
    assert [text.split()[0] for text in notes] == ["21", "22"]
    assert notes[0].endswith("/5393") and notes[1].endswith("/5525")


@pytest.mark.parametrize("scale,left", [(0.7, 10), (1, 45), (1.8, 95)])
def test_two_digit_note_marker_width_scales_with_digit_count(scale, left):
    """移动并缩放两位编号，不把略宽编号连成一条注释，右侧链接仍完整。"""
    from docvortex.analyzers.native.pdf.text_assembly.footnotes import _split_page_footnote_entries

    lines = []
    for row, marker in enumerate(("21", "22")):
        y = (100 + row * 14) * scale
        for text, x, width in [(marker, 0, 6.7), ("https://example.org/a-long-note", 15.7, 116)]:
            lines.append(
                _text_line(
                    text,
                    (left + x * scale, y, left + (x + width) * scale, y + 8.2 * scale),
                    len(lines),
                    visual_row_id=row,
                    split_from_row=True,
                    median_glyph_width=2.8 * scale,
                    font_signature=("Synthetic", 0),
                )
            )
    assert [
        [line.source_index for line in entry] for entry in _split_page_footnote_entries(lines, (400 * scale, 500 * scale))
    ] == [[0, 1], [2, 3]]


@pytest.mark.parametrize(
    "number,markers",
    [
        (66, (8, 9)),
        (69, (56, 57)),
        (74, (3,)),
        (75, (4, 5, 6)),
        (77, (9,)),
        (86, (1, 2, 3)),
        (87, (4, 5, 6, 7)),
        (91, (1, 2)),
        (92, (3,)),
        (93, (2,)),
        (95, (10,)),
        (97, (12,)),
        (99, (10,)),
        (101, (25,)),
        (102, (33,)),
        (123, (1,)),
        (124, (2, 3)),
        (125, (8,)),
        (126, (4,)),
        (135, (1,)),
        (153, (1,)),
        (156, (2,)),
    ],
)
def test_lower_numbered_notes_have_all_members_and_exclude_running_footer(number, markers):
    """原页有线和无线脚注都按编号分项，不能把末尾页脚混入条目或留下游离编号。"""
    import re

    blocks = _pages(f"review_{number}")[0]
    notes = [_visible_text(b["content"]) for b in blocks if b["type"] == "page_footnote"]
    for marker in markers:
        matches = [text for text in notes if re.match(rf"^{marker}(?:[.]|\s)", text)]
        assert len(matches) == 1 and len(matches[0]) > 12, (number, marker, notes)
    assert not any(
        "ARTHUR J. CAPLAN" in text or "| Types of Sources" in text or "BEHAVIORAL ECONOMICS PRACTICUM" in text for text in notes
    )
    assert not any(
        b["type"] in {"text", "paragraph_title"}
        and b["bbox"][1] < 0.92
        and _visible_text(b["content"]).strip() in {str(m) for m in markers}
        for b in blocks
    )


def test_poster_contact_instruction_below_decoration_is_body_text():
    """海报底部联络说明与上方同字号内容连贯，无编号和缩字号证据时不能制造页脚注。"""
    match = next(b for b in _pages("review_103")[0] if "EMAIL REBECCA" in _visible_text(b["content"]))
    assert match["type"] in {"text", "footer"}


@pytest.mark.parametrize("scale,left,width", [(0.7, 20, 230), (1, 45, 330), (1.8, 110, 480)])
@pytest.mark.parametrize("kind", ["note", "same_size", "no_marker", "bold_heading", "no_clearance"])
def test_unruled_bottom_note_requires_number_shrink_and_body_clearance(scale, left, width, kind):
    """无线编号小字有正文净空才是脚注；普通编号正文、无编号文字及缩小标题均保持原类型。"""
    ys = (400, 416, 575) if kind == "no_clearance" else (400, 416, 432)
    lines = [
        _text_line("An unrelated ordinary body line", (left, y * scale, left + width, (y + 12) * scale), i)
        for i, y in enumerate(ys)
    ]
    height = (12 if kind == "same_size" else 9) * scale
    text = "Another line without a numeric marker" if kind == "no_marker" else "5. A different source explanation"
    for i, (word, y) in enumerate([(text, 590), ("and its complete continued description", 603)]):
        lines.append(
            _text_line(
                word,
                (left, y * scale, left + width, y * scale + height),
                i + 3,
                dominant_font_weight=700 if kind == "bold_heading" else 400,
            )
        )
    groups = auxiliary_text._unruled_numbered_footnote_groups(
        [(line, line.bbox) for line in lines], (left + width + 40 * scale, 800 * scale)
    )
    assert groups == ([{3, 4}] if kind == "note" else [])


@pytest.mark.parametrize("scale,left", [(0.7, 40), (1, 65), (1.8, 110)])
@pytest.mark.parametrize("kind", ["matching", "unmatched", "ordinary_digit"])
def test_unruled_spanning_same_scale_note_requires_raised_native_reference(scale, left, kind):
    """同字号跨栏注释必须对应正文的原生上标，普通数字与其他编号不能代替对应证据。"""
    lines = [
        _text_line("Ordinary body with an inline reference", (left, y * scale, left + 300 * scale, (y + 12) * scale), i)
        for i, y in enumerate((400, 416, 432))
    ]
    line = lines[0]
    for i, letter in enumerate("body" + ("3" if kind == "unmatched" else "2")):
        small = i == 4 and kind != "ordinary_digit"
        height = (6 if small else 12) * scale
        top = (402 if small else 400) * scale
        line.chars.append(
            {
                "char": letter,
                "bbox": (left + i * 6 * scale, top, left + (i + 1) * 6 * scale, top + height),
                "origin": (left + i * 6 * scale, (408 if small else 412) * scale),
            }
        )
    for i, (word, y) in enumerate([("2. An independent spanning annotation", 600), ("A complete continuation follows", 616)]):
        lines.append(_text_line(word, (left - 20 * scale, y * scale, left + 280 * scale, (y + 14) * scale), 3 + i))
    groups = auxiliary_text._unruled_numbered_footnote_groups(
        [(line, line.bbox) for line in lines], (left + 320 * scale, 800 * scale)
    )
    assert groups == ([{3, 4}] if kind == "matching" else [])


@pytest.mark.parametrize("scale,left", [(0.7, 40), (1, 70), (1.8, 110)])
@pytest.mark.parametrize("numbered", [True, False])
def test_short_rule_outside_body_indent_uses_small_numbered_note_left_edge(scale, left, numbered):
    """正文缩进不能排除位于栏外的编号；短线、同行编号与小字正文共同提供左缘证据。"""
    lines = [
        _text_line("Ordinary continued body paragraph", (left + 28 * scale, y * scale, left + 420 * scale, (y + 14) * scale), i)
        for i, y in enumerate((530, 548, 566))
    ]
    lines.extend(
        [
            _text_line("8" if numbered else "x", (left, 590 * scale, left + 3 * scale, 596 * scale), 3),
            _text_line(
                "A complete annotation with a source", (left + 18 * scale, 589 * scale, left + 350 * scale, 600 * scale), 4
            ),
            _text_line("and its continuation", (left + 18 * scale, 601 * scale, left + 250 * scale, 612 * scale), 5),
        ]
    )
    lane = _TextLane(left + 28 * scale, left + 420 * scale, [(line, line.bbox) for line in lines])
    result = auxiliary_text._footnote_lane_members(
        lane, (left, 585 * scale, left + 72 * scale, 586 * scale), (600 * scale, 800 * scale), page_median_height=14 * scale
    )
    assert bool(result) is numbered


def test_bibliography_page_range_continuation_with_loose_boxes_is_not_unruled_note():
    """参考文献的页码续行虽以数字句点开始，物理行净空不足时不能制造脚注。"""
    lines = [_text_line("Ordinary bibliographic description", (40, y, 340, y + 10), i) for i, y in enumerate((500, 520, 540))]
    lines.extend(
        [
            _text_line("Journal 2025, 18: 123-", (40, 623.8, 340, 642.4), 3, effective_height=10.4),
            _text_line("124. An additional publication detail", (40, 635.1, 340, 653.7), 4, effective_height=9),
        ]
    )
    assert auxiliary_text._unruled_numbered_footnote_groups([(line, line.bbox) for line in lines], (400, 800)) == []


@pytest.mark.parametrize("scale,left,width", [(0.7, 12, 230), (1, 45, 300), (1.8, 95, 480)])
@pytest.mark.parametrize("kind", ["table", "prose", "no_rule", "closed"])
def test_open_table_tracks_require_multiple_columns_and_repeated_rows(scale, left, width, kind):
    """改变栏宽和字号，长列线恢复整表；穿越列线的正文或缺少横线的排列不制造表格。"""
    from docvortex.analyzers.native.pdf import models, table_detection

    top, bottom = 60 * scale, 160 * scale
    tracks = [left + 0.15 * width, left + 0.75 * width]
    rules = [models._AxisLine((x - 0.05, top, x + 0.05, bottom), 0.1, "vertical") for x in tracks]
    if kind == "closed":
        rules.extend(models._AxisLine((x - 0.05, top, x + 0.05, bottom), 0.1, "vertical") for x in [left, left + width])
    if kind != "no_rule":
        rules.append(models._AxisLine((left, 80 * scale, left + width, 80.1 * scale), 0.1, "horizontal"))
    lines = []
    for index, (word, offset) in enumerate([("No", 0.02), ("Category", 0.2), ("Amount", 0.8)]):
        lines.append(
            _text_line(word, (left + offset * width, 63 * scale, left + offset * width + 35 * scale, 73 * scale), index)
        )
    for row in range(5):
        words = [(str(row + 1), 0.02, 8 * scale), ("A general entry", 0.2, 90 * scale), (str(17 + row * 3), 0.8, 20 * scale)]
        if kind == "prose":
            words = [("An ordinary paragraph crosses the proposed columns.", 0.02, 0.95 * width)]
        for word, offset, span in words:
            lines.append(
                _text_line(
                    word,
                    (left + offset * width, (84 + 14 * row) * scale, left + offset * width + span, (94 + 14 * row) * scale),
                    len(lines),
                )
            )
    source = models._PageSource((left + width + 40, 400 * scale), lines, [], rules)
    candidates = table_detection._detect_open_vertical_tables(source, [])
    assert len(candidates) == (1 if kind == "table" else 0)
    if candidates:
        assert candidates[0].core_bbox[1] == top and candidates[0].core_bbox[3] == bottom
        assert candidates[0].line_indices == set(range(18))


def test_split_table_caption_seed_keeps_same_baseline_title_text_outside_header():
    """表题编号与题名为独立原生run时仍组成完整图注，不把下一行表头加入表题。"""
    from docvortex.analyzers.native.pdf import models, table_detection

    lines = [
        _text_line("Table II.", (30, 30, 75, 40), 0),
        _text_line("A general comparison", (90, 30, 230, 40), 1),
        _text_line("Category Amount", (30, 56, 220, 66), 2),
    ]
    source = models._PageSource((300, 400), lines, [], [])
    result = table_detection._bounded_table_caption_annotations(source, (25, 50, 250, 200), 10)
    assert len(result) == 1 and result[0].line_indices == {0, 1}


def test_table_header_recovery_stops_at_previous_caption():
    """表7上方的表6图注是独立排版屏障，不能作为多列表头向上扩张。"""
    tables = [block for block in _pages("review_190")[0] if block["type"] == "table"]
    assert len(tables) == 2
    assert tables[1]["bbox"][1] > 0.19


def test_appendix_multiline_heading_remains_one_title():
    """附录D的两行粗体标题是一条完整题名，编号分类不能制造新的行间段界。"""
    blocks = _pages("two_column_retrieval")[15]
    titles = [
        block
        for block in blocks
        if block["type"] == "paragraph_title" and "BM25-QE and Neural" in _visible_text(block["content"])
    ]
    assert len(titles) == 1 and "20 Analysis" in _visible_text(titles[0]["content"])


@pytest.mark.parametrize('page,anchors',[(9,['8 Limitations']),(15,['C Zero-Shot Baselines']),(17,['F Results Validation Set','G Ablations Validation Set'])])
def test_reviewed_retrieval_section_headings_are_complete_separate_titles(page,anchors):
    """原页编号章节与附录字母题名完整独立，表体及紧随正文不进入标题。"""
    blocks=_pages('two_column_retrieval')[page-1]
    for anchor in anchors:
        matches=[b for b in blocks if _visible_text(b.get('content',''))==anchor]
        assert len(matches)==1 and matches[0]['type']=='paragraph_title'
        assert matches[0]['bbox'][3]-matches[0]['bbox'][1]<.025


def test_three_column_small_labels_keep_equal_heading_status():
    """原页同排的三栏小标题具有相同视觉层级，不能只把右栏识别为标题。"""
    blocks=_pages('review_181')[0]
    for anchor in ['Our Purpose','Our Mission','What We Do']:
        matches=[b for b in blocks if _visible_text(b.get('content',''))==anchor]
        assert len(matches)==1 and matches[0]['type']=='paragraph_title'


def test_metric_value_arrow_and_citation_keep_separate_companion_heading():
    """原页指标和三栏同式的小标题分别保持独立，下面较小字号的解释才是正文。"""
    blocks=_pages('review_184')[0]
    matches=[b for b in blocks if '1.8X' in _visible_text(b.get('content',''))]
    assert len(matches)==1 and matches[0]['type']=='paragraph_title'
    text=_visible_text(matches[0]['content']);assert '↑' in text and text.startswith('1.8X')
    subtitle=[b for b in blocks if _visible_text(b.get('content',''))=='Higher Return of Information']
    assert len(subtitle)==1 and subtitle[0]['type']=='paragraph_title'


def test_same_style_short_heading_before_lettered_item_remains_complete():
    """原页Trash与Replace同字体、字号、字重及左缘，均为独立小节题名，不含下方字母编号项目。"""
    blocks=_pages('review_69')[0]
    for anchor in ['Replace','Trash']:
        matches=[b for b in blocks if _visible_text(b.get('content',''))==anchor]
        assert len(matches)==1 and matches[0]['type']=='paragraph_title'


def test_regular_material_and_numbered_procedure_items_are_not_headings():
    """原页材料及六项操作的首项与其余项目同为常规正文，圆点或数字序号不构成小节标题。"""
    blocks=_pages('review_169')[0]
    for anchor in ['Reagent grade CaCO', 'Reagent grade CaO', 'Reagent grade CaSO',
                   'Coarse dolomitic limestone', 'Fine dolomitic limestone', 'Control (no amendments)',
                   'Label four plastic bags', 'Weigh 20 g', 'Weigh 0.1 gram',
                   'Add the liming material to the soil', 'Add a few mL', 'Close the bags']:
        matches=[b for b in blocks if anchor in _visible_text(b.get('content',''))]
        assert len(matches)==1 and matches[0]['type']=='text'


def test_retrieval_italic_method_tail_completes_previous_paragraph_before_emphasized_start():
    """原页同一方法名的斜体短尾接回前段，加粗Parameter Efficiency为另起段的行内强调。"""
    blocks = _pages('two_column_retrieval')[5]
    previous = [block for block in blocks if _visible_text(block.get('content', '')).startswith('Intuitively,')]
    assert len(previous) == 1 and _visible_text(previous[0]['content']).endswith('CE MAML + Query FT.')
    following = [block for block in blocks if _visible_text(block.get('content', '')).startswith('Parameter Efficiency.')]
    assert len(following) == 1 and following[0]['type'] == 'text'
    assert '+ Query FT.' not in _visible_text(following[0]['content'])


def test_retrieval_caption_does_not_disable_indented_body_paragraph_boundary():
    """图注后恢复正文段界识别：前栏续段只有一句，下一缩进段独立且保留完整论述。"""
    blocks = _pages('two_column_retrieval')[8]
    stub = [block for block in blocks if _visible_text(block.get('content', '')).startswith('over BM25 without')]
    assert len(stub) == 1 and _visible_text(stub[0]['content']) == 'over BM25 without query expansion.12'
    paragraph = [block for block in blocks if _visible_text(block.get('content', '')).startswith('For the re-ranking methods,')]
    assert len(paragraph) == 1 and _visible_text(paragraph[0]['content']).endswith('fewer documents.')


def test_added_cation_blank_answer_column_is_preserved_as_complete_table():
    """原页表13.2含表头、五种离子和右栏空白记录位置；应恢复六行两列表格及独立表题。"""
    from bs4 import BeautifulSoup

    blocks = _pages('review_165')[0]
    tables = [block for block in blocks if block['type'] == 'table' and 'Added cation' in _visible_text(block['content'])]
    assert len(tables) == 1
    rows = BeautifulSoup(tables[0]['content'], 'html.parser').find_all('tr')
    assert len(rows) == 6 and all(len(row.find_all(['td', 'th'])) == 2 for row in rows)
    assert all(not row.find_all(['td', 'th'])[1].get_text(strip=True) for row in rows[1:])
    captions = [block for block in blocks if _visible_text(block.get('content', '')).startswith('Table 13.2.')]
    assert len(captions) == 1 and captions[0]['type'] == 'caption'


def test_unruled_experiment_table_has_complete_four_column_header_and_three_rows():
    """原页顶部实验配方为四列三行数据的无框线表，完整表头和体积单位不能留为游离标题或正文。"""
    from bs4 import BeautifulSoup

    blocks = _pages('review_117')[0]
    tables = [block for block in blocks if block['type'] == 'table']
    assert len(tables) == 1
    rows = BeautifulSoup(tables[0]['content'], 'html.parser').find_all('tr')
    assert len(rows) == 4 and all(len(row.find_all(['td', 'th'])) == 4 for row in rows)
    assert [cell.get_text(' ', strip=True) for cell in rows[0].find_all(['td', 'th'])] == [
        'Saccharometer', 'DI Water', 'Glucose Solution', 'Yeast Suspension']
    assert [cell.get_text(' ', strip=True) for cell in rows[1].find_all(['td', 'th'])] == ['2', '24 ml', '0 ml', '4 ml']


def test_page_clipped_comparison_form_keeps_three_columns_wrapped_headers_and_blank_answers():
    """原页右侧被物理页界裁切，比较表仍有三列六行；两列双行表头不能切丢括号，五项空白答案必须保留。"""
    from bs4 import BeautifulSoup

    blocks=_pages('review_119')[0]
    tables=[block for block in blocks if block['type']=='table']
    assert len(tables)==1
    rows=BeautifulSoup(tables[0]['content'],'html.parser').find_all('tr')
    assert len(rows)==6 and all(len(row.find_all('td'))==3 for row in rows)
    headers=[cell.get_text(' ',strip=True) for cell in rows[0].find_all('td')]
    assert headers==['','Mitosis (begins with a single cell)','Meiosis (begins with a single cell)']
    labels=['#chromosomesinparentcells','#DNAreplications','#nucleardivisions','#daughtercellsproduced','purpose']
    assert [''.join(row.find('td').get_text().split()) for row in rows[1:]]==labels
    assert all(not cell.get_text(strip=True) for row in rows[1:] for cell in row.find_all('td')[1:])
    assert not any(block['type']=='table' and 'Using your beads' in _visible_text(block['content']) for block in blocks)


@pytest.mark.parametrize('scale,left,width',[(.7,12,210),(1,70,330),(1.8,190,600)])
@pytest.mark.parametrize('kind',['form','no_page_edge','missing_track','filled_answer','few_rules','shifted_header','missing_row_label','terminal_header','claimed'])
def test_clipped_blank_form_requires_page_edge_physical_grid_and_all_empty_answer_rows(scale,left,width,kind):
    """变化字号、位置和表宽；仅恢复页界裁切且三列框线完整的空白表单，不吞入有答案、正文或无框区域。"""
    from docvortex.analyzers.native.pdf.models import _PageSource,_AxisLine
    from docvortex.analyzers.native.pdf.table_detection import _detect_page_clipped_blank_form_tables

    right=left+width
    columns=[left,left+width/3,left+2*width/3,right]
    rules=[_AxisLine((left,y*scale,right,(y+.2)*scale),.2*scale,'horizontal') for y in [100,136,156,176,196,216,236]]
    if kind=='few_rules':rules=[rules[i] for i in [0,1,4,6]]
    rules.extend(_AxisLine((x, (136 if index==2 else 100)*scale,x+.1*scale,236*scale),.1*scale,'vertical')
                 for index,x in enumerate(columns[:-1]) if kind!='missing_track' or index!=2)
    lines=[]
    for row in range(2):
        for col in [1,2]:
            x=columns[col]+(3 if col==1 else -3)*scale
            y=(104+14*row+(8 if kind=='shifted_header' and col==2 else 0))*scale
            line=_text_line('Alternate cycle' if row==0 else '(starts with one cell)',(x,y,x+width/3-12*scale,y+10*scale),len(lines))
            if kind=='terminal_header':line.paragraph_terminal=True
            lines.append(line)
    for row in range(5):
        if kind=='missing_row_label' and row==2:continue
        lines.append(_text_line('Another row label',(left+3*scale,(140+20*row)*scale,columns[1]-10*scale,(150+20*row)*scale),len(lines)))
    if kind=='filled_answer':lines.append(_text_line('Some answer',(columns[1]+4*scale,143*scale,columns[1]+50*scale,153*scale),len(lines)))
    page_size=(right+(40*scale if kind=='no_page_edge' else 0),320*scale)
    source=_PageSource(page_size,lines,[],rules)
    excluded=[(left,100*scale,right,237*scale)] if kind=='claimed' else []
    candidates=_detect_page_clipped_blank_form_tables(source,excluded)
    assert bool(candidates) is (kind=='form')
    if candidates:
        assert candidates[0].inferred_grid_authoritative and candidates[0].line_indices==set(range(9))
        assert len(candidates[0].inferred_grid)==11


@pytest.mark.parametrize('scale,left',[(.7,12),(1,70),(1.8,190)])
@pytest.mark.parametrize('kind',['blank_form','numeric_header','filled_answers','few_rows','ordinary_two_column'])
def test_multiline_blank_answer_header_is_not_physical_row_undercount(scale,left,kind):
    """双行纯文字列名可作为一行表头；数值记录、有答案、行证据不足及普通多列数据仍触发漏行保护。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableToken,NativeTableTextRow,NativeTableText
    from docvortex.analyzers.native.pdf._table_recovery.vector import _physical_row_dense_baseline_pairs

    tracks=[left+i*100*scale for i in range(4)]
    rows=[]
    for row,y in enumerate([5,20,45,65,85]):
        if kind=='few_rows' and row==4:continue
        cols=[1,2] if row<2 else ([0,1] if kind in {'filled_answers','ordinary_two_column'} else [0])
        tokens=[]
        for col in cols:
            text='123' if kind=='numeric_header' and row<2 else 'Other label'
            box=(tracks[col]+10*scale,y*scale,tracks[col]+60*scale,(y+8)*scale)
            tokens.append(NativeTableToken(text,box,(),()))
        bounds=(min(t.bbox[0] for t in tokens),y*scale,max(t.bbox[2] for t in tokens),(y+8)*scale)
        rows.append(NativeTableTextRow(row,bounds,tuple(tokens),()))
    text=NativeTableText((),tuple(rows),5*scale,10*scale)
    pairs=_physical_row_dense_baseline_pairs(text,tracks,[y*scale for y in [0,40,60,80,100]])
    assert bool(pairs) is (kind!='blank_form')


def test_captioned_fish_table_keeps_two_columns_and_spanning_header():
    """原页鱼种表的整行底色表头跨两列，四个俗名和斜体学名分列完整，Table 6.1为独立表题。"""
    from bs4 import BeautifulSoup

    blocks = _pages('review_132')[0]
    tables = [block for block in blocks if block['type'] == 'table' and 'Fish species' in _visible_text(block['content'])]
    assert len(tables) == 1
    rows = BeautifulSoup(tables[0]['content'], 'html.parser').find_all('tr')
    assert len(rows) == 5 and rows[0].find('td')['colspan'] == '2'
    assert all(len(row.find_all(['td', 'th'])) == 2 for row in rows[1:])
    assert [row.find_all(['td', 'th'])[0].get_text(' ', strip=True) for row in rows[1:]] == [
        'Potosi Pupfish', 'La Palma Pupfish', 'Butterfly Splitfin', 'Golden Skiffia']
    assert [row.find_all(['td', 'th'])[1].get_text(' ', strip=True) for row in rows[1:]] == [
        'Cyprinodon alvarezi', 'Cyprinodon longidorsalis', 'Ameca splendens', 'Skiffia francesae']
    assert all(row.find_all(['td', 'th'])[1].find('i') or row.find_all(['td', 'th'])[1].find('em') for row in rows[1:])
    caption = [block for block in blocks if _visible_text(block.get('content', '')).startswith('Table 6.1:')]
    assert len(caption) == 1 and caption[0]['type'] == 'caption'


@pytest.mark.parametrize('page,number,metric',[(15,6,'nDCG@20'),(18,10,'Recall@1000')])
def test_parallel_table_panel_notes_bind_own_body_and_shared_caption_is_unique(page,number,metric):
    """原页双面板各有a/b说明，总表题归入同组表格且只出现一次，不能游离为正文或错绑另一栏说明。"""
    from docvortex import parse

    blocks=parse(FIXTURES/'two_column_retrieval.pdf',keep_model_json=True).to_dict()['pages'][page-1]['blocks']
    panels=[block for block in blocks if block['type']=='table'
            and metric not in _visible_text(next(child for child in block['content'] if child['type']=='table_body').get('content',''))
            and block['bbox'][1]>.1 and (page==15 or block['bbox'][1]>.6)]
    assert len(panels)==2
    panels.sort(key=lambda block:block['bbox'][0])
    for panel,letter,anchor in zip(panels,['a','b'],['validation set','test set']):
        captions=[child for child in panel['content'] if child['type']=='table_caption']
        notes=[child for child in captions if _visible_text(child['content']).startswith(f'({letter})')]
        assert len(notes)==1 and anchor in _visible_text(notes[0]['content'])
        assert not any(_visible_text(child['content']).startswith('(b)' if letter=='a' else '(a)') for child in captions)
    overall=[child for panel in panels for child in panel['content'] if child['type']=='table_caption'
             and _visible_text(child['content']).startswith(f'Table {number}:')]
    assert len(overall)==1 and metric in _visible_text(overall[0]['content'])
    assert not any(block['type']=='text' and _visible_text(block.get('content','')).startswith(f'Table {number}:') for block in blocks)


@pytest.mark.parametrize('scale,left,width',[(.7,12,150),(1,70,190),(1.8,210,290)])
@pytest.mark.parametrize('kind',['panels','no_overall','no_right_note','different_row','wide_gap','large_note','far_note','body_between','independent_captions'])
def test_shared_table_caption_needs_aligned_panels_native_notes_and_clear_corridor(scale,left,width,kind):
    """改变栏宽、字号和位置；总表题与a/b说明确认共同区域，独立表题、缺项、错位和正文阻隔拒绝。"""
    from docvortex.analyzers.native.pdf.visual_annotations import (
        _build_visual_parents,_collect_annotation_candidates,_add_caption_supported_table_panel_parents,
        _classify_and_bind_visual_annotations,
    )

    gap=(100 if kind=='wide_gap' else 30)*scale
    rx=left+width+gap
    blocks=[]
    for col,x in enumerate([left,rx]):
        top=(120 if kind=='different_row' and col==1 else 100)*scale
        blocks.append({'type':'table','bbox':(x,top,x+width,200*scale),'content':'<table><tr><td>Different values</td></tr></table>'})
    for col,x in enumerate([left,rx]):
        if kind=='no_right_note' and col==1:continue
        height=(16 if kind=='large_note' and col==1 else 10)*scale
        top=(225 if kind=='far_note' and col==1 else 205)*scale
        blocks.append({'type':'text','bbox':(x+10*scale,top,x+width-10*scale,top+height),
                       'content':f'({"ab"[col]}) Another measured set.','_line_heights':[height]})
    if kind!='no_overall':
        blocks.append({'type':'text','bbox':(left,235*scale,rx+width,265*scale),
                       'content':'Table 21: Unrelated evaluation measurements. More explanatory sentences.', '_line_heights':[10*scale]})
    if kind=='body_between':blocks.append({'type':'text','bbox':(left,218*scale,rx+width,229*scale),
                                          'content':'Some independent body prose ends here.', '_line_heights':[10*scale]})
    if kind=='independent_captions':
        blocks[-1]['bbox']=(left,235*scale,left+width,245*scale)
        blocks.append({'type':'text','bbox':(rx,235*scale,rx+width,245*scale),
                       'content':'Table 22: A separate table.', '_line_heights':[10*scale]})
    page_size=(rx+width+20*scale,350*scale)
    candidates=_collect_annotation_candidates(blocks)
    parents=_build_visual_parents(blocks,page_size,10*scale)
    _add_caption_supported_table_panel_parents(blocks,page_size,parents,candidates)
    assert any(len(parent.member_indices)>1 for parent in parents) is (kind=='panels')
    if kind=='panels':
        regions=_classify_and_bind_visual_annotations(blocks,page_size)
        assert len(regions)==1
        assert [block['type'] for block in regions[0]]==['table','caption','table','caption','caption']
        assert regions[0][1]['content'].startswith('(a)') and regions[0][3]['content'].startswith('(b)')


@pytest.mark.parametrize('scale,left,width',[(.7,15,210),(1,70,290),(1.8,190,430)])
@pytest.mark.parametrize('kind',['table','regular_header','two_rows','shifted_column','prose','missing_label','claimed','formula'])
def test_unruled_numeric_grid_requires_repeated_columns_and_complete_emphasized_header(scale,left,width,kind):
    """改变位置、字号、列宽和文字；只有三行数值与完整粗体列名同时成立才恢复表格。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.table_detection import _detect_unruled_numeric_column_tables

    columns=[left+col*width/3 for col in range(4)]
    header=_text_line('Vessel Water Nutrient Culture',(left,80*scale,left+width+35*scale,90*scale),0,
                      font_signature=('GenericSerif',0),font_coverage=1,
                      dominant_font_weight=400 if kind=='regular_header' else 700)
    header.chars=[{'char':'Label'[col],'bbox':(x,80*scale,x+5*scale,90*scale)} for col,x in enumerate(columns)]
    if kind=='missing_label':header.chars.pop()
    lines=[header]
    for row in range(2 if kind=='two_rows' else 3):
        for col,x in enumerate(columns):
            if kind=='shifted_column' and row==1 and col==2:x+=7*scale
            text='unrelated prose' if kind=='prose' and col==1 else (str(row+5) if col==0 else f'{row+col} ml')
            line=_text_line(text,(x,(92+14*row)*scale,x+22*scale,(102+14*row)*scale),len(lines))
            if kind=='formula':line.formula_candidate_only=True
            lines.append(line)
    source=_PageSource((left+width+100*scale,250*scale),lines,[],[])
    excluded=[(left,80*scale,left+width+40*scale,150*scale)] if kind=='claimed' else []
    candidates=_detect_unruled_numeric_column_tables(source,excluded)
    assert bool(candidates) is (kind=='table')
    if candidates:
        assert candidates[0].line_indices==set(range(13))
        assert candidates[0].preserve_inline_font_styles


@pytest.mark.parametrize('scale,left,width',[(.7,15,140),(1,70,180),(1.8,190,260)])
@pytest.mark.parametrize('kind',['table','no_fill','light_fill','no_caption','few_rows','missing_right','misaligned','claimed'])
def test_pair_text_table_requires_native_header_band_caption_and_each_column_start(scale,left,width,kind):
    """移动表格并改变字号和宽度；填色表头、表题、四行双列字符缺一不可，原生合并行仍可恢复。"""
    from docvortex.analyzers.native.pdf.models import _PageSource,_AxisLine
    from docvortex.analyzers.native.pdf.table_detection import _detect_captioned_pair_text_tables
    from docvortex.document.pdf.native_contracts import PDFPathInfo

    right=left+55*scale
    lines=[_text_line('Other classified species',(left+3*scale,103*scale,left+width-3*scale,113*scale),0)]
    for row in range(3 if kind=='few_rows' else 4):
        y=(119+16*row)*scale
        rx=right+(8*scale if kind=='misaligned' and row==3 else 0)
        groups=[('Common species',left+3*scale,left+30*scale),('Scientific name',rx,rx+50*scale)]
        if row in {1,2}:
            line=_text_line('Common species Scientific name',(groups[0][1],y,rx+50*scale,y+10*scale),len(lines))
            line.chars=[{'char':'C','bbox':(groups[0][1],y,groups[0][2],y+10*scale)}]
            if kind!='missing_right' or row!=2:line.chars.append({'char':'S','bbox':(rx,y,rx+5*scale,y+10*scale)})
            lines.append(line)
        else:
            for text,x,end in groups:
                line=_text_line(text,(x,y,end,y+10*scale),len(lines))
                line.chars=[{'char':text[0],'bbox':line.bbox}]
                lines.append(line)
    if kind!='no_caption':lines.append(_text_line('Table 8.4: A different collection',(left,187*scale,left+width,197*scale),len(lines)))
    rules=[_AxisLine((left,y*scale,left+width,(y+.5)*scale),.5*scale,'horizontal') for y in [100,179]]
    fills=[] if kind=='no_fill' else [PDFPathInfo((left,100*scale,left+width,116*scale),5,True,False,0,0,
                                                (210,210,210,255) if kind=='light_fill' else (40,60,40,255))]
    source=_PageSource((left+width+40*scale,250*scale),lines,[],rules,path_infos=fills)
    excluded=[(left,100*scale,left+width,180*scale)] if kind=='claimed' else []
    candidates=_detect_captioned_pair_text_tables(source,excluded)
    assert bool(candidates) is (kind=='table')
    if candidates:
        assert len(candidates[0].line_indices)==7
        assert len(candidates[0].annotations)==1


@pytest.mark.parametrize('scale,left',[(.7,12),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['italic','bold','both','plain','sup','disabled','mismatch'])
def test_native_table_font_styles_escape_source_text_and_preserve_scripts(scale,left,kind):
    """字体标志和粗细来自原生字符，转义原文；上下标可与强调共存，未启用或文字不匹配时不生成强调。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableCell,NativeTableGlyph
    from docvortex.analyzers.native.pdf.table_text_styles import _render_styled_cell

    chars=[{'char':char,'char_idx':i,'bbox':(left+i*5*scale,100*scale,left+(i+1)*5*scale,110*scale),
            'font':{'name':'UnrelatedFamily','flags':64 if kind in {'italic','both','sup','disabled','mismatch'} else 0,
                    'weight':700 if kind in {'bold','both'} else 400}} for i,char in enumerate('<A2')]
    glyphs=[NativeTableGlyph(i,i,char['char'],char['bbox'],0) for i,char in enumerate(chars)]
    cell=NativeTableCell(0,0,1,1,(left,100*scale,left+15*scale,110*scale),'<Other>' if kind=='mismatch' else '<A2',tuple(range(3)))
    actual=_render_styled_cell(cell,glyphs,10*scale,{2:'sup'} if kind=='sup' else {},
                               chars_by_source={i:c for i,c in enumerate(chars)},preserve_font_styles=kind!='disabled')
    expected={'italic':'<i>&lt;A2</i>','bold':'<b>&lt;A2</b>','both':'<b><i>&lt;A2</i></b>',
              'plain':'&lt;A2','sup':'<i>&lt;A</i><sup><i>2</i></sup>','disabled':'&lt;A2','mismatch':'&lt;Other&gt;'}
    assert actual==expected[kind]


def test_ai_pack_open_table_keeps_headers_and_final_highlight_row_inside():
    """原页三种AI产品加左侧行名共四列，表头及末行Highlight均归入同一完整表体。"""
    from bs4 import BeautifulSoup

    blocks = _pages('review_182')[0]
    tables = [block for block in blocks if block['type'] == 'table']
    assert len(tables) == 1
    rows = BeautifulSoup(tables[0]['content'], 'html.parser').find_all('tr')
    assert len(rows) == 4 and all(len(row.find_all(['td', 'th'])) == 4 for row in rows)
    text = BeautifulSoup(tables[0]['content'], 'html.parser').get_text(' ', strip=True)
    for anchor in ['OCR', 'Recommendation', 'Product semantic search', 'Highlight', 'E-commerce subject (Shopee)']:
        assert anchor in text
        assert not any(anchor in _visible_text(block.get('content', '')) for block in blocks if block['type'] != 'table')


def test_table_dominated_slide_keeps_native_large_document_title():
    """整表成员认领后正文不能只用两行大题名统计，大标题及上方小标签仍保持原有独立层级。"""
    blocks = _pages('review_182')[0]
    title = [block for block in blocks if _visible_text(block.get('content', '')).startswith('Upstage offers 3 AI packs')]
    assert len(title) == 1 and title[0]['type'] == 'doc_title'
    assert _visible_text(title[0]['content']).endswith('making a tangible impact on your business')
    assert any(_visible_text(block.get('content', '')) == 'AI Pack' for block in blocks)


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
@pytest.mark.parametrize('canonical', [False, True])
@pytest.mark.parametrize('table_evidence', [False, True])
def test_table_native_body_typography_does_not_turn_large_title_into_body(scale, left, canonical, table_evidence):
    """表体认领前后的原生常规行仍支持正文尺度，保留大題名相对字号；缺少证据时沿用现有回退。"""
    from docvortex.analyzers.native.pdf.models import _PreparedPage
    from docvortex.analyzers.native.pdf.title_analysis.body_profile import _infer_document_body_profile

    title = [_text_line('A large title across the page', (left, y*scale, left+450*scale, (y+30)*scale), index)
             for index,y in enumerate([40, 73])]
    table_lines = [_text_line('Several ordinary words inside one complete table cell',
                             (left, (180+14*index)*scale, left+260*scale, (190+14*index)*scale), index+2)
                   for index in range(8)]
    prepared = _PreparedPage((left+600*scale, 500*scale), title, [(left, 180*scale, left+400*scale, 310*scale)], [], [],
                             table_body_profile_lines=table_lines if table_evidence else [])
    profile = _infer_document_body_profile([prepared], use_canonical_scale=canonical)
    assert profile is not None and profile.body_height == (10 if table_evidence else 30)*scale


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
def test_dense_document_body_overrides_sparse_table_page_typography(scale, left):
    """其他页已有充分正文时不以表格的小字污染全文画像，保证正文和表体样式的统计职责分离。"""
    from docvortex.analyzers.native.pdf.models import _PreparedPage
    from docvortex.analyzers.native.pdf.title_analysis.body_profile import _infer_document_body_profile

    title = [_text_line('A large document title', (left, 40*scale, left+400*scale, 70*scale), 0)]
    table = [_text_line('Dense small cell text', (left, (150+8*i)*scale, left+300*scale, (157+8*i)*scale), i+1)
             for i in range(20)]
    body = [_text_line('A complete ordinary body sentence with several words',
                       (left, (150+16*i)*scale, left+400*scale, (162+16*i)*scale), i+21) for i in range(10)]
    table_page = _PreparedPage((left+600*scale, 500*scale), title, [], [], [], table_body_profile_lines=table)
    body_page = _PreparedPage(table_page.page_size, body, [], [], [])
    profile = _infer_document_body_profile([table_page, body_page])
    assert profile is not None and profile.body_height == pytest.approx(12*scale)


@pytest.mark.parametrize('scale,left,width', [(1, 30, 240), (.75, 180, 190), (1.6, 65, 370)])
@pytest.mark.parametrize('kind', ['blank_table', 'no_caption', 'regular_header', 'normal_spaces', 'two_large_gaps', 'long_key', 'filled_answer', 'short_list', 'image'])
def test_captioned_blank_table_requires_two_headers_and_repeated_short_left_keys(scale, left, width, kind):
    """空白列需由独立表题、横线及双列表头共同证明，列表、正文、普通空格和内容图不能成为表格。"""
    from docvortex.analyzers.native.pdf import models, table_detection

    caption = _text_line('Table 7. General observations', (left, 25*scale, left+.9*width, 38*scale), 0)
    header = _text_line('First Label Second Complete Header', (left+4*scale, 64*scale, left+120*scale, 74*scale), 1,
                        font_signature=('GeneralRegular' if kind == 'regular_header' else 'GeneralBold', 0), font_coverage=1)
    x = left+4*scale
    for word_index,word in enumerate(header.text.split()):
        if word_index:
            gap = 10 if word_index == 2 and kind != 'normal_spaces' or word_index == 3 and kind == 'two_large_gaps' else 2
            x += gap*scale
        for char in word:
            header.chars.append({'char': char, 'bbox': (x, 64*scale, x+3*scale, 74*scale)})
            x += 3*scale
    header.bbox = (left+4*scale, 64*scale, x, 74*scale)
    keys = [_text_line('Control' if index == 4 else chr(65+index),
                       (left+4*scale, (80+16*index)*scale, left+32*scale, (90+16*index)*scale), index+2)
            for index in range(5)]
    if kind == 'long_key':
        keys[2].text = 'An ordinary full length sentence'
    elif kind == 'filled_answer':
        keys[2].bbox = (left+100*scale, 112*scale, left+140*scale, 122*scale)
    elif kind == 'short_list':
        keys.pop()
    lines = [header, *keys] if kind == 'no_caption' else [caption, header, *keys]
    rules = [models._AxisLine((left, y*scale, left+width, (y+.2)*scale), .2*scale, 'horizontal') for y in [60, 160]]
    source = models._PageSource((left+width+40, 400*scale), lines, [], rules)
    excluded = [(left, 60*scale, left+width, 161*scale)] if kind == 'image' else []
    candidates = table_detection._detect_captioned_blank_answer_tables(source, excluded)
    assert len(candidates) == (1 if kind == 'blank_table' else 0)
    if candidates:
        assert candidates[0].line_indices == {1, 2, 3, 4, 5, 6}
        assert len(candidates[0].inferred_grid) == 10


@pytest.mark.parametrize('scale,left,width', [(1, 30, 240), (.75, 180, 190), (1.6, 65, 370)])
@pytest.mark.parametrize('kind', ['headers', 'single', 'misaligned', 'crosses_column', 'caption', 'far', 'same_column'])
def test_open_table_outside_headers_require_distinct_columns_and_same_visual_row(scale, left, width, kind):
    """列线上方表头需独占不同列且同排，单条标题、跨列文字和不同层级不向表顶扩张。"""
    from docvortex.analyzers.native.pdf.table_detection import _open_table_header_members

    bounds = (left, 100*scale, left+width, 260*scale)
    tracks = [(left+fraction*width-.1, 100*scale, left+fraction*width+.1, 260*scale) for fraction in [1/3, 2/3]]
    a = _text_line('Local Label', (left+.08*width, 86*scale, left+.25*width, 96*scale), 0)
    b = _text_line('Second Label', (left+.4*width, 86*scale, left+.6*width, 96*scale), 1)
    if kind == 'misaligned':
        b.bbox = (left+.4*width, 80*scale, left+.6*width, 90*scale)
    elif kind == 'crosses_column':
        b.bbox = (left+.28*width, 86*scale, left+.6*width, 96*scale)
    elif kind == 'caption':
        b.text = 'Table 4. General data'
    elif kind == 'far':
        b.bbox = (left+.4*width, 60*scale, left+.6*width, 70*scale)
    elif kind == 'same_column':
        b.bbox = (left+.1*width, 86*scale, left+.25*width, 96*scale)
    lines = [a] if kind == 'single' else [a, b]
    result = _open_table_header_members(lines, bounds, tracks, 10*scale)
    assert result == ([a, b] if kind == 'headers' else [])


@pytest.mark.parametrize('scale,left,width', [(1, 30, 240), (.75, 180, 190), (1.6, 65, 370)])
@pytest.mark.parametrize('kind', ['three_rules', 'two_rules', 'divergent_ends'])
def test_open_table_unequal_column_ends_require_three_complete_row_rules(scale, left, width, kind):
    """略有错开的列线末端仅在三条同宽分隔线证据下采用最长完整边界，弱证据及明显越界不放宽。"""
    from docvortex.analyzers.native.pdf import models, table_detection

    rules = [models._AxisLine((left+fraction*width-.1, 80*scale, left+fraction*width+.1, bottom*scale), .2, 'vertical')
             for fraction,bottom in zip([.25, .5, .75], [220, 207 if kind != 'divergent_ends' else 200, 218])]
    rules.extend(models._AxisLine((left, y*scale-.1, left+width, y*scale+.1), .2, 'horizontal')
                 for y in ([80, 140] if kind == 'two_rules' else [80, 140, 200]))
    lines = [_text_line(f'Entry {row} {column}',
                        (left+(.05+.25*column)*width, (90+20*row)*scale,
                         left+(.20+.25*column)*width, (100+20*row)*scale), 4*row+column)
             for row in range(6) for column in range(4)]
    source = models._PageSource((left+width+50, 400*scale), lines, [], rules)
    candidates = table_detection._detect_open_vertical_tables(source, [])
    assert len(candidates) == (1 if kind == 'three_rules' else 0)
    if candidates:
        assert candidates[0].core_bbox[3] == 220*scale


@pytest.mark.parametrize('scale,left,width', [(1, 30, 220), (.75, 180, 180), (1.6, 65, 340)])
@pytest.mark.parametrize('kind', ['tail', 'lowercase', 'capital_label', 'completed_previous', 'far_gap', 'font_conflict', 'bold', 'formula', 'table'])
def test_short_italic_sentence_tail_requires_unfinished_wide_body_and_same_family(scale, left, width, kind):
    """斜体收句短尾在缩放和移位后接回正文，独立标签、已收句前段、公式及障碍不被强行续接。"""
    from docvortex.analyzers.native.pdf.line_layout import _should_connect_text_rows

    previous = _text_line('An unfinished full sentence extends into the next',
                          (left, 100*scale, left+width, 110*scale), 0,
                          font_signature=('GeneralRegu', 0), font_coverage=1, dominant_font_weight=400)
    current = _text_line('and further detail.' if kind == 'lowercase' else '+ Further Detail.',
                         (left, 112*scale, left+.3*width, 122*scale), 1,
                         font_signature=('GeneralReguItal', 64), font_coverage=1, dominant_font_weight=400)
    current.paragraph_terminal = True
    tables = []
    if kind == 'capital_label':
        current.text = 'Another Independent Label.'
    elif kind == 'completed_previous':
        previous.paragraph_terminal = True
        previous.text += '.'
    elif kind == 'far_gap':
        current.bbox = (left, 135*scale, left+.3*width, 145*scale)
    elif kind == 'font_conflict':
        current.font_signature = ('OtherFaceItalic', 64)
    elif kind == 'bold':
        current.dominant_font_weight = 700
    elif kind == 'formula':
        current.formula_candidate_only = True
    elif kind == 'table':
        tables = [(left-5, 110*scale, left+width, 112*scale)]
    lane = _TextLane(left, left+width, [(previous, previous.bbox), (current, current.bbox)])
    result = _should_connect_text_rows((previous, previous.bbox), (current, current.bbox), lane, scale, 0, tables, [])
    assert result is (kind in {'tail', 'lowercase'})


@pytest.mark.parametrize('scale,left,width', [(1, 30, 220), (.75, 180, 180), (1.6, 65, 340)])
@pytest.mark.parametrize('kind', ['body', 'caption_continues', 'no_blank', 'same_size', 'wrong_next_font', 'next_caption'])
def test_completed_small_caption_releases_body_paragraph_indent_protection(scale, left, width, kind):
    """完整小字图注后的留白与较大字号正文解除图注保护，图注续句或缺少排版转换时保持保护。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _prose_paragraph_break_sources

    lines = []
    body_height = 10 if kind == 'same_size' else 11
    specifications = [
        ('Figure 3: A complete description starts here', 100, 10, 0, width),
        ('The description continues across this full row', 112, 10, 0, width),
        ('The description ends.' if kind != 'caption_continues' else 'The description continues', 124, 10, 0, .65*width),
        ('previous body continuation.11', 136 if kind == 'no_blank' else 145, body_height, 0, .75*width),
        ('For another independent paragraph the discussion', 158, body_height, 8, width-8*scale),
        ('returns to the stable left edge with regular text', 171, body_height, 0, width),
    ]
    for index,(text,top,height,indent,span) in enumerate(specifications):
        line = _text_line(text, (left+indent*scale, top*scale, left+indent*scale+span, (top+height)*scale), index,
                          font_signature=('GeneralBody', 0), font_coverage=1)
        line.paragraph_terminal = index == 3 or index == 2 and kind != 'caption_continues'
        line.caption_start = index == 0 or index == 4 and kind == 'next_caption'
        if index == 4 and kind == 'wrong_next_font':
            line.font_signature = ('OtherBody', 0)
        lines.append(line)
    lane = _TextLane(left, left+width, [(line, line.bbox) for line in lines])
    assert (4 in _prose_paragraph_break_sources(lane, 2*scale, 0)) is (kind == 'body')


@pytest.mark.parametrize('scale,left,width', [(1, 30, 220), (.75, 180, 180), (1.6, 65, 340)])
@pytest.mark.parametrize('kind', ['emphasis', 'no_terminal', 'no_weight', 'body_same_weight', 'no_following', 'far_gap', 'formula'])
def test_short_tail_then_leading_bold_run_starts_new_body_paragraph(scale, left, width, kind):
    """短收句后独立加粗行首及常规续行构成新正文段，弱字重、未收句、公式和远距均不适用。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _leading_typography_reset_break_sources

    previous = _text_line('A short final clause.' if kind != 'no_terminal' else 'An unfinished clause',
                          (left, 100*scale, left+.25*width, 110*scale), 0)
    current = _text_line('General Label. A new complete discussion begins',
                         (left, 116*scale, left+width, 126*scale), 1,
                         dominant_font_weight=400 if kind == 'no_weight' else 600,
                         leading_emphasis_width=.45*width, leading_typography_width=.45*width)
    following = _text_line('This ordinary line continues the same discussion',
                           (left, 130*scale, left+width, 140*scale), 2,
                           dominant_font_weight=600 if kind == 'body_same_weight' else 400)
    if kind == 'far_gap':
        current.bbox = (left, 150*scale, left+width, 160*scale)
    elif kind == 'formula':
        current.formula_candidate_only = True
    lines = [previous, current] if kind == 'no_following' else [previous, current, following]
    lane = _TextLane(left, left+width, [(line, line.bbox) for line in lines])
    assert (1 in _leading_typography_reset_break_sources(lane, scale, 0)) is (kind == 'emphasis')


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
@pytest.mark.parametrize('kind', ['metric', 'far_arrow', 'missing_peer', 'formula', 'ambiguous', 'subtitle', 'misaligned', 'font_conflict'])
def test_metric_heading_fragments_require_unique_arrow_and_parallel_heading(scale, left, kind):
    """移动和缩放指标后仍聚合完整标题；远距、公式、歧义、字体冲突及下一行说明保持原成员。"""
    from docvortex.analyzers.native.pdf.line_merging import _merge_metric_heading_fragments

    font = ('GeneralMetric', 0)
    value = _text_line('23.4%', (left, 100*scale, left+60*scale, 130*scale), 0,
                       font_signature=font, font_coverage=1)
    arrow = _text_line('↗2', (left+64*scale, 101*scale, left+80*scale, 123*scale), 1,
                       font_signature=font, font_coverage=1, semantic_type='paragraph_title')
    peer = _text_line('Another Measure', (left+240*scale, 102*scale, left+390*scale, 132*scale), 2,
                      font_signature=font, font_coverage=1, semantic_type='paragraph_title')
    body = _text_line('Independent explanation', (left, 142*scale, left+200*scale, 158*scale), 3)
    lines = [value, arrow, peer, body]
    if kind == 'far_arrow':
        arrow.bbox = (left+100*scale, 101*scale, left+116*scale, 123*scale)
    elif kind == 'missing_peer':
        peer.semantic_type = None
    elif kind == 'formula':
        value.formula_candidate_only = True
    elif kind == 'ambiguous':
        duplicate = _text_line('↑3', arrow.bbox, 4, font_signature=font, font_coverage=1, semantic_type='paragraph_title')
        lines.append(duplicate)
    elif kind == 'subtitle':
        value.text = 'Several ordinary words'
    elif kind == 'misaligned':
        peer.bbox = (left+240*scale, 202*scale, left+390*scale, 232*scale)
    elif kind == 'font_conflict':
        arrow.font_signature = ('OtherFace', 0)
    result = _merge_metric_heading_fragments(lines, (left+450*scale, 500*scale))
    if kind == 'metric':
        assert len(result) == 3
        merged = next(line for line in result if line.text.startswith('23.4%'))
        assert merged.text == '23.4% ↗2' and merged.semantic_type == 'paragraph_title'
        assert merged.bbox == (left, 100*scale, left+80*scale, 130*scale)
        assert body in result and body.semantic_type is None
    else:
        assert result == lines


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
@pytest.mark.parametrize('kind', ['labels', 'no_seed', 'two_columns', 'font_conflict', 'misaligned', 'inside_image', 'ambiguous_intro', 'narrow_intro'])
def test_parallel_small_column_labels_require_repeated_geometry_and_known_level(scale, left, kind):
    """三栏小题名按同排与独立简介复核层级，普通标签、图内文字和不一致排版不被晋升。"""
    from docvortex.analyzers.native.pdf.title_analysis.page_titles import _restore_parallel_small_column_labels

    geometry = []
    labels = []
    for column in range(3 if kind != 'two_columns' else 2):
        x = left + 180*scale*column
        heading = _text_line(f'Local Label {chr(65+column)}', (x, 100*scale, x+55*scale, 110*scale), column*2,
                             font_signature=('GeneralLabels', 0), font_coverage=1,
                             semantic_type='paragraph_title' if column == 0 and kind != 'no_seed' else None)
        intro = _text_line('An independent introduction', (x, 122*scale, x+120*scale, 136*scale), column*2+1)
        if column == 2 and kind == 'font_conflict':
            heading.font_signature = ('UnrelatedLabels', 0)
        elif column == 2 and kind == 'misaligned':
            heading.bbox = (x, 103*scale, x+55*scale, 113*scale)
        elif column == 2 and kind == 'narrow_intro':
            intro.bbox = (x, 122*scale, x+60*scale, 136*scale)
        labels.append(heading)
        geometry.extend([(heading, heading.bbox), (intro, intro.bbox)])
    if kind == 'ambiguous_intro':
        duplicate = _text_line('Another competing introduction', geometry[-1][1], 8)
        geometry.append((duplicate, duplicate.bbox))
    before = [line.semantic_type for line in labels]
    containers = [(left-5*scale, 95*scale, left+450*scale, 160*scale)] if kind == 'inside_image' else []
    _restore_parallel_small_column_labels(geometry, containers)
    assert [line.semantic_type for line in labels] == (['paragraph_title']*3 if kind == 'labels' else before)


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
@pytest.mark.parametrize('kind', ['bullet', 'number', 'bold', 'single', 'nonconsecutive', 'misaligned', 'explicit', 'caption'])
def test_regular_repeated_items_supply_body_evidence_without_demoting_real_heading(scale, left, kind):
    """同排常规项目为首项提供正文反证，但字号、字重、编号或明确题名反证必须保留标题。"""
    from docvortex.analyzers.native.pdf.models import _DocumentBodyProfile
    from docvortex.analyzers.native.pdf.title_analysis.structural import _demote_regular_repeated_item_titles

    number = kind != 'bullet'
    first = _text_line(('1.' if number else '•')+' First complete operation',
                       (left, 100*scale, left+180*scale, 110*scale), 0,
                       font_signature=('GeneralBody', 0), font_coverage=1,
                       dominant_font_weight=700 if kind == 'bold' else 400, semantic_type='paragraph_title')
    second = _text_line(('5.' if kind == 'nonconsecutive' else '2.' if number else '•')+' Second complete operation',
                        (left, 124*scale, left+190*scale, 134*scale), 1,
                        font_signature=first.font_signature, font_coverage=1, dominant_font_weight=first.dominant_font_weight)
    if kind == 'misaligned':
        second.bbox = (left+20*scale, 124*scale, left+210*scale, 134*scale)
    elif kind == 'explicit':
        first.explicit_section_title = True
    elif kind == 'caption':
        first.caption_start = True
    profile = _DocumentBodyProfile(10*scale, 400, frozenset({first.font_signature}))
    _demote_regular_repeated_item_titles([first] if kind == 'single' else [first, second], profile)
    assert first.semantic_type == (None if kind in {'bullet', 'number'} else 'paragraph_title')


@pytest.mark.parametrize('scale,left', [(1, 30), (.75, 180), (1.6, 65)])
@pytest.mark.parametrize('prefix', ['d.', 'Continued'])
def test_runin_lettered_item_does_not_demote_preceding_independent_heading(scale, left, prefix):
    """字母项目的行内强调只降级自身，真正的标题续行碎片仍将相同样式前缀降为正文。"""
    from docvortex.analyzers.native.pdf.line_merging import _demote_runin_title_fragments

    font = ('GeneralBold', 0)
    heading = _text_line('Local Section', (left, 100*scale, left+70*scale, 110*scale), 0,
                         font_signature=font, font_coverage=1, dominant_font_weight=700, semantic_type='paragraph_title')
    item = _text_line(prefix, (left, 114*scale, left+50*scale, 124*scale), 1,
                      font_signature=font, font_coverage=1, dominant_font_weight=700, semantic_type='paragraph_title')
    body = _text_line('An ordinary continuation of the item', (left+55*scale, 114*scale, left+250*scale, 124*scale), 2)
    _demote_runin_title_fragments([heading, item, body], (left+300*scale, 500*scale))
    assert item.semantic_type is None
    assert heading.semantic_type == ('paragraph_title' if prefix == 'd.' else None)


@pytest.mark.parametrize("scale,left,width", [(1, 30, 220), (1.6, 70, 310), (0.8, 100, 180)])
@pytest.mark.parametrize("kind", ["page_footnote", "header"])
def test_marginal_column_order_preserves_note_entries_and_header_rows(scale, left, width, kind):
    """改变字号、位置和栏宽后脚注仍沿栏读完，普通复合页眉仍按视觉行排序。"""
    blocks = []
    for column in range(2):
        for row in range(2):
            x = left + column * (width + 35 * scale)
            y = (600 + row * 35) * scale
            blocks.append(
                {
                    "type": kind,
                    "bbox": (x, y, x + width, y + 20 * scale),
                    "content": f"{column}:{row}",
                    "_line_heights": [10 * scale],
                }
            )
    result = pipeline._sort_marginal_blocks(list(reversed(blocks)), (1000 * scale, 850 * scale))
    expected = ["0:0", "0:1", "1:0", "1:1"] if kind == "page_footnote" else ["0:0", "1:0", "0:1", "1:1"]
    assert [block["content"] for block in result] == expected


@pytest.mark.parametrize("scale,left", [(1, 45), (1.6, 120), (0.8, 70)])
@pytest.mark.parametrize("hanging", [True, False])
def test_numbered_heading_continuation_uses_indent_not_numeric_prefix(scale, left, hanging):
    """同字族、字号和续行缩进确认长标题；另一个左对齐编号标题保持独立段界。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_bold_numbered_heading_rows

    first = _text_line(
        "D Different neural ranking analysis for the top",
        (left, 80 * scale, left + 270 * scale, 94 * scale),
        0,
        dominant_font_weight=700,
        font_coverage=1,
        font_signature=("opaque-heading", 0),
    )
    indent = 20 * scale if hanging else 0
    second = _text_line(
        "20 Queries" if hanging else "E Other experiments",
        (left + indent, 93 * scale, left + 140 * scale, 107 * scale),
        1,
        dominant_font_weight=700,
        font_coverage=1,
        font_signature=("opaque-heading", 0),
    )
    _classify_bold_numbered_heading_rows([first, second], (600 * scale, 800 * scale), [], appendix_only=True)
    assert first.semantic_type == second.semantic_type == "paragraph_title"
    assert (first.title_band_id == second.title_band_id) is hanging


@pytest.mark.parametrize("scale,left", [(1, 45), (1.6, 110), (0.8, 70)])
@pytest.mark.parametrize("kind", ["note", "same_ink", "missing_marker", "no_gap"])
def test_single_line_note_ink_evidence_requires_number_gap_and_real_shrink(scale, left, kind):
    """错误行框下仍依据原生字形缩小识别脚注，普通同字号编号段及紧贴编号不命中。"""
    from reportlab.pdfgen.canvas import Canvas

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(700 * scale, 800 * scale))
    canvas.setFont("Helvetica", 10 * scale)
    for y in (500, 480, 460):
        canvas.drawString(left, y * scale, "Several ordinary sentences establish the body character scale")
    canvas.setFont("Helvetica", (10 if kind == "same_ink" else 8) * scale)
    prefix = "" if kind == "missing_marker" else "4. " if kind == "no_gap" else "4.    "
    canvas.drawString(left, 104 * scale, prefix + "Another different numbered explanatory note")
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
    body = [line for line in source.lines if line.bbox[1] < 500 * scale]
    note = next(line for line in source.lines if line.bbox[1] > 650 * scale)
    em = body[0].effective_height
    note.effective_height = em
    note.bbox = (note.bbox[0], note.bbox[1], note.bbox[2], note.bbox[1] + em)
    matched = auxiliary_text._single_line_ink_note_evidence(
        note, [(line, line.bbox) for line in body], (left, 680 * scale, left + 500 * scale, 680.2 * scale), source.page_size, em
    )
    assert matched is (kind == "note")


def test_document_title_does_not_include_smaller_author_row():
    """题名两行完整保留，作者行字号收缩且有多作者标记，属于独立文本。"""
    blocks = _pages("two_column_retrieval")[0]
    titles = [_visible_text(b["content"]) for b in blocks if b["type"] == "doc_title"]
    assert len(titles) == 1 and "Few-Shot Document Re-Ranking" in titles[0]
    assert "Baumgärtner" not in titles[0]
    assert any("Baumgärtner" in _visible_text(b["content"]) and b["type"] != "doc_title" for b in blocks)


def test_multiple_bold_author_rows_are_not_document_title():
    """原页两行题名之后四行带贡献标记的作者名单，不能作为题名续行。"""
    blocks = _pages("review_185")[0]
    title = next(block for block in blocks if block["type"] == "doc_title")
    assert "Depth Up-Scaling" in _visible_text(title["content"])
    assert "Kim" not in _visible_text(title["content"])
    assert any("Upstage AI, South Korea" in _visible_text(block["content"]) and block["type"] == "text" for block in blocks)


@pytest.mark.parametrize(
    "number,probes",
    [
        (192, ("Danny Hernandez", "Albert Q Jiang", "Jared Kaplan", "Aran Komatsuzaki", "Stephanie Lin")),
        (193, ("Jack W Rae", "Rafael Rafailov", "Noam Shazeer")),
        (194, ("Chengrun Yang", "Longhui Yu")),
    ],
)
def test_single_page_author_year_bibliography_keeps_whole_entries(number, probes):
    """原页悬挂缩进和逐条空白冻结完整文献条目，不依赖前页 References 标题。"""
    blocks = _pages(f"review_{number}")[0]
    for probe in probes:
        matches = [block for block in blocks if _visible_text(block["content"]).startswith(probe)]
        assert len(matches) == 1
        text = _visible_text(matches[0]["content"])
        tails = {
            "Danny Hernandez": "arXiv:2102.01293.",
            "Albert Q Jiang": "arXiv:2310.06825.",
            "Jared Kaplan": "arXiv:2001.08361.",
            "Aran Komatsuzaki": "arXiv:2212.05055.",
            "Stephanie Lin": "pages 3214",
            "Jack W Rae": "arXiv:2112.11446.",
            "Rafael Rafailov": "arXiv:2305.18290.",
            "Noam Shazeer": "arXiv:1701.06538.",
            "Chengrun Yang": "arXiv:2309.03409.",
            "Longhui Yu": "arXiv:2309.12284.",
        }
        assert tails[probe] in text, (probe, matches)
        assert "202" in _visible_text(matches[0]["content"]) or "201" in _visible_text(matches[0]["content"])


@pytest.mark.parametrize(
    "number,region",
    [(109, (0.092, 0.124, 0.231, 0.142)), (111, (0.092, 0.699, 0.222, 0.722)), (170, (0.141, 0.607, 0.423, 0.623))],
)
def test_compact_vector_math_and_local_number_are_not_omitted(number, region):
    """空字符的原生矢量公式只要求完整裁图，短公式与局部括号编号共同确认。"""
    equations = [block for block in _pages(f"review_{number}")[0] if block["type"] == "equation"]
    assert any(
        all(block["bbox"][i] <= value + 0.006 if i < 2 else block["bbox"][i] >= value - 0.006 for i, value in enumerate(region))
        for block in equations
    ), equations


def test_shaded_header_tables_retain_sparse_numeric_rows_and_blank_cells():
    """原页三个折旧表均有共享列，后半只有单列数据也必须留在完整表体。"""
    tables = [block for block in _pages("review_127")[0] if block["type"] == "table"]
    assert len(tables) == 3
    assert "4.46%" in _visible_text(tables[0]["content"])
    assert all("<table" in _visible_text(block["content"]) for block in tables)


def test_native_slide_table_takes_priority_over_duplicate_raster_background():
    """同一表格的原生单元格优先于截图背景，列头、跨行组及末行保留一次。"""
    blocks = _pages("table_slide")[0]
    tables = [block for block in blocks if block["type"] == "table"]
    assert len(tables) == 1
    assert all(
        probe in _visible_text(tables[0]["content"])
        for probe in ("Service Stage", "Function Name", "Guide and help", "Expected Benefit")
    )
    assert not any(block["type"] == "image" and block["bbox"][1] < 0.75 for block in blocks)


@pytest.mark.parametrize("scale,left", [(1, 45), (1.5, 85), (0.8, 110)])
@pytest.mark.parametrize("kind", ["chart", "equal_bands", "no_values"])
def test_bar_geometry_generalizes_without_turning_equal_bands_into_charts(scale, left, kind):
    """移动图体和改变字号仍按共同基线确认柱图，等长色带与无数值矩形提供反例。"""
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.analyzers.native.pdf.graphics import _detect_native_bar_graphics

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(800 * scale, 850 * scale))
    canvas.setFont("Helvetica", 10 * scale)
    for y in (750, 725, 700):
        canvas.drawString(left, y * scale, "Independent native text provides the document scale")
    for row, value in enumerate((70, 110, 155)):
        length = (130 if kind == "equal_bands" else value) * scale
        y = (500 - row * 40) * scale
        canvas.setFillColorRGB(0.25, 0.45, 0.8)
        canvas.rect(left, y, length, 15 * scale, stroke=0, fill=1)
        if kind != "no_values":
            canvas.setFillColorRGB(0, 0, 0)
            canvas.drawString(left + length + 8 * scale, y, str(value))
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
        result = _detect_native_bar_graphics(source, 10 * scale)
    assert bool(result) == (kind == "chart")


@pytest.mark.parametrize("scale,left", [(1, 45), (1.6, 110), (0.8, 80)])
@pytest.mark.parametrize("numeric", [True, False])
def test_shared_shaded_columns_require_data_rows_not_parallel_prose(scale, left, numeric):
    """不同列宽、字号和横移下恢复数字表格，带底色的并排正文不应形成表格。"""
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.analyzers.native.pdf.table_detection import _detect_shaded_header_tables

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(800 * scale, 850 * scale))
    positions = [left + value * scale for value in (0, 95, 205, 330)]
    canvas.setFont("Helvetica", 10 * scale)
    canvas.setFillColorRGB(0.93, 0.93, 0.93)
    canvas.rect(left, 690 * scale, 450 * scale, 20 * scale, stroke=0, fill=1)
    canvas.setFillColorRGB(0, 0, 0)
    for column, x in enumerate(positions):
        canvas.drawString(x + 3 * scale, 697 * scale, f"Column {column + 1}")
    for row in range(6):
        for column, x in enumerate(positions):
            text = str((row + 1) * (column + 2)) if numeric else "Ordinary words"
            canvas.drawString(x + 3 * scale, (677 - row * 20) * scale, text)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
        result = _detect_shaded_header_tables(source, [])
    assert bool(result) is numeric


@pytest.mark.parametrize(
    "page,starts",
    [
        (1, ("2 Related Work", "2.1 Information")),
        (3, ("3 Datasets",)),
        (4, ("5 Methods", "5.1")),
        (6, ("6 Results", "6.1")),
        (13, ("A Dataset Details", "B 1st Stage")),
        (15, ("D BM25", "E Retrieval")),
        (17, ("H Models", "I 2nd Stage Retrieval")),
    ],
)
def test_bold_section_headings_have_complete_members_and_boundaries(page, starts):
    """数字或附录字母与粗体同行构成真实标题，不能与下一标题或正文粘连。"""
    titles = [_visible_text(b["content"]) for b in _pages("two_column_retrieval")[page] if b["type"] == "paragraph_title"]
    assert all(sum(text.startswith(start) for text in titles) == 1 for start in starts), titles
    assert all(sum(start in text for start in starts) <= 1 for text in titles)


def test_index_retains_bold_when_font_weight_metadata_is_missing():
    """同字符字形笔画提供缺失字重的补证据，目录条目不能丢失粗体 span。"""
    indices = [b for b in _pages("opaque_index_fonts")[0] if b["type"] == "index"]
    assert indices
    assert any(
        isinstance(b["content"], list)
        and any("bold" in span.get("styles", []) and "Introduction" in span.get("content", "") for span in b["content"])
        for b in indices
    )


@pytest.mark.parametrize("number", [44, 108, 113])
def test_contents_title_and_all_index_rows_are_preserved(number):
    """明确目录标题与五个以上条目提供局部证据，表格线和窄页边不改变目录语义。"""
    blocks = _pages(f"review_{number}")[0]
    assert any(
        "contents" in _visible_text(block["content"]).lower() and block["type"] in {"doc_title", "paragraph_title"}
        for block in blocks
    )
    assert any(block["type"] == "index" for block in blocks)
    assert not any(block["type"] == "table" for block in blocks)
    if number == 44:
        assert any(block["type"] == "index" and "Executive Summary" in _visible_text(block["content"]) for block in blocks)
    if number == 108:
        assert any(
            block["type"] == "index"
            and "Links by Chapter" in _visible_text(block["content"])
            and "Image Credits" in _visible_text(block["content"])
            for block in blocks
        )
    if number == 113:
        # 原页末条目录到0.952，独立页码1在0.972；边界来自原页字形而非候选框。
        assert all(block["bbox"][3] < 0.96 for block in blocks if block["type"] == "index")


def test_styled_native_index_bundle_roundtrips_without_invalid_nested_defaults(tmp_path):
    """公开 parse 到结果包再回读应保留目录粗体，嵌套条目不得序列化顶层专用续段字段。"""
    from docvortex import parse, load_bundle

    result = parse(FIXTURES / "opaque_index_fonts.pdf", keep_model_json=True)
    result.save_bundle(tmp_path / "bundle")
    restored = load_bundle(tmp_path / "bundle")
    assert restored.middle_json == result.middle_json


@pytest.mark.parametrize("size,left", [(10, 45), (17, 110)])
@pytest.mark.parametrize("font,is_bold", [("Helvetica-Bold", True), ("Helvetica", False), ("Courier", False)])
def test_missing_weight_uses_same_character_ink_with_plain_font_negatives(size, left, font, is_bold):
    """隐藏字体名字和字重后，真实加粗字形仍有证据，常规字体及不同字族不能误加粗。"""
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.analyzers.native.pdf.inline.glyph_weight import glyph_weight_style_lines
    from docvortex.analyzers.native.pdf.native_text import _build_native_line_items_from_chars

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(600, 800))
    sentence = "Comparable native character shapes provide reliable evidence"
    canvas.setFont("Times-Roman", size)
    for y in (700, 670, 640):
        canvas.drawString(left, y, sentence)
    canvas.setFont(font, size)
    canvas.drawString(left, 570, sentence)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        snapshot = document._extract_native_page(0)
        geometry = snapshot.text_geometry
        for char in geometry.chars:
            original = char["font"]
            char["font"] = dict(
                original, name="opaque_control" if "Times" in original["name"] else "opaque_candidate", flags=0, weight=0
            )
        lines = _build_native_line_items_from_chars(geometry.chars, snapshot.page_size)
        evidence = glyph_weight_style_lines(lines, geometry, lambda: document.render_page(0, scale=3))
    assert any("bold" in interval.styles for line in evidence for interval in line.style_ranges) is is_bold


@pytest.mark.parametrize("native_text,complex_image", [(False, False), (True, True)])
def test_full_page_raster_without_background_evidence_is_retained(native_text, complex_image):
    """纯图片页和带原生文字的复杂内容图必须保留，不能由全页尺寸直接判背景。"""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    image = Image.new("RGB", (128, 128), "white")
    if complex_image:
        for y in range(128):
            for x in range(128):
                image.putpixel((x, y), (20, 80, 130) if (x // 4 + y // 4) % 2 else (240, 180, 40))
    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(600, 800))
    canvas.drawImage(ImageReader(image), 0, 0, width=600, height=800)
    if native_text:
        for y in (700, 650, 600):
            canvas.drawString(50, y, "Ordinary native text over a full page content image")
    canvas.save()
    image.close()
    with PDFDocument(payload.getvalue()) as document:
        blocks = pipeline._analyze_native_document(document)[0]
    assert any(block["type"] == "image" and block["bbox"][2] - block["bbox"][0] > 0.95 for block in blocks)


@pytest.mark.parametrize("scale,left,width", [(1, 40, 240), (1.7, 90, 410), (0.8, 75, 160)])
@pytest.mark.parametrize("kind", ["footnote", "same_size", "no_number", "fraction"])
def test_short_rule_requires_number_shrink_and_clearance(scale, left, width, kind):
    """独立改变字号、栏宽和位置，排除同字号分隔线、无编号文字及分式。"""
    body = [
        _text_line("Ordinary paragraph", (left, y * scale, left + width, (y + 12) * scale), i)
        for i, y in enumerate((520, 535, 550))
    ]
    height = (12 if kind == "same_size" else 9) * scale
    text = "18 A different note" if kind != "no_number" else "Another sentence"
    first = _text_line(text, (left, 589 * scale, left + width, 589 * scale + height), 3)
    second = _text_line("continued note", (left, 600 * scale, left + width, 600 * scale + height), 4)
    marker = _text_line("18", (left, 589 * scale, left + 7 * scale, 589 * scale + height), 5)
    if kind != "no_number":
        first.bbox = (left + 20 * scale, first.bbox[1], first.bbox[2], first.bbox[3])
        second.bbox = (left + 20 * scale, second.bbox[1], second.bbox[2], second.bbox[3])
        body.append(marker)
    if kind == "fraction":
        body[-1].bbox = (left, 574 * scale, left + width, 584 * scale)
    lines = [(line, line.bbox) for line in [*body, first, second]]
    lane = _TextLane(left, left + width, lines)
    result = auxiliary_text._footnote_lane_members(
        lane, (left, 585 * scale, left + 34 * scale, 585.2 * scale), (600 * scale, 800 * scale), page_median_height=12 * scale
    )
    assert bool(result) == (kind == "footnote")


def test_pie_chart_side_caption_keeps_its_complete_explanation():
    """原页右侧图注是单一完整说明，不能因页面正文尺度污染而变成段落标题。"""
    blocks = _pages("review_124")[0]
    caption = next(block for block in blocks if "Figure 2.9" in _visible_text(block["content"]))
    assert caption["type"] == "caption"
    assert _visible_text(caption["content"]).endswith("hard to read.")
    assert caption["bbox"][0] > 0.7
    assert not any(block["type"] == "paragraph_title" and "Figure 2.9" in _visible_text(block["content"]) for block in blocks)


@pytest.mark.parametrize(
    "number,anchors",
    [
        (56, ("FIT =", "Note:")),
        (57, ("FIT =",)),
        (58, ("PKS =", "Note:", "Source:")),
        (59, ("PKS =", "Heat value", "Source:")),
        (60, ("Average price", "Source:")),
        (61, ("Source: Author.",)),
        (95, ("Niederle and Vesterlund",)),
        (99, ("Pope and Schweitzer",)),
        (102, ("Kaza et al.",)),
    ],
)
def test_chart_notes_are_bound_as_complete_visual_annotations(number, anchors):
    """原页图后来源、缩写和数值口径属于图注释，每个文字锚点完整且仅出现一次。"""
    blocks = _pages(f"review_{number}")[0]
    for anchor in anchors:
        matches = [b for b in blocks if anchor in _visible_text(b.get("content", ""))]
        assert matches and all(b["type"] == "footnote" for b in matches), (
            number,
            anchor,
            [(b["type"], _visible_text(b["content"])) for b in matches],
        )
        if number not in (59, 60, 61, 95):
            assert len(matches) == 1
    assert not any(b["type"] == "footnote" and len(_visible_text(b.get("content", ""))) > 650 for b in blocks)


def test_macrofouler_caption_parenthetical_tail_stays_in_one_caption():
    """原页同字号斜体的括注续行是图题的一部分，需与首行合成完整图注。"""
    blocks = _pages("review_63")[0]
    captions = [b for b in blocks if b["type"] == "caption"]
    assert len(captions) == 1
    text = _visible_text(captions[0]["content"])
    assert "Figure 3." in text and "charruana" in text and "Trinidad" in text


@pytest.mark.parametrize("number,markers", [(70, ("Diagram 2", "Diagram 3")), (71, ("Diagram 4",)), (72, ("Diagram 5",))])
def test_diagram_number_and_multiline_title_bind_the_correct_chart(number, markers):
    """原页Diagram编号与旁边说明共同构成图注，不能作为章节标题或页面脚注。"""
    blocks = _pages(f"review_{number}")[0]
    for marker in markers:
        matches = [b for b in blocks if marker in _visible_text(b.get("content", ""))]
        assert len(matches) == 1 and matches[0]["type"] == "caption"
        assert "Distribution" in _visible_text(matches[0]["content"]) or "Participation" in _visible_text(matches[0]["content"])


@pytest.mark.parametrize("scale,left", [(0.7, 20), (1, 40), (1.8, 110)])
@pytest.mark.parametrize("kind", ["source", "no_caption", "barrier", "far_outdent"])
def test_outdented_source_requires_nearby_caption_and_unobstructed_gap(scale, left, kind):
    """移动图体与图题并改变字号；短来源允许有限外悬，但无图题、正文阻隔及远距不能绑定。"""
    from docvortex.analyzers.native.pdf.visual_annotations import _classify_and_bind_visual_annotations

    def block(content, box, typ="text"):
        """构造保留行高的注释块，用通用区域验证规则。"""
        return {
            "type": typ,
            "content": content,
            "bbox": tuple((v + (left if i % 2 == 0 else 0)) * scale for i, v in enumerate(box)),
            "_line_heights": [10 * scale],
            "_font_signatures": {("Synthetic", 0)},
        }

    blocks = [
        block("", (80, 100, 320, 250), "image"),
        block("Figure 7. A distinct chart", (60, 80, 340, 90)),
        block("Source: Independent publisher", (40, 260, 100, 270)),
    ]
    if kind == "no_caption":
        blocks.pop(1)
    if kind == "barrier":
        blocks.append(block("A body paragraph", (30, 251, 330, 259)))
    if kind == "far_outdent":
        blocks[-1]["bbox"] = ((left - 160) * scale, 260 * scale, (left - 100) * scale, 270 * scale)
    source = next(b for b in blocks if b["content"].startswith("Source:"))
    _classify_and_bind_visual_annotations(blocks, (600 * scale, 800 * scale))
    assert (source["type"] == "footnote") == (kind == "source")


def test_labeled_chart_note_and_source_keep_distinct_semantic_boundaries():
    """原页Note与Source有独立标签及物理行，不能只因同字号把两种注释拼成一条。"""
    notes = [_visible_text(b["content"]) for b in _pages("review_56")[0] if b["type"] == "footnote"]
    assert sum(text.startswith("Note:") for text in notes) == 1
    assert sum(text.startswith("Source:") for text in notes) == 1
    assert all(not ("Note:" in text and "Source:" in text) for text in notes)


@pytest.mark.parametrize(
    "number,anchor,kind",
    [(78, "Figure 1.10", "caption"), (100, "Yoeli et al.", "footnote"), (170, "Table 16.5", "caption")],
)
def test_neighboring_visual_annotations_keep_their_native_role(number, anchor, kind):
    """原页明确图表题及图后作者来源保持完整注释，不能因粗体或悬挂位置退化为正文标题。"""
    matches = [
        b
        for b in _pages(f"review_{number}")[0]
        if (text := _visible_text(b.get("content", ""))).startswith("(" if number == 100 else anchor) and anchor in text
    ]
    assert len(matches) == 1 and matches[0]["type"] == kind


@pytest.mark.parametrize("number,count", [(74, 1), (75, 1), (76, 3), (77, 1)])
def test_compound_path_bar_charts_own_native_axes_and_values(number, count):
    """原页多根柱被合成同一PDF路径，图体必须完整认领坐标和柱值，来源与正文留在图外。"""
    blocks = _pages(f"review_{number}")[0]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == count
    assert len([b for b in blocks if b["type"] == "caption"]) == count
    assert len([b for b in blocks if b["type"] == "footnote" and "Source" in _visible_text(b.get("content", ""))]) == count
    assert not any(b["type"] == "equation" for b in blocks)
    if number == 74:
        assert images[0]["bbox"][1] < 0.17 and images[0]["bbox"][3] > 0.43
        assert any(b["type"] == "text" and "It is also noteworthy" in _visible_text(b["content"]) for b in blocks)


@pytest.mark.parametrize("scale,left", [(0.7, 20), (1, 45), (1.8, 110)])
@pytest.mark.parametrize("kind", ["mixed_bars", "equal_bands", "no_values", "curves"])
def test_compound_path_components_require_rectangles_and_external_numeric_evidence(scale, left, kind):
    """改变位置字号与宽度；一个路径中的正负柱可识别，等长底色、无数值和曲线提供反例。"""
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.analyzers.native.pdf.graphics import _detect_native_bar_graphics

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(700 * scale, 800 * scale))
    canvas.setFont("Helvetica", 10 * scale)
    for y in (720, 700, 680):
        canvas.drawString(left, y * scale, "Unrelated words establish the document text scale")
    path = canvas.beginPath()
    for i, value in enumerate((60, -95, 135, -65)):
        height = (90 if kind == "equal_bands" else abs(value)) * scale
        x, baseline = left + i * 55 * scale, 450 * scale
        y = baseline if value > 0 or kind == "equal_bands" else baseline - height
        if kind == "curves":
            path.moveTo(x, y)
            path.curveTo(x + 10 * scale, y + height, x + 20 * scale, y, x + 15 * scale, y + height)
            path.close()
        else:
            path.rect(x, y, 15 * scale, height)
        if kind != "no_values":
            canvas.drawString(x, (y + height + 8 * scale) if value > 0 else y - 12 * scale, str(value))
    canvas.setFillColorRGB(0.1, 0.4, 0.7)
    canvas.drawPath(path, stroke=0, fill=1)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
        outputs = _detect_native_bar_graphics(source, 10 * scale)
    assert bool(outputs) == (kind == "mixed_bars")


@pytest.mark.parametrize("scale,left", [(0.7, 20), (1, 45), (1.8, 110)])
def test_compound_shaded_cell_grid_cannot_supply_a_partial_bar_baseline(scale, left):
    """同一填充路径的多行多列色块属于表格，不能只抽取某列共同边并用邻列数字当柱值。"""
    from reportlab.pdfgen.canvas import Canvas
    from docvortex.analyzers.native.pdf.graphics import _detect_native_bar_graphics

    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(700 * scale, 800 * scale))
    canvas.setFont("Helvetica", 10 * scale)
    path = canvas.beginPath()
    for row in range(4):
        for column, width in enumerate((75, 125, 100)):
            x = left + (0, 75, 200)[column] * scale
            y = (500 - row * 30) * scale
            path.rect(x, y, width * scale, 20 * scale)
            canvas.drawString(x + 8 * scale, y + 5 * scale, str((row + 2) * (column + 3)))
    canvas.setFillColorRGB(0.3, 0.5, 0.8)
    canvas.drawPath(path, stroke=0, fill=1)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
        assert not _detect_native_bar_graphics(source, 10 * scale)


def test_stacked_series_keep_all_native_numeric_labels_inside_the_chart():
    """原页正确的堆叠柱图必须保持完整成员，色段接缝不能抢占共同零轴并丢出文字标签。"""
    blocks = _pages("review_36")[0]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 1
    for block in blocks:
        if block["type"] == "text":
            x, y = (block["bbox"][0] + block["bbox"][2]) / 2, (block["bbox"][1] + block["bbox"][3]) / 2
            assert not any(b["bbox"][0] < x < b["bbox"][2] and b["bbox"][1] < y < b["bbox"][3] for b in images)


def test_compound_header_cells_do_not_turn_known_tables_into_bar_charts():
    """两层表头可共用下沿但没有横向共同零轴；不能将可变列宽当作柱长。"""
    assert len([b for b in _pages("review_81")[0] if b["type"] == "table"]) == 2
    assert not any(b["type"] == "image" and b["bbox"][1] > 0.35 for b in _pages("review_78")[0])


@pytest.mark.parametrize("prefix", ["TableFormer", "Tabular", "Figurehead", "Algorithmic", "SchemeXYZ"])
def test_visual_marker_prefix_inside_a_word_is_not_a_caption_seed(prefix):
    """图表标记只能在词界或紧邻数字处结束，普通复合词不能被罗马数字分支截断成图题。"""
    from docvortex.analyzers.native.pdf.visual_annotations import _is_strong_caption_text

    assert not _is_strong_caption_text(prefix + " supplies ordinary body text.")
    assert _is_strong_caption_text("Table IV. Independent statistics")
    assert _is_strong_caption_text("Figure2. Independent chart")


@pytest.mark.parametrize("scale,left", [(0.7, 20), (1, 45), (1.8, 110)])
@pytest.mark.parametrize("independent", [False, True])
def test_caption_start_respects_full_body_continuation_and_short_source_boundary(scale, left, independent):
    """图号出现在正常满行续段首部时不拆段；独立小字来源后的图题则保留边界。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _caption_to_body_break_sources

    prior = _text_line(
        "Source: Independent statistics" if independent else "A continuous explanation refers to the illustrated result in",
        (left, 100 * scale, left + (130 if independent else 280) * scale, 110 * scale),
        0,
    )
    current = _text_line(
        "Figure 8. Independent chart" if independent else "Figure 8 the category values are transformed for comparison",
        (left, 120 * scale, left + 280 * scale, (132 if independent else 130) * scale),
        1,
    )
    current.caption_start = True
    lane = _TextLane(left, left + 300 * scale, [(prior, prior.bbox), (current, current.bbox)])
    assert (current.source_index in _caption_to_body_break_sources(lane)) is independent


def test_compound_bar_crop_excludes_complete_two_line_caption():
    """原页两行图题均在柱图上方；裁图不得重复包含第二行括号说明。"""
    blocks = _pages('review_77')[0]
    image = next(b for b in blocks if b['type'] == 'image')
    caption = next(b for b in blocks if b['type'] == 'caption')
    assert image['bbox'][1] >= caption['bbox'][3]
    assert 'in thousands' not in _visible_text(image['content'])


@pytest.mark.parametrize('scale,left', [(0.7, 25), (1, 70), (1.8, 160)])
@pytest.mark.parametrize('tail_is_caption', [True, False])
def test_top_graphic_caption_protection_excludes_numeric_axes(scale, left, tail_is_caption):
    """位置字号变化不影响缩进图题续行保护；相同位置的数值轴标签必须留在图体。"""
    from docvortex.analyzers.native.pdf.graphics import _graphic_caption_line_indices_to_preserve
    from docvortex.analyzers.native.pdf.models import _GraphicCandidate
    seed = _text_line('Figure 3. Relative changes', (left, 30*scale, left+220*scale, 40*scale), 0)
    tail = _text_line('(in percent)' if tail_is_caption else '400', (left+35*scale, 41*scale, left+100*scale, 51*scale), 1)
    candidate = _GraphicCandidate(core_bbox=(left, 58*scale, left+250*scale, 190*scale), lane_index=-1)
    protected = _graphic_caption_line_indices_to_preserve([seed, tail], [candidate], 10*scale)
    assert seed.source_index in protected
    assert (tail.source_index in protected) is tail_is_caption


def test_small_styled_caption_tail_is_not_an_independent_heading():
    """原页Figure13.6的同尺度斜体尾行属于完整图题，不是章节标题。"""
    blocks = _pages('review_140')[0]
    captions = [b for b in blocks if b['type']=='caption']
    caption = next(b for b in captions if '13.6' in _visible_text(b['content']))
    assert 'according to the IUCN' in _visible_text(caption['content'])
    assert 'updated November 2018' in _visible_text(caption['content'])
    assert not any(b['type']=='paragraph_title' and 'according to' in _visible_text(b['content']) for b in blocks)


def test_tiny_logo_caption_retains_last_word_and_precedes_following_heading():
    """原页两行图题以Logo结束，随后IMPLEMENTATION有独立粗体字号及留白。"""
    blocks = _pages('review_151')[0]
    caption = next(b for b in blocks if b['type']=='caption')
    assert _visible_text(caption['content']).rstrip().endswith('Logo')
    assert not any(b['type']=='paragraph_title' and _visible_text(b['content']).strip()=='Logo' for b in blocks)
    image = next(b for b in blocks if b['type']=='image')
    heading = next(b for b in blocks if 'IMPLEMENTATION' == _visible_text(b['content']).strip())
    assert blocks.index(image) < blocks.index(heading)


def test_centered_credit_caption_ends_before_justified_body_reset():
    """原页图题及署名居中，以USGS括号结束；下一段首行缩进并恢复两端对齐。"""
    blocks = _pages('review_173')[0]
    caption = next(b for b in blocks if b['type']=='caption')
    text = _visible_text(caption['content'])
    assert 'USGS)' in text and 'Our nearest astronomical' not in text
    body = next(b for b in blocks if 'Our nearest astronomical' in _visible_text(b['content']))
    assert body['type']=='text' and body['bbox'][1] >= caption['bbox'][3]


def test_small_logo_caption_keeps_complete_publication_reference():
    """原页Figure7.4的图题包含完整书名与作者年份，不能分裂成脚注或正文。"""
    blocks = _pages('review_177')[0]
    caption = next(b for b in blocks if b['type']=='caption' and 'Figure 7.4' in _visible_text(b['content']))
    assert '2020)' in _visible_text(caption['content'])
    assert 'materials' not in _visible_text(caption['content']).lower()


@pytest.mark.parametrize('scale,left', [(0.7, 25), (1, 70), (1.8, 160)])
@pytest.mark.parametrize('strong_heading', [False, True])
def test_caption_tail_recovery_requires_small_compatible_regular_typography(scale, left, strong_heading):
    """同字体小尾行可回收；放大或字体变更的真实标题不能仅凭靠近图题被吞入。"""
    from docvortex.analyzers.native.pdf.text_assembly.annotations import _caption_tail_matches_seed
    seed = {'bbox':(left,100*scale,left+200*scale,110*scale),'content':'Figure 5. A simple overview',
            '_line_heights':[10*scale],'_font_signatures':{('Sample',0)}}
    tail = {'type':'paragraph_title','bbox':(left,112*scale,left+150*scale,(126 if strong_heading else 122)*scale),
            'content':'Different words','_line_heights':[(14 if strong_heading else 10)*scale],
            '_font_signatures':{('SampleBold',0) if strong_heading else ('Sample',0)}}
    assert _caption_tail_matches_seed(seed,tail,10*scale) is (not strong_heading)


@pytest.mark.parametrize('scale,left', [(0.7, 25), (1, 70), (1.8, 160)])
@pytest.mark.parametrize('justified_reset', [True,False])
def test_centered_closed_caption_requires_indent_and_repeated_full_body_rows(scale,left,justified_reset):
    """改变栏宽及位置；署名闭合后稳定首行缩进和连续满栏正文才形成段界。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _caption_to_body_break_sources
    width=300*scale
    lines=[_text_line('Figure 8. A general caption',(left+20*scale,20*scale,left+width-20*scale,30*scale),0),
           _text_line('(A short attribution)',(left+100*scale,33*scale,left+200*scale,43*scale),1),
           _text_line('The next line begins a new passage',(left+9*scale,46*scale,left+width,56*scale),2),
           _text_line('with another line of unrelated prose',(left if justified_reset else left+20*scale,59*scale,left+width,69*scale),3),
           _text_line('and a further continuation',(left if justified_reset else left+40*scale,72*scale,left+width,82*scale),4)]
    lines[0].caption_start=True
    lane=_TextLane(left,left+width,[(line,line.bbox) for line in lines])
    assert (lines[2].source_index in _caption_to_body_break_sources(lane)) is justified_reset


@pytest.mark.parametrize('scale,left', [(0.7, 25), (1, 70), (1.8, 160)])
@pytest.mark.parametrize('two_columns', [False,True])
def test_centered_standalone_visual_region_preserves_column_reading_flow(scale,left,two_columns):
    """单栏图注整体先于下方小标题；真实双栏保留左栏正文后再读右栏图的顺序。"""
    width=(140 if two_columns else 300)*scale
    first_lines=[_text_line('An unrelated full paragraph line',(left, (20+i*13)*scale, left+width, (30+i*13)*scale),i) for i in range(3)]
    last_lines=[_text_line('Another ordinary paragraph line',(left,(150+i*13)*scale,left+width,(160+i*13)*scale),i+10) for i in range(3)]
    before={'type':'text','bbox':(left,20*scale,left+width,56*scale),'content':'Earlier prose','_text_lines':first_lines}
    after={'type':'text','bbox':(left,150*scale,left+width,186*scale),'content':'Later prose','_text_lines':last_lines}
    x=left+(180 if two_columns else 100)*scale
    image={'type':'image','bbox':(x,70*scale,x+90*scale,115*scale),'content':''}
    caption={'type':'caption','bbox':(x,117*scale,x+90*scale,127*scale),'content':'Figure 4. Other content'}
    heading={'type':'paragraph_title','bbox':(left,135*scale,left+80*scale,145*scale),'content':'Next section'}
    blocks=[before,heading,image,caption,after]
    if two_columns:
        right_lines=[_text_line('Different right column paragraph',(x,(20+i*13)*scale,x+140*scale,(30+i*13)*scale),i+20) for i in range(3)]
        blocks.append({'type':'text','bbox':(x,20*scale,x+140*scale,56*scale),'content':'Right prose','_text_lines':right_lines})
    result=pipeline._sort_blocks_with_visual_row_groups(blocks,(left+400*scale,250*scale),visual_annotation_regions=[[image,caption]])
    assert (result.index(image)<result.index(heading)) is (not two_columns)
    assert result.index(image)+1 == result.index(caption)


@pytest.mark.parametrize('scale,left', [(0.7, 25), (1, 70), (1.8, 160)])
def test_parallel_visual_regions_keep_left_to_right_order_with_minor_caption_height_offset(scale,left):
    """同排两图的图题高度略有差别，独立图体仍按左到右阅读，不能各自扩成同一整栏。"""
    lines1=[_text_line('An ordinary line of full body text',(left,(20+i*13)*scale,left+300*scale,(30+i*13)*scale),i) for i in range(3)]
    lines2=[_text_line('Another line of full body text',(left,(180+i*13)*scale,left+300*scale,(190+i*13)*scale),i+10) for i in range(3)]
    body1={'type':'text','bbox':(left,20*scale,left+300*scale,56*scale),'content':'Earlier prose','_text_lines':lines1}
    body2={'type':'text','bbox':(left,180*scale,left+300*scale,216*scale),'content':'Later prose','_text_lines':lines2}
    regions=[]
    for i,offset in enumerate([20,170]):
        x=left+offset*scale
        caption={'type':'caption','bbox':(x,(70-i)*scale,x+100*scale,(80-i)*scale),'content':f'Figure {i+1}. Generic plot'}
        image={'type':'image','bbox':(x,84*scale,x+100*scale,165*scale),'content':''}
        regions.append([caption,image])
    result=pipeline._sort_blocks_with_visual_row_groups([body1,body2,*regions[0],*regions[1]],(left+400*scale,260*scale),visual_annotation_regions=regions)
    assert result.index(regions[0][0])<result.index(regions[1][0])


@pytest.mark.parametrize('number,anchor',[(36,'2. General Profile'),(37,'3. Impact on Business')])
def test_oversized_chapter_heading_above_rule_is_not_a_running_header(number,anchor):
    """原页超过正文字号两倍的编号章节标题带横线装饰，不能据横线误标页眉。"""
    blocks=_pages(f'review_{number}')[0]
    heading=next(b for b in blocks if anchor in _visible_text(b['content']))
    assert heading['type']=='paragraph_title'


def test_centered_large_display_heading_above_image_remains_section_title():
    """原页居中标题采用独立较大字号与上下留白；靠近图片不应使真实标题降为正文。"""
    heading=next(b for b in _pages('review_175')[0] if 'Orion Region at Different Wavelengths.' in _visible_text(b['content']))
    assert heading['type']=='paragraph_title'


@pytest.mark.parametrize('number',[155,171])
def test_large_native_contents_heading_is_separate_from_index_entries(number):
    """原页目录上方的大字号Contents是标题，目录链接条目保持独立index。"""
    blocks=_pages(f'review_{number}')[0]
    heading=next(b for b in blocks if _visible_text(b['content']).strip()=='Contents')
    assert heading['type']=='paragraph_title'
    index=next(b for b in blocks if b['type']=='index')
    assert heading['bbox'][3]<index['bbox'][1]


def test_complete_stacked_chart_and_caption_are_image_members():
    """原页堆叠柱图及图例属于同一image，图题属于其caption，不能还原成表格或碎文本。"""
    blocks=_pages('review_37')[0]
    assert sum(b['type']=='image' for b in blocks)==1
    assert sum(b['type']=='caption' for b in blocks)==1
    assert not any(b['type']=='table' for b in blocks)


@pytest.mark.parametrize('scale,left',[(0.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['chapter','header','caption'])
def test_top_rule_respects_relative_size_and_existing_caption_role(scale,left,kind):
    """页首横线按相对字号区分章节和普通页眉；已有图注身份的行不应被改为页眉。"""
    from docvortex.analyzers.native.pdf import models
    h=(30 if kind=='chapter' else 10)*scale
    top=_text_line('4. A different chapter' if kind=='chapter' else 'An unrelated label',(left,35*scale,left+350*scale,35*scale+h),0)
    top.caption_start=kind=='caption'
    body=[_text_line('Another full line of ordinary body text',(left,(110+i*15)*scale,left+450*scale,(120+i*15)*scale),i+1) for i in range(5)]
    rule=models._AxisLine((left,80*scale,left+500*scale,80*scale),1*scale,'horizontal')
    source=models._PageSource((600*scale+left,800*scale),[top,*body],[],[rule])
    prepared=pipeline._prepare_page_source(source)
    auxiliary_text._classify_rule_delimited_headers([prepared])
    assert top.semantic_type==({'chapter':'paragraph_title','header':'header','caption':None}[kind])


@pytest.mark.parametrize('scale,left',[(0.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('display_heading',[True,False])
def test_image_adjacency_keeps_large_independent_heading_but_suppresses_plain_caption(scale,left,display_heading):
    """不同位置栏宽下，独立大字体图前标题可识别；普通同字号图注不凭居中位置升格。"""
    from docvortex.analyzers.native.pdf.title_analysis.page_titles import _classify_page_titles
    body=[_text_line('This ordinary body line contains several different words',(left,(90+i*14)*scale,left+300*scale,(100+i*14)*scale),i) for i in range(5)]
    heading=_text_line('Different heading above a visual',(left+40*scale,180*scale,left+260*scale,(196 if display_heading else 190)*scale),20)
    for line in body:
        line.font_signature=('BodySample',0);line.font_coverage=1;line.dominant_font_weight=400
    heading.font_signature=('DisplaySample' if display_heading else 'BodySample',0);heading.font_coverage=1;heading.dominant_font_weight=400
    tail=[_text_line('Figure 9. This ordinary caption contains unrelated words',(left,290*scale,left+300*scale,300*scale),21)]
    tail[0].font_signature=('BodySample',0);tail[0].font_coverage=1;tail[0].caption_start=True
    _classify_page_titles([*body,heading,*tail],(left+400*scale,500*scale),page_index=1,container_bboxes=[(left,205*scale,left+300*scale,280*scale)],caption_container_bboxes=[(left,205*scale,left+300*scale,280*scale)])
    assert (heading.semantic_type=='paragraph_title') is display_heading


@pytest.mark.parametrize('scale,left',[(0.7,25),(1,70),(1.8,160)])
def test_overlapping_header_row_envelope_does_not_make_small_date_a_title(scale,left):
    """小号日期的来源包络虽跨两行，校准字形尺度仍小于正文，不能误为编号章节标题。"""
    from docvortex.analyzers.native.pdf import models
    top=_text_line('2031 6 Months',(left,35*scale,left+200*scale,65*scale),0)
    top.em_height=7*scale
    body=[_text_line('An unrelated ordinary line of full body text',(left,(110+i*15)*scale,left+450*scale,(120+i*15)*scale),i+1) for i in range(5)]
    for line in body:line.em_height=10*scale
    source=models._PageSource((left+600*scale,800*scale),[top,*body],[],[models._AxisLine((left,80*scale,left+500*scale,80*scale),scale,'horizontal')])
    auxiliary_text._classify_rule_delimited_headers([pipeline._prepare_page_source(source)])
    assert top.semantic_type=='header'


def test_large_unnumbered_document_heading_above_rule_is_not_header():
    """原页上方大字号VersionHistory为主标题；下方同名章节和表题不作为本条验收范围。"""
    heading=next(b for b in _pages('review_180')[0] if _visible_text(b['content']).strip()=='Version History' and b['bbox'][1]<.15)
    assert heading['type'] in {'doc_title','paragraph_title'}


def test_caption_graphic_excludes_unrelated_underline_body_and_display_heading():
    """原页短下划线属于上方正文，两行人物标题独立，图体从两幅照片顶边开始。"""
    blocks = _pages('review_174')[0]
    image = next(b for b in blocks if b['type'] == 'image')
    assert image['bbox'][1] > .52
    heading = next(b for b in blocks if 'Tycho Brahe (1546' in _visible_text(b['content']))
    assert heading['type'] == 'paragraph_title'
    assert '(1571' in _visible_text(heading['content'])
    body = next(b for b in blocks if 'observers in Europe.' in _visible_text(b['content']))
    assert body['type'] == 'text' and 'pre-telescopic' in _visible_text(body['content'])


def test_caption_graphic_does_not_consume_short_second_heading_line():
    """原页同字体居中标题的SST续行属于标题，图体应从三幅照片顶边开始。"""
    blocks = _pages('review_176')[0]
    image = next(b for b in blocks if b['type'] == 'image')
    assert image['bbox'][1] > .69
    heading = next(b for b in blocks if 'Observations from the Spitzer' in _visible_text(b['content']))
    assert heading['type'] == 'paragraph_title' and '(SST).' in _visible_text(heading['content'])


def test_outdented_numbered_caption_binds_to_complete_stacked_chart():
    """原页图题沿正文栏左边缘外悬于居中图体，仍属于完整堆叠柱图。"""
    blocks = _pages('review_36')[0]
    caption = next(b for b in blocks if 'Figure 2.1:' in _visible_text(b['content']))
    assert caption['type'] == 'caption'


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('display',[True,False])
def test_raster_graphic_floor_protects_display_heading_but_not_nearby_axis_values(scale,left,display):
    """改变字号栏宽位置，大字号居中标题及短续行形成屏障，同字号刻度仍可归入图体。"""
    from docvortex.analyzers.native.pdf.graphics import _caption_graphic_display_heading_floor
    from docvortex.analyzers.native.pdf.models import _PageSource
    h=(14 if display else 10)*scale
    image=(left,110*scale,left+240*scale,250*scale)
    caption=(left,260*scale,left+240*scale,270*scale)
    heading=_text_line('Different display heading' if display else '12 24 36',(left+10*scale,60*scale,left+230*scale,60*scale+h),0)
    tail=_text_line('(Another detail)',(left+80*scale,80*scale,left+160*scale,80*scale+h),1)
    heading.font_signature=tail.font_signature=('Unrelated',0)
    source=_PageSource((left+300*scale,400*scale),[heading,tail],[],[],image_bboxes=[image])
    actual=_caption_graphic_display_heading_floor(source,caption,10*scale)
    assert actual == pytest.approx((80*scale+h) if display else 0)


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('blocked',[False,True])
def test_outdented_caption_requires_unobstructed_unique_parent(scale,left,blocked):
    """外悬编号图题可绑定近邻图，插入正文屏障后不得越过正文认领同一图体。"""
    from docvortex.analyzers.native.pdf.visual_annotations import _VisualParent, _choose_annotation_relation
    cap={'type':'text','bbox':(left,50*scale,left+220*scale,60*scale),'content':'Figure 8. An unrelated chart','_line_heights':[10*scale]}
    image={'type':'image','bbox':(left+100*scale,80*scale,left+360*scale,200*scale),'content':''}
    blocks=[cap,image]
    if blocked:blocks.append({'type':'text','bbox':(left,65*scale,left+360*scale,75*scale),'content':'A separate ordinary body passage'})
    parent=_VisualParent(member_indices=(1,),angle=0,local_bbox=image['bbox'])
    relation=_choose_annotation_relation(0,'caption',blocks,(left+450*scale,300*scale),[parent],{0})
    assert (relation is not None) is (not blocked)


def test_parallel_caption_does_not_split_constant_width_unfinished_body():
    """原页右栏CVCC段落横向宽度不变，左侧图注结束导致的栏带变化不是自然段断点。"""
    blocks = _pages('review_177')[0]
    paragraph = next(b for b in blocks if b['type'] == 'text' and 'CVCC’s logo is more complex' in _visible_text(b['content']))
    assert 'print materials' in _visible_text(paragraph['content'])
    assert 'a larger graphic works fine.' in _visible_text(paragraph['content'])


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('barrier',['none','indent','terminal','hard','fonts'])
def test_changed_declared_lane_requires_actual_full_row_continuity(scale,left,barrier):
    """栏带改变但行左右边缘恒定时可接续；真实缩进、句末、硬段界和字体变化继续分段。"""
    from docvortex.analyzers.native.pdf.text_assembly.merging import _merge_unterminated_text_components
    rows1=[(left,(20+i*13)*scale,left+200*scale,(30+i*13)*scale) for i in range(2)]
    rows2=[(left+(8*scale if barrier=='indent' and i==0 else 0),(46+i*13)*scale,left+200*scale,(56+i*13)*scale) for i in range(2)]
    first={'type':'text','bbox':(left,20*scale,left+200*scale,43*scale),'content':'Other text continues' + ('.' if barrier=='terminal' else ''),
           '_local_line_bboxes':rows1,'_line_heights':[10*scale]*2,'_lane_interval':(left,left+200*scale),'_lane_is_span':False,'_font_signatures':{('Body',0)}}
    second={'type':'text','bbox':(left,46*scale,left+200*scale,69*scale),'content':'into the next unrelated line',
            '_local_line_bboxes':rows2,'_line_heights':[10*scale]*2,'_lane_interval':(left-100*scale,left+200*scale),'_lane_is_span':False,
            '_font_signatures':{('Changed' if barrier=='fonts' else 'Body',0)},'_protected_hard_break_before':barrier=='hard'}
    result=_merge_unterminated_text_components([first,second])
    assert len(result)==(1 if barrier=='none' else 2)


def test_numerical_chapter_margin_text_and_page_number_are_separate_from_formula():
    """原页横线上方为普通章节页眉及35，单页可作独立文字但绝不能整体成为公式。"""
    blocks=_pages('review_144')[0]
    margin=[b for b in blocks if b['bbox'][3]<.11]
    assert len(margin)==2
    assert not any(b['type']=='equation' for b in margin)
    assert any(_visible_text(b['content']).strip()=='35' for b in margin)


@pytest.mark.parametrize('number',[33,34])
def test_prologue_margin_and_roman_page_number_do_not_become_equation(number):
    """原页Prologue及罗马页码为横线上的普通文字，独立公式只能来自下方数学行。"""
    margin=[b for b in _pages(f'display_math_{number}')[0] if b['bbox'][3]<.08]
    assert len(margin)==2 and not any(b['type']=='equation' for b in margin)
    assert any(_visible_text(b['content']).strip()=='Prologue' for b in margin)


def test_independent_performance_charts_include_complete_zero_axis_and_labels():
    """原页右图横轴从0.694至0.925，图体裁图必须含两端及完整模型标签，三图保持独立。"""
    blocks=_pages('native_bars_183')[0]
    images=[b for b in blocks if b['type']=='image']
    assert len(images)==3
    right=max(images,key=lambda b:b['bbox'][0])
    assert right['bbox'][0] <= .6945 and right['bbox'][2] >= .925
    assert not any(b['type']=='paragraph_title' and _visible_text(b['content']).strip() in {'0.882','0.735','20%'} for b in blocks)


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['zero','floating','oversized','opposite'])
def test_verified_bar_zero_axis_requires_matching_baseline_orientation_and_span(scale,left,kind):
    """改变字号位置，零轴两端须覆盖柱组并同向同基线；浮动长装饰线与跨栏线不扩图框。"""
    from docvortex.analyzers.native.pdf.graphics import _bar_zero_axis_bboxes
    from docvortex.analyzers.native.pdf.models import _AxisLine,_PageSource
    bounds=(left+100*scale,80*scale,left+300*scale,200*scale)
    box=(left+(0 if kind=='oversized' else 70)*scale,(150 if kind=='floating' else 199.5)*scale,left+(600 if kind=='oversized' else 330)*scale,(151 if kind=='floating' else 200.5)*scale)
    line=_AxisLine(box,scale,'vertical' if kind=='opposite' else 'horizontal')
    source=_PageSource((left+700*scale,400*scale),[],[],[line])
    result=_bar_zero_axis_bboxes(source,bounds,False,200*scale,10*scale)
    assert bool(result) is (kind=='zero')


def test_short_version_history_grid_keeps_multiline_dates_and_four_columns():
    """原页四列三行历史表有内列线和两条行线，日期跨行及末列链接必须留在对应单元格。"""
    from bs4 import BeautifulSoup
    blocks=_pages('review_180')[0]
    tables=[b for b in blocks if b['type']=='table']
    assert len(tables)==1
    soup=BeautifulSoup(tables[0]['content'], 'html.parser')
    rows=soup.find_all('tr')
    assert len(rows)==3 and all(len(row.find_all(['td','th'],recursive=False))==4 for row in rows)
    assert 'April 30, 2022' in rows[1].get_text(' ',strip=True)
    assert 'June 3, 2022' in rows[2].get_text(' ',strip=True)
    assert 'Introduction to Open Educational Resources' in rows[2].get_text(' ',strip=True)
    assert tables[0]['bbox'][3]>.59


def test_unnumbered_centered_bold_table_caption_is_not_a_cell_or_heading():
    """原页贴近表顶的居中粗体VersionHistory为独立表题，更上方的大标题仍为章节标题。"""
    blocks=_pages('review_180')[0]
    caption=next(b for b in blocks if _visible_text(b['content']).strip()=='Version History' and .46<b['bbox'][1]<.49)
    assert caption['type']=='caption'
    heading=next(b for b in blocks if _visible_text(b['content']).strip()=='Version History' and .41<b['bbox'][1]<.45)
    assert heading['type']=='paragraph_title'


@pytest.mark.parametrize('previous,following,expected',[
    ('Different month 19,','2035',' '),('Another description,','15',' '),('12,','345',''),
    ('C,','2',''),('https://example.org/','123',''),('17','32',''),
])
def test_multiline_cell_punctuation_separates_prose_and_numeric_tail(previous,following,expected):
    """文字日期和短语逗号后的数字有空格，数字分组、短变量、URL及紧凑数字不增加词界。"""
    from docvortex.analyzers.native.pdf._table_recovery.text import _cell_row_separator
    assert _cell_row_separator(previous,previous,following)==expected


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['grid','few_tracks','one_rule','crossing_prose'])
def test_short_open_table_needs_repeated_columns_physical_rules_and_cell_boundaries(scale,left,kind):
    """短表凭内列线、完整表头和两条行线成立；弱线、少列线及跨列长正文不制造表格。"""
    from docvortex.analyzers.native.pdf.models import _AxisLine,_PageSource
    from docvortex.analyzers.native.pdf.table_detection import _detect_open_vertical_tables
    tracks=[100,180] if kind=='few_tracks' else [100,180,340]
    rules=[_AxisLine((left+(x-.3)*scale,100*scale,left+(x+.3)*scale,180*scale),.6*scale,'vertical') for x in tracks]
    rules.extend(_AxisLine((left,y*scale,left+480*scale,(y+.6)*scale),.6*scale,'horizontal') for y in ([130] if kind=='one_rule' else [130,155]))
    lines=[]
    for row,y in enumerate([110,138,162]):
        for col,x in enumerate([5,105,185,345]):
            lines.append(_text_line(f'Other value {row},{col}',(left+x*scale,y*scale,left+(x+45)*scale,(y+10)*scale),len(lines)))
    if kind=='crossing_prose':lines[-1]=_text_line('An ordinary complete passage spanning all the columns',(left,162*scale,left+480*scale,172*scale),lines[-1].source_index)
    source=_PageSource((left+600*scale,300*scale),lines,[],rules)
    candidates=_detect_open_vertical_tables(source,[])
    assert bool(candidates) is (kind=='grid')


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['caption','regular','off_center','far','number'])
def test_unnumbered_table_caption_needs_bold_centered_short_nearby_text(scale,left,kind):
    """无编号表题需贴近已确认表顶且短、居中、强调；普通正文、偏列标题、远距和数字不绑定。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.table_detection import _bounded_table_caption_annotations
    x=(20 if kind=='off_center' else 190)*scale+left;y=(55 if kind=='far' else 85)*scale
    line=_text_line('2931' if kind=='number' else 'Another table description',(x,y,x+100*scale,y+10*scale),0)
    line.dominant_font_weight=400 if kind=='regular' else 700
    source=_PageSource((left+600*scale,300*scale),[line],[],[])
    annotations=_bounded_table_caption_annotations(source,(left,100*scale,left+480*scale,180*scale),10*scale)
    assert bool(annotations) is (kind=='caption')


def test_display_roman_chapter_marker_and_wrapped_label_form_one_heading():
    """原页大号罗马编号III与紧随的两行章节名组成完整标题，不应拆成text和doc_title。"""
    blocks = _pages('review_80')[0]
    headings = [b for b in blocks if 'Regulatory cholesterol' in _visible_text(b['content'])]
    assert len(headings) == 1 and headings[0]['type'] == 'paragraph_title'
    assert _visible_text(headings[0]['content']).startswith('III.')
    assert headings[0]['bbox'][1] < .12 and headings[0]['bbox'][3] > .25


def test_numbered_poster_steps_with_spaced_or_compact_dash_keep_equal_heading_status():
    """原页01至06的粗体步骤标题视觉同级，02和03紧接短横线的编号同样属于完整标题。"""
    import re
    blocks = _pages('review_103')[0]
    steps = [b for b in blocks if re.match(r'^0[1-6]\s*-', _visible_text(b['content']))]
    assert len(steps) == 6 and all(b['type'] == 'paragraph_title' for b in steps)
    assert all(b['bbox'][3]-b['bbox'][1] < .02 for b in steps)


def test_drop_cap_word_keeps_body_style_and_real_citation_superscript():
    """原页下沉大写T与his组成正文首词，不能将后半词标为上标；真正的引用28仍保留上标。"""
    blocks = _pages('review_80')[0]
    matches = [b for b in blocks if 'This report defines' in _visible_text(b['content'])]
    assert len(matches) == 1 and matches[0]['type'] == 'text'
    spans = matches[0]['content']
    assert not any(s.get('content') == 'his' and 'superscript' in s.get('styles', []) for s in spans)
    assert any(s.get('content') == '28' and 'superscript' in s.get('styles', []) for s in spans)


def test_vector_formula_numbers_eight_to_twelve_remain_in_complete_crop():
    """原页右侧括号8至12均属于对应公式，裁图必须覆盖视觉冻结的每个编号位置。"""
    equations = [b for b in _pages('review_109')[0] if b['type'] == 'equation']
    assert len(equations) == 6
    for top, bottom, right in [(.209,.232,.244),(.277,.304,.261),(.348,.375,.355),(.420,.445,.274),(.617,.636,.303)]:
        matches = [b for b in equations if b['bbox'][1] <= top+.002 and b['bbox'][3] >= bottom-.002]
        assert len(matches) == 1 and matches[0]['bbox'][2] >= right-.002


@pytest.mark.parametrize('number,markers',[(129,['15.19','15.20','15.21','15.22']),(130,['15.42'])])
def test_left_margin_number_is_owned_by_its_unique_vector_equation(number,markers):
    """原页左侧括号编号进入唯一公式裁图，不再独立输出正文或标题；公式主体保持完整。"""
    blocks=_pages(f'review_{number}')[0]
    # 编号只属于左侧独立陈列式；新恢复的行内裁图另有严格数量及区域验收，不能要求它们带编号。
    equations=[b for b in blocks if b['type']=='equation' and b['bbox'][0] <= .09]
    assert len(equations)==len(markers)
    assert all(b['bbox'][0] <= .09 for b in equations)
    assert not any(f'({marker})'==_visible_text(b.get('content','')) for marker in markers for b in blocks if b['type']!='equation')


def test_table_numeric_rows_end_before_two_independent_explanatory_paragraphs():
    """原页最后数据行2016结束表体，Another way和We may两段正文不能被外层横线吞进表格。"""
    blocks=_pages('review_130')[0]
    tables=[b for b in blocks if b['type']=='table']
    assert len(tables)==1 and tables[0]['bbox'][3] < .33
    text=_visible_text(tables[0]['content'])
    assert all(str(year) in text for year in range(2012,2017))
    assert 'Another way' not in text and 'We may' not in text
    for anchor,tail in [('Another way','scatter graph.'),('We may','Figure 15.3.')]:
        matches=[b for b in blocks if anchor in _visible_text(b.get('content',''))]
        assert len(matches)==1 and matches[0]['type']=='text'
        assert _visible_text(matches[0]['content']).endswith(tail)


@pytest.mark.parametrize('number,first,second',[(86,'This report, prepared','We identified 10 countries'),(87,'members should specify','Some jurisdictions do not list')])
def test_blank_paragraph_gap_survives_trailing_superscript_citation(number,first,second):
    """原页正文句末引用上标不应掩盖段尾；明显空行前后的两个自然段保持独立且完整。"""
    blocks=_pages(f'review_{number}')[0]
    a=[b for b in blocks if _visible_text(b.get('content','')).startswith(first)]
    b=[b for b in blocks if _visible_text(b.get('content','')).startswith(second)]
    assert len(a)==len(b)==1 and a[0]['type']==b[0]['type']=='text'
    assert second not in _visible_text(a[0]['content'])
    assert a[0]['bbox'][3] < b[0]['bbox'][1]


def test_bold_country_names_continue_the_unfinished_body_enumeration():
    """原页We found的三行同段，最后的粗体国家名为逗号枚举续行，不能拆成独立段落。"""
    blocks=_pages('review_86')[0]
    matches=[b for b in blocks if _visible_text(b.get('content','')).startswith('We found that')]
    assert len(matches)==1 and matches[0]['type']=='text'
    assert _visible_text(matches[0]['content']).endswith('Nigeria, Philippines, and Thailand.')
    assert len(matches[0]['lines'])==3


def test_regular_multiline_body_after_italic_section_is_not_promoted_to_heading():
    """原页This game三行常规正文不能因上方以斜体为主而成为标题，真正的大写章节题仍独立。"""
    blocks=_pages('review_97')[0]
    body=[b for b in blocks if _visible_text(b.get('content','')).startswith('This game shares')]
    assert len(body)==1 and body[0]['type']=='text'
    assert _visible_text(body[0]['content']).endswith('portrays the game as follows:')
    title=[b for b in blocks if _visible_text(b.get('content',''))=='BURNING BRIDGES GAME']
    assert len(title)==1 and title[0]['type']=='paragraph_title'
    question=[b for b in blocks if _visible_text(b.get('content','')).startswith('What’s the outcome')]
    assert len(question)==1 and question[0]['type']=='text'
    assert _visible_text(question[0]['content']).endswith('Escalation Game?')


@pytest.mark.parametrize('number,count,anchors',[
    (56,6,['General wood:','Liquid biomass:','Unutilised wood:','Construction wood waste:','Waste materials and other biomass:','Biogas:']),
    (66,5,['full-service restaurants,','limited-service restaurants','cafes/bars/pop-ups','kiosks and stalls','catering or']),
    (69,3,['choosing a common type','choosing a common color','avoiding combinations']),
    (104,4,['acquisitions and list curation','editorial work','design and production','distribution and marketing']),
    (123,6,['Collected via surveys','Inputted into a database','Stored on secure servers','Cleaned for accuracy','Analyzed to understand','Presented as a bar graph']),
    (181,4,['Plug-and-play','Ensuring performance','Providing a platform','AI consulting service']),
])
def test_repeated_bullets_keep_complete_separate_items_without_absorbing_intro(number,count,anchors):
    """原页圆点项目按标记与缩进完整分项，短项也独立；游离圆点、简介或下一项目不能混入。"""
    blocks=_pages(f'review_{number}')[0]
    items=[b for b in blocks if _visible_text(b.get('content','')).lstrip().startswith('•')]
    assert len(items)==count
    for anchor in anchors:
        matches=[b for b in items if anchor in _visible_text(b['content'])]
        assert len(matches)==1 and _visible_text(matches[0]['content']).count('•')==1
        assert len(_visible_text(matches[0]['content']).strip())>2
    if number==56:
        assert _visible_text(items[0]['content']).endswith('shell (PKS) and palm trunk')
    if number==69:
        assert _visible_text(items[-1]['content']).endswith('sorters.')
    if number==181:
        assert _visible_text(items[2]['content']).endswith('AI solutions')


def test_historical_chinese_engineering_lists_keep_each_native_bullet_item():
    """已维护原件的三处中文列表按圆点分项，正文、章节、代码和图注仍由历史逐页指纹单独冻结。"""
    from test_flash_pdf_char_geometry import _read_pdf_fixture
    source=Path(__file__).parent/'pdfs/flash_layout/mixed_text_layout_sample.pdf.xor'
    with PDFDocument(_read_pdf_fixture(source)) as pdf:
        pages=pipeline._analyze_native_document(pdf)
    for page,count in [(5,3),(6,6),(9,5)]:
        items=[b for b in pages[page-1] if _visible_text(b.get('content','')).startswith('•')]
        assert len(items)==count and all(_visible_text(b['content']).count('•')==1 for b in items)


@pytest.mark.parametrize('scale,left,width',[(.7,12,260),(1,90,400),(1.6,220,680)])
@pytest.mark.parametrize('split',[False,True])
def test_short_bullet_items_and_wrapped_tail_survive_scale_width_and_marker_font(scale,left,width,split):
    """改变栏宽、位置、字号和符号字体，三个项目及首项续行完整，简介和后文保持独立。"""
    from docvortex.analyzers.native.pdf.text_assembly.assembly import _build_text_blocks
    lines=[]
    for text,y,x,fill,font in [('A separate introductory explanatory paragraph.',70,0,.9,'GenericBody'),('Ordinary first item contains some extended descriptive evidence',100,12,.9,'GenericBody'),('its complete wrapped ending.',113,12,.4,'GenericBody'),('Another short item',126,12,.45,'GenericBody'),('The third ordinary item ends here.',139,12,.8,'GenericBody'),('A separate unrelated following paragraph.',170,0,.9,'GenericBody')]:
        index=len(lines)
        if y in {100,126,139}:
            if split:
                marker=_text_line('•',(left,y*scale,left+3*scale,(y+10)*scale),index,font_signature=('UnrelatedMarkerFamily',0),font_coverage=1);lines.append(marker);index+=1
            else:text='• '+text;x=0
        lines.append(_text_line(text,(left+x*scale,y*scale,left+fill*width,(y+10)*scale),index,font_signature=(font,0),font_coverage=1))
    blocks=_build_text_blocks(lines,[],(left+width+30,250*scale))
    items=[b for b in blocks if b['content'].startswith('•')]
    assert len(items)==3 and all(b['content'].count('•')==1 for b in items)
    assert items[0]['content'].endswith('its complete wrapped ending.')
    assert all('introductory' not in b['content'] and 'unrelated' not in b['content'] for b in items)


@pytest.mark.parametrize('scale,left',[(.7,12),(1,90),(1.6,220)])
@pytest.mark.parametrize('kind',['paired','single','different_scale','unaligned','same_row','far','ambiguous','no_words','formula','caption'])
def test_detached_bullet_boundaries_need_repeated_aligned_unambiguous_native_text(scale,left,kind):
    """孤立符号、数学点、相邻双宿主、错栏及样式跨度不能凭圆点制造条目边界。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _repeated_bullet_break_sources
    rows=[]
    for n in range(1 if kind=='single' else 2):
        y=(100+n*(0 if kind=='same_row' else 120 if kind=='far' else 26))*scale
        x=left+(30*scale if kind=='unaligned' and n else 0)
        h=(16 if kind=='different_scale' and n else 10)*scale
        marker=_text_line('•',(x,y,x+3*scale,y+h),len(rows),font_signature=('GenericMarker',0),font_coverage=1);rows.append((marker,marker.bbox))
        body=_text_line('x' if kind=='no_words' else 'Another ordinary descriptive item',(x+12*scale,y,x+160*scale,y+h),len(rows),font_signature=('GenericBody',0),font_coverage=1)
        if kind=='formula':body.formula_candidate_only=True
        if kind=='caption':body.caption_start=True
        rows.append((body,body.bbox))
        if kind=='ambiguous':
            extra=_text_line('Another possible simultaneous text host',(x+15*scale,y,x+160*scale,y+h),len(rows));rows.append((extra,extra.bbox))
    assert bool(_repeated_bullet_break_sources(rows)) is (kind=='paired')


@pytest.mark.parametrize('scale,left,width',[(.7,12,260),(1,90,400),(1.6,220,680)])
@pytest.mark.parametrize('kind',['body','bold','large','short','different_family','large_gap','explicit','unfinished','no_sentence'])
def test_full_width_sentence_body_run_requires_complete_regular_prose(scale,left,width,kind):
    """同栏三行常规句构成正文反证；粗体、大号、短行、跳字体、空行与已确认标题均不触发保护。"""
    from docvortex.analyzers.native.pdf.models import _LaneBodyProfile
    from docvortex.analyzers.native.pdf.title_analysis.lane_titles import _is_full_width_sentence_body_run
    words=[
        'Several ordinary people, working together, discuss this detailed example',
        'The following observations (including all related evidence) describe our analysis',
        'Another independent explanatory sentence completes the extended description:',
    ]
    if kind=='unfinished':words[-1]='Another independent explanatory sentence continues without any punctuation'
    if kind=='no_sentence':words[0]=words[0].replace(',','');words[1]=words[1].replace('(','').replace(')','')
    rows=[]
    for index,text in enumerate(words):
        y=(100+index*13+(10 if kind=='large_gap' and index>0 else 0))*scale
        height=(14 if kind=='large' else 10)*scale
        line=_text_line(text,(left,y,left+width*(.6 if kind=='short' and index==2 else .95),y+height),index,font_signature=('OtherFamily' if kind=='different_family' and index==1 else 'GenericText',0),font_coverage=1)
        line.dominant_font_weight=700 if kind=='bold' else 400
        if kind=='explicit' and index==0:line.explicit_section_title=True
        rows.append((line,line.bbox))
    profile=_LaneBodyProfile(body_height=10*scale,body_font=('GenericTextItalic',0),body_weight=400,regular_gap=3*scale,style_support={})
    assert _is_full_width_sentence_body_run(rows,0,width,profile) is (kind=='body')


@pytest.mark.parametrize('scale,left,width',[(.7,12,260),(1,90,400),(1.6,220,680)])
@pytest.mark.parametrize('kind',['reset','unfinished','no_outdent','still_italic','caption','large_gap','only_one_italic'])
def test_complete_indented_italic_quote_does_not_absorb_regular_following_body(scale,left,width,kind):
    """完成的连续缩进斜体与常规正文重置拆段；句内强调、同缩进、图注或孤立斜体不产生此边界。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _italic_quote_to_body_break_sources
    from docvortex.analyzers.native.pdf.inline.types import PDF_FONT_ITALIC_FLAG
    rows=[]
    for index,text in enumerate(['The earlier quoted line continues with detailed explanatory evidence','The final sentence of the quoted passage ends here.','Another explanatory question starts after the completed quoted description','A short continuation of the regular body.']):
        if index==1 and kind=='unfinished':text=text.rstrip('.')
        x=left+(20*scale if index<2 or kind=='no_outdent' else 0)
        y=(100+13*index+(10 if kind=='large_gap' and index>=2 else 0))*scale
        flags=PDF_FONT_ITALIC_FLAG if index<2 or kind=='still_italic' else 0
        if kind=='only_one_italic' and index==0:flags=0
        line=_text_line(text,(x,y,left+width*.95,y+10*scale),index,font_signature=('GenericText',flags),font_coverage=1)
        if kind=='caption' and index==0:line.caption_start=True
        rows.append((line,line.bbox))
    lane=_TextLane(left=left,right=left+width,lines=rows,is_span=False)
    assert (2 in _italic_quote_to_body_break_sources(lane)) is (kind=='reset')


@pytest.mark.parametrize('scale,left,width',[(.7,12,350),(1,90,440),(1.6,220,600)])
@pytest.mark.parametrize('kind',['blank','compact','unfinished','caption','formula','different_style'])
def test_blank_gap_paragraph_boundary_uses_native_terminal_evidence(scale,left,width,kind):
    """改变引用编号、字号及栏宽；原生句末证据加空行拆段，普通续行、图注、公式与字体变化不制造同类正文段界。"""
    from docvortex.analyzers.native.pdf.text_assembly.rows import _prose_paragraph_break_sources
    y=113 if kind=='compact' else 125
    previous=_text_line('Some earlier completed explanatory sentence.73' if kind!='unfinished' else 'An unfinished ordinary sentence continues',(left,100*scale,left+.95*width,110*scale),0,font_signature=('GenericText',0),font_coverage=1)
    previous.paragraph_terminal=kind!='unfinished'
    current=_text_line('Another unrelated complete paragraph starts here',(left,y*scale,left+width,(y+10)*scale),1,font_signature=('OtherFamily' if kind=='different_style' else 'GenericText',0),font_coverage=1)
    following=_text_line('The next ordinary line of the same paragraph',(left,(y+13)*scale,left+width,(y+23)*scale),2,font_signature=('GenericText',0),font_coverage=1)
    if kind=='caption':previous.caption_start=True
    if kind=='formula':current.formula_candidate_only=True
    lane=_TextLane(left=left,right=left+width,lines=[(l,l.bbox) for l in [previous,current,following]],is_span=False)
    assert (1 in _prose_paragraph_break_sources(lane,3*scale,0)) is (kind=='blank')


@pytest.mark.parametrize('scale,left,width',[(.7,12,350),(1,90,440),(1.6,220,600)])
@pytest.mark.parametrize('kind',['enumeration','heading','unfinished_tail','large_gap','large_type','different_family','table'])
def test_emphasized_short_tail_is_body_only_after_unfinished_close_comma_enumeration(scale,left,width,kind):
    """正文中的粗体短尾跨通用字重后缀续接；标题、未终止短行、大净空、字号变化、异字体及表格屏障拒绝。"""
    from docvortex.analyzers.native.pdf.line_layout import _should_connect_text_rows
    gap=15 if kind=='large_gap' else 3
    height=17 if kind=='large_type' else 10
    previous=_text_line('Some examples include various differently emphasized names,',(left,100*scale,left+width,110*scale),0,font_signature=('GenericSerif-Regular',0),font_coverage=1,dominant_font_weight=400)
    tail='Azure, Magenta, and Silver' + ('' if kind=='unfinished_tail' else '.')
    current=_text_line(tail,(left,(110+gap)*scale,left+.45*width,(110+gap+height)*scale),1,font_signature=('OtherFamily-Bold' if kind=='different_family' else 'GenericSerif-Bold',0),font_coverage=1,dominant_font_weight=700,semantic_type='paragraph_title' if kind=='heading' else None)
    lane=_TextLane(left=left,right=left+width,lines=[(l,l.bbox) for l in [previous,current]],is_span=False)
    tables=[(left,111*scale,left+width,112*scale)] if kind=='table' else []
    actual=_should_connect_text_rows((previous,previous.bbox),(current,current.bbox),lane,3*scale,0,tables,[])
    assert actual is (kind=='enumeration')


def test_scatter_figure_caption_binds_image_after_table_body_is_trimmed():
    """公共MiddleJson中Figure15.3两行图题绑定下方散点图，不能作为上方表格的表题。"""
    from docvortex import parse
    result=parse(FIXTURES/'review_130.pdf',keep_model_json=True)
    blocks=result.to_dict()['pages'][0]['blocks']
    images=[b for b in blocks if b['type']=='image']
    assert len(images)==1
    captions=[c for c in images[0]['content'] if c['type']=='image_caption']
    assert len(captions)==1 and 'Figure 15.3.' in _visible_text(captions[0]['content'])
    assert _visible_text(captions[0]['content']).endswith('Potential New Investment')
    assert not any('Figure 15.3.' in _visible_text(c.get('content','')) for b in blocks if b['type']=='table' for c in b['content'])


@pytest.mark.parametrize('scale,left,width',[(.7,13,350),(1,70,440),(1.6,210,600)])
@pytest.mark.parametrize('kind',['body','note','same_font','one_paragraph','short_tail','ruled_cell','continued_data','few_rows'])
def test_unruled_table_tail_needs_numeric_columns_and_independent_larger_body_paragraphs(scale,left,width,kind):
    """改变字号、位置、栏宽和无关文字；表后正文释放，表注、同字号单元格、连续段、边框内文本及续行数据保留。"""
    from docvortex.analyzers.native.pdf.models import _PageSource,_TableCandidate,_AxisLine
    from docvortex.analyzers.native.pdf.table_detection import _trim_unruled_table_prose_tail
    lines=[]
    for row in range(2 if kind=='few_rows' else 4):
        for col,value in enumerate([str(2034+row),f'{8+row}%',f'{4+row}%']):
            x=left+(.02+.35*col)*width
            lines.append(_text_line(value,(x,(100+20*row)*scale,x+25*scale,(110+20*row)*scale),len(lines)))
    height=10 if kind=='same_font' else 12
    positions=[190,204,218,232] if kind=='one_paragraph' else [190,204,230,244]
    if kind=='short_tail':positions=positions[:2]
    for index,y in enumerate(positions):
        text='Independent ordinary explanation about the displayed values and their relationship.'
        if kind=='note' and index==0:text='Note: '+text
        lines.append(_text_line(text,(left,y*scale,left+width,(y+height)*scale),len(lines)))
    if kind=='continued_data':
        for col in range(3):
            x=left+(.02+.35*col)*width
            lines.append(_text_line(str(22+col),(x,260*scale,x+25*scale,270*scale),len(lines)))
    core=(left,60*scale,left+width,280*scale)
    rules=[_AxisLine((left,60*scale,left+.1,280*scale),.1,'vertical')] if kind=='ruled_cell' else []
    source=_PageSource((left+width+30,350*scale),lines,[],rules)
    candidate=_TableCandidate(bbox=core,local_bbox=core,core_bbox=core,angle=0,score=8,line_indices={l.source_index for l in lines},annotations=[])
    _trim_unruled_table_prose_tail(source,candidate)
    assert (candidate.core_bbox[3] < core[3]) is (kind=='body')
    if kind=='body':
        assert candidate.core_bbox[3] < 190*scale
        assert candidate.line_indices == {l.source_index for l in lines[:12]}


@pytest.mark.parametrize('scale,left',[(.7,11),(1,70),(1.8,210)])
@pytest.mark.parametrize('kind',['left','right','word','far','ambiguous','off_lane','footnote','different_row','claimed'])
def test_vector_text_number_binding_needs_numeric_marker_lane_and_unique_nearby_core(scale,left,kind):
    """改变字号、栏宽和编号位置；左右编号保留唯一归属，普通括号词、远距、多主体、脚注与其他行拒绝。"""
    from docvortex.analyzers.native.pdf.formulas import _VectorFormulaCandidate, _attach_vector_formula_text_numbers
    y=180 if kind=='different_row' else 100
    x=240 if kind=='right' else (50 if kind=='off_lane' else 0)
    text='(Other)' if kind=='word' else ('(8)' if kind=='right' else '(7.31)')
    width=15 if kind=='right' else 29
    line=_text_line(text,(left+x*scale,y*scale,left+(x+width)*scale,(y+10)*scale),0)
    if kind=='footnote':line.semantic_type='page_footnote'
    core_left=100 if kind=='far' else 38
    candidates=[_VectorFormulaCandidate(0,(left+core_left*scale,92*scale,left+210*scale,110*scale),{4})]
    if kind=='ambiguous':candidates.append(_VectorFormulaCandidate(0,(left+40*scale,94*scale,left+100*scale,111*scale),{5}))
    lane=_TextLane(left=left,right=left+255*scale,lines=[(line,line.bbox)],is_span=False)
    claimed=_attach_vector_formula_text_numbers(candidates,[lane],10*scale,{0} if kind=='claimed' else set())
    assert bool(claimed) is (kind in {'left','right'})
    assert sum(c.has_number for c in candidates)==int(kind in {'left','right'})
    if claimed:
        assert candidates[0].bbox[0] <= line.bbox[0] and candidates[0].bbox[2] >= line.bbox[2]


@pytest.mark.parametrize('scale,left',[(.65,12),(1,90),(1.75,230)])
@pytest.mark.parametrize('kind',['drop_cap','formula','small_cap','above_cap','misaligned','uppercase','number'])
def test_drop_cap_script_repair_requires_word_baseline_and_large_descending_cap(scale,left,kind):
    """改变字词、字号和位置；下沉首字母恢复正文，公式、真正高位脚本、错基线和非小写词尾拒绝。"""
    from docvortex.analyzers.native.pdf.inline.scripts import _classify_script_runs, _drop_cap_body_indices
    text = 'Able' if kind != 'uppercase' else 'ABCD'
    if kind == 'number': text = 'A123'
    cap_height = 15 if kind == 'small_cap' else 55
    baseline = 95 if kind == 'above_cap' else 124
    chars=[];boxes={};origins={}
    for i,char in enumerate(text):
        bbox=(left,100*scale,left+45*scale,(100+cap_height)*scale) if i==0 else (left+(50+6*i)*scale,(baseline-8)*scale,left+(55+6*i)*scale,baseline*scale)
        chars.append({'char':char,'char_idx':i,'bbox':bbox,'font':{'name':'GenericSerif','weight':400}})
        boxes[i]=bbox
        origins[i]=(bbox[0],((100+cap_height) if i==0 else baseline+(3 if kind=='misaligned' and i==2 else 0))*scale)
    indices=_drop_cap_body_indices(chars,boxes,origins)
    assert bool(indices) is (kind in {'drop_cap','formula'})
    if kind=='drop_cap':
        for preclassified in [None,[bytes([0,1,1,1])]]:
            roles,counts,flags=_classify_script_runs(chars,boxes,origins,[None]*4,preclassified_native=preclassified)
            assert roles==['body']*4 and counts==[4]*4 and flags==[False]*4
    if kind=='formula':
        roles,_,flags=_classify_script_runs(chars,boxes,origins,[0]*4,preclassified_native=[bytes([0,1,1,1])])
        assert roles[1:]==['sup']*3 and flags==[True]*4


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('kind',['chapter','page_number','small_label','offset','container'])
def test_display_roman_heading_requires_scale_alignment_emphasis_and_free_region(scale,left,kind):
    """改变字号位置和字体，罗马章号仅与邻近大号标题合并；页码、普通字、错位或图内标签均拒绝。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_display_roman_heading_rows
    marker_height=(10 if kind=='page_number' else 30)*scale
    marker=_text_line('VII.',(left,100*scale,left+40*scale,100*scale+marker_height),0)
    label_height=(10 if kind=='small_label' else 26)*scale
    label_left=left+(30*scale if kind=='offset' else 0)
    label=_text_line('Different chapter',(label_left,130*scale,label_left+180*scale,130*scale+label_height),1)
    tail=_text_line('A wrapped heading',(label_left,156*scale,label_left+180*scale,182*scale),2)
    marker.font_signature=('MarkerFamily',0);label.font_signature=tail.font_signature=('OtherFamily',0)
    marker.dominant_font_weight=label.dominant_font_weight=tail.dominant_font_weight=700
    marker.font_coverage=label.font_coverage=tail.font_coverage=1
    label.semantic_type=tail.semantic_type='doc_title'
    lines=[marker,label,tail]
    lines.extend(_text_line('Some other ordinary body words',(left+250*scale,(140+15*i)*scale,left+430*scale,(150+15*i)*scale),3+i) for i in range(3))
    containers=[(left-5*scale,95*scale,left+200*scale,190*scale)] if kind=='container' else []
    _classify_display_roman_heading_rows(lines,(left+500*scale,400*scale),containers)
    assert (marker.semantic_type=='paragraph_title') is (kind=='chapter')
    if kind=='chapter':assert all(l.title_band_id==0 and l.semantic_type=='paragraph_title' for l in lines[:3])


@pytest.mark.parametrize('scale,left',[(.7,25),(1,70),(1.8,160)])
@pytest.mark.parametrize('text,expected',[
    ('02- Another procedural heading',True),('03–A different heading',True),('07— More unrelated instructions',True),
    ('02-26',False),('02-variable expression',False),('02- Other complete sentence.',False),
])
def test_compact_step_dash_requires_label_and_preserves_range_prose_negatives(scale,left,text,expected):
    """紧接编号的短横线可引出粗体标题，数值范围、小写变量及完整句末正文仍不成为标题。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_bold_numbered_heading_rows
    line=_text_line(text,(left,100*scale,left+180*scale,110*scale),0)
    line.dominant_font_weight=700
    line.font_coverage=1
    _classify_bold_numbered_heading_rows([line],(left+500*scale,400*scale),[])
    assert (line.semantic_type=='paragraph_title') is expected


def test_closed_grid_table_does_not_claim_prose_above_its_physical_header():
    """原页闭合网格从Genes表头开始，上方两个自然段、圆点及引导句必须独立保留。"""
    blocks = _pages('review_120')[0]
    tables = [b for b in blocks if b['type'] == 'table']
    assert len(tables) == 1
    table = tables[0]
    assert .33 < table['bbox'][1] < .345
    assert table['bbox'][3] < .84
    text = _visible_text(table['content'])
    assert 'Genes in DNA' in text and 'Characteristics' in text
    assert 'Sickle cell hemoglobin and normal hemoglobin differ' not in text
    assert 'Valine (Val)' not in text and 'The chart on the next page' not in text
    body = [b for b in blocks if b['type'] == 'text' and b['bbox'][3] < table['bbox'][1]]
    assert any('different properties of sickle cell hemoglobin' in _visible_text(b['content']) for b in body)
    assert any('Valine (Val)' in _visible_text(b['content']) for b in body)
    assert any('symptoms of sickle cell anemia.' in _visible_text(b['content']) for b in body)


@pytest.mark.parametrize('scale,left,width',[(.7,13,250),(1,80,400),(1.8,210,660)])
@pytest.mark.parametrize('kind',['prose','two_rows','short_labels','bold_header','caption','open_grid','no_top','other_column','unrelated_members','near_header'])
def test_closed_grid_protects_boundary_only_against_member_prose_spanning_remote_rule(scale,left,width,kind):
    """移动缩放闭合网格和页顶横线；多行表头、开放表、异栏或非成员正文不能触发弱区间排除。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf.models import _PageSource, _TableCandidate, _AxisLine, _Fragment, _VisualRow
    from docvortex.analyzers.native.pdf.table_rules import _RuleCandidateDraft
    from docvortex.analyzers.native.pdf.table_detection import _exclude_prose_spanning_rule_drafts
    top = 130 if kind == 'near_header' else 200
    bottom = top + 90
    core = (left, top*scale, left+width, bottom*scale)
    rules = [_AxisLine((x-.1*scale,top*scale,x+.1*scale,bottom*scale),.2*scale,'vertical')
             for x in [left,left+.5*width,left+width]]
    if kind == 'open_grid':rules = rules[:2]
    rules += [_AxisLine((left,y*scale,left+width,(y+.2)*scale),.2*scale,'horizontal')
              for y in ([bottom] if kind == 'no_top' else [top,bottom])]
    lines=[]
    for n in range(2 if kind == 'two_rows' else 3):
        x=left+2*width if kind == 'other_column' else left
        text='An ordinary unrelated descriptive paragraph has several complete words'
        if kind == 'short_labels':text='Brief label'
        line=_text_line(text,(x,(105+14*n)*scale,x+.85*width,(115+14*n)*scale),n,
                        dominant_font_weight=700 if kind=='bold_header' else 400)
        lines.append(line)
    rows=[_VisualRow([_Fragment(l.text,l.bbox,l.bbox,l.source_index)],(l.bbox[1]+l.bbox[3])/2,l.bbox) for l in lines]
    if kind == 'unrelated_members':rows=[]
    boundary = SimpleNamespace(bbox=(left,100*scale,left+width,100.2*scale))
    lower = SimpleNamespace(bbox=(left,(top+30)*scale,left+width,(top+30.2)*scale))
    draft = _RuleCandidateDraft(SimpleNamespace(angle=0,median_height=10*scale),[boundary,lower],rows,
                                lines[0] if kind=='caption' else None,[],False,7)
    grid = _TableCandidate(core,core,0,100,core_bbox=core)
    source = _PageSource((left+3*width,400*scale),lines,[],rules)
    result = _exclude_prose_spanning_rule_drafts(source,[draft,grid])
    assert (draft not in result) == (kind=='prose')
    assert grid in result


@pytest.mark.parametrize('name,figures',[('review_40',[1]),('review_41',[3]),('review_42',[5]),('review_43',[7,8])])
def test_survey_charts_have_complete_unique_bodies_labels_and_captions(name,figures):
    """实际横柱、纵柱、单环与同心环图均完整认领标签，所有连续图题唯一绑定自己的图体。"""
    from docvortex import parse
    blocks=parse(FIXTURES/f'{name}.pdf',keep_model_json=True).to_dict()['pages'][0]['blocks']
    images=[b for b in blocks if b['type']=='image']
    assert len(images)==len(figures)
    for image,number in zip(images,figures):
        captions=[c for c in image['content'] if c['type']=='image_caption']
        assert len(captions)==1 and f'Figure {number}' in _visible_text(captions[0]['content'])
        assert '%' not in _visible_text(captions[0]['content'])
        assert not any(b['type'] in {'equation','paragraph_title'} and b['bbox'][0]>=image['bbox'][0] and b['bbox'][2]<=image['bbox'][2] and b['bbox'][1]>=image['bbox'][1] and b['bbox'][3]<=image['bbox'][3] for b in blocks)
    if name=='review_42':assert _visible_text(images[0]['content'][0]['content']).endswith('travelling to conflict zones')
    # 原页实际写作COVID-1，保留源文字而不是擅自补上数字9。
    if name=='review_43':assert any('during COVID-1' in _visible_text(c['content']) for c in images[1]['content'] if c['type']=='image_caption')


@pytest.mark.parametrize('name,first,last',[('review_41','There were instances','of condemning the act'),('review_42','However,','minds are controlled, even though')])
def test_emphasized_survey_quotations_stay_complete_text_not_headings_or_equations(name,first,last):
    """粗体居中引用与首行分散排版的斜体引用都是完整正文，不是标题或展示公式。"""
    blocks=_pages(name)[0]
    matches=[b for b in blocks if first in _visible_text(b['content'])]
    assert len(matches)==1 and matches[0]['type']=='text'
    assert last in _visible_text(matches[0]['content'])


def test_quote_introduction_is_body_before_the_indented_interview_passage():
    """普通字体的两行引语引导句不是小节标题，后面斜体引用仍独立。"""
    blocks=_pages('review_43')[0]
    b=next(b for b in blocks if 'Another interviewee from Indonesia' in _visible_text(b['content']))
    assert b['type']=='text' and _visible_text(b['content']).endswith('observed that:')
    assert 'Based' not in _visible_text(b['content'])


@pytest.mark.parametrize('scale,left',[(.7,15),(1,80),(1.8,200)])
@pytest.mark.parametrize('kind',['ring','single','off_center','oval','tiny','no_caption','one_percent','no_stroke','nested_form','far_caption'])
def test_concentric_graphic_requires_caption_two_native_outlines_and_percentage_labels(scale,left,kind):
    """同心轮廓与百分比、编号图题共同确认图体；孤立圆、椭圆、装饰点、Form和无图题区域拒绝。"""
    from docvortex.document.pdf._document import PDFPathInfo
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.graphics import _detect_captioned_concentric_path_graphics
    d=(30 if kind=='tiny' else 140)*scale
    box=(left,200*scale,left+d,(200*scale)+d)
    paths=[]
    for n in range(1 if kind=='single' else 2):
        x=left+(30*scale if kind=='off_center' and n else 0)
        y=200*scale
        bounds=(x,y,x+d,y+d*(1.7 if kind=='oval' else 1))
        paths.append(PDFPathInfo(bbox=bounds,segment_count=13-n*3,fill_visible=False,stroke_visible=kind!='no_stroke',form_depth=int(kind=='nested_form'),source_index=n))
    caption=_text_line('Figure 9: A different statistical description',(left,(10 if kind=='far_caption' else 140)*scale,left+150*scale,(20 if kind=='far_caption' else 150)*scale),0)
    lines=[] if kind=='no_caption' else [caption]
    for n in range(1 if kind=='one_percent' else 2):
        lines.append(_text_line(f'{23+n*40}%',(left+n*d*.5,230*scale,left+n*d*.5+25*scale,240*scale),n+1))
    source=_PageSource((left+500*scale,700*scale),lines,[],[],path_infos=paths)
    assert bool(_detect_captioned_concentric_path_graphics(source,10*scale)) == (kind=='ring')


@pytest.mark.parametrize('scale,left',[(.7,15),(1,80),(1.8,200)])
@pytest.mark.parametrize('kind',['italic','bold_closed','regular','short','no_quote','large','math','different_family','large_gap'])
def test_emphasized_quote_prose_requires_connected_native_sentence_rows_and_quote_evidence(scale,left,kind):
    """改变位置字号及措辞；多行引用保护正文，普通标题、短引号、数学式、字体变化和大净空拒绝。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _mark_emphasized_quote_prose
    from docvortex.analyzers.native.pdf.inline.types import PDF_FONT_ITALIC_FLAG
    lines=[]
    for n in range(2 if kind=='short' else 6):
        text=('“Another complete quoted sentence discusses unrelated evidence' if n==0 else 'The next explanatory line contains ordinary descriptive words')
        if kind in {'bold_closed','no_quote'} and n==0:text=text.lstrip('“')
        if kind=='bold_closed' and n==5:text+='.”'
        if kind=='math' and n==2:text+=' = x'
        h=(17 if kind=='large' else 10)*scale
        y=(100+n*(40 if kind=='large_gap' else 13))*scale
        flags=PDF_FONT_ITALIC_FLAG if kind not in {'bold_closed','regular'} else 0
        weight=700 if kind=='bold_closed' else 400
        line=_text_line(text,(left,y,left+210*scale,y+h),n,font_signature=('Other' if kind=='different_family' and n%2 else 'Generic',flags),dominant_font_weight=weight,font_coverage=1)
        line.visual_row_id=n
        lines.append(line)
    lines.extend(_text_line('Some unrelated ordinary body words',(left+270*scale,(80+13*n)*scale,left+470*scale,(90+13*n)*scale),10+n) for n in range(10))
    _mark_emphasized_quote_prose(lines)
    assert all(line.semantic_type=='text' and line.paragraph_group is not None for line in lines[:6]) == (kind in {'italic','bold_closed'})


@pytest.mark.parametrize('scale,left',[(.7,15),(1,80),(1.8,200)])
@pytest.mark.parametrize('kind',['wrapped','subset','numeric','different_weight','other_family','different_column','gap','inside','reference'])
def test_wrapped_figure_caption_uses_last_row_and_preserves_native_subset_font_continuation(scale,left,kind):
    """图题末行决定图体距离；同字体子集续行可合并，数值标签、异字体、异栏及图内文字不能伪装成图题。"""
    from docvortex.analyzers.native.pdf.graphics import _graphic_caption_line_indices_to_preserve
    from docvortex.analyzers.native.pdf.models import _GraphicCandidate
    lines=[]
    for n,text in enumerate(['Figure 9: A different descriptive title','Another wrapped description about ordinary values','The final complete caption continuation']):
        if kind=='reference' and n==0:text='Figure 9 presents ordinary unrelated descriptive values'
        if kind=='numeric' and n==2:text='42%'
        x=left+250*scale if kind=='different_column' and n==2 else left
        y=(100+13*n+(20 if kind=='gap' and n==2 else 0))*scale
        font='ABCDEF+GenericBold' if kind=='subset' and n==2 else 'Other' if kind=='other_family' and n==2 else 'GenericBold'
        line=_text_line(text,(x,y,x+170*scale,y+10*scale),n,font_signature=(font,0),dominant_font_weight=400 if kind=='different_weight' and n==2 else 700,font_coverage=1)
        lines.append(line)
    top=125 if kind=='inside' else 170
    candidate=_GraphicCandidate((left,top*scale,left+190*scale,310*scale),0)
    result=_graphic_caption_line_indices_to_preserve(lines,[candidate],10*scale)
    assert (2 in result and lines[2].semantic_type=='caption') == (kind in {'wrapped','subset'})


def test_multiline_figure_reference_prose_cannot_be_promoted_to_caption():
    """原件Figure3 presents是正文引用句，不能因多行接近图体而被归为图注。"""
    blocks=_pages('two_column_retrieval')[15]
    matches=[b for b in blocks if 'Figure 3 presents the number' in _visible_text(b['content'])]
    assert len(matches)==1 and matches[0]['type']=='text'
    assert 'the methods rank different documents on top.' in _visible_text(matches[0]['content'])


def test_long_indented_italic_quotation_in_plastics_report_remains_complete_body():
    """原页长斜体引用由开引号至闭引号完整保留，不能作为段落标题。"""
    blocks=_pages('review_68')[0]
    matches=[b for b in blocks if 'Despite these efforts' in _visible_text(b['content'])]
    assert len(matches)==1 and matches[0]['type']=='text'
    assert 'integration of the various reports, if available, a challenge.' in _visible_text(matches[0]['content'])


def test_centered_emphasized_summary_heading_above_grid_is_not_body():
    """原页表格上方居中的独立粗体标题不能混为正文。"""
    blocks = _pages('review_88')[0]
    matches = [b for b in blocks if _visible_text(b['content']) == 'Comparative Summary Table']
    assert len(matches) == 1 and matches[0]['type'] in {'paragraph_title','caption'}


@pytest.mark.parametrize("left,width", [(20, 220), (80, 360), (150, 590)])
@pytest.mark.parametrize("kind", ["numbered", "grid", "legend"])
@pytest.mark.parametrize("font,weight,expected", [
    (("Unrelated-Bold", 0), 225, True),
    (("Another-Semibold", 0), 560, True),
    (("Generic", 1 << 18), 225, True),
    (("Generic", 0), 700, True),
    (("Generic", 0), 560, False),
    (("Generic", 0), None, False),
])
def test_heading_bold_metadata_survives_old_pdfium_weight_values(left, width, kind, font, weight, expected):
    """独立改变栏宽和字体身份，旧字重必须有明确粗体证据才能支持编号或网格上方标题。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import (
        _classify_bold_numbered_heading_rows,
        _classify_native_display_resets,
    )
    h = 10
    x = left if kind in {"numbered", "legend"} else left + .5 * width - 60
    title = _metric_fixture_line(
        "D E" if kind == "legend" else "3 Other Independent Topic" if kind == "numbered" else "Independent Grid Summary",
        (x, 50, x + 120, 65), 20, effective_height=15, font_signature=font,
        dominant_font_weight=weight, font_coverage=1,
    )
    if kind in {"numbered", "legend"}:
        _classify_bold_numbered_heading_rows([title], (left + width + 20, 500), [])
    else:
        rows = [title] + [
            _metric_fixture_line("Ordinary complete prose with several unrelated words.",
                (left, 300 + index * 14, left + width, 310 + index * 14), index,
                effective_height=h, font_signature=("Body", 0), dominant_font_weight=400, font_coverage=1)
            for index in range(8)
        ]
        _classify_native_display_resets(rows, (left + width + 20, 500), [], [(left, 77, left + width, 250)],
            page_index=0, reference_lines=rows)
    assert (title.semantic_type == "paragraph_title") is (expected and kind != "legend")


def test_repeated_light_display_style_titles_survive_equal_native_font_boxes():
    """两个轻字重标题虽与正文字框同高，重复字体和上下区域仍证明独立标题。"""
    blocks = _pages('review_118')[0]
    for text in ['Cellular Replication','Growth and the Creation of Life']:
        matches = [b for b in blocks if _visible_text(b['content']) == text]
        assert len(matches) == 1 and matches[0]['type'] == 'paragraph_title'


def test_brochure_multiline_heading_and_colon_subheadings_are_independent_titles():
    """折页大标题两行完整合并，各同级冒号小标题独立成块。"""
    blocks = _pages('review_163')[0]
    main = [b for b in blocks if ''.join(_visible_text(b['content']).split()) == 'HOWCANYOUHELP?']
    assert len(main) == 1 and main[0]['type'] == 'paragraph_title'
    for text in ['As a boater:','As a developer:','As a homeowner:']:
        matches = [b for b in blocks if _visible_text(b['content']) == text]
        assert len(matches) == 1 and matches[0]['type'] == 'paragraph_title'


def test_brochure_resource_heading_is_a_complete_section_title():
    """折页中间栏两行资源标题属于章节标题，不能成为独立文档题名。"""
    blocks = _pages('review_163')[0]
    matches = [b for b in blocks if _visible_text(b['content']) == 'FURTHER RESOURCES']
    assert len(matches) == 1 and matches[0]['type'] == 'paragraph_title'


def test_chinese_multiline_investment_section_is_one_title_not_equation():
    """原页两行投资章节题名完整聚合，同行大空白不能制造公式。"""
    blocks = _pages('securities_report')[3]
    matches = [b for b in blocks if '投资建议：主品牌短期承压' in _visible_text(b['content'])]
    assert len(matches) == 1 and matches[0]['type'] == 'paragraph_title'
    assert '来增长' in _visible_text(matches[0]['content'])
    assert '可期，关注高分红优质男装龙头投资机遇' in _visible_text(matches[0]['content'])
    assert not any(b['type'] == 'equation' and b['bbox'][1] < .16 for b in blocks)


@pytest.mark.parametrize('scale,left,width',[(.7,15,220),(1,55,360),(1.6,130,590)])
@pytest.mark.parametrize('kind',['light','light_single','light_body_font','light_small','light_sentence','table','table_regular','table_off_center','table_inside','multi','multi_single','multi_other_font','multi_far','multi_formula','chinese','chinese_first','colon','colon_same_weight','colon_single','colon_far'])
def test_native_display_resets_need_repeated_style_or_complete_container_geometry(scale,left,width,kind):
    """移动缩放标题和正文，字体重复、跨行对齐及容器净空必须成立；正文与表内文字不晋升。"""
    from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_native_display_resets
    h=10*scale
    rows=[]
    # 固定自然语言正文样本用于字号参照；坐标与栏宽均随参数变化。
    for index in range(8):
        y=(300+index*14)*scale
        rows.append(_metric_fixture_line('Ordinary complete prose with several unrelated words.',(left,y,left+width,y+h),index,
                                         effective_height=h,font_signature=('Body',0),dominant_font_weight=400,font_coverage=1))
    tables=[];images=[];targets=[]
    if kind.startswith('light'):
        for index in range(1 if kind=='light_single' else 2):
            y=(40+index*100)*scale
            text='Quiet Topic.' if kind=='light_sentence' else 'Quiet Topic'
            hh=.6*h if kind=='light_small' else h
            line=_metric_fixture_line(text,(left,y,left+100*scale,y+hh),20+index,effective_height=hh,
                                      font_signature=('Body' if kind=='light_body_font' else 'Display',0),dominant_font_weight=300,font_coverage=1)
            rows.append(line);targets.append(line)
            for offset in [12,24]:
                rows.append(_metric_fixture_line('Independent native body sentence with several unrelated words.',(left,y+hh+offset*scale,left+width,y+hh+offset*scale+h),40+index*2+offset,
                                                  effective_height=h,font_signature=('Body',0),dominant_font_weight=400,font_coverage=1))
    elif kind.startswith('table'):
        yy=50*scale
        xx=left+(.5*width-60*scale if kind!='table_off_center' else 0)
        line=_metric_fixture_line('Independent Grid Summary',(xx,yy,xx+120*scale,yy+1.5*h),20,effective_height=1.5*h,
                                 font_signature=('Display',0),dominant_font_weight=400 if kind=='table_regular' else 700,font_coverage=1)
        rows.append(line);targets.append(line)
        top=45*scale if kind=='table_inside' else 77*scale
        tables=[(left,top,left+width,250*scale)]
    elif kind.startswith('multi') or kind.startswith('chinese'):
        chinese=kind.startswith('chinese')
        for index in range(1 if kind in ['multi_single','chinese','chinese_first'] else 2):
            xx=left+index*.5*width
            for number in range(2):
                y=(40+number*(60 if kind=='multi_far' else 25))*scale
                text='自然章节：题名' if chinese and number==0 else '自然章节续行' if chinese else 'STRUCTURED HEADING' if number==0 else 'CONTINUED TOPIC'
                if kind=='multi_formula':text='x = y + z'
                font='Other' if kind=='multi_other_font' and index==1 else 'Display'
                line=_metric_fixture_line(text,(xx,y,xx+.3*width,y+2*h),20+2*index+number,effective_height=2*h,
                                          font_signature=(font,0),dominant_font_weight=400 if chinese else 700,font_coverage=1)
                rows.append(line);targets.append(line)
    else:
        for index in range(1 if kind=='colon_single' else 3):
            y=(40+index*80)*scale
            line=_metric_fixture_line('For ordinary participants:',(left,y,left+100*scale,y+h),20+index,effective_height=h,
                                      font_signature=('Display',0),dominant_font_weight=450 if kind=='colon_same_weight' else 700,font_coverage=1)
            rows.append(line);targets.append(line)
            for offset in [12,24]:
                yy=y+h+offset*scale+(50*scale if kind=='colon_far' else 0)
                rows.append(_metric_fixture_line('Complete entry with several ordinary words.',(left+h,yy,left+width,yy+h),40+index*2+offset,effective_height=h,
                                                  font_signature=('Body',0),dominant_font_weight=400,font_coverage=1))
    _classify_native_display_resets(rows,(left+width+20,500*scale),images,tables,
                                    page_index=1 if kind=='chinese' else 0,reference_lines=rows)
    assert all(line.semantic_type=='paragraph_title' for line in targets)==(kind in ['light','table','multi','chinese','colon'])
    if kind in ['multi','chinese']:
        assert targets[0].title_band_id==targets[1].title_band_id
        assert targets[0].paragraph_group==targets[1].paragraph_group


def test_regular_financial_risk_prose_cannot_become_repeated_multiline_titles():
    """财报风险正文与短标题同字体时，重复换行不能把完整说明句提升为标题。"""
    with PDFDocument(str(Path(__file__).parents[2]/'demo/pdfs/caibao1.pdf')) as document:
        blocks = pipeline._analyze_native_document(document)[18]
    for anchor in ['若国内宏观经济增速放缓','目前，全球芯片产业短缺问题仍未得到解决']:
        matches = [b for b in blocks if anchor in _visible_text(b['content'])]
        assert len(matches) == 1 and matches[0]['type'] == 'text'


def test_captioned_photo_cannot_absorb_a_parallel_prose_column():
    """右侧照片不能认领左侧连续正文，后两行仍归入同一完整段落。"""
    blocks=_pages('review_62')[0]
    photos=[b for b in blocks if b['type']=='image']
    assert len(photos)==1 and photos[0]['bbox'][0]>.45
    para=[b for b in blocks if 'Shipping remains' in _visible_text(b['content'])]
    assert len(para)==1 and para[0]['type']=='text'
    assert 'Thailand, and Sri Lanka.' in _visible_text(para[0]['content'])


def test_captioned_raster_table_cannot_absorb_the_previous_paragraph_last_word():
    """表格图片只保留原生图片边界，cross-section完整续行仍归上方正文。"""
    blocks=_pages('review_110')[0]
    image=next(b for b in blocks if b['type']=='image')
    assert image['bbox'][1]>.44
    para=next(b for b in blocks if 'The Reynolds experiment determines' in _visible_text(b['content']))
    assert _visible_text(para['content']).endswith('cross-section.')


def test_photo_frame_and_contained_photo_are_one_raster_object():
    """一张大照片的阴影边框和内图只输出一个图块，三个独立侧栏插图保持。"""
    images=[b for b in _pages('review_118')[0] if b['type']=='image']
    large=[b for b in images if b['bbox'][0]<.2 and b['bbox'][2]<.8]
    assert len(large)==1 and len(images)==4


def test_native_blank_photo_margin_cannot_include_resource_title_ink():
    """图片自身白色顶边可以裁去，标题文字和海牛主体都完整保留。"""
    blocks=_pages('review_163')[0]
    image=next(b for b in blocks if b['type']=='image' and .3<b['bbox'][0]<.4 and b['bbox'][1]<.2)
    title=next(b for b in blocks if _visible_text(b['content'])=='FURTHER RESOURCES')
    assert image['bbox'][1]>=title['bbox'][3] and image['bbox'][1]<.155
    assert image['bbox'][3]>.5


def test_restored_light_titles_preserve_two_separate_complete_native_body_paragraphs():
    """原页prokaryotes收句后，Cell division首行独立成段且两段文字完整。"""
    blocks=_pages('review_118')[0]
    first=next(b for b in blocks if 'One of the characteristics' in _visible_text(b['content']))
    second=next(b for b in blocks if 'Cell division in eukaryotes' in _visible_text(b['content']))
    assert first is not second and first['type']==second['type']=='text'
    assert _visible_text(first['content']).endswith('these organelles and prokaryotes.')
    assert _visible_text(second['content']).endswith('the cell prepares to divide.')


def test_photo_side_recycling_paragraph_preserves_short_final_native_rows():
    """左侧回收说明绕过右图图注后继续至收句，不能因栏宽变化断开。"""
    blocks=_pages('review_69')[0]
    matches=[b for b in blocks if 'McDonalds has installed' in _visible_text(b['content'])]
    assert len(matches)==1 and matches[0]['type']=='text'
    # 原页和原生字层均写为recycling. initiatives.，只修复段落归属，不改写原文标点。
    assert 'recycling. initiatives.' in _visible_text(matches[0]['content'])


@pytest.mark.parametrize('scale,left',[(.7,15),(1,65),(1.6,110)])
@pytest.mark.parametrize('kind',['prose','inside','numeric','two_rows','sparse','far','font_change','offset'])
def test_raster_side_prose_protection_requires_native_continuous_language_rows(scale,left,kind):
    """图片旁同栏连续正文才被保护；图内标签、数轴、稀疏行和字体/缩进屏障不建立段组。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.graphics import _raster_outside_prose_sources
    h=10*scale
    image=(left+120*scale,95*scale,left+220*scale,140*scale)
    lines=[]
    for index in range(2 if kind=='two_rows' else 4):
        x=left+130*scale if kind=='inside' else left+(30*scale if kind=='offset' and index%2 else 0)
        y=(100+index*(35 if kind=='far' else 14))*scale
        text='100' if kind=='numeric' else 'Sparse labels' if kind=='sparse' else 'Connected ordinary native prose continues here'
        if index==3 and kind=='prose':text='until the final sentence.'
        end=x+(60 if index==3 and kind=='prose' else 100)*scale
        lines.append(_metric_fixture_line(text,(x,y,end,y+h),index,effective_height=h,paragraph_terminal=index==3,
                                         font_signature=(f'Other{index}' if kind=='font_change' else 'Body',0),dominant_font_weight=400,font_coverage=1))
    source=_PageSource((left+250*scale,300*scale),lines,[],[],image_bboxes=[image])
    protected=_raster_outside_prose_sources(source,[image],set(),h)
    assert bool(protected)==(kind=='prose')
    if kind=='prose':
        assert len(protected)==4 and len({line.paragraph_group for line in lines})==1


@pytest.mark.parametrize('scale,left',[(.7,15),(1,65),(1.6,110)])
@pytest.mark.parametrize('kind',['frame','small_inset','caption_border','partial','same_area','wide_padding'])
def test_contained_photo_deduplication_keeps_separate_content_and_native_border_labels(scale,left,kind):
    """移动缩放近重合照片边框，只合并高覆盖且边缘无文字的嵌套帧。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.graphics import _deduplicate_contained_photo_frames
    outer=(left,100*scale,left+200*scale,230*scale)
    inner=(left+5*scale,104*scale,left+195*scale,223*scale)
    if kind=='small_inset':inner=(left+40*scale,140*scale,left+100*scale,200*scale)
    if kind=='partial':inner=(left+5*scale,104*scale,left+205*scale,223*scale)
    if kind=='same_area':inner=outer
    if kind=='wide_padding':inner=(left+20*scale,104*scale,left+198*scale,223*scale)
    lines=[_metric_fixture_line('Native label',(left+5*scale,100*scale,left+90*scale,103*scale),0,effective_height=3*scale)] if kind=='caption_border' else []
    source=_PageSource((left+250*scale,300*scale),lines,[],[])
    assert len(_deduplicate_contained_photo_frames(source,[outer,inner]))==(1 if kind=='frame' else 2)


@pytest.mark.parametrize('size',[(120,100),(360,300),(720,600)])
@pytest.mark.parametrize('kind',['white','transparent','dark','top_ink','no_margin','huge_margin','all_white'])
def test_blank_top_pixels_require_full_width_clear_margin_and_keep_all_visible_ink(size,kind):
    """像素证据只证明全宽白色/透明小顶边，黑底、顶边墨迹及大空白不裁切。"""
    from PIL import Image,ImageDraw
    from docvortex.document.pdf.native_objects import _blank_image_top_fraction
    background=(255,255,255,0) if kind=='transparent' else (0,0,0,255) if kind=='dark' else (255,255,255,255)
    with Image.new('RGBA',size,background) as image:
        draw=ImageDraw.Draw(image)
        top=0 if kind=='no_margin' else round(.2*size[1]) if kind=='huge_margin' else round(.05*size[1])
        if kind!='all_white':draw.rectangle((0,top,size[0]-1,size[1]-1),fill=(50,120,180,255))
        if kind=='top_ink':draw.point((size[0]//2,0),fill=(0,0,0,255))
        fraction=_blank_image_top_fraction(image)
        assert (fraction>0)==(kind in ['white','transparent'])
        if fraction:assert abs(fraction-.05)<1/size[1]


@pytest.mark.parametrize('scale,left,width',[(.7,15,220),(1,55,360),(1.6,130,590)])
@pytest.mark.parametrize('kind',['proven','no_pixels','ordinary_text','small_heading','inside_ink','no_overlap'])
def test_blank_raster_margin_is_consumed_only_under_independent_emphasized_display_text(scale,left,width,kind):
    """改变图片和标题位置/字号，像素白边、大字号和独立相交三种证据缺一不可。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.document.pdf.native_contracts import PDFImageInfo
    h=10*scale
    box=(left,100*scale,left+width,300*scale)
    trimmed=(left,112*scale,left+width,300*scale)
    rows=[_metric_fixture_line('Ordinary complete body sentence.',(left,400*scale,left+width,400*scale+h),0,effective_height=h)]
    hh=h if kind=='small_heading' else 2*h
    x=left+width+20 if kind=='no_overlap' else left+.1*width
    y=95*scale if kind=='inside_ink' else 90*scale
    rows.append(_metric_fixture_line('DISPLAY TITLE',(x,y,x+.8*width,y+hh),1,effective_height=hh,dominant_font_weight=400 if kind=='ordinary_text' else 700))
    source=_PageSource((left+width+50,500*scale),rows,[],[],image_bboxes=[box])
    # 增加普通字号参照行，避免仅有一个标题和一行正文时以平均字号反向推断标题。
    rows.extend([_metric_fixture_line('Other ordinary body sentence.',(left,(420+i*14)*scale,left+width,(430+i*14)*scale),i+2,effective_height=h) for i in range(3)])
    info=PDFImageInfo(box,None,blank_top_bbox=None if kind=='no_pixels' else trimmed)
    result=pipeline._exclude_proven_blank_top_under_display_title(source,[info])
    assert result==([trimmed] if kind=='proven' else [box])


def test_public_photo_side_prose_keeps_every_justified_native_run_in_source_order():
    """公共解析必须保持左右散排的第二行在完整段内，弱收句断言不能掩盖中间缺行。"""
    from docvortex import parse
    blocks=parse(FIXTURES/'review_62.pdf',keep_model_json=True).to_dict()['pages'][0]['blocks']
    paragraphs=[b for b in blocks if b['type']=='text' and 'Shipping remains' in _visible_text(b['content'])]
    assert len(paragraphs)==1
    text=_visible_text(paragraphs[0]['content'])
    assert text.startswith('Shipping remains as the only scientifically documented pathway for marine biological invasion')
    assert text.endswith('Thailand, and Sri Lanka.')
    assert sum('documented pathway' in _visible_text(b.get('content','')) for b in blocks if b['type']=='text')==1


def test_ruled_contents_keeps_every_entry_page_and_native_bold_style_in_one_index():
    """目录横线不是数据表，九条目录和页码完整唯一聚合并保留原生粗体。"""
    blocks=_pages('review_44')[0]
    indices=[b for b in blocks if b['type']=='index']
    assert len(indices)==1 and not any(b['type']=='table' for b in blocks)
    text=_visible_text(indices[0]['content'])
    for label,page in [('Executive Summary','4'),('Legal Framework','6'),('Election Administration','11'),('Civil Society Engagement','15'),('Political Parties, Candidates Registration and Election Campaign','18'),('Media Freedom and Access to Information','25'),('Voter Education and Awareness','29'),('Participation of Marginalized Sectors','31'),('Recommendations','39')]:
        import re
        assert re.search(re.escape(label)+r'\s+'+page+r'(?:\s|$)',text)
    assert any('bold' in span.get('styles',[]) and 'Executive Summary' in span.get('content','') for span in indices[0]['content'])


def test_directory_continuation_includes_its_first_unpaged_chapter_like_later_chapters():
    """首个Part V与后续同式章节共同归入目录，不能把页首章节截为游离正文。"""
    blocks = _pages('review_172')[0]
    indices = [block for block in blocks if block['type'] == 'index']
    assert len(indices) == 1
    text = _visible_text(indices[0]['content'])
    assert text.startswith('Part V. Chapter Five - Comparing Associations Between Multiple Variables')
    for part in ['Part VI.', 'Part VII.', 'Part VIII.', 'Part IX.']:
        assert text.count(part) == 1
    assert not any('Part V.' in _visible_text(block['content']) for block in blocks if block['type'] != 'index')


def test_native_unpaged_contents_is_one_index_below_its_explicit_heading():
    """有明确Contents题名和连续编号的五条无页码目录形成index，背景不再认领原生正文。"""
    blocks = _pages('review_198')[0]
    headings = [block for block in blocks if _visible_text(block['content']) == 'Contents']
    assert len(headings) == 1 and headings[0]['type'] == 'paragraph_title'
    indices = [block for block in blocks if block['type'] == 'index']
    assert len(indices) == 1
    text = _visible_text(indices[0]['content'])
    for item in ['1. Overview of OCR Pack', '2. Introduction of Product Services and Key Features',
                 '3. Product - Detail Specification', '4. Integration Policy', '5. FAQ']:
        assert text.count(item) == 1
    assert not any(block['type'] == 'image' for block in blocks)


def _directory_fixture_row(text, x, y, width, h, index, font='Body'):
    """用可缩放的原生行构造目录行，页码判定保持真实入口行为。"""
    from docvortex.analyzers.native.pdf.index_blocks import _IndexRow, _index_row_ends_in_page_number
    line = _metric_fixture_line(text, (x, y, x + width, y + h), index, effective_height=h,
                                font_signature=(font, 0), font_coverage=1)
    return _IndexRow([line], [line.bbox], line.bbox, text, _index_row_ends_in_page_number(text))


def _public_first_page_blocks(name):
    """通过公开解析与MiddleJson转换检查注释的实际父对象，避免只验证扁平标签。"""
    from docvortex import parse
    return parse(FIXTURES / f'{name}.pdf', keep_model_json=True).to_dict()['pages'][0]['blocks']


def test_cross_column_first_bullet_is_separate_from_its_introduction_and_precedes_remaining_items():
    """首条项目与正文拆开，四条条目按左栏末条到右栏续项的真实阅读顺序连续保留。"""
    blocks = _public_first_page_blocks('review_39')
    introduction = next(block for block in blocks if 'In all survey phases' in _visible_text(block['content']))
    assert _visible_text(introduction['content']).endswith('made changes were:')
    items = [block for block in blocks if _visible_text(block['content']).startswith('•')]
    assert len(items) == 4
    anchors = ['Adapting to social distancing;', 'Devising new ways', 'Moving into new products', 'Reducing employee salaries.']
    assert all(anchor in _visible_text(block['content']) for anchor, block in zip(anchors, items))


def test_italic_game_derivation_is_one_paragraph_and_keeps_both_outlined_inline_formulas():
    """同式斜体推导完整成段，两个无Unicode的矢量公式必须各有非空裁图，后续普通问题分开。"""
    blocks = _public_first_page_blocks('review_97')
    text = next(block for block in blocks if 'Here, Player 2 applies' in _visible_text(block['content']))
    assert text['type'] == 'text' and 'concede and be done with it.' in _visible_text(text['content'])
    assert 'What’s the outcome' not in _visible_text(text['content'])
    formulas = [block for block in blocks if block['type'] == 'equation' and .54 < block['bbox'][1] < .64]
    assert len(formulas) == 2 and all(block.get('image_path') or block.get('image_base64') for block in formulas)


def test_native_paragraph_flow_around_four_outlined_math_fragments_keeps_one_complete_body():
    """上段的期望值、方差与行内完整等式保持四幅裁图；不能把同一物理行前后文本拆段。"""
    blocks = _public_first_page_blocks('review_129')
    text = next(block for block in blocks if 'the distributions were identically distributed' in _visible_text(block['content']))
    assert text['type'] == 'text' and _visible_text(text['content']).endswith('each partner would face would be:')
    formulas = [block for block in blocks if block['type'] == 'equation' and .13 < block['bbox'][1] < .20]
    assert len(formulas) == 4 and all(block.get('image_path') or block.get('image_base64') for block in formulas)
    assert text['bbox'][1] < .15 and .23 < text['bbox'][3] < .24


def test_short_vector_lhs_and_long_aligned_rhs_form_one_complete_equation():
    """短左式行末居中扁平运算字形与紧接长右式构成完整等式，不能拆成两幅公式。"""
    blocks = _public_first_page_blocks('review_169')
    equations = [block for block in blocks if block['type'] == 'equation']
    assert len(equations) == 1 and equations[0]['bbox'][1] < .11 and equations[0]['bbox'][3] > .15
    assert len([block for block in blocks if _visible_text(block['content']).startswith('•') and block['bbox'][1] < .25]) == 4


def test_captioned_mixed_port_table_preserves_two_header_levels_and_ten_complete_rows():
    """PORT与SHIPCALLS两层表头及十行三列完整成表，独立表题属于该表且正文不混入。"""
    from bs4 import BeautifulSoup
    blocks = _public_first_page_blocks('review_64')
    tables = [block for block in blocks if block['type'] == 'table']
    assert len(tables) == 1
    soup = BeautifulSoup(_visible_text(tables[0]['content']), 'html.parser')
    assert len(soup.find_all('tr')) == 12
    text = soup.get_text(' ', strip=True)
    assert all(anchor in text for anchor in ['PORT','SHIPCALLS','Foreign','Domestic','MANILA','LUCENA','4,428'])
    assert any(child['type']=='table_caption' and 'Top 10 ports' in _visible_text(child['content']) for child in tables[0]['content'])
    assert 'The port of Manila' not in text


def test_captioned_remittance_table_preserves_seven_columns_group_header_and_source_note():
    """七列表保留增长率分组表头、八个国家和数据，表题及来源归属唯一。"""
    from bs4 import BeautifulSoup
    blocks = _public_first_page_blocks('review_78')
    tables = [block for block in blocks if block['type'] == 'table']
    assert len(tables) == 1
    soup = BeautifulSoup(_visible_text(tables[0]['content']), 'html.parser')
    assert len(soup.find_all('tr')) == 10
    assert any(cell.get('colspan')=='5' and 'Average Annual Growth' in cell.get_text() for cell in soup.find_all(['td','th']))
    assert all(anchor in soup.get_text(' ', strip=True) for anchor in ['AMS','Remittance','2000-2004','2019-2020','Cambodia','Viet Nam','17,200'])
    assert any(child['type']=='table_caption' and 'Table 1.4' in _visible_text(child['content']) for child in tables[0]['content'])
    assert any(child['type']=='table_footnote' and 'KNOMAD' in _visible_text(child['content']) for child in tables[0]['content'])


def test_saccharometer_table_keeps_four_complete_numeric_unit_rows_and_its_caption():
    """四列四行实验数据含星号单位并不构成公式，表头和两行表题完整归属。"""
    from bs4 import BeautifulSoup
    blocks = _public_first_page_blocks('review_116')
    tables = [block for block in blocks if block['type']=='table']
    table = next(block for block in tables if 'Table 2.' in _visible_text(block['content']))
    soup = BeautifulSoup(_visible_text(table['content']), 'html.parser')
    assert len(soup.find_all('tr'))==5 and all(len(row.find_all(['td','th']))==4 for row in soup.find_all('tr'))
    assert '*12 ml' in soup.get_text(' ',strip=True) and 'Yeast Suspension' in soup.get_text(' ',strip=True)
    assert any(child['type']=='table_caption' and 'concentrations.' in _visible_text(child['content']) for child in table['content'])


def test_single_data_row_continuation_uses_repeated_headers_and_keeps_instruction_outside_cells():
    """重复四列表头支持单行续表；加粗两行倍量说明作为前表脚注，不能吞入续表表头。"""
    from bs4 import BeautifulSoup
    tables = [block for block in _public_first_page_blocks('review_116') if block['type']=='table']
    assert len(tables)==2
    soup = BeautifulSoup(_visible_text(tables[1]['content']), 'html.parser')
    assert len(soup.find_all('tr'))==2 and all(len(row.find_all(['td','th']))==4 for row in soup.find_all('tr'))
    assert '16 ml' in soup.get_text(' ',strip=True) and '12 ml' in soup.get_text(' ',strip=True)
    assert any(child['type']=='table_footnote' and 'Double these amounts' in _visible_text(child['content']) and 'below' in _visible_text(child['content']) for child in tables[0]['content'])
    assert 'Double these amounts' not in soup.get_text()


def test_wrapped_bold_heading_takes_its_final_word_back_from_regular_prose():
    """第二行加粗plastics.属于标题，后续India说明至technical advice保持完整正文。"""
    blocks = _public_first_page_blocks('review_68')
    heading = next(block for block in blocks if 'Regulated Storage, Manufacture and Use of' in _visible_text(block['content']))
    assert heading['type'] == 'paragraph_title'
    assert _visible_text(heading['content']).endswith('plastics.')
    prose = next(block for block in blocks if 'India required its states' in _visible_text(block['content']))
    assert prose is not heading and prose['type'] == 'text'
    assert 'technical advice of the Department of Science and' in _visible_text(prose['content'])
    assert not _visible_text(prose['content']).startswith('plastics.')


def test_short_open_heading_and_image_displaced_final_word_preserve_intro_before_charts():
    """普通字号独立题名与引导段分离；被图像挤到下方的末词format.归回正文并在两图前阅读。"""
    blocks = _public_first_page_blocks('review_107')
    heading = next(block for block in blocks if _visible_text(block['content']) == 'Print vs. Digital')
    assert heading['type'] == 'paragraph_title'
    prose = next(block for block in blocks if 'Why do some researchers' in _visible_text(block['content']))
    assert prose['type'] == 'text'
    assert _visible_text(prose['content']).endswith('preferences with each format.')
    assert len([block for block in blocks if block['type'] == 'image']) == 2
    assert prose['index'] < min(block['index'] for block in blocks if block['type'] == 'image')


def test_paragraph_final_italic_hyperlink_is_a_continuation_of_regular_native_prose():
    """同栏紧邻的斜体书名链接是未收句正文的段尾，不能因字体变体断开。"""
    blocks = _public_first_page_blocks('review_153')
    text_blocks = [block for block in blocks if block['type'] == 'text' and 'in Open Educational Resources' in _visible_text(block['content'])]
    assert len(text_blocks) == 1
    text = _visible_text(text_blocks[0]['content'])
    assert 'The findings were presented in Open Educational Resources' in text
    assert '2019.' in text


def test_framed_side_description_is_a_complete_caption_of_the_single_photo():
    """右侧Figure 6装饰卡片完整作为左图图注，不能输出成独立图片或遗漏说明。"""
    blocks = _public_first_page_blocks('review_73')
    images = [block for block in blocks if block['type'] == 'image']
    assert len(images) == 1
    captions = [child for child in images[0]['content'] if child.get('type') == 'image_caption']
    assert len(captions) == 1
    text = _visible_text(captions[0]['content'])
    for anchor in ['Figure 6', 'DPN Argentina', 'Content: World Health', 'Day Celebration', '(7 April 2021).']:
        assert text.count(anchor) == 1


def test_unlabelled_italic_caption_binds_to_the_photo_above_it():
    """紧贴照片的小字号斜体说明属于图注；与下方隔开的大段正文保持独立。"""
    blocks = _public_first_page_blocks('review_106')
    image = next(block for block in blocks if block['type'] == 'image')
    captions = [child for child in image['content'] if child.get('type') == 'image_caption']
    assert len(captions) == 1
    assert _visible_text(captions[0]['content']) == 'An example of a conceptual map created by one of our interviewees'
    assert any(block['type'] == 'text' and 'It seemed at times' in _visible_text(block['content']) for block in blocks)


def test_graph_caption_between_a_numeric_grid_and_a_plot_binds_to_the_plot():
    """表图之间的Graph图题与模板链接关联下方折线图，上方原生电子表格不认领该图注。"""
    blocks = _public_first_page_blocks('review_128')
    images = sorted([block for block in blocks if block['type'] == 'image'], key=lambda block: block['bbox'][1])
    assert len(images) == 1
    assert len([block for block in blocks if block['type'] == 'table']) == 1
    captions = [child for child in images[0]['content'] if child.get('type') == 'image_caption']
    assert len(captions) == 1
    assert 'Figure 13.3. Graph of Projection Estimates' in _visible_text(captions[0]['content'])
    assert 'Open Template in Microsoft Excel' in _visible_text(captions[0]['content'])


def test_adapted_table_source_and_dagger_explanation_bind_to_the_upper_table():
    """Table adapted来源及同段匕首说明是上表注释，不能流入两个问题或下表。"""
    blocks = _public_first_page_blocks('review_170')
    tables = sorted([block for block in blocks if block['type'] == 'table'], key=lambda block: block['bbox'][1])
    assert len(tables) == 2
    notes = [child for child in tables[0]['content'] if child.get('type') == 'table_footnote']
    assert len(notes) == 1
    text = _visible_text(notes[0]['content'])
    assert text.startswith('Table adapted from Jones et al. (1988) with permission.')
    assert 'Strip cropping uses a four-year rotation' in text
    assert text.endswith('Meadow includes alfalfa, clover, grass, etc.')
    assert 'How does the erosion rate' not in text


@pytest.mark.parametrize('scale,left', [(.7, 15), (1, 50), (1.6, 100)])
@pytest.mark.parametrize('kind', ['wrap', 'far', 'font', 'indent', 'chapter', 'paged', 'no_sidecar', 'wide'])
def test_wrapped_directory_labels_keep_their_page_after_only_a_matching_close_continuation(scale, left, kind):
    """目录续行必须同式、近距和同缩进；远行、章节、另一条页码行与宽正文不归并。"""
    from docvortex.analyzers.native.pdf.index_blocks import _IndexRow, _join_index_label_continuations
    from docvortex.analyzers.native.pdf.geometry import _bbox_union_many
    h = 10 * scale
    label = _directory_fixture_row('Connected directory label', left, 40 * scale, 150 * scale, h, 0)
    page = _directory_fixture_row('24', left + 220 * scale, 40 * scale, 15 * scale, h, 1, 'Page')
    first = _IndexRow(label.members + page.members, label.local_member_bboxes + page.local_member_bboxes,
                      _bbox_union_many([label.local_bbox, page.local_bbox]), 'Connected directory label 24', True)
    if kind == 'no_sidecar':
        first = _directory_fixture_row('Connected directory label 24', left, 40 * scale, 200 * scale, h, 0)
    continuation = _directory_fixture_row('Chapter II. Other section' if kind == 'chapter' else 'Next item 28' if kind == 'paged' else 'continued label',
                                          left + (30 * scale if kind == 'indent' else 0),
                                          (70 if kind == 'far' else 49) * scale,
                                          (230 if kind == 'wide' else 80) * scale, h, 2,
                                          'Other' if kind == 'font' else 'Body')
    result = _join_index_label_continuations([first, continuation])
    assert len(result) == (1 if kind == 'wrap' else 2)
    if kind == 'wrap':
        assert result[0].content == 'Connected directory label continued label 24'
        assert [member.source_index for member in result[0].members] == [0, 2, 1]


@pytest.mark.parametrize('scale,left', [(.7, 15), (1, 50), (1.6, 100)])
@pytest.mark.parametrize('kind', ['chapter', 'one_peer', 'font', 'indent', 'far', 'ordinary', 'paged'])
def test_leading_directory_chapter_requires_repeated_matching_internal_section_rows(scale, left, kind):
    """首个无页码章节只在内部至少两次同式章节与稳定目录已确认时归入，普通题名不能吞入。"""
    from docvortex.analyzers.native.pdf.index_blocks import _include_repeated_leading_index_section
    h = 10 * scale
    leading = _directory_fixture_row('Unrelated ordinary heading' if kind == 'ordinary' else 'Chapter I. Beginning 1' if kind == 'paged' else 'Chapter I. Beginning',
                                     left, 30 * scale, 180 * scale, h, 0,
                                     'Other' if kind == 'font' else 'Chapter')
    first = _directory_fixture_row('Section first 12', left, (100 if kind == 'far' else 60) * scale, 250 * scale, h, 1)
    second = _directory_fixture_row('Chapter II. Middle', left + (20 * scale if kind == 'indent' else 0), 100 * scale, 170 * scale, h, 2, 'Chapter')
    third = _directory_fixture_row('Chapter III. End', left + (20 * scale if kind == 'indent' else 0), 140 * scale, 170 * scale, h, 3, 'Chapter')
    candidate = [first, second] if kind == 'one_peer' else [first, second, third]
    result = _include_repeated_leading_index_section([leading, *candidate], candidate)
    assert (result[0] is leading) == (kind == 'chapter')


@pytest.mark.parametrize('scale,left', [(.7, 15), (1, 50), (1.6, 100)])
@pytest.mark.parametrize('kind', ['contents', 'tiny_number', 'no_heading', 'few', 'font', 'sequence', 'indent', 'far', 'sentence'])
def test_unpaged_index_requires_explicit_contents_and_short_consecutive_typographic_entries(scale, left, kind):
    """无页码目录需要明确题名和五条连续短项；步骤、正文收句、字号字体及缩进断层都拒绝。"""
    from docvortex.analyzers.native.pdf.index_blocks import _unpaged_numbered_index_bands
    h = 10 * scale
    heading = _directory_fixture_row('Instructions' if kind == 'no_heading' else 'Contents', left, 15 * scale, 110 * scale, 20 * scale, 0)
    rows = [heading]
    for index in range(3 if kind == 'few' else 5):
        number = 8 if index == 2 and kind == 'sequence' else index + 1
        row = _directory_fixture_row(f'{number}. Complete ordinary sentence.' if kind == 'sentence' else f'{number}. A useful short topic',
                                     left + (30 * scale if index == 2 and kind == 'indent' else 0),
                                     (120 if kind == 'far' else 60) * scale + index * 20 * scale, 200 * scale, h,
                                     index + 1, 'Other' if kind == 'font' and index == 2 else 'Body')
        rows.append(row)
        if kind == 'tiny_number' and index == 1:
            rows.append(_directory_fixture_row('6', left + 80 * scale, 94 * scale, 3 * scale, 3 * scale, 99, 'Tiny'))
    result = _unpaged_numbered_index_bands(rows)
    assert bool(result) == (kind in {'contents', 'tiny_number'})
    if result:
        assert len(result[0][1]) == 5 and result[0][0] is heading



def test_securities_report_analysis_keeps_five_native_paragraphs_and_short_advisory_boundaries():
    """原页五段核心分析保持独立，风险提示与投资建议不能被同字体正文吞并。"""
    blocks=_pages('securities_report')[0]
    anchors=('第三季度收入下滑 11%，销售费用率提升，盈利承压。', '存货大幅增长', '第三季度主品牌线下承压', '风险提示：', '投资建议：')
    matches=[next(block for block in blocks if anchor in _visible_text(block['content'])) for anchor in anchors]
    assert len({id(block) for block in matches})==5
    assert all(block['type']=='text' for block in matches)
    assert _visible_text(matches[3]['content']).endswith('渠道拓展不及预期。')
    assert _visible_text(matches[4]['content']).endswith('维持“优于大市”评级。')


def test_securities_related_reports_keep_independent_heading_and_five_complete_entries():
    """相关研究报告题名单独成标题；每份两行报告与日期保持一项，不能全部粘成正文。"""
    blocks=_pages('securities_report')[0]
    heading=[block for block in blocks if _visible_text(block['content'])=='相关研究报告']
    assert len(heading)==1 and heading[0]['type']=='paragraph_title'
    entries=[block for block in blocks if _visible_text(block['content']).startswith('《某服装企业')]
    assert len(entries)==5 and all(block['type']=='text' for block in entries)
    assert all(_visible_text(block['content']).count('《')==1 for block in entries)
    assert all('202' in _visible_text(block['content']).split('——')[-1] for block in entries)


def test_adjustment_section_keeps_native_heading_intro_and_four_numbered_paragraphs():
    """原页短题名、六行引言与四个顿号编号段分别完整，栅格表格仍保留为图片。"""
    blocks=_pages('securities_report')[2]
    heading=[block for block in blocks if _visible_text(block['content'])=='盈利预测调整说明']
    assert len(heading)==1 and heading[0]['type']=='paragraph_title'
    intro=[block for block in blocks if _visible_text(block['content']).startswith('预计 2024-2026')]
    assert len(intro)==1 and _visible_text(intro[0]['content']).endswith('调整细节如下：')
    items=[block for block in blocks if __import__('re').match(r'^[1-4]、', _visible_text(block['content']))]
    assert len(items)==4 and all(block['type']=='text' for block in items)
    assert [_visible_text(block['content'])[:2] for block in items]==['1、','2、','3、','4、']
    assert any(block['type']=='image' and block['bbox'][1]>.39 for block in blocks)


def test_securities_six_numbered_panels_follow_visual_rows_and_keep_caption_body_adjacency():
    """原页六幅成对图表按逐行编号阅读，每个完整图注紧邻自己唯一图体。"""
    blocks=_pages('securities_report')[1]
    captions=[block for block in blocks if block['type']=='caption']
    numbers=[int(__import__('re').match(r'^图(\d+)',_visible_text(block['content'])).group(1)) for block in captions]
    assert numbers==list(range(1,7))
    for caption in captions:
        at=next(index for index,block in enumerate(blocks) if block is caption)
        assert blocks[at+1]['type']=='image'
        assert abs(blocks[at+1]['bbox'][0]-caption['bbox'][0])<.03


def test_securities_basic_data_uses_complete_six_row_pair_table_and_keeps_its_heading():
    """基础数据为六行两列表，评级文字和各单位数字完整，不能整体作为公式。"""
    from bs4 import BeautifulSoup
    page=_pages('securities_report')[0]
    matches=[block for block in page if block['type']=='table' and '52周最高价' in __import__('re').sub(r'\s+','',_visible_text(block['content']))]
    assert len(matches)==1
    rows=BeautifulSoup(_visible_text(matches[0]['content']),'html.parser').find_all('tr')
    assert len(rows)==6 and all(len(row.find_all(['td','th']))==2 for row in rows)
    assert [__import__('re').sub(r'\s+','',''.join(row.stripped_strings)) for row in rows]==[
        '投资评级优于大市(维持)','合理估值6.50-7.00元','收盘价5.65元',
        '总市值/流通市值27136/27136百万元','52周最高价/最低价10.04/5.15元','近3个月日均成交额363.96百万元']
    assert not any(block['type']=='equation' and block['bbox'][0]>.6 and .28<block['bbox'][1]<.35 for block in page)
    assert any(_visible_text(block['content'])=='基础数据' for block in page)


def test_four_securities_financial_tables_include_each_year_header_and_do_not_merge_stacked_tables():
    """财务页四表按独立年份表头分开，六列含五个年份及所有数据，上下表不能纵向粘连。"""
    from bs4 import BeautifulSoup
    page=_pages('securities_report')[4]
    tables=[block for block in page if block['type']=='table']
    assert len(tables)==4
    titles=('资产负债表（百万元）','利润表（百万元）','现金流量表（百万元）','关键财务与估值指标')
    for title in titles:
        found=[block for block in tables if title in _visible_text(block['content'])]
        assert len(found)==1
        rows=BeautifulSoup(_visible_text(found[0]['content']),'html.parser').find_all('tr')
        assert len(rows)=={'资产负债表（百万元）':22,'利润表（百万元）':16,'现金流量表（百万元）':22,'关键财务与估值指标':16}[title]
        assert [cell.get_text(' ',strip=True) for cell in rows[0].find_all(['td','th'])]==[title,'2022','2023','2024E','2025E','2026E']
        assert all(len(row.find_all(['td','th']))==6 for row in rows)
        assert all(other==title or other not in _visible_text(found[0]['content']) for other in titles)
        if title=='现金流量表（百万元）':
            assert [cell.get_text(' ',strip=True) for cell in rows[-1].find_all(['td','th'])]==['权益自由现金流','3970','4862','4001','3616','3978']
        if title=='资产负债表（百万元）':
            assert any('短期借款及交易性金融负债'==__import__('re').sub(r'\s+','',row.find(['td','th']).get_text()) for row in rows)
    assert not any(block['type']=='text' and __import__('re').fullmatch(r'202[23456]E?',_visible_text(block['content'])) for block in page)
