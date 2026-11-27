"""验证公开 Flash 对年份、合计线、独立币符和表间正文的处理。"""

from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from pypdf.generic import TextStringObject
from reportlab.pdfgen.canvas import Canvas

from _tagged_pdf_fixtures import TaggedPdfBuilder, serialized
from docvortex import parse
from docvortex.analyzers.native.pdf import table_detection, table_materialization

_ROWS = (
    ("Service receipts", "$ 4,117", "$ 2,430"),
    ("Fixed charges", "(19,004)", "-8,298"),
    ("Other receipts", "4 487", "—"),
    ("Tax adjustment", "535.20", "370.01"),
    ("Balance due", "$ 16,006", "$ (15,149)"),
)


def _print_amount_tables(font, em, x, spacing, mode):
    """生成两张横线很少的真实 PDF 表，另在表间放会被错误跨表合并的正文。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(685, 680))
    document_height = 680

    def draw(y, column, text):
        """用源字体打印单格，币符和数值在会计对齐模式下独立绘制。"""
        if column == 0:
            canvas.drawString(x, document_height - y, text)
        elif text.startswith("$"):
            number = text[1:].lstrip()
            canvas.drawString(columns[column - 1] - 4.7 * em, document_height - y, "$")
            canvas.drawRightString(columns[column - 1], document_height - y, number)
        else:
            canvas.drawRightString(columns[column - 1], document_height - y, text)

    columns = (x + spacing, x + 1.5 * spacing)
    for table_index, top in enumerate((122.0, 410.0)):
        canvas.setFont(font, em)
        canvas.setLineWidth(0.35)
        row_step = 1.45 * em
        canvas.drawString(x, document_height - top + 0.95 * em, "Separate account summary")
        if mode == "vertical-grid":
            canvas.line(
                columns[0] - 5.5 * em,
                document_height - top + em,
                columns[0] - 5.5 * em,
                document_height - top - (len(_ROWS) + 1) * row_step,
            )
        labels = ("2021", "2020") if mode != "unrelated-years" else ("2021", "1980")
        for col, year in enumerate(labels, start=1):
            draw(top, col, year)
        for index, row in enumerate(_ROWS, start=1):
            y = top + row_step * index
            if mode == "extra-left-track":
                canvas.drawRightString(columns[0] - 0.4 * spacing, document_height - y, "824.5")
            if index == len(_ROWS):
                if mode != "no-subtotal-rule":
                    canvas.line(x, document_height - y + em, columns[-1] + em, document_height - y + em)
                canvas.line(x, document_height - y - 0.45 * em, columns[-1] + em, document_height - y - 0.45 * em)
            for col, value in enumerate(row):
                if table_index and index == 1 and col == 0:
                    value = "Maintenance receipts"
                if mode == "drift" and index == 2 and col == 1:
                    canvas.drawRightString(columns[0] - 1.5 * em, document_height - y, value)
                else:
                    draw(y, col, value)
    canvas.setFont(font, em)
    for index, text in enumerate(
        (
            "The office usually sends an update when each term ends and the balances can be confirmed.",
            "The next schedule is based on a separate reporting unit with its own accounting controls.",
        )
    ):
        canvas.drawString(x, 405 - index * 1.5 * em, text)
    canvas.drawString(x, 35, "These measures summarize the remaining balances for each reporting line.")
    canvas.drawString(24, 27, "1 8")
    canvas.showPage()
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("font,em,left,spacing", [("Helvetica", 10.0, 61.0, 245.0), ("Times-Roman", 12.0, 82.0, 274.0)])
def test_period_amount_tables_keep_periods_and_currency_in_separate_grids(font, em, left, spacing, monkeypatch):
    """公共表输出逐格验证两张表，并核对求解器唯一采用了全部源字。"""
    confirmed = []
    original = table_materialization._is_verified_table_region

    def capture(table_input, body, rows, cols, expected_sources, body_band=None):
        """在既有认领校验中读取真实字形归属和完成情况。"""
        passed = original(table_input, body, rows, cols, expected_sources, body_band)
        if passed:
            confirmed.append((body, expected_sources))
        return passed

    monkeypatch.setattr(table_materialization, "_is_verified_table_region", capture)
    data = _print_amount_tables(font, em, left, spacing, "table")
    page = parse(data, file_suffix="pdf", parse_mode="txt", pdf_structure="auto").to_dict()["pages"][0]
    tables = [block for block in page["blocks"] if block["type"] == "table"]
    assert len(tables) == len(confirmed) == 2
    assert tables[0]["bbox"][3] < tables[1]["bbox"][1]
    heading_indices = [
        block["index"]
        for block in page["blocks"]
        if block["type"] != "table" and "Separate account summary" in str(block["content"])
    ]
    assert heading_indices[0] < tables[0]["index"] < heading_indices[-1] < tables[1]["index"]
    assert confirmed[0][1].isdisjoint(confirmed[1][1])
    for index, table in enumerate(tables):
        [body] = [item["content"] for item in table["content"] if item["type"] == "table_body"]
        rows = BeautifulSoup(body, "html.parser").select("tr")
        actual = [tuple(cell.get_text().strip() for cell in row.select("td")) for row in rows]
        expected = list(_ROWS)
        if index:
            expected[0] = ("Maintenance receipts", *_ROWS[0][1:])
        assert actual == [("", "2021", "2020"), *expected]
        parsed, sources = confirmed[index]
        assigned = [i for cell in parsed.cells for i in cell.source_char_indices]
        assert len(assigned) == len(sources)
        assert set(assigned) == sources
    prose = str([block for block in page["blocks"] if block["type"] != "table"])
    assert "each term ends" in prose
    assert "separate reporting unit" in prose
    assert page["blocks"][-1]["type"] == "text"
    assert "1 8" in str(page["blocks"][-1]["content"])


@pytest.mark.parametrize("mode", ["unrelated-years", "no-subtotal-rule", "drift", "extra-left-track", "vertical-grid"])
def test_period_amount_tables_require_years_stable_tracks_and_subtotal_rules(mode, monkeypatch):
    """单独改变年份、合计实体线或金额对齐时，公开路径不能新增表区认领。"""
    proposals = []
    original = table_detection._detect_period_amount_tables

    def capture(source, excluded, existing):
        """只记录新增规则的提名，其他候选沿原路径参加。"""
        result = original(source, excluded, existing)
        proposals.extend(result)
        return result

    monkeypatch.setattr(table_detection, "_detect_period_amount_tables", capture)
    data = _print_amount_tables("Helvetica", 10.0, 61.0, 245.0, mode)
    parse(data, file_suffix="pdf", parse_mode="txt", pdf_structure="auto")
    assert proposals == []


def test_period_amount_table_preserves_preceding_partial_structure_title():
    """带标签题名和残余金额表共享少量框边时，标题仍独立并先于表格。"""
    builder = TaggedPdfBuilder(2)
    heading = builder.text("Budget note", page=1, x=28, y=324)
    context = builder.text("Accounting disclosures", page=0)

    def draw(x, y, value):
        """只绘制原生可见表格字符，以确保表体实际走残余分析。"""
        builder.commands[1].extend(f"BT /F1 12 Tf {x} {y} Td ".encode() + serialized(TextStringObject(value)) + b" Tj ET\n")

    for x, heading_year in [(157, "2021"), (225, "2020")]:
        draw(x, 310, heading_year)
    for i, (a, b) in enumerate(zip(("321", "422", "551", "257", "155"), ("211", "119", "140", "217", "172"))):
        draw(28, 293 - 17 * i, f"Position {i + 1}")
        draw(163.66, 293 - 17 * i, a)
        draw(231.66, 293 - 17 * i, b)
    builder.commands[1].extend(b"0.35 w 28 236 m 264 236 l S 28 221 m 264 221 l S\n")
    source = builder.finish([builder.node("P", [context]), builder.node("P", [heading], page=1)])
    result = parse(source, file_suffix="pdf", parse_mode="txt", pdf_structure="auto").to_dict()
    heading_block, table = result["pages"][1]["blocks"]
    assert "Budget note" in str(heading_block["content"])
    assert table["type"] == "table"
    assert heading_block["bbox"][3] <= table["bbox"][1]
    [html] = [item["content"] for item in table["content"] if item["type"] == "table_body"]
    rows = BeautifulSoup(html, "html.parser").select("tr")
    assert [cell.get_text() for cell in rows[0].select("td")] == ["", "2021", "2020"]
