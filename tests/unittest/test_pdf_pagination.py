"""验证 PDF 实际页归属、分页完整性及超高内容的降级行为。"""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image as PillowImage
from pypdf import PdfReader
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from docvortex.render import render_pdf
from docvortex.render._internal.pdf.pagination import protect_paragraphs, protect_tables
from docvortex.render._internal.pdf.styles import FRAME_PADDING, PAGE_MARGIN, build_pdf_styles
from docvortex.render._internal.pdf.table import _PdfLongTable
from docvortex.schema import ImageBlock, ListBlock, MiddleJson, PageInfo, ParagraphTitleBlock, Producer, TableBlock, TextBlock

WIDTH = A4[0] - 2 * (PAGE_MARGIN + FRAME_PADDING)
HEIGHT = A4[1] - 2 * (PAGE_MARGIN + FRAME_PADDING)


def _page_texts(payload: bytes) -> list[str]:
    """提取每页文字，以实际 PDF 页归属验证分页而非检查实现类型。"""
    return [page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages]


def _story_pdf(story: list) -> bytes:
    """用与产品一致的内容框渲染可精确控制页底余量的测试文档。"""
    output = BytesIO()
    SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
    ).build(story)
    return output.getvalue()


def _lines(count: int, prefix: str = "LINE") -> Paragraph:
    """生成每行有独立标记的段落，支持检查孤行与重复丢失。"""
    return Paragraph("<br/>".join(f"{prefix}_{i:03}" for i in range(count)), build_pdf_styles().body)


def _protect(content: list, *, table: bool = False) -> list:
    """按生产阈值运行分页保护，再交给真实 ReportLab 文档分页。"""
    helper = protect_tables if table else protect_paragraphs
    return helper(content, width=WIDTH, height=HEIGHT, canvas=Canvas(BytesIO()))


def _table(rows: int, *, row_lines: int = 1) -> _PdfLongTable:
    """构造带重复表头的原生长表，每行文字标记可跨页核对。"""
    style = build_pdf_styles().table_cell
    data = [[Paragraph("HEADER", style)]]
    data.extend([Paragraph("<br/>".join(f"ROW_{row:03}_{line:03}" for line in range(row_lines)), style)] for row in range(rows))
    return _PdfLongTable(data, colWidths=[WIDTH], repeatRows=1, splitByRow=1, splitInRow=1)


def _middle(blocks: list) -> MiddleJson:
    """构造用于公共渲染入口的严格文档，避免测试依赖其他测试模块。"""
    return MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=blocks)],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _image(*, index: int = 0, ordinal: int = 0, tall: bool = False, long_note: bool = False) -> ImageBlock:
    """生成离线图片及说明，覆盖单图、多图与超长说明。"""
    output = BytesIO()
    PillowImage.new("RGB", (200, 2400 if tall else 600), (30, 80, 130)).save(output, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    children = [
        {"type": "image_body", "index": index, "content": "", "image_base64": uri},
        {"type": "image_caption", "content": [{"type": "text", "content": f"CAPTION_{ordinal}"}]},
    ]
    if long_note:
        children.append(
            {"type": "image_footnote", "content": [{"type": "text", "content": "NOTE_START " + "word " * 3000 + "NOTE_END"}]}
        )
    return ImageBlock.model_validate({"type": "image", "index": index, "content": children})


@pytest.mark.parametrize("lines,kept", [(10, True), (11, False), (12, False)])
def test_paragraph_height_threshold_controls_actual_page_assignment(lines: int, kept: bool) -> None:
    """短段整块换页，刚超过四分之一页的段落允许利用当前页余量。"""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 40), *_protect([_lines(lines)])]))
    assert len(pages) == 2
    assert ("LINE_000" not in pages[0]) is kept
    assert f"LINE_{lines - 1:03}" in pages[1]
    assert all("".join(pages).count(f"LINE_{i:03}") == 1 for i in range(lines))


@pytest.mark.parametrize("remaining", [20, 40, 180])
def test_long_paragraph_avoids_single_lines_at_page_boundaries(remaining: int) -> None:
    """长段分页时不在两侧留下孤立单行，且超整页内容能持续分页。"""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - remaining), *_protect([_lines(100)])]))
    counts = [sum(f"LINE_{i:03}" in page for i in range(100)) for page in pages]
    assert sum(counts) == 100
    assert all(count == 0 or count >= 2 for count in counts)


