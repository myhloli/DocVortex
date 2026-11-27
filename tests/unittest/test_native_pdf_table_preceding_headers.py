"""验证表前横隔线两侧的完整列名、题注和独立正文格。"""

from __future__ import annotations

from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf import pipeline, table_materialization
from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_to_upright
from docvortex.analyzers.native.pdf.table_rules import _numbered_rule_captions

_BODY = (
    ("North", "Checked", "12.80", "1.20", "3.10"),
    ("Ridge", "Verified", "14.07", "2.25", "4.85"),
    ("South", "Checked", "15.03", "3.50", "-1.25"),
    ("Coast", "Verified", "16.55", "4.70", "5.80"),
)
_LABELS = ("Site", "Status", "Rise", "Drop", "Result")
_LAYERED_MODES = {"layered", "no-partial", "misplaced-partial"}


def _source_pdf(mode: str, font: str, em: float, width: float, angle: int = 0) -> bytes:
    """把正文和漏掉的框外表头印到真实 PDF，独立改变字体、字号、栏宽和方向。"""
    stream = BytesIO()
    pdf = Canvas(stream, pagesize=(560, 560))
    if angle:
        pdf.translate(280, 280)
        pdf.rotate(angle)
        pdf.translate(-280, -280)
    left, rail = 90.0, 360.0
    edges = [left + width * part for part in (0, 0.19, 0.43, 0.61, 0.83, 1)]
    centers = [(a + b) / 2 for a, b in zip(edges, edges[1:])]
    floor = rail - len(_BODY) * 1.8 * em
    pdf.setLineWidth(0.4)
    for i in range(len(_BODY) + 1):
        if i or mode != "no-rail":
            pdf.line(left, rail - i * 1.8 * em, edges[-1], rail - i * 1.8 * em)
    for x in edges:
        pdf.line(x, floor, x, rail)
    pdf.setFont(font, em)
    caption = "Table 1. Measurements from field stations"
    pdf.drawString(left, rail + (4.3 if mode in _LAYERED_MODES else 2.9) * em, caption)

    if mode in _LAYERED_MODES:
        pdf.drawCentredString((edges[2] + edges[-1]) / 2, rail + 1.65 * em, "Intervals")
        if mode != "no-partial":
            start = edges[2] if mode != "misplaced-partial" else centers[2]
            pdf.line(start, rail + 1.30 * em, edges[-1], rail + 1.30 * em)
        labels = ("Site", "Status", "1", "3", "7")
    elif mode == "prose":
        labels = ()
        pdf.drawString(left, rail + 0.35 * em, "Several sites can be inspected at irregular intervals.")
    elif mode == "caption":
        labels = ()
        pdf.drawString(left, rail + 0.35 * em, "Table 2. Comparative results for the supervised fields")
    elif mode == "numeric":
        labels = ("North", "Checked", "12.99", "8.24", "3.03")
    elif mode == "complete":
        labels = ()
    else:
        labels = _LABELS

    for x, title in zip(centers, labels):
        if mode == "joined-caption" and title == _LABELS[0]:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            pdf.setFont("STSong-Light", em)
            pdf.drawCentredString(x, rail + 0.35 * em, "表3观测")
            pdf.setFont(font, em)
            continue
        pdf.drawCentredString(x, rail + 0.35 * em, title)
    for i, values in enumerate(_BODY):
        y = rail - (1.1 + i * 1.8) * em
        for col, (x, value) in enumerate(zip(centers, values, strict=True)):
            if mode == "styled" and i == 3 and col == 2:
                text = pdf.beginText(x - 0.6 * em, y)
                text.textOut(value)
                text.setFont(font, 0.62 * em)
                text.setRise(0.38 * em)
                text.textOut("2")
                pdf.drawText(text)
            else:
                pdf.drawCentredString(x, y, value.replace(".", "") if mode in _LAYERED_MODES and col >= 2 else value)
    pdf.drawString(left, floor - 2.4 * em, "Text resumes after the table.")
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _table_contents(source: bytes) -> tuple[str, str]:
    """从公开 MiddleJson 中读取唯一表格的 HTML 和关联题注。"""
    page = parse(source, file_suffix="pdf", parse_mode="txt", pdf_structure="auto").to_dict()["pages"][0]
    [table] = [block for block in page["blocks"] if block["type"] == "table"]
    bodies = [item["content"] for item in table["content"] if item["type"] == "table_body"]
    captions = [line["content"] for item in table["content"] if item["type"] == "table_caption" for line in item["content"]]
    return bodies[0], " ".join(captions)


