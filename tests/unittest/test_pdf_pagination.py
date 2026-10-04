"""Verify PDF actual page ownership, paging integrity, and degradation behavior for ultra-high content."""

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
    """Extract text from each page and verify pagination with actual PDF page ownership instead of checking implementation type."""
    return [page.extract_text() or "" for page in PdfReader(BytesIO(payload)).pages]


def _story_pdf(story: list) -> bytes:
    """Render test documents with product-consistent content boxes with precise control of footer margins."""
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
    """Generate paragraphs with independent markers for each line, and support checking for orphan lines and duplicate losses."""
    return Paragraph("<br/>".join(f"{prefix}_{i:03}" for i in range(count)), build_pdf_styles().body)


def _protect(content: list, *, table: bool = False) -> list:
    """Run paging protection against production thresholds before handing over real ReportLab document paging."""
    helper = protect_tables if table else protect_paragraphs
    return helper(content, width=WIDTH, height=HEIGHT, canvas=Canvas(BytesIO()))


def _table(rows: int, *, row_lines: int = 1) -> _PdfLongTable:
    """Construct a native table with repeated headers, and each row of text marks can be checked across pages."""
    style = build_pdf_styles().table_cell
    data = [[Paragraph("HEADER", style)]]
    data.extend([Paragraph("<br/>".join(f"ROW_{row:03}_{line:03}" for line in range(row_lines)), style)] for row in range(rows))
    return _PdfLongTable(data, colWidths=[WIDTH], repeatRows=1, splitByRow=1, splitInRow=1)


def _middle(blocks: list) -> MiddleJson:
    """Construct strict documentation for public rendering entries to avoid tests relying on other test modules."""
    return MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=blocks)],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _image(*, index: int = 0, ordinal: int = 0, tall: bool = False, long_note: bool = False) -> ImageBlock:
    """Generate offline pictures and descriptions, covering single pictures, multiple pictures and super long descriptions."""
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
    """Short paragraphs are paged in their entirety. Paragraphs that are just over a quarter of a page are allowed to use the current page margin."""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 40), *_protect([_lines(lines)])]))
    assert len(pages) == 2
    assert ("LINE_000" not in pages[0]) is kept
    assert f"LINE_{lines - 1:03}" in pages[1]
    assert all("".join(pages).count(f"LINE_{i:03}") == 1 for i in range(lines))


@pytest.mark.parametrize("remaining", [20, 40, 180])
def test_long_paragraph_avoids_single_lines_at_page_boundaries(remaining: int) -> None:
    """When paginating long sections, no single lines will be left on either side, and over-full page content can be paginated continuously."""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - remaining), *_protect([_lines(100)])]))
    counts = [sum(f"LINE_{i:03}" in page for i in range(100)) for page in pages]
    assert sum(counts) == 100
    assert all(count == 0 or count >= 2 for count in counts)


def test_heading_stays_with_protected_paragraph() -> None:
    """Nested guard blocks return the true height and the title does not stay on the previous page alone."""
    heading = Paragraph("HEADING", build_pdf_styles().heading(2))
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 60), heading, *_protect([_lines(5)])]))
    assert "HEADING" not in pages[0]
    assert "HEADING" in pages[1] and "LINE_000" in pages[1]


@pytest.mark.parametrize("rows,kept", [(8, True), (18, True), (19, False), (45, False)])
def test_table_height_threshold_includes_annotations(rows: int, kept: bool) -> None:
    """The half-page threshold includes descriptions and spacing, and long tables can still start on the current page and repeat the header."""
    notes = Paragraph("TABLE_NOTE", build_pdf_styles().caption)
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 100), *_protect([_table(rows), notes], table=True)]))
    assert ("ROW_000_000" not in pages[0]) is kept
    assert "TABLE_NOTE" in pages[-1] and f"ROW_{rows - 1:03}_000" in pages[-1]
    assert all(page.count("HEADER") == 1 for page in pages if "ROW_" in page)
    assert all("".join(pages).count(f"ROW_{i:03}_000") == 1 for i in range(rows))


