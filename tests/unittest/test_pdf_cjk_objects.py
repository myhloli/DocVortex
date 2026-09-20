"""验证 CJK 非文本片段的移行、基线、宽度恢复与真实 PDF 输出。"""

from copy import deepcopy
from io import BytesIO

import pytest
from pypdf import PdfReader
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as rl_paragraph

from docvortex.render import PdfLayout, render_pdf
from docvortex.render._internal.pdf.diagnostics import collect_pdf_diagnostics
from docvortex.render._internal.pdf.formula import FormulaRenderer, FormulaVector
from docvortex.render._internal.pdf.inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph
from docvortex.render._internal.pdf.renderer import _PdfCanvas
from docvortex.render._internal.pdf.styles import build_pdf_styles
from docvortex.schema import EquationInlineSpan, TextSpan
from test_pdf_original_layout import _middle, _text


class _SizedFormulas(FormulaRenderer):
    """以明确宽高的矢量替代字体度量，让边界与基线测试不依赖 ZiaMath 字形。"""

    def render(self, latex: str, *, inline: bool, font_size: float, color: str = "#1f2937") -> FormulaVector:
        """构造包含实际墨迹的公式代理，绘制测试能检测静默丢失。"""
        width = float(latex)
        drawing = Drawing(width, 20)
        drawing.add(Rect(0, 0, width, 20, fillColor=colors.red, strokeColor=None))
        return FormulaVector(drawing, width, 20, 15, 5)


def _paragraph(parts: list[str | float], *, cached: bool = False, anchor: str | None = None, indent: float = 0):
    """通过生产段落构造入口生成中文、公式、锚点和显式换行。"""
    spans = [
        TextSpan(type="text", content=part)
        if isinstance(part, str)
        else EquationInlineSpan(type="equation_inline", content=str(part))
        for part in parts
    ]
    context = PdfInlineContext(_SizedFormulas(), PdfAnchorRegistry([anchor] if anchor else []), cache_paragraphs=cached)
    return build_pdf_paragraph(
        spans,
        build_pdf_styles().body.clone("object-test", fontSize=10, leading=12, firstLineIndent=indent),
        context=context,
        page_idx=2,
        block_index=7,
        block_type="text",
        max_width=float("inf"),
        preserve_newlines=True,
        anchor=anchor,
    )


def _images(paragraph):
    """提取最终组行中保留下来的公式回调，而不是原始输入片段。"""
    return [
        f.cbDefn
        for line in paragraph.blPara.lines
        for f in line.words
        if getattr(getattr(f, "cbDefn", None), "kind", None) == "img"
    ]


def _text_content(paragraph) -> str:
    """检查组行结果中的文字顺序及是否被分页插入了空格。"""
    return "".join(f.text for line in paragraph.blPara.lines for f in line.words)


def _assert_bounds(paragraph) -> None:
    """独立重算每行的真实对象宽度，不只信任 Paragraph 返回的框宽。"""
    for line in paragraph.blPara.lines:
        actual = sum(
            getattr(fragment.cbDefn, "width", 0)
            if hasattr(fragment, "cbDefn")
            else rl_paragraph.stringWidth(fragment.text, fragment.fontName, fragment.fontSize)
            for fragment in line.words
        )
        assert actual <= line.maxWidth + 1e-7
        assert line.currentWidth == pytest.approx(actual)


@pytest.mark.parametrize("cached", [False, True])
def test_formula_moves_intact_to_next_line(cached):
    """公式放不进剩余空间时整体移行，不能被当作悬挂标点或缩到剩余空隙。"""
    paragraph = _paragraph(["中文前", 80.0, "后文"], cached=cached)
    paragraph.wrap(100, 1000)
    assert not any(hasattr(f, "cbDefn") for f in paragraph.blPara.lines[0].words)
    assert [image.width for image in _images(paragraph)] == [80]
    assert _text_content(paragraph) == "中文前后文"
    _assert_bounds(paragraph)


def test_latin_backtracking_crosses_formula_without_ord_error():
    """Latin 溢出向前回溯会经过空文本公式，覆盖第二处 ord 调用。"""
    paragraph = _paragraph(["中AAAAAAAA", 20.0, "aaaaaaa"])
    paragraph.wrap(100, 1000)
    assert len(_images(paragraph)) == 1
    assert _text_content(paragraph) == "中AAAAAAAAaaaaaaa"
    _assert_bounds(paragraph)


@pytest.mark.parametrize("cached", [False, True])
def test_oversize_formula_fits_empty_line_and_restores_on_rewrap(cached):
    """只缩放临时回调，宽窄交替后宽高与基线从基准恢复，原始最小列宽不变。"""
    paragraph = _paragraph([300.0, "中文"], cached=cached)
    original = next(f.cbDefn for f in paragraph.frags if hasattr(f, "cbDefn"))
    natural_minimum = paragraph.minWidth()
    for width in [100, 40, 400]:
        paragraph.wrap(width, 1000)
        image = _images(paragraph)[0]
        ratio = min(width / 300, 1)
        assert (image.width, image.height, image.valign) == pytest.approx((300 * ratio, 20 * ratio, -5 * ratio))
        assert paragraph.blPara.lines[0].ascent == pytest.approx(15 * ratio)
        _assert_bounds(paragraph)
    assert (original.width, original.height, original.valign) == (300, 20, -5)
    assert paragraph.minWidth() == natural_minimum


