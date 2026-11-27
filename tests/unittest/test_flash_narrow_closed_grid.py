"""用物理窄表及其外部正文检验候选生成和合并前的两个独立决定。"""

from types import SimpleNamespace

import pytest

from _flash_pdf_test_utils import _text_line
from docvortex.analyzers.native.pdf.geometry import _transform_axis_lines
from docvortex.analyzers.native.pdf.models import _AxisLine, _PageSource, _TableCandidate
from docvortex.analyzers.native.pdf.table_detection import _exclude_prose_spanning_rule_drafts
from docvortex.analyzers.native.pdf.table_rules import (
    _RuleCandidateDraft,
    _build_closed_rule_grid_candidates,
    _build_fragments,
    _cluster_fragment_rows,
)


@pytest.mark.parametrize("scale,left", [(1.0, 245.0), (0.7, 42.0)])
@pytest.mark.parametrize("kind", ["score_sheet", "no_interior", "one_occupied", "three_lines"])
def test_narrow_physical_grid_requires_real_tracks_and_occupied_columns(scale, left, kind):
    """不足长横线门槛的窄表，由重复短横线、长竖轨及至少两列源文字共同认定。"""

    def box(x0, y0, x1, y1):
        """平移并缩放局部布局以检测绝对尺度依赖。"""
        return ((left + x0) * scale, y0 * scale, (left + x1) * scale, y1 * scale)

    ys = [110.0 + 12 * i for i in range(12 if kind != "three_lines" else 3)]
    rules = [_AxisLine(box(0, y, 64, y), 0.4 * scale, "horizontal") for y in ys]
    for x in [0, 64] if kind == "no_interior" else [0, 32, 64]:
        rules.append(_AxisLine(box(x, ys[0], x, ys[-1]), 0.4 * scale, "vertical"))
    lines = [_text_line("Pages", box(2, 112, 23, 120), 0)]
    if kind != "one_occupied":
        lines.append(_text_line("Mark", box(40, 112, 60, 120), 1))
    for n, y in enumerate(ys[1:-1], 1):
        lines.append(_text_line(f"{2 * n}–{2 * n + 1}", box(4, y + 2, 23, y + 10), n + 1))
    size = (420 * scale, 300 * scale)
    em = 10 * scale
    axes = _transform_axis_lines(rules, size, 0)
    rows = _cluster_fragment_rows(_build_fragments(lines, size), em)
    found = _build_closed_rule_grid_candidates(rows, lines, size, 0, em, axes, [])
    if kind == "score_sheet":
        assert len(found) == 1
        assert found[0].core_bbox == pytest.approx(box(0, ys[0], 64, ys[-1]))
    else:
        assert not found


@pytest.mark.parametrize("control", ["adjacent_prose", "actual_cell", "outside_y", "only_labels"])
def test_enclosing_draft_yields_to_narrow_grid_only_with_external_prose(control):
    """仅正文实际位于小表旁、外表又无同高度独立列轨时排除弱大框；保留真实嵌套单元格。"""
    em = 10.0
    rules = [_AxisLine((10, 10, 300, 10), 0.4, "horizontal"), _AxisLine((10, 220, 300, 220), 0.4, "horizontal")]
    lower = 230 if control == "outside_y" else 60
    lines = [
        _text_line("A short descriptive paragraph ends here." if control != "only_labels" else "42", (12, y, 170, y + em), i)
        for i, y in enumerate([lower, lower + 18])
    ]
    if control == "actual_cell":
        rules.append(_AxisLine((10, 10, 10, 220), 0.4, "vertical"))
    source = _PageSource((310, 300), lines, [], rules)
    context = SimpleNamespace(median_height=em, angle=0)
    draft = _RuleCandidateDraft(context, rules[:2], [], None, [], False, 22)
    core = (220, 40, 290, 150)
    grid = _TableCandidate(core, core, 0, 122, core_bbox=core)
    accepted = _exclude_prose_spanning_rule_drafts(source, [draft, grid])
    assert grid in accepted
    assert (draft in accepted) == (control != "adjacent_prose")