def test_table_annotations_follow_first_and_last_fragments() -> None:
    """The first and last descriptions of long tables follow the first and last fragments respectively, and the endnotes will not be squeezed into a separate page."""
    styles = build_pdf_styles()
    before = Paragraph("BEFORE_TABLE", styles.caption)
    after = Paragraph("AFTER_TABLE", styles.caption)
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 40), *_protect([before, _table(80), after], table=True)]))
    assert next(i for i, page in enumerate(pages) if "BEFORE_TABLE" in page) == next(
        i for i, page in enumerate(pages) if "ROW_000_000" in page
    )
    assert "ROW_079_000" in pages[-1] and "AFTER_TABLE" in pages[-1]


def test_normal_table_row_moves_before_in_row_splitting() -> None:
    """When ordinary tall rows cannot fit at the end of the page, the entire page will be changed without splitting the cells in advance."""
    pages = _page_texts(_story_pdf([Spacer(1, HEIGHT - 80), *_protect([_table(5, row_lines=20)], table=True)]))
    assert "ROW_000_000" not in pages[0]
    assert "ROW_000_000" in pages[1] and "ROW_000_019" in pages[1]
    assert all("".join(pages).count(f"ROW_{row:03}_{line:03}") == 1 for row in range(5) for line in range(20))


def test_oversized_table_row_splits_on_fresh_page_with_caption() -> None:
    """If a single line exceeds the entire page, it can still be split. The last paragraph and description are on the same page and the content is complete."""
    after = Paragraph("AFTER_TABLE", build_pdf_styles().caption)
    pages = _page_texts(_story_pdf(_protect([_table(1, row_lines=150), after], table=True)))
    assert len(pages) >= 3
    assert "ROW_000_149" in pages[-1] and "AFTER_TABLE" in pages[-1]
    assert all("".join(pages).count(f"ROW_000_{i:03}") == 1 for i in range(150))


@pytest.mark.parametrize("count,tall,long_note", [(1, False, False), (1, True, False), (2, True, False), (1, True, True)])
def test_public_pdf_images_fit_frames_and_keep_short_captions(count: int, tall: bool, long_note: bool) -> None:
    """The public portal can render superelevation images and multi-images, and place short descriptions and images on the same page."""
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
    """When the superelevation image, title and legend exactly fill a page, no blank page will be generated, and the image will always be within the content frame."""
    heading = ParagraphTitleBlock(type="paragraph_title", index=0, level=2, content=[{"type": "text", "content": "HEADING"}])
    reader = PdfReader(BytesIO(render_pdf(_middle([heading, _image(index=1, tall=True)]))))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text() or ""
    assert "HEADING" in text and "CAPTION_0" in text
    positions = []

    def record_image(operator, operands, matrix, text_matrix):
        """Record the actual transformation of the PDF image when drawing, checking for boundaries instead of just checking for resource existence."""
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
    """Long lists continue to use the current page, and the granularity of protection is individual items rather than the entire list."""
    filler = [TextBlock(type="text", index=i, content=[{"type": "text", "content": f"FILLER_{i}"}]) for i in range(20)]
    items = [TextBlock(type="text", content=[{"type": "text", "content": f"ITEM_{i:03}"}]) for i in range(60)]
    pages = _page_texts(render_pdf(_middle([*filler, ListBlock(type="list", index=20, content=items)])))
    assert "ITEM_000" in pages[0]
    assert "ITEM_059" in pages[-1] and len(pages) > 1
    assert all("".join(pages).count(f"ITEM_{i:03}") == 1 for i in range(60))


def test_long_table_preserves_rowspans_across_page_boundaries() -> None:
    """Ordinary cross-row merges in multi-page tables remain intact, with repeated headers and tails indicating normal output."""
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
    """The public portal protects the true semantic paragraphs and HTML short tables and maintains deterministic output."""
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
