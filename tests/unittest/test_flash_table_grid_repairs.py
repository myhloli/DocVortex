"""用完整原件和独立几何变体锁定募投网格及单列有字的跨页续表修复。"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex import load_bundle, parse
from docvortex.analyzers.native.pdf import pipeline
from docvortex.analyzers.native.pdf.graphics import _detect_native_bar_graphics
from docvortex.document.pdf import PDFDocument
from docvortex.analyzers.native.pdf.table_rules import _native_multicolumn_grid_bboxes

FIXTURES = Path(__file__).parent / "pdfs/native_pdf_tables"


@lru_cache(maxsize=None)
def _parsed(name):
    """公开解析完整原件，保留真实上下页上下文及 ModelJson。"""
    return parse(FIXTURES / (name + ".pdf"), keep_model_json=True)


def _table_rows(block):
    """读取实际输出 HTML 单元格，断言期望由原页抄录而非生成。"""
    return BeautifulSoup(block["content"], "html.parser").find_all("tr")


def test_fundraising_grid_keeps_complete_cells_and_merged_rows():
    """原页是十四行十一列表；项目六的折行金额不可落入独立图片或正文。"""
    result = _parsed("annual_report_fundraising_projects_table")
    page = result.model_json.pages[0]
    tables = [block for block in page if block["type"] == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert table["bbox"][1] < 0.087 and table["bbox"][3] > 0.908
    rows = _table_rows(table)
    assert len(rows) == 14
    assert all(sum(int(cell.get("colspan", 1)) for cell in row.find_all("td")) == 11 for row in rows)
    assert [int(cell["colspan"]) for row in rows for cell in row.find_all("td", colspan=True)] == [11, 11, 10]
    project = next(row for row in rows if "翔兆" in row.get_text())
    assert [cell.get_text("", strip=True) for cell in project.find_all("td")] == [
        "6、收购翔兆科技100%股权项目",
        "否",
        "-",
        "15,686",
        "1,568.6",
        "12,548.8",
        "80.00%",
        "不适用",
        "3,207.97",
        "不适用",
        "否",
    ]
    assert all(block is table or block["bbox"][3] <= table["bbox"][1] or block["bbox"][1] >= table["bbox"][3] for block in page)
    assert len([b for b in result.to_dict()["pages"][0]["blocks"] if b["type"] == "table"]) == 1


def test_service_continuation_keeps_empty_cells_and_excludes_following_prose():
    """第4页闭合三列续表只有右列有字，合同期限处于表格下方并独立输出。"""
    result = _parsed("procurement_document_blank_page_tables")
    page = result.model_json.pages[3]
    tables = [block for block in page if block["type"] == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert table["bbox"][0] < 0.148 and table["bbox"][2] > 0.852
    assert table["bbox"][1] < 0.095 and table["bbox"][3] < 0.221
    rows = _table_rows(table)
    assert len(rows) == 1
    cells = [cell.get_text("", strip=True) for cell in rows[0].find_all("td")]
    assert cells == [
        "",
        "",
        "物，做好出库单及出库表登记，保证采购方产品出入安全，每月邮寄出库单与采购方进行对账，做到账实相符；5.为采购方配送机供品到指定地点（即从物流送货收货、仓库上货、到指定地点卸货及跟单交接签收等工作）。6.如遇需走空运的特殊情况，需安排人员到机场提货。",
    ]
    following = [b for b in page if b["type"] == "text" and "1.7 合同期限" in str(b["content"])]
    assert len(following) == 1 and following[0]["bbox"][1] >= table["bbox"][3]
    assert all(b is table or b["bbox"][1] >= table["bbox"][3] for b in page)
    assert len([b for b in result.to_dict()["pages"][3]["blocks"] if b["type"] == "table"]) == 1


def _continuation_pdf(left, widths, font_size, kind):
    """独立改变位置、列宽和字号；相近负例仅撤销一项真实续表证据。"""
    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(650, 800))

    def draw_grid(top, bottom, columns, header=False):
        """绘制原生闭合网格；前页三列均有字，当前页只在最后一列写续文。"""
        canvas.setFont("Helvetica", font_size)
        for y in (top, bottom):
            canvas.line(columns[0], y, columns[-1], y)
        for x in columns:
            canvas.line(x, bottom, x, top)
        if header:
            canvas.line(columns[0], top - 30, columns[-1], top - 30)
            for index, title in enumerate(("Service", "Place", "Requirement")):
                canvas.drawString(columns[index] + 4, top - 20, title)
                canvas.drawString(columns[index] + 4, bottom + 20, ("Shipping", "Terminal", "4. Handle goods")[index])
        else:
            canvas.drawString(columns[-2] + 4, top - 25, "5. Deliver goods to the agreed place.")
            canvas.drawString(columns[-2] + 4, top - 50, "6. Collect urgent goods at the airport.")

    columns = [left]
    for width in widths:
        columns.append(columns[-1] + width)
    if kind != "isolated":
        draw_grid(170, 50, columns, header=True)
        if kind == "text_after_previous":
            canvas.drawString(left, 25, "Independent paragraph after the table.")
        canvas.showPage()
    if kind == "skipped_page":
        canvas.showPage()
    current = columns.copy()
    if kind == "shifted_track":
        current[1] += 15
    if kind == "different_width":
        current[-1] -= 30
    if kind == "text_before":
        canvas.setFont("Helvetica", font_size)
        canvas.drawString(left, 705, "Independent paragraph before the grid.")
    if kind == "image_before":
        from PIL import Image
        from reportlab.lib.utils import ImageReader

        canvas.drawImage(ImageReader(Image.new("RGB", (80, 35), "navy")), left, 700, width=80, height=35)
    draw_grid(660, 570, current)
    canvas.setFont("Helvetica", font_size)
    canvas.drawString(left, 540, "Following paragraph stays outside.")
    canvas.save()
    return payload.getvalue()


@pytest.mark.parametrize("left,widths,font_size", [(40, (70, 90, 280), 9), (95, (50, 75, 300), 9), (50, (95, 80, 260), 12)])
@pytest.mark.parametrize(
    "kind",
    [
        "continued",
        "shifted_track",
        "different_width",
        "text_before",
        "text_after_previous",
        "image_before",
        "skipped_page",
        "isolated",
    ],
)
def test_single_occupied_column_requires_adjacent_matching_terminal_table(left, widths, font_size, kind):
    """孤立网格、插页、正文屏障、图片屏障和任一列轨错位均不能冒充跨页续表。"""
    result = parse(_continuation_pdf(left, widths, font_size, kind), file_suffix="pdf", keep_model_json=True, parse_mode="txt")
    page = result.model_json.pages[-1]
    tables = [b for b in page if b["type"] == "table"]
    assert bool(tables) == (kind == "continued")
    if tables:
        cells = _table_rows(tables[0])[0].find_all("td")
        assert len(cells) == 3
        assert [cell.get_text(strip=True) for cell in cells[:2]] == ["", ""]
        assert "5. Deliver goods" in cells[2].get_text() and "6. Collect urgent" in cells[2].get_text()
        assert "Following paragraph" not in tables[0]["content"]


def _shaded_grid_source(left, widths, font_size, color, kind):
    """用分散绘制的不同列宽底色模拟错误柱组，独立网格及 Form 提供混淆反例。"""
    payload = BytesIO()
    canvas = Canvas(payload, pagesize=(650, 800))
    canvas.setFont("Helvetica", font_size)
    columns = [left]
    for width in widths:
        columns.append(columns[-1] + width)
    for row in range(4):
        y = 560 - row * 35
        for column, width in enumerate(widths):
            canvas.setFillColorRGB(*color)
            canvas.rect(columns[column], y, width, 22, stroke=0, fill=1)
            canvas.setFillColorRGB(0, 0, 0)
            canvas.drawString(columns[column] + 8, y + 5, str((row + 2) * (column + 3)))
    if kind != "no_grid":
        # 宽列的短矩形只是单元格底色，表格实体仍由完整外轨和反复内列线证明。
        for y in (590, 555, 520, 485, 450):
            canvas.line(columns[0], y, columns[-1], y)
        for x in columns:
            canvas.line(x, 450, x, 590)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
    if kind == "form":
        source.form_bboxes = [(left - 5, 205, columns[-1] + 5, 355)]
    return source


@pytest.mark.parametrize(
    "left,widths,font_size,color",
    [
        (40, (60, 130, 85), 9, (0.3, 0.5, 0.8)),
        (95, (80, 160, 70), 9, (0.7, 0.2, 0.1)),
        (50, (100, 190, 80), 12, (0.1, 0.6, 0.3)),
    ],
)
@pytest.mark.parametrize("kind", ["grid", "no_grid", "form", "missing_outer", "missing_columns"])
def test_repeated_native_cell_grid_overrides_partial_bar_groups(left, widths, font_size, color, kind):
    """完整有字网格优先于局部柱组；缺网格及 Form 容器继续保留原有图形路径。"""
    source = _shaded_grid_source(left, widths, font_size, color, kind)
    if kind == "missing_outer":
        source.drawing_lines = [r for r in source.drawing_lines if r.orientation != "vertical" or abs(r.bbox[0] - left) > 1]
    elif kind == "missing_columns":
        source.drawing_lines = [
            r
            for r in source.drawing_lines
            if r.orientation != "vertical" or abs(r.bbox[0] - left) < 1 or abs(r.bbox[0] - (left + sum(widths))) < 1
        ]
    detected = _detect_native_bar_graphics(source, font_size)
    assert bool(detected) == (kind != "grid")


def test_graphic_inside_one_cell_is_not_overridden_by_surrounding_table():
    """表格内单格图形没有跨列证据，不可因外侧存在完整表格就撤销图形归属。"""
    source = _shaded_grid_source(40, (180, 80, 100), 9, (0.3, 0.5, 0.8), "grid")
    assert _native_multicolumn_grid_bboxes(source, 9, (45, 210, 200, 350)) == []
    assert _native_multicolumn_grid_bboxes(source, 9, (45, 215, 390, 230)) == []


@pytest.mark.parametrize("name", sorted(["annual_report_fundraising_projects_table", "procurement_document_blank_page_tables"]))
def test_repaired_tables_survive_public_html_markdown_and_bundle(name, tmp_path):
    """公开导出保持完整单元格、可读取图片及包回读一致性，避免内部正确但导出再次拆坏。"""
    result = _parsed(name)
    result.export(tmp_path / "document.html", output_format="html")
    result.export(tmp_path / "document.md", output_format="markdown")
    result.save_bundle(tmp_path / "bundle")
    assert load_bundle(tmp_path / "bundle").to_dict() == result.to_dict()
    html = (tmp_path / "document.html").read_text(encoding="utf-8")
    markdown = (tmp_path / "document.md").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("table")
    for image in soup.find_all("img", src=True):
        if not image["src"].startswith("data:"):
            assert (tmp_path / image["src"]).is_file()
    if name.startswith("annual"):
        project_rows = [row for row in soup.find_all("tr") if "6、收购翔兆" in row.get_text()]
        assert len(project_rows) == 1
        # 3,207.97 在原表小计及合计中还出现两次；这里只校验项目六对应的单元格唯一归属。
        project_text = project_rows[0].get_text()
        assert project_text.count("12,548.8") == 1 and project_text.count("3,207.97") == 1
        assert "12,548.8" in markdown and "3,207.97" in markdown
    else:
        assert soup.get_text().count("6.如遇需走空运") == 1
        assert "6.如遇需走空运" in markdown and "1.7 合同期限" in markdown