def test_public_pdf_keeps_a_compact_score_table_separate_from_page_instructions():
    """通过公开解析检查评分栏的可编辑内容和表外说明，生成的 PDF 不依赖本机语料。"""
    from io import BytesIO
    from bs4 import BeautifulSoup
    from reportlab.pdfgen import canvas
    from docvortex import parse

    stream = BytesIO()
    draw = canvas.Canvas(stream, pagesize=(500, 650))
    draw.setFont("Helvetica", 12)
    draw.drawString(30, 610, "Candidate instructions")
    draw.line(25, 570, 480, 570)
    draw.line(25, 135, 480, 135)
    for i, sentence in enumerate(
        [
            "Read these directions before entering your answers.",
            "Question numbers refer to the corresponding booklet.",
            "Use one clearly marked page for each response.",
            "Complete the summary grid after writing your answers.",
        ]
    ):
        draw.drawString(26, 510 - i * 48, sentence)
    for y in range(315, 460, 12):
        draw.line(398, y, 470, y)
    for x in (398, 435, 470):
        draw.line(x, 315, x, 459)
    draw.setFont("Helvetica", 8)
    draw.drawString(402, 450, "Task")
    draw.drawString(438, 450, "Score")
    for row in range(1, 12):
        draw.drawString(402, 450 - 12 * row, str(row))
    draw.showPage()
    draw.save()
    result = parse(stream.getvalue(), file_suffix="pdf", parse_mode="txt")
    blocks = result.to_dict()["pages"][0]["blocks"]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1 and tables[0]["bbox"][0] > 0.75
    body = next(b for b in tables[0]["content"] if b["type"] == "table_body")
    rows = BeautifulSoup(body["content"], "html.parser").find_all("tr")
    assert len(rows) == 12 and rows[0].get_text(" ", strip=True) == "Task Score"
    assert all(label in rows[index].get_text(strip=True) for index, label in [(1, "1"), (11, "11")])
    visible = " ".join(str(b["content"]) for b in blocks if b["type"] == "text")
    assert "Question numbers" in visible and "summary grid" in visible


@pytest.mark.parametrize(
    "mode", ["values", "chinese_form", "striped_numbers", "color_key", "symbols", "axes_only", "form_total", "binary_color"]
)
def test_narrow_grid_preserves_information_carried_by_paint_or_symbols(mode):
    """保留真数值矩阵及空分值栏，拒绝仅用颜色、形状或坐标标签承载实质内容的窄格图。"""
    from docvortex.document.pdf.native_contracts import PDFPathInfo

    size = (450, 300)
    ys = [40 + 14 * i for i in range(8)]
    xs = [155, 185, 215]
    rules = [_AxisLine((xs[0], y, xs[-1], y), 0.3, "horizontal") for y in ys]
    rules.extend(_AxisLine((x, ys[0], x, ys[-1]), 0.3, "vertical") for x in xs)
    paths = []
    lines = []
    for row, top in enumerate(ys[:-1]):
        for col, left in enumerate(xs[:-1]):
            if row == 0:
                value = "" if mode == "axes_only" else ("项", "分")[col]
            elif mode in {"values", "striped_numbers"}:
                value = str((col + row) % 9)
            elif mode in {"chinese_form", "form_total"}:
                value = f"阶段{row}" if col == 0 else ""
            elif mode == "symbols":
                value = str(row) if col == 0 else "M"
            else:
                value = str(row) if col == 0 else ""
            if value:
                lines.append(_text_line(value, (left + 3, top + 2, left + 20, top + 11), len(lines)))
            if row and mode == "symbols" and col == 1:
                bbox = (left + 4, top + 1, left + 15, top + 10)
                paths.append(PDFPathInfo(bbox, 3, False, True, 0, len(paths)))
            if row and mode == "form_total" and (col == 1 or row == len(ys) - 2):
                bbox = (left, top, xs[col + 1], ys[row + 1])
                paths.append(PDFPathInfo(bbox, 5, True, False, 0, len(paths), (215, 215, 215, 255), (bbox,)))
            if row and mode == "binary_color" and col == 1 and row % 2:
                bbox = (left, top, xs[col + 1], ys[row + 1])
                paths.append(PDFPathInfo(bbox, 5, True, False, 0, len(paths), (190, 30, 80, 255), (bbox,)))
            if row and (mode == "striped_numbers" and row % 2 or mode == "color_key" and col == 1):
                color = (
                    (210, 210, 210, 255)
                    if mode == "striped_numbers"
                    else ((255, 75, 50, 255) if row % 2 else (70, 120, 190, 255))
                )
                bbox = (left, top, xs[col + 1], ys[row + 1])
                paths.append(PDFPathInfo(bbox, 5, True, False, 0, len(paths), color, (bbox,)))
    em = 10.0
    rows = _cluster_fragment_rows(_build_fragments(lines, size), em)
    candidates = _build_closed_rule_grid_candidates(
        rows, lines, size, 0, em, _transform_axis_lines(rules, size, 0), [], path_infos=paths
    )
    assert bool(candidates) == (mode in {"values", "chinese_form", "striped_numbers", "form_total"})
