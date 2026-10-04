"""Convert legacy PPT internal semantic model to DocVortex paging model-list."""

from __future__ import annotations

from typing import Any, BinaryIO

from ..legacy.ole import BoundedOleReader
from ..._shared.xycut import sort_entries
from ..streams import read_stream_bytes_from_start
from .....schema import BlockType
from ..rich_text import OfficeRichTextSegment, build_rich_text_from_segments, build_rich_text_html_from_segments

from .models import (
    PptChartElement,
    PptEquationElement,
    PptImageElement,
    PptParagraph,
    PptPresentation,
    PptSlide,
    PptTableCell,
    PptTableElement,
    PptTextElement,
    PptTextRun,
)
from .parser import parse_ppt_document

PPT_XYCUT_BETA = 2.0
PPT_XYCUT_DENSITY_THRESHOLD = 0.9


class PptConverter:
    """Convert PowerPoint 97–2003 binary stream to paged raw blocks."""

    def __init__(self) -> None:
        """Initialize the stateless converter output."""

        self.pages: list[list[dict[str, Any]]] = []

    def convert(self, file_binary: BinaryIO) -> None:
        """Reads the input stream, parses the three cores OLE streams and generates model-list."""

        file_bytes = read_stream_bytes_from_start(file_binary)
        with BoundedOleReader(file_bytes) as ole:
            presentation = parse_ppt_document(
                ole.read_stream("PowerPoint Document"),
                current_user=ole.read_stream("Current User", required=False),
                pictures=ole.read_stream("Pictures", required=False),
            )
        self.pages = self._presentation_to_pages(presentation)

    @staticmethod
    def _run_styles(run: PptTextRun) -> list[str]:
        """Convert internal character attributes to DocVortex rich text style names."""

        styles: list[str] = []
        if run.bold:
            styles.append("bold")
        if run.italic:
            styles.append("italic")
        if run.underline:
            styles.append("underline")
        if run.strike:
            styles.append("strikethrough")
        if run.baseline is not None and run.baseline > 0:
            styles.append("superscript")
        elif run.baseline is not None and run.baseline < 0:
            styles.append("subscript")
        return styles

    @classmethod
    def _paragraph_content(cls, paragraph: PptParagraph) -> list[dict[str, Any]]:
        """Construct paragraph run directly into structured Span."""

        segments = [
            OfficeRichTextSegment(
                text=run.text.replace("\n", " "),
                style=cls._run_styles(run),
                hyperlink=run.hyperlink,
            )
            for run in paragraph.runs
            if run.text
        ]
        return build_rich_text_from_segments(segments, trim_plain_edges=True)

    @classmethod
    def _append_list_paragraph(
        cls,
        root_blocks: list[dict[str, Any]],
        stack: list[dict[str, Any]],
        paragraph: PptParagraph,
    ) -> None:
        """Place a list paragraph into the corresponding level and create intermediate lists as needed."""

        depth = max(0, int(paragraph.depth))
        while len(stack) > depth + 1:
            stack.pop()
        while len(stack) < depth + 1:
            attribute = paragraph.list_kind or "unordered"
            new_list: dict[str, Any] = {
                "type": BlockType.LIST,
                "attribute": attribute,
                "ilevel": len(stack),
                "content": [],
            }
            if attribute == "ordered" and paragraph.start is not None:
                new_list["start"] = paragraph.start
            if stack:
                stack[-1]["content"].append(new_list)
            else:
                root_blocks.append(new_list)
            stack.append(new_list)
        current = stack[depth]
        expected_attribute = paragraph.list_kind or "unordered"
        if current.get("attribute") != expected_attribute:
            del stack[depth:]
            cls._append_list_paragraph(root_blocks, stack, paragraph)
            return
        content = cls._paragraph_content(paragraph)
        if content:
            current["content"].append({"type": BlockType.TEXT, "content": content})

    @classmethod
    def _text_element_blocks(
        cls,
        element: PptTextElement,
        *,
        title_candidate: bool,
    ) -> list[dict[str, Any]]:
        """Convert text shapes to titles, body text, and nested lists raw blocks."""

        blocks: list[dict[str, Any]] = []
        list_stack: list[dict[str, Any]] = []
        title_consumed = False
        for paragraph in element.paragraphs:
            content = cls._paragraph_content(paragraph)
            if not content:
                continue
            if title_candidate and not title_consumed and paragraph.list_kind is None:
                blocks.append(
                    {
                        "type": BlockType.PARAGRAPH_TITLE,
                        "content": content,
                        "level": 2,
                        "_ppt_title_candidate": True,
                    }
                )
                title_consumed = True
                list_stack.clear()
                continue
            if paragraph.list_kind is not None:
                cls._append_list_paragraph(blocks, list_stack, paragraph)
                continue
            list_stack.clear()
            blocks.append({"type": BlockType.TEXT, "content": content})
        return blocks

    @classmethod
    def _table_cell_content(cls, cell: PptTableCell) -> str:
        """Connect multiple paragraphs within table cells into HTML content."""
        paragraphs: list[str] = []
        for paragraph in cell.paragraphs:
            segments = [
                OfficeRichTextSegment(
                    text=run.text.replace("\n", " "),
                    style=cls._run_styles(run),
                    hyperlink=run.hyperlink,
                )
                for run in paragraph.runs
                if run.text
            ]
            if content := build_rich_text_html_from_segments(segments, trim_plain_edges=True):
                paragraphs.append(content)
        return "<br/>".join(paragraphs)

    @classmethod
    def _table_html(cls, table: PptTableElement) -> str:
        """Generate stable HTML table with rowspan/colspan by origin cell."""

        origins = {(cell.row, cell.col): cell for cell in table.cells}
        covered: set[tuple[int, int]] = set()
        rows: list[str] = []
        for row in range(table.rows):
            cells: list[str] = []
            for col in range(table.cols):
                if (row, col) in covered:
                    continue
                cell = origins.get((row, col))
                if cell is None:
                    cells.append("<td></td>")
                    continue
                attributes: list[str] = []
                if cell.row_span > 1:
                    attributes.append(f'rowspan="{cell.row_span}"')
                if cell.col_span > 1:
                    attributes.append(f'colspan="{cell.col_span}"')
                attribute_text = f" {' '.join(attributes)}" if attributes else ""
                cells.append(f"<td{attribute_text}>{cls._table_cell_content(cell)}</td>")
                for covered_row in range(row, row + cell.row_span):
                    for covered_col in range(col, col + cell.col_span):
                        if (covered_row, covered_col) != (row, col):
                            covered.add((covered_row, covered_col))
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return f'<table border="1">{"".join(rows)}</table>'

    @classmethod
    def _element_blocks(
        cls,
        element: PptTextElement | PptImageElement | PptEquationElement | PptChartElement | PptTableElement,
        *,
        slide_height: int,
        is_first_text_element: bool,
    ) -> list[dict[str, Any]]:
        """Convert a semantic element to raw blocks."""

        if isinstance(element, PptImageElement):
            return [{"type": BlockType.IMAGE, "image_base64": element.image_base64}]
        if isinstance(element, PptEquationElement):
            return [{"type": BlockType.EQUATION, "content": element.latex}]
        if isinstance(element, PptChartElement):
            block: dict[str, Any] = {
                "type": BlockType.CHART,
                "content": element.content,
            }
            if element.image_base64:
                block["image_base64"] = element.image_base64
            return [block]
        if isinstance(element, PptTableElement):
            return [{"type": BlockType.TABLE, "content": cls._table_html(element)}]

        authoritative_title = element.text_type in {0, 6}
        first_paragraph = element.paragraphs[0] if element.paragraphs else None
        heuristic_title = bool(
            is_first_text_element
            and element.is_placeholder
            and first_paragraph is not None
            and first_paragraph.list_kind is None
            and element.bbox[1] <= slide_height * 0.35
            and len("".join(run.text for run in first_paragraph.runs).strip()) <= 200
        )
        return cls._text_element_blocks(
            element,
            title_candidate=authoritative_title or heuristic_title,
        )

    @classmethod
    def _slide_to_page(cls, slide: PptSlide, presentation: PptPresentation) -> list[dict[str, Any]]:
        """Sort a slide by XYCut++ and append notes steadily to the end."""

        entries: list[dict[str, Any]] = []
        text_seen = False
        for element in slide.elements:
            is_first_text = isinstance(element, PptTextElement) and not text_seen
            blocks = cls._element_blocks(
                element,
                slide_height=presentation.height,
                is_first_text_element=is_first_text,
            )
            if isinstance(element, PptTextElement) and blocks:
                text_seen = True
            if blocks:
                entries.append({"bbox": element.bbox, "blocks": blocks, "order": element.order})
        ordered_entries = sort_entries(
            entries,
            beta=PPT_XYCUT_BETA,
            density_threshold=PPT_XYCUT_DENSITY_THRESHOLD,
        )
        page = [block for entry in ordered_entries for block in entry["blocks"]]
        for paragraph in slide.notes:
            content = cls._paragraph_content(paragraph)
            if content:
                page.append({"type": BlockType.PAGE_FOOTNOTE, "content": content})
        return page

    @classmethod
    def _presentation_to_pages(cls, presentation: PptPresentation) -> list[list[dict[str, Any]]]:
        """Converts the entire presentation and promotes the first valid title to the document title."""

        pages = [cls._slide_to_page(slide, presentation) for slide in presentation.slides]
        document_title_promoted = False
        for page in pages:
            candidates = [block for block in page if block.pop("_ppt_title_candidate", False)]
            if not candidates:
                continue
            first = candidates[0]
            if not document_title_promoted:
                first["type"] = BlockType.DOC_TITLE
                first["level"] = 1
                document_title_promoted = True
            for candidate in candidates[1:] if first["type"] == BlockType.DOC_TITLE else candidates:
                candidate["type"] = BlockType.PARAGRAPH_TITLE
                candidate["level"] = 2
        return pages
