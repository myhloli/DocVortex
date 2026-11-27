"""验证 Flash 按页诊断的原始成员、图形并集、读序和公开模型契约。"""

from unittest.mock import patch

import pytest
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary
from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf.layout_diagnostics import (
    MISSING_VISUAL,
    READING_BOUNDARY,
    TEXT_OWNERSHIP,
    LayoutDiagnosticPage,
    glyph_index_ranges,
    retain_glyph_members,
    suppress_repeated_marginal_visuals,
)
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.native_contracts import PDFPathInfo


def _source_line(first: int, bbox: tuple[float, float, float, float], text: str = "Printable body words") -> _LineItem:
    """构造有稳定 PDFium 字符索引的可见原生行。"""
    return _LineItem(
        text,
        bbox,
        0,
        first,
        chars=[{"char_idx": first + index, "char": value} for index, value in enumerate(text)],
        effective_height=bbox[3] - bbox[1],
    )


def _block(kind: str, box: tuple[float, float, float, float], *ranges: tuple[int, int]) -> dict:
    """生成以百分之一页坐标表示的模型块，附实际承接字形。"""
    block = {"type": kind, "bbox": [value / 100 for value in box]}
    retain_glyph_members(block, glyph_index_ranges(index for start, end in ranges for index in range(start, end)))
    return block


def test_text_ownership_counts_consecutive_rows_and_noncontained_duplicates():
    """可见正文两行均过半才报告，独立重复与真正无主相同计数。"""
    evidence = LayoutDiagnosticPage((100, 100))
    evidence.record_rows([_source_line(100, (10, 10, 40, 15)), _source_line(200, (10, 17, 40, 22))])
    first = _block("text", (10, 10, 40, 15), (100, 120))
    second = _block("image", (9, 16, 41, 23), (200, 220))
    assert evidence.diagnose([first, second], 3) == []
    assert evidence.diagnose([first], 3) == []
    independent_repeat = _block("text", (45, 10, 90, 22), (100, 120))
    result = evidence.diagnose([first, independent_repeat], 3)
    assert [(item.code, item.page_index) for item in result] == [(TEXT_OWNERSHIP, 3)]
    contained = _block("text", (12, 11, 39, 14), (100, 120))
    assert evidence.diagnose([first, contained], 3) == []
    nested = _block("list", (9, 9, 42, 16), (100, 120))
    nested["content"] = [first]
    assert evidence.diagnose([nested, second], 3) == []


def test_visibly_nonempty_source_can_trigger_on_empty_page():
    """有已确认正文的空结果直接报告，空白和生成字形不参与。"""
    evidence = LayoutDiagnosticPage((100, 100))
    line = _source_line(0, (10, 10, 45, 15))
    line.chars.extend([{"char_idx": 700, "char": " "}, {"char_idx": 701, "char": "X", "is_generated": True}])
    evidence.record_rows([line])
    assert [item.code for item in evidence.diagnose([], 0)] == [TEXT_OWNERSHIP]
    assert LayoutDiagnosticPage((100, 100)).diagnose([], 0) == []


def test_visual_coverage_uses_union_and_only_output_graphics():
    """文字表格和重复裁片无法填补缺失图面，覆盖刚过半与小装饰均不报警。"""
    evidence = LayoutDiagnosticPage((100, 100))
    evidence.record_visuals("位图", [(10, 10, 60, 30)])
    same_crop = _block("image", (10, 10, 30, 30))
    table = _block("table", (10, 10, 60, 30))
    result = evidence.diagnose([same_crop, same_crop, table], 0)
    assert [item.code for item in result] == [MISSING_VISUAL]
    assert "40.0%" in result[0].message
    assert evidence.diagnose([same_crop, _block("image", (25, 10, 35, 30))], 0) == []
    evidence = LayoutDiagnosticPage((100, 100))
    evidence.record_vector_visuals([(0, 0, 19, 10), (0, 0, 100, 8)])
    assert evidence.diagnose([], 0) == []


def test_marginal_repeat_requires_three_pages_at_same_position_and_respects_caption():
    """同图页边条须重复三页，正文图、不同放置和题注确认的独立图都继续打分。"""
    banner = (15.0, 2.0, 85.0, 8.0)
    body = (15.0, 52.0, 85.0, 58.0)
    different_position = (10.0, 2.0, 80.0, 8.0)
    pages = [LayoutDiagnosticPage((100, 100)) for _ in range(6)]
    for page in pages[:4]:
        page.record_visuals("位图", [banner])
        page.record_marginal_rasters({banner: "same-pixels"}, protected=[banner] if page is pages[3] else [])
    pages[4].record_visuals("位图", [body])
    pages[4].record_marginal_rasters({body: "same-pixels"})
    pages[5].record_visuals("位图", [different_position])
    pages[5].record_marginal_rasters({different_position: "same-pixels"})
    suppress_repeated_marginal_visuals(pages[:2])
    assert all(page.diagnose([], index) for index, page in enumerate(pages[:2]))
    suppress_repeated_marginal_visuals(pages)
    assert all(not page.diagnose([], index) for index, page in enumerate(pages[:3]))
    assert all(page.diagnose([], index) for index, page in enumerate(pages[3:], 3))