def test_heading_stays_with_protected_paragraph() -> None:
    """嵌套保护块返回真实高度，标题不会独自留在上一页。"""
    heading = Paragraph("HEADING", build_pdf_styles().heading(2))
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 60), heading, *_protect([_lines(5)])]))
    assert "HEADING" not in pages[0]
    assert "HEADING" in pages[1] and "LINE_000" in pages[1]


@pytest.mark.parametrize("rows,kept", [(8, True), (18, True), (19, False), (45, False)])
def test_table_height_threshold_includes_annotations(rows: int, kept: bool) -> None:
    """半页阈值包含说明与间距，长表仍能从当前页开始并重复表头。"""
    notes = Paragraph("TABLE_NOTE", build_pdf_styles().caption)
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 100), *_protect([_table(rows), notes], table=True)]))
    assert ("ROW_000_000" not in pages[0]) is kept
    assert "TABLE_NOTE" in pages[-1] and f"ROW_{rows - 1:03}_000" in pages[-1]
    assert all(page.count("HEADER") == 1 for page in pages if "ROW_" in page)
    assert all("".join(pages).count(f"ROW_{i:03}_000") == 1 for i in range(rows))


def test_table_annotations_follow_first_and_last_fragments() -> None:
    """长表首尾说明分别跟随首末片段，尾注不会被挤到单独一页。"""
    styles = build_pdf_styles()
    before = Paragraph("BEFORE_TABLE", styles.caption)
    after = Paragraph("AFTER_TABLE", styles.caption)
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 40), *_protect([before, _table(80), after], table=True)]))
    assert next(i for i, page in enumerate(pages) if "BEFORE_TABLE" in page) == next(
        i for i, page in enumerate(pages) if "ROW_000_000" in page
    )
    assert "ROW_079_000" in pages[-1] and "AFTER_TABLE" in pages[-1]


def test_normal_table_row_moves_before_in_row_splitting() -> None:
    """普通高行在页尾放不下时整体换页，不提前拆开单元格。"""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 80), *_protect([_table(5, row_lines=20)], table=True)]))
    assert "ROW_000_000" not in pages[0]
    assert "ROW_000_000" in pages[1] and "ROW_000_019" in pages[1]
    assert all("".join(pages).count(f"ROW_{row:03}_{line:03}") == 1 for row in range(5) for line in range(20))


def test_oversized_table_row_splits_on_fresh_page_with_caption() -> None:
    """单行超过整页时仍可拆分，末段与说明同页且内容完整。"""
    after = Paragraph("AFTER_TABLE", build_pdf_styles().caption)
    pages = _page_texts(_story_pdf(_protect([_table(1, row_lines=150), after], table=True)))
    assert len(pages) >= 3
    assert "ROW_000_149" in pages[-1] and "AFTER_TABLE" in pages[-1]
    assert all("".join(pages).count(f"ROW_000_{i:03}") == 1 for i in range(150))


@pytest.mark.parametrize("count,tall,long_note", [(1, False, False), (1, True, False), (2, True, False), (1, True, True)])
def test_public_pdf_images_fit_frames_and_keep_short_captions(count: int, tall: bool, long_note: bool) -> None:
    """公共入口可渲染超高图与多图，并将简短说明和图片放在同页。"""
    filler = [TextBlock(type="text", index=i, content=[{"type": "text", "content": f"FILLER_{i}"}]) for i in range(15)]
    images = [_image(index=15 + i, ordinal=i, tall=tall, long_note=long_note) for i in range(count)]
    reader = PdfReader(BytesIO(render_pdf(_middle([*filler, *images]))))
    pages = [page.extract_text() or "" for page in reader.pages]
    for ordinal in range(count):
        page = next(page for page in reader.pages if f"CAPTION_{ordinal}" in (page.extract_text() or ""))
        assert page.images
    if long_note:
        assert "NOTE_START" in "".join(pages) and "NOTE_END" in pages[-1]
    assert all(page.images or (page.extract_text() or "").strip() for page in reader.pages)