def test_first_line_indent_adjacent_formulas_and_explicit_breaks():
    """首行缩进、连续公式和显式换行共同作用时，所有对象只出现一次。"""
    paragraph = _paragraph([120.0, 60.0, "中文\n结束"], anchor="target", indent=30)
    paragraph.wrap(100, 1000)
    assert [image.width for image in _images(paragraph)] == [70, 60]
    assert _text_content(paragraph) == "中文结束"
    assert paragraph.blPara.lines[-1].words[-1].text == "结束"
    _assert_bounds(paragraph)


def test_zero_width_anchor_survives_after_forced_break():
    """宽度为零的末尾 anchor 仍需要绘制，不能被累计宽度判断丢弃。"""
    from docvortex.render._internal.pdf.paragraph import CJKParagraph

    style = build_pdf_styles().body.clone("anchor-test", wordWrap="CJK")
    paragraph = CJKParagraph('中文<br/><a name="end"/>', style)
    paragraph.wrap(100, 1000)
    output = BytesIO()
    canvas = _PdfCanvas(output, document_title="anchors")
    paragraph.drawOn(canvas, 20, 100)
    assert canvas._destinations["end"].fmt is not None
    canvas.save()


def test_page_split_preserves_objects_text_and_natural_geometry():
    """分页不修改父段落，不补空格，续段重新变宽后恢复原始公式尺寸。"""
    paragraph = _paragraph(["中文", 180.0, "后文\n"] * 12, cached=True)
    paragraph.wrap(100, 1000)
    original_text = _text_content(paragraph)
    pieces = paragraph.split(100, 90)
    assert len(pieces) == 2
    assert _text_content(paragraph) == original_text
    for piece in pieces:
        piece.wrap(100, 1000)
        _assert_bounds(piece)
    assert "".join(_text_content(piece) for piece in pieces) == original_text
    assert sum(len(_images(piece)) for piece in pieces) == 12
    pieces[1].wrap(300, 1000)
    assert all(image.width == 180 for image in _images(pieces[1]))


def test_small_formula_diagnostic_only_describes_final_drawing():
    """试排不报告低字号，最终绘制时诊断带有实际页块定位。"""
    paragraph = _paragraph([300.0, "中文"])
    with collect_pdf_diagnostics() as diagnostics:
        paragraph.wrap(30, 1000)
        assert not diagnostics
        canvas = _PdfCanvas(BytesIO(), document_title="diagnostics")
        paragraph.drawOn(canvas, 20, 100)
        canvas.save()
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "pdf_layout_small_text"
    assert diagnostics[0].page_index == 2
    assert "block_index=7" in diagnostics[0].message


@pytest.mark.parametrize("layout", [PdfLayout.AUTO, PdfLayout.ORIGINAL, PdfLayout.REFLOW])
def test_public_render_preserves_vectors_links_and_input(layout):
    """真实长公式经过公开渲染入口，输出矢量与链接并保持 MiddleJson 不变。"""
    block = _text("中文前文", bbox=(0.1, 0.1, 0.4, 0.8))
    block["content"] += [
        {"type": "equation_inline", "content": "+".join(["x_i"] * 80)},
        {"type": "hyperlink", "url": "https://example.com", "content": [{"type": "text", "content": "中文后文"}]},
    ]
    middle = _middle([{"page_idx": 0, "blocks": [block]}])
    before = deepcopy(middle.to_dict())
    payload = render_pdf(middle, layout=layout)
    reader = PdfReader(BytesIO(payload))
    assert "中文前文" in reader.pages[0].extract_text()
    assert "中文后文" in reader.pages[0].extract_text()
    assert reader.pages[0]["/Annots"][0].get_object()["/A"]["/URI"] == "https://example.com"
    assert b" c" in reader.pages[0].get_contents().get_data()
    assert middle.to_dict() == before


def test_safe_path_does_not_change_reportlab_globals():
    """安全路径不修改第三方的断行、字符对象或 Paragraph 入口。"""
    original = (rl_paragraph.cjkFragSplit, rl_paragraph.cjkU, Paragraph.breakLinesCJK)
    _paragraph(["中文", 300.0]).wrap(100, 1000)
    assert original == (rl_paragraph.cjkFragSplit, rl_paragraph.cjkU, Paragraph.breakLinesCJK)


def test_table_cell_keeps_natural_width_and_draws_fitted_formula():
    """表格列宽测量保留公式自然宽度，实际窄列绘制仍可缩放且不污染后续宽列。"""
    from reportlab.platypus import Table

    paragraph = _paragraph(["中文", 180.0, "后文"], cached=True)
    minimum = paragraph.minWidth()
    for width in (100, 300):
        canvas = _PdfCanvas(BytesIO(), document_title="table")
        table = Table([[paragraph]], colWidths=[width + 12])
        table.wrapOn(canvas, width + 12, 1000)
        table.drawOn(canvas, 20, 100)
        canvas.save()
        assert _images(paragraph)[0].width == min(180, width)
        assert paragraph.minWidth() == minimum
        _assert_bounds(paragraph)


def test_mutating_special_paragraph_back_to_plain_clears_safe_state():
    """可变段落恢复为单一纯文本后，绘制不能继续把普通行结构当作富片段处理。"""
    paragraph = _paragraph(["中文", 180.0])
    paragraph.wrap(100, 1000)
    paragraph.frags = [paragraph.frags[0]]
    paragraph.wrap(100, 1000)
    canvas = _PdfCanvas(BytesIO(), document_title="mutated")
    paragraph.drawOn(canvas, 20, 100)
    canvas.save()
    assert not paragraph._pdf_safe_cjk