def test_repeated_vector_banner_ignores_thin_underlines_and_keeps_other_drawings():
    """跨页矢量标识采用实际图形主体，文字下划线和独立新图保留各自命运。"""
    box = (10.0, 4.0, 90.0, 13.0)
    background = PDFPathInfo(box, 5, True, False, 0, 0, (255, 255, 255, 255))
    badge = PDFPathInfo((85.0, 5.0, 88.0, 12.0), 31, True, False, 0, 1, (255, 255, 255, 255))
    new_badge = PDFPathInfo((85.0, 5.0, 88.0, 12.0), 20, True, False, 0, 1, (0, 0, 0, 255))
    pages = [LayoutDiagnosticPage((100, 100)) for _ in range(4)]
    for index, page in enumerate(pages):
        page.record_vector_visuals([box])
        underline = PDFPathInfo((15.0 + index, 7.0, 56.0, 7.1), 5, True, False, 0, 2)
        page.record_marginal_vectors([background, badge if index < 3 else new_badge, underline])
    suppress_repeated_marginal_visuals(pages)
    assert all(not page.diagnose([], index) for index, page in enumerate(pages[:3]))
    assert [item.code for item in pages[3].diagnose([], 3)] == [MISSING_VISUAL]


@pytest.mark.parametrize("reverse", [False, True])
def test_reading_boundary_requires_a_continuous_two_column_band(reverse):
    """并栏正文折返触发，通栏标题切开两段后同样的栏序可以成立。"""
    evidence = LayoutDiagnosticPage((100, 100))
    evidence.record_rows(
        [_source_line(y * 100 + left, (left, y, right, y + 3)) for left, right in ((10, 40), (55, 90)) for y in (10, 16, 30)]
    )
    evidence.columns[0] = ((10, 40), (55, 90))
    col_1, col_2 = [(10, 40), (55, 90)][:: -1 if reverse else 1]
    first = _block("text", (col_1[0], 10, col_1[1], 20), *[(y * 100 + col_1[0], y * 100 + col_1[0] + 20) for y in (10, 16)])
    next_col = _block(
        "text", (col_2[0], 10, col_2[1], 35), *[(y * 100 + col_2[0], y * 100 + col_2[0] + 20) for y in (10, 16, 30)]
    )
    return_col = _block("text", (col_1[0], 25, col_1[1], 35), (3000 + col_1[0], 3020 + col_1[0]))
    assert [item.code for item in evidence.diagnose([first, next_col, return_col], 0)] == [READING_BOUNDARY]
    # 独立上下两段双栏（第三块完全位于前两块下方）不视为同带折返。
    lower_tier = _block("text", (col_1[0], 36, col_1[1], 45), (3000 + col_1[0], 3020 + col_1[0]))
    upper_right = _block(
        "text", (col_2[0], 10, col_2[1], 20), *[(y * 100 + col_2[0], y * 100 + col_2[0] + 20) for y in (10, 16, 30)]
    )
    assert evidence.diagnose([first, upper_right, lower_tier], 0) == []
    span = _block("paragraph_title", (10, 22, 90, 25))
    assert evidence.diagnose([first, next_col, span, return_col], 0) == []
    assert evidence.diagnose([first, return_col, next_col], 0) == []
    image = _block("image", (col_2[0], 10, col_2[1], 35))
    assert evidence.diagnose([first, next_col, return_col, image], 0) == []


def test_hard_paragraph_boundaries_use_source_glyph_members():
    """两个原始位置确实被同一正文块承接才触发硬段界。"""
    evidence = LayoutDiagnosticPage((100, 100))
    a = _source_line(10, (10, 10, 40, 15))
    a.layout_glyph_ranges = glyph_index_ranges(range(10, 20))
    b = _source_line(30, (10, 19, 40, 24))
    b.layout_glyph_ranges = glyph_index_ranges(range(30, 40))
    evidence.record_break(a, b)
    merged = _block("text", (10, 10, 40, 24), (10, 20), (30, 40))
    assert [item.code for item in evidence.diagnose([merged], 0)] == [READING_BOUNDARY]
    assert evidence.diagnose([_block("text", (10, 10, 40, 24), (10, 20))], 0) == []