def test_full_height_image_reserves_title_and_caption_space() -> None:
    """超高图与标题及图注恰好占满一页时不产生空白页，图像始终位于内容框内。"""
    heading = ParagraphTitleBlock(type="paragraph_title", index=0, level=2, content=[{"type": "text", "content": "HEADING"}])
    reader = PdfReader(BytesIO(render_pdf(_middle([heading, _image(index=1, tall=True)]))))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text() or ""
    assert "HEADING" in text and "CAPTION_0" in text
    positions = []

    def record_image(operator, operands, matrix, text_matrix):
        """记录 PDF 图像绘制时的真实变换，核对边界而非只检查资源存在。"""
        if operator == b"Do":
            positions.append(matrix[:])

    reader.pages[0].extract_text(visitor_operand_before=record_image)
    assert positions
    margin = PAGE_MARGIN + FRAME_PADDING
    for width, _, _, height, x, y in positions:
        assert x >= margin - 1e-4 and y >= margin - 1e-4
        assert x + width <= A4[0] - margin + 1e-4
        assert y + height <= A4[1] - margin + 1e-4


def test_list_keeps_individual_items_without_moving_entire_list() -> None:
    """长列表继续利用当前页，保护粒度是单项而不是整张列表。"""
    filler = [TextBlock(type="text", index=i, content=[{"type": "text", "content": f"FILLER_{i}"}]) for i in range(20)]
    items = [TextBlock(type="text", content=[{"type": "text", "content": f"ITEM_{i:03}"}]) for i in range(60)]
    pages = _page_texts(render_pdf(_middle([*filler, ListBlock(type="list", index=20, content=items)])))
    assert "ITEM_000" in pages[0]
    assert "ITEM_059" in pages[-1] and len(pages) > 1
    assert all("".join(pages).count(f"ITEM_{i:03}") == 1 for i in range(60))


def test_long_table_preserves_rowspans_across_page_boundaries() -> None:
    """多页表格中的普通跨行合并保持完整，重复表头和末尾说明正常输出。"""
    style = build_pdf_styles().table_cell
    data = [[Paragraph("HEADER", style), ""]]
    spans = []
    for pair in range(40):
        row = len(data)
        data.extend(
            [
                [Paragraph(f"SPAN_{pair}", style), Paragraph(f"PAIR_{pair:03}_A", style)],
                ["", Paragraph(f"PAIR_{pair:03}_B", style)],
            ]
        )
        spans.append(("SPAN", (0, row), (0, row + 1)))
    table = _PdfLongTable(data, colWidths=[WIDTH / 2] * 2, repeatRows=1, splitByRow=1, splitInRow=1, style=spans)
    pages = _page_texts(_story_pdf(_protect([table, Paragraph("AFTER_TABLE", style)], table=True)))
    assert len(pages) >= 2
    for pair in range(40):
        page = next(page for page in pages if f"PAIR_{pair:03}_A" in page)
        assert f"PAIR_{pair:03}_B" in page
    assert "AFTER_TABLE" in pages[-1] and "PAIR_039_B" in pages[-1]


def test_public_pdf_short_text_and_html_table_move_whole() -> None:
    """公共入口保护真实语义段落和 HTML 短表，并保持确定性输出。"""
    filler = [TextBlock(type="text", index=i, content=[{"type": "text", "content": f"FILLER_{i}"}]) for i in range(28)]
    paragraph = TextBlock(type="text", index=28, content=[{"type": "text", "content": "START " + "word " * 110 + " END"}])
    table = TableBlock.model_validate(
        {
            "type": "table",
            "index": 28,
            "content": [
                {
                    "type": "table_body",
                    "index": 28,
                    "content": "<table>" + "".join(f"<tr><td>ROW_{i}</td></tr>" for i in range(8)) + "</table>",
                }
            ],
        }
    )
    for block, start, end in [(paragraph, "START", "END"), (table, "ROW_0", "ROW_7")]:
        middle = _middle([*filler, block])
        original = middle.model_dump()
        payload = render_pdf(middle)
        assert payload == render_pdf(middle)
        assert middle.model_dump() == original
        pages = _page_texts(payload)
        assert start not in pages[0] and start in pages[1] and end in pages[1]
