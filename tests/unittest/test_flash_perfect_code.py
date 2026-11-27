"""校验原生 PDF 填充代码首行与框外编号的所有权。"""

from io import BytesIO

import pytest
from reportlab.lib import colors
from reportlab.pdfgen import canvas

from docvortex.api import parse, render
from docvortex.analyzers.native import PdfModel
from docvortex.document.pdf import PDFDocument
from docvortex.render.contracts import HtmlRenderOptions


def _numbered_panel_pdf(marker_x, rows, *, pitch=12):
    """生成上下两个填充框和独立编号，保留真实 PDF 字形与填充路径。"""
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(400, 420))
    pdf.setFillColor(colors.HexColor("#f8f8f8"))
    pdf.rect(62, 325, 276, 46, stroke=0, fill=1)
    pdf.rect(62, 156, 276, 136, stroke=0, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Courier", 10)
    pdf.drawString(68, 355, "int keep = 1;")
    pdf.drawString(68, 343, "// stable")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(40, 309, "Read the example below.")
    pdf.drawString(marker_x, 278, "2.")
    pdf.setFont("Courier", 10)
    for i, (indent, text) in enumerate(rows):
        if text:
            pdf.drawString(68 + indent * 6, 276 - i * pitch, text)
    pdf.showPage()
    pdf.save()
    return data.getvalue()


@pytest.mark.parametrize(("marker_x", "pitch"), [(49, 12), (51, 10)])
def test_background_code_keeps_external_counter_outside_complete_program(marker_x, pitch):
    """跨框首行应按源字形拆分，原代码、缩进、计数和相邻正文均保留。"""
    rows = [
        (0, "import pkg.io;"),
        (0, ""),
        (0, "class Reader {"),
        (2, "int count;"),
        (2, "int value() {"),
        (4, 'print("left    right");'),
        (4, "return count;"),
        (2, "}"),
        (0, "}"),
    ]
    source = _numbered_panel_pdf(marker_x, rows, pitch=pitch)
    with PDFDocument(source) as document:
        pages = PdfModel().predict(document)
    blocks = pages[0]
    code = [block["content"] for block in blocks if block["type"] == "code"]
    assert code == [
        "int keep = 1;\n// stable",
        "\n".join(" " * indent + text for indent, text in rows),
    ]
    assert sum(str(block.get("content")).count("import pkg.io;") for block in blocks) == 1
    assert any(block["type"] == "text" and "Read the example below." in str(block["content"]) for block in blocks)
    marker_position = next(i for i, block in enumerate(blocks) if block["type"] != "code" and "2." in str(block["content"]))
    program_position = next(
        i for i, block in enumerate(blocks) if block["type"] == "code" and "import pkg.io;" in block["content"]
    )
    assert marker_position < program_position
    result = parse(source, file_suffix="pdf", parse_mode="txt")
    output = render(
        result.middle_json, "html", assets=result.assets, options=HtmlRenderOptions(standalone=False)
    ).content.decode()
    assert output.count("<pre") == 2
    assert "    return count;" in output


def test_background_numbered_monospace_prose_keeps_its_existing_layout():
    """灰底有编号但缺少程序锚点的逐行说明不给首行拆分。"""
    lines = [
        (0, "Use the larger control knob"),
        (0, "then move toward the center"),
        (2, "wait for the panel to show green"),
        (0, "leave the other settings alone"),
    ]
    with PDFDocument(_numbered_panel_pdf(50, lines)) as pdf:
        pages = PdfModel().predict(pdf)
    assert [block["content"] for block in pages[0] if block["type"] == "code"] == ["int keep = 1;\n// stable"]
    assert any("2. Use the larger control knob" in str(block.get("content")) for block in pages[0])


_UNNUMBERED_ROWS = (
    (0, "struct Record {"),
    (4, "int size;"),
    (4, "int evaluate() {"),
    (8, "if (size) {"),
    (12, "return 1;"),
    (8, "}"),
    (4, "}"),
    (0, ""),
    (4, "void run() {"),
    (8, "if (size) {"),
    (12, "perform();"),
    (4, "}}"),
    (0, ""),
    (0, "}"),
)


def _unnumbered_brace_pdf(left, pitch):
    """在无编号的等宽灰框中同时打印独立闭括号和真实同行的连续闭括号。"""
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(340, 390))
    pdf.setFillColor(colors.HexColor("#f8f8f8"))
    pdf.rect(left, 150, 255, 167, stroke=0, fill=1)
    pdf.setFillColor(colors.black)
    pdf.setFont("Courier", 10)
    for i, (indent, text) in enumerate(_UNNUMBERED_ROWS):
        if text:
            pdf.drawString(left + 5 + 6 * indent, 300 - i * pitch, text)
    pdf.showPage()
    pdf.save()
    return data.getvalue()


@pytest.mark.parametrize(("left", "pitch"), [(41, 9.4), (59, 10.8)])
def test_unnumbered_code_only_splits_closing_braces_on_distinct_source_rows(left, pitch):
    """由原页逐行恢复未编号代码，保留真正同行的闭括号、缩进及空行。"""
    from bs4 import BeautifulSoup

    source = _unnumbered_brace_pdf(left, pitch)
    expected = "\n".join(" " * indent + text for indent, text in _UNNUMBERED_ROWS)
    with PDFDocument(source) as document:
        [page] = PdfModel().predict(document)
    assert [block["content"] for block in page if block["type"] == "code"] == [expected]
    result = parse(source, file_suffix="pdf", parse_mode="txt")
    artifact = render(result.middle_json, "html", assets=result.assets, options=HtmlRenderOptions(standalone=False))
    assert BeautifulSoup(artifact.content, "html.parser").select_one("pre code").get_text() == expected


def test_unnumbered_code_declines_partial_repair_when_other_source_rows_disagree(monkeypatch):
    """紧行文字也被投影合并时不按闭括号单一信号重写整个代码区域。"""
    from docvortex.analyzers.native.pdf import code_blocks

    source = _unnumbered_brace_pdf(41, 8.25)
    with PDFDocument(source) as document:
        actual = PdfModel().predict(document)
    assert any("}}      perform();" in block["content"] for block in actual[0] if block["type"] == "code")

    def retain_projection(content, *_):
        """临时关闭修复分支用于比较原投影。"""
        return content

    monkeypatch.setattr(code_blocks, "_restore_split_closing_braces", retain_projection)
    with PDFDocument(source) as document:
        assert PdfModel().predict(document) == actual