@pytest.mark.parametrize("pdf_structure", ["auto", "off"])
def test_predict_uses_same_output_format_and_local_page_positions(pdf_structure):
    """同一支持结构或几何的 PDF 开启诊断不改模型，结构正例使用页列表位置。"""
    builder = TaggedPdfBuilder(2)
    first = builder.node("P", [builder.text("Paragraph on first page")])
    two_columns = [builder.node("P", [builder.text("A left column line", page=1, x=20, y=y)], page=1) for y in (350, 332, 314)]
    right = builder.node("P", [builder.text("A right column line", page=1, x=165, y=y) for y in (350, 332, 314)], page=1)
    with PDFDocument(builder.finish([first, two_columns[0], right, *two_columns[1:]], declared_ua="1")) as document:
        model = PdfModel()
        ordinary = model.predict(document, pdf_structure=pdf_structure)
        assert model.diagnostics == ()
        assert model.layout_page_confidences == ()
        assert model.layout_page_assessments == ()
        with patch.object(document, "render_page", side_effect=AssertionError("诊断无需渲染整页")):
            inspected = model.predict(document, pdf_structure=pdf_structure, collect_layout_diagnostics=True)
        assert inspected == ordinary
        assert "_layout_glyph" not in repr(inspected)
        wanted = [(READING_BOUNDARY, 1)] if pdf_structure == "auto" else []
        assert [(item.code, item.page_index) for item in model.diagnostics if item.code == READING_BOUNDARY] == wanted
        assert model.layout_page_confidences == (1.0,) * len(inspected)
        assert len(model.layout_page_assessments) == len(inspected)
        assert all(item.confidence == 1.0 and item.primary_domain is None and item.reasons == () for item in model.layout_page_assessments)
        with patch.object(document, "get_page_vector_geometry", side_effect=AssertionError("无路径页不应重读矢量几何")):
            rechecked = model.predict(document, pdf_structure=pdf_structure, collect_layout_diagnostics=True)
        assert rechecked == inspected
        model.predict(document, pdf_structure=pdf_structure)
        assert model.diagnostics == ()
        assert model.layout_page_confidences == ()
        assert model.layout_page_assessments == ()


def test_partial_tagged_analysis_checks_reading_order_after_merge():
    """混合页的结构正文与残余几何块完成排序后再判断，隐藏与 Artifact 字形排除。"""
    builder = TaggedPdfBuilder()
    body = builder.node("P", [builder.text("Visible tagged prose", y=350)])
    builder.text("Visible residual prose", y=325)
    builder.text("Hidden residual prose", y=300, invisible=True)
    builder.text("Decoration residual", y=283, artifact=True)
    with PDFDocument(builder.finish([body])) as document:
        model = PdfModel()
        ordinary = model.predict(document)
        inspected = model.predict(document, collect_layout_diagnostics=True)
        assert inspected == ordinary
        assert "Visible residual prose" in repr(inspected)
        assert model.layout_page_confidences == (1.0,)


def test_tagged_table_does_not_hide_large_bitmap_omission():
    """被结构单元格认领的实际大图仍需独立图面，表格 HTML 不覆盖。"""
    builder = TaggedPdfBuilder()
    reference = builder.text("Cell label", y=330)
    image = DecodedStreamObject()
    image.set_data(bytes((0, 32, 208, 232, 52, 28) * 16))
    image.update(
        pdf_dictionary(
            Type=NameObject("/XObject"),
            Subtype=NameObject("/Image"),
            Width=NumberObject(8),
            Height=NumberObject(4),
            ColorSpace=NameObject("/DeviceRGB"),
            BitsPerComponent=NumberObject(8),
        )
    )
    builder.pages[0]["/Resources"][NameObject("/XObject")] = pdf_dictionary(Im=builder.writer._add_object(image))
    builder.commands[0].extend(b"/TD <</MCID 0>> BDC q 80 0 0 80 20 230 cm /Im Do Q EMC")
    cell = builder.node("TD", [reference])
    table = builder.node("Table", [builder.node("TR", [cell])])
    with PDFDocument(builder.finish([table])) as document:
        model = PdfModel()
        ordinary = model.predict(document)
        inspected = model.predict(document, collect_layout_diagnostics=True)
        assert [block["type"] for block in inspected[0]] == ["table"]
        assert inspected == ordinary
        assert [item.code for item in model.diagnostics] == [MISSING_VISUAL]