@pytest.mark.parametrize(
    "mode,font,em,width,angle",
    [
        ("flat", "Helvetica", 9.0, 260.0, 0),
        ("flat", "Times-Roman", 12.0, 300.0, 0),
        ("flat", "Helvetica", 9.5, 290.0, 90),
        ("layered", "Times-Roman", 10.0, 270.0, 0),
        ("styled", "Helvetica", 11.0, 300.0, 0),
    ],
)
def test_preceding_printed_header_moves_full_source_lines(mode, font, em, width, angle, monkeypatch):
    """公开格值、跨列、正文 HTML 字节及题注行须与源布局一致。"""
    calls = []
    rendered_bodies = []
    original = table_materialization.recover_preceding_table_header
    solve_body = table_materialization.recover_table_result

    def capture(source, table_input, body, protected_lines, obstacles):
        """记录真实运行中已恢复的表头来源、正文和边界。"""
        recovered = original(source, table_input, body, protected_lines, obstacles)
        if recovered:
            line_chars = {
                char["char_idx"]
                for line in source.lines
                if line.source_index in recovered.line_indices
                for char in line.chars
                if char["char"].strip()
            }
            calls.append((recovered, body, line_chars))
        return recovered

    def capture_rendered_body(*args, **kwargs):
        """保留首个表头判定前、正文经上下标和字距渲染后的精确字节。"""
        result = solve_body(*args, **kwargs)
        if result:
            rendered_bodies.append(result[0])
        return result

    monkeypatch.setattr(table_materialization, "recover_preceding_table_header", capture)
    monkeypatch.setattr(table_materialization, "recover_table_result", capture_rendered_body)
    html, caption = _table_contents(_source_pdf(mode, font, em, width, angle))
    [record] = calls
    header, body, line_chars = record
    rows = BeautifulSoup(html, "html.parser").select("tr")
    values = [tuple(td.get_text(strip=True) for td in row.select("td")) for row in rows]
    if mode == "layered":
        assert values[:2] == [("", "", "Intervals"), ("Site", "Status", "1", "3", "7")]
        assert rows[0].select("td")[-1]["colspan"] == "3"
    else:
        assert values[:1] == [_LABELS]
    expected_body = list(_BODY)
    if mode == "layered":
        expected_body = [row[:2] + tuple(value.replace(".", "") for value in row[2:]) for row in expected_body]
    elif mode == "styled":
        expected_body[3] = (*_BODY[3][:2], _BODY[3][2] + "2", *_BODY[3][3:])
        assert BeautifulSoup(html, "html.parser").find("sup").get_text() == "2"
    actual_body = [tuple(value.replace(" ", "") for value in row) for row in values[header.result.rows :]]
    assert actual_body == expected_body
    [rendered_body] = rendered_bodies
    assert html.endswith(rendered_body.removeprefix("<table><tbody>"))
    assert caption == "Table 1. Measurements from field stations"
    ids = [i for cell in header.result.cells for i in cell.source_char_indices]
    assert len(ids) == len(set(ids))
    assert set(ids).isdisjoint(glyph.source_index for glyph in body.text.glyphs)
    assert set(ids) == line_chars


@pytest.mark.parametrize("mode", ["prose", "caption", "numeric", "no-rail", "complete", "no-partial", "misplaced-partial"])
def test_preceding_material_requires_label_and_physical_rule_evidence(mode, monkeypatch):
    """真实题注和连续正文、数字记录、错位或缺少实体隔线均不会转给表体。"""
    original = table_materialization.recover_preceding_table_header
    attempted = []

    def capture(source, table_input, body, protected_lines, obstacles):
        """即使上游框已包含内容，也检查每次外区扩张都不会误判。"""
        decision = original(source, table_input, body, protected_lines, obstacles)
        attempted.append(decision)
        return decision

    monkeypatch.setattr(table_materialization, "recover_preceding_table_header", capture)
    parse(_source_pdf(mode, "Helvetica", 10.0, 270.0), file_suffix="pdf", parse_mode="txt", pdf_structure="auto")
    assert attempted and all(decision is None for decision in attempted)


def test_preceding_labels_cannot_take_confirmed_joined_caption_line(monkeypatch):
    """保留检测器已认领的紧连中文表号首行，即使同基线的其他文本吻合表列。"""
    materialize = pipeline._materialize_table_blocks
    marked = []

    def with_detected_caption(source, candidates, **kwargs):
        """用真实源行和已有编号判定构造确有归属的题注边界。"""
        [caption_box] = [box for box, number in _numbered_rule_captions(source.lines, [], source.page_size, 0) if number == "3"]
        [candidate] = candidates
        assert caption_box == _rotate_bbox_to_upright(
            next(line.bbox for line in source.lines if line.text == "表3观测"),
            source.page_size,
            candidate.angle,
        )
        marked.append(caption_box)
        kwargs["caption_owners"][id(candidate)] = [(caption_box, True)]
        return materialize(source, candidates, **kwargs)

    monkeypatch.setattr(pipeline, "_materialize_table_blocks", with_detected_caption)
    html, _ = _table_contents(_source_pdf("joined-caption", "Helvetica", 10.0, 270.0))
    assert len(marked) == 1
    assert "表3观测" not in html
    assert len(BeautifulSoup(html, "html.parser").select("tr")) == len(_BODY)
