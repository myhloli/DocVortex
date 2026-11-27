"""验证 PDF 的粗体段首在准确的物理段界内保留。"""

from io import BytesIO

from bs4 import BeautifulSoup
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.analyzers.native.pdf.text_assembly.rows import _leading_typography_reset_break_sources
from docvortex.api import parse, render
from docvortex.render.contracts import HtmlRenderOptions


def _runin_pdf(*, paragraph_gap):
    """以真实 Times 字形生成正常续行和带粗体标签的新段。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(370, 380))
    canvas.setFont("Times-Roman", 9)
    canvas.drawString(45, 258, "A short summary places the model within the evaluation pipeline and")
    canvas.drawString(45, 246, "the second pass tracks changes across the complete set of observations.")
    canvas.drawString(45, 234, "the results stayed close across all evaluation runs.")
    top = 222 - paragraph_gap
    title = "Compact Spatial Feature Extraction."
    canvas.setFont("Times-Bold", 9)
    canvas.drawString(45, top, title)
    canvas.setFont("Times-Roman", 9)
    offset = stringWidth(title, "Times-Bold", 9) + 4
    canvas.drawString(45 + offset, top, "The model returns a stable report.")
    canvas.drawString(45, top - 12, "Additional tests preserve enough details to compare nearby results.")
    canvas.drawString(45, top - 24, "Other notes cover the influence of variation across several sources.")
    canvas.showPage()
    canvas.save()
    return stream.getvalue()


def test_public_runin_paragraph_preserves_bold_and_body():
    """公开 HTML 的粗体提示保留为其正文段的第一个成员。"""
    result = parse(_runin_pdf(paragraph_gap=1.2), file_suffix="pdf", parse_mode="txt")
    output = render(result.middle_json, "html", options=HtmlRenderOptions(standalone=False), assets=result.assets).content
    soup = BeautifulSoup(output, "html.parser")
    runin = next(paragraph for paragraph in soup.select("p") if "Compact Spatial Feature" in paragraph.get_text())
    assert runin.select_one("strong").get_text() == "Compact Spatial Feature Extraction."
    assert "The model returns a stable report." in runin.get_text()
    assert "Additional tests preserve enough details" in runin.get_text()
    assert runin.get_text().startswith("Compact Spatial Feature Extraction.")


def test_runin_geometry_does_not_create_break_for_formula_or_bold_inside_row():
    """合成真实边缘字体信号，验证旧公式上下文和行中强调仍留在已有段内。"""
    regular = ("Times-Roman", 0)
    bold = ("Times-Bold", 0)
    first = _LineItem(
        "Earlier observations give comparable results.",
        (20, 20, 188, 29),
        0,
        1,
        effective_height=9,
        dominant_font_weight=400,
        paragraph_terminal=True,
        edge_fonts=(regular, regular),
    )
    lead = _LineItem(
        "Compact Spatial Feature Extraction. The model returns a stable report.",
        (20, 33.1, 250, 42.1),
        0,
        2,
        effective_height=9.0,
        dominant_font_weight=700,
        leading_emphasis_width=116,
        edge_fonts=(bold, regular),
    )
    tail = _LineItem(
        "Additional tests preserve enough details to compare nearby results.",
        (20, 45.1, 245, 54.1),
        0,
        3,
        effective_height=9.0,
        dominant_font_weight=400,
        edge_fonts=(regular, regular),
    )
    lane = _TextLane(20, 250)

    def boundary():
        """以同样的物理行输入段界判断。"""
        lane.lines = [(line, line.bbox) for line in (first, lead, tail)]
        return lead.source_index in _leading_typography_reset_break_sources(lane, 3.0, 0.0)

    assert boundary()
    lead.bbox = (20, 32, 250, 41)
    tail.bbox = (20, 44, 245, 53)
    assert not boundary()
    lead.bbox = (20, 33.1, 250, 42.1)
    tail.bbox = (20, 45.1, 245, 54.1)
    lead.paragraph_formula_context = True
    assert not boundary()
    lead.paragraph_formula_context = False
    lead.leading_emphasis_width = None
    lead.edge_fonts = (regular, regular)
    assert not boundary()
