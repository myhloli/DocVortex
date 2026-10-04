"""Convert Word 97–2003 semantic model to DocVortex paging model-list."""

from __future__ import annotations

from html import escape
import re
from typing import Any, BinaryIO, Iterable

from ..errors import LegacyOfficeEncryptedError
from ..image import serialize_office_image
from ..legacy.ole import BoundedOleReader
from ..streams import read_stream_bytes_from_start
from .....schema import RAW_CAPTION, BlockType
from .....content.spans import append_equation_span, extend_inline_spans, inline_span_plain_text, strip_span_dicts, text_spans
from ..rich_text import OfficeRichTextSegment, build_rich_text_from_segments, build_rich_text_html_from_segments

from .fib import parse_fib
from ..equation.mtef import read_object_pool_equations
from ..xls.embedded_chart import extract_embedded_chart_html
from .models import (
    DocCharStyle,
    DocChartPayload,
    DocDocument,
    DocElement,
    DocImage,
    DocImagePayload,
    DocParagraph,
    DocSection,
    DocTable,
    DocTableCell,
    DocTextRun,
    DocVisualPayload,
)
from .parser import parse_doc_document

_OBJECT_POOL_CHART_RE = re.compile(
    r"^ObjectPool/_([0-9]+)/(?:Workbook|Book)$",
    re.IGNORECASE,
)


def _read_object_pool_charts(ole: BoundedOleReader) -> dict[int, str]:
    """Read the editable chart with Workbook/Book in DOC ObjectPool."""

    charts: dict[int, str] = {}
    for stream_name in ole.stream_names(prefix="ObjectPool/"):
        match = _OBJECT_POOL_CHART_RE.match(stream_name)
        if match is None:
            continue
        storage_id = int(match.group(1))
        if storage_id in charts:
            continue
        content = extract_embedded_chart_html(ole.read_stream(stream_name))
        if content:
            charts[storage_id] = content
    return charts


class DocConverter:
    """Convert Word 97–2003 OLE binary stream to section raw blocks."""

    def __init__(self) -> None:
        """Initialize empty output."""

        self.pages: list[list[dict[str, Any]]] = []

    def convert(self, file_binary: BinaryIO) -> None:
        """Read input OLE streams, parse DOC and generate model-list."""

        file_bytes = read_stream_bytes_from_start(file_binary)
        with BoundedOleReader(file_bytes) as ole:
            if ole.has_stream("EncryptedPackage") or ole.has_stream("EncryptionInfo"):
                raise LegacyOfficeEncryptedError("encrypted OOXML package is not a binary DOC")
            word_document = ole.read_stream("WordDocument")
            fib = parse_fib(word_document)
            if fib.base.encrypted or fib.base.obfuscated:
                raise LegacyOfficeEncryptedError("password-protected DOC is unsupported")
            preferred = "1Table" if fib.base.uses_1table else "0Table"
            alternate = "0Table" if preferred == "1Table" else "1Table"
            table_stream = ole.read_stream(preferred, required=False)
            if not table_stream:
                table_stream = ole.read_stream(alternate, required=bool(fib.base.complex))
            data_stream = ole.read_stream("Data", required=False)
            native_equations = read_object_pool_equations(ole)
            native_charts = _read_object_pool_charts(ole)
            document = parse_doc_document(
                word_document,
                table_stream,
                data_stream,
                fib,
                native_equations=native_equations,
                native_charts=native_charts,
            )
        self.pages = self._document_pages(document)

    @staticmethod
    def _style_names(style: DocCharStyle) -> list[str]:
        """Convert DOC character attributes into DocVortex rich text style names."""

        names: list[str] = []
        if style.bold:
            names.append("bold")
        if style.italic:
            names.append("italic")
        if style.underline:
            names.append("underline")
        if style.emphasis:
            names.append("emphasis")
        if style.strike:
            names.append("strikethrough")
        if style.superscript:
            names.append("superscript")
        elif style.subscript:
            names.append("subscript")
        return names

    @classmethod
    def _rich_text(cls, runs: Iterable[DocTextRun], *, trim: bool = True) -> list[dict[str, Any]]:
        """Convert DOC runs directly into structured Span."""

        spans: list[dict[str, Any]] = []
        segments: list[OfficeRichTextSegment] = []

        def flush_segments() -> None:
            """Output the common rich text accumulated before the formula boundary."""

            if not segments:
                return
            extend_inline_spans(spans, build_rich_text_from_segments(segments, trim_plain_edges=trim and not spans))
            segments.clear()

        for run in runs:
            if not run.text:
                continue
            if run.formula:
                flush_segments()
                latex = run.text.replace("<", r"\lt ").replace(">", r"\gt ")
                append_equation_span(spans, latex)
                continue
            segments.append(
                OfficeRichTextSegment(
                    text=run.text,
                    style=cls._style_names(run.style),
                    hyperlink=run.hyperlink,
                )
            )
        flush_segments()
        return strip_span_dicts(spans) if trim else spans

    @classmethod
    def _cell_rich_text_html(cls, runs: Iterable[DocTextRun], *, trim: bool = True) -> str:
        """Serialize DOC table cell runs to secure HTML."""
        parts: list[str] = []
        segments: list[OfficeRichTextSegment] = []

        def flush_segments() -> None:
            """Output cumulative cell text at formula boundary."""
            if not segments:
                return
            parts.append(build_rich_text_html_from_segments(segments, trim_plain_edges=trim and not parts))
            segments.clear()

        for run in runs:
            if not run.text:
                continue
            if run.formula:
                flush_segments()
                latex = run.text.replace("<", r"\lt ").replace(">", r"\gt ")
                parts.append(f"<eq>{escape(latex, quote=False)}</eq>")
                continue
            segments.append(
                OfficeRichTextSegment(
                    text=run.text,
                    style=cls._style_names(run.style),
                    hyperlink=run.hyperlink,
                )
            )
        flush_segments()
        return "".join(parts)

    @staticmethod
    def _plain_text(paragraph: DocParagraph) -> str:
        """Return the visible text of the paragraph without internal markup."""

        return "".join(run.text for run in paragraph.runs)

    @staticmethod
    def _serialize_image(payload: DocImagePayload) -> str | None:
        """Multiplexing Office picture serialization and vector occupancy strategy."""

        return serialize_office_image(
            payload.data,
            part_name=f"image.{payload.extension}",
            content_type=payload.content_type,
            render_size_emu=payload.render_size_emu,
        )

    @classmethod
    def _image_block(cls, payload: DocVisualPayload) -> dict[str, Any] | None:
        """Convert pictures, formulas or chart payloads to corresponding raw block."""

        if isinstance(payload, DocChartPayload):
            block: dict[str, Any] = {
                "type": BlockType.CHART,
                "content": payload.content,
            }
            if payload.preview is not None:
                image_base64 = cls._serialize_image(payload.preview)
                if image_base64:
                    block["image_base64"] = image_base64
            return block

        if payload.equation_latex:
            return {
                "type": BlockType.EQUATION,
                "content": payload.equation_latex,
            }

        image_base64 = cls._serialize_image(payload)
        if image_base64 is None:
            return None
        return {"type": BlockType.IMAGE, "image_base64": image_base64}

    @classmethod
    def _toc_runs(cls, paragraph: DocParagraph) -> list[DocTextRun]:
        """Removed tab/page number for typesetting purposes only from the end of the TOC paragraph."""

        runs = list(paragraph.runs)
        while runs and re.fullmatch(r"[\s\t]*\d+[\s\t]*", runs[-1].text):
            runs.pop()
        if runs:
            cleaned = re.sub(r"\t+\s*\d+\s*$", "", runs[-1].text)
            if cleaned != runs[-1].text:
                last = runs[-1]
                runs[-1] = DocTextRun(cleaned, last.style, last.hyperlink)
        return runs

    @classmethod
    def _toc_anchor(cls, paragraph: DocParagraph) -> str | None:
        """Read the internal bookmark pointed to by the TOC hyperlink."""

        for run in paragraph.runs:
            if run.hyperlink and run.hyperlink.startswith("#") and len(run.hyperlink) > 1:
                return run.hyperlink[1:]
        return paragraph.anchor

    @classmethod
    def _append_index_item(
        cls,
        page: list[dict[str, Any]],
        stack: list[dict[str, Any]],
        paragraph: DocParagraph,
    ) -> None:
        """Append a TOC paragraph to the index tree of the corresponding level."""

        content = cls._rich_text(cls._toc_runs(paragraph))
        for payload in paragraph.images:
            if isinstance(payload, DocImagePayload) and payload.equation_latex:
                append_equation_span(content, payload.equation_latex)
        if not content:
            return
        level = min(max(paragraph.toc_level or 0, 0), 8)
        while len(stack) > level + 1:
            stack.pop()
        while len(stack) < level + 1:
            index_block: dict[str, Any] = {
                "type": BlockType.INDEX,
                "ilevel": len(stack),
                "content": [],
            }
            if stack:
                stack[-1]["content"].append(index_block)
            else:
                page.append(index_block)
            stack.append(index_block)
        leaf: dict[str, Any] = {"type": BlockType.TEXT, "content": content}
        anchor = cls._toc_anchor(paragraph)
        if anchor:
            leaf["anchor"] = anchor
        stack[level]["content"].append(leaf)

    @classmethod
    def _append_list_item(
        cls,
        page: list[dict[str, Any]],
        stack: list[dict[str, Any]],
        identity: int | None,
        paragraph: DocParagraph,
    ) -> int:
        """Append a DOC list paragraph to the nested raw list tree."""

        info = paragraph.list_info
        if info is None:
            return identity or -1
        content = cls._rich_text(paragraph.runs)
        for payload in paragraph.images:
            if isinstance(payload, DocImagePayload) and payload.equation_latex:
                append_equation_span(content, payload.equation_latex)
        if not content and not paragraph.images:
            return identity or info.identity
        if identity != info.identity:
            stack.clear()
            identity = info.identity
        level = min(max(info.level, 0), 8)
        while len(stack) > level + 1:
            stack.pop()
        while len(stack) < level + 1:
            list_block: dict[str, Any] = {
                "type": BlockType.LIST,
                "attribute": "ordered" if info.ordered else "unordered",
                "ilevel": len(stack),
                "content": [],
            }
            if info.ordered:
                list_block["start"] = info.start
            if stack:
                stack[-1]["content"].append(list_block)
            else:
                page.append(list_block)
            stack.append(list_block)
        current = stack[level]
        expected = "ordered" if info.ordered else "unordered"
        if current.get("attribute") != expected:
            del stack[level:]
            return cls._append_list_item(page, stack, None, paragraph)
        if content:
            leaf: dict[str, Any] = {"type": BlockType.TEXT, "content": content}
            if info.label:
                leaf["list_label"] = info.label
            current["content"].append(leaf)
        return info.identity

    @classmethod
    def _paragraph_blocks(cls, paragraph: DocParagraph) -> list[dict[str, Any]]:
        """Project non-table of contents, non-ordinary list paragraphs to raw blocks."""

        content = cls._rich_text(paragraph.runs)
        blocks: list[dict[str, Any]] = []
        formula_runs = [run for run in paragraph.runs if run.formula and run.text]
        ordinary_text = "".join(run.text for run in paragraph.runs if not run.formula).strip()
        list_info = paragraph.list_info
        exact_label = list_info.label if list_info is not None and list_info.ordered else None
        if exact_label and (paragraph.is_title or paragraph.heading_level is not None):
            content = [*text_spans(f"{exact_label} "), *content]
        if (
            formula_runs
            and not ordinary_text
            and not (paragraph.is_title or paragraph.heading_level is not None or paragraph.is_caption or paragraph.is_code)
        ):
            blocks.extend({"type": BlockType.EQUATION, "content": run.text} for run in formula_runs)
        elif content:
            if paragraph.is_title:
                block: dict[str, Any] = {
                    "type": BlockType.DOC_TITLE,
                    "level": 1,
                    "content": content,
                }
            elif paragraph.heading_level is not None:
                block = {
                    "type": BlockType.PARAGRAPH_TITLE,
                    "level": min(max(paragraph.heading_level + 1, 2), 6),
                    "is_numbered_style": False,
                    "content": content,
                }
            elif paragraph.is_caption:
                block = {"type": RAW_CAPTION, "content": content}
            elif paragraph.is_code:
                block = {"type": BlockType.CODE, "content": cls._plain_text(paragraph)}
            else:
                block = {"type": BlockType.TEXT, "content": content}
            if paragraph.anchor and block["type"] in {BlockType.DOC_TITLE, BlockType.PARAGRAPH_TITLE}:
                block["anchor"] = paragraph.anchor
            blocks.append(block)
        for payload in paragraph.images:
            image = cls._image_block(payload)
            if image is not None:
                blocks.append(image)
        return blocks

    @classmethod
    def _cell_list_html(cls, paragraphs: list[DocParagraph]) -> str:
        """Serialize consecutive table cell list paragraphs into nested HTML list."""

        result: list[str] = []
        stack: list[str] = []
        for paragraph in paragraphs:
            info = paragraph.list_info
            if info is None:
                while stack:
                    result.append(f"</{stack.pop()}>")
                result.append(cls._cell_paragraph_html(paragraph))
                continue
            level = min(max(info.level, 0), 8)
            tag = "ol" if info.ordered else "ul"
            while len(stack) > level + 1:
                result.append(f"</{stack.pop()}>")
            while len(stack) < level + 1:
                result.append(f"<{tag}>")
                stack.append(tag)
            if stack[-1] != tag:
                result.append(f"</{stack.pop()}><{tag}>")
                stack.append(tag)
            label = f"{escape(info.label)} " if info.label and info.ordered else ""
            result.append(
                f"<li>{label}{cls._cell_rich_text_html(paragraph.runs)}{cls._cell_images_html(paragraph.images)}</li>"
            )
        while stack:
            result.append(f"</{stack.pop()}>")
        return "".join(result)

    @classmethod
    def _cell_paragraph_html(cls, paragraph: DocParagraph) -> str:
        """Serialize a table cell paragraph and its inline picture."""

        parts: list[str] = []
        content = cls._cell_rich_text_html(paragraph.runs, trim=False)
        if content:
            parts.append(f"<p>{content}</p>")
        parts.append(cls._cell_images_html(paragraph.images))
        return "".join(parts)

    @classmethod
    def _cell_images_html(
        cls,
        payloads: list[DocVisualPayload],
    ) -> str:
        """Serialize pictures or comment formulas in table paragraphs to inline HTML."""

        parts: list[str] = []
        for payload in payloads:
            if isinstance(payload, DocChartPayload):
                if payload.preview is not None:
                    image = cls._serialize_image(payload.preview)
                    if image:
                        parts.append(f'<img src="{escape(image, quote=True)}"/>')
                parts.append(payload.content)
                continue
            if payload.equation_latex:
                parts.append(f"<eq>{escape(payload.equation_latex, quote=False)}</eq>")
                continue
            image = cls._serialize_image(payload)
            if image:
                parts.append(f'<img src="{escape(image, quote=True)}"/>')
        return "".join(parts)

    @classmethod
    def _cell_html(cls, cell: DocTableCell) -> str:
        """Recursively serialize paragraphs and nested tables in cells."""

        parts: list[str] = []
        paragraph_buffer: list[DocParagraph] = []

        def flush() -> None:
            """Output the current continuous paragraph buffer."""

            nonlocal paragraph_buffer
            if paragraph_buffer:
                parts.append(cls._cell_list_html(paragraph_buffer))
                paragraph_buffer = []

        for block in cell.blocks:
            if isinstance(block, DocParagraph):
                paragraph_buffer.append(block)
            elif isinstance(block, DocTable):
                flush()
                parts.append(cls._table_html(block))
            elif isinstance(block, DocImage):
                flush()
                if block.payload.equation_latex:
                    parts.append(f"<eq>{escape(block.payload.equation_latex, quote=False)}</eq>")
                    continue
                image = cls._serialize_image(block.payload)
                if image:
                    parts.append(f'<img src="{escape(image, quote=True)}"/>')
        flush()
        return "".join(parts)

    @classmethod
    def _table_html(cls, table: DocTable) -> str:
        """Serialize Word table with rowspan/colspan and nested content."""

        rows: list[str] = []
        for row in table.rows:
            tag = "th" if row.header else "td"
            cells: list[str] = []
            for cell in row.cells:
                attributes: list[str] = []
                if cell.row_span > 1:
                    attributes.append(f'rowspan="{cell.row_span}"')
                if cell.col_span > 1:
                    attributes.append(f'colspan="{cell.col_span}"')
                suffix = f" {' '.join(attributes)}" if attributes else ""
                cells.append(f"<{tag}{suffix}>{cls._cell_html(cell)}</{tag}>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows)}</table>"

    @classmethod
    def _element_blocks(cls, element: DocElement) -> list[dict[str, Any]]:
        """Convert text semantic elements to raw block."""

        if isinstance(element, DocImage):
            image = cls._image_block(element.payload)
            return [image] if image is not None else []
        if isinstance(element, DocTable):
            return [{"type": BlockType.TABLE, "content": cls._table_html(element)}]
        return cls._paragraph_blocks(element)

    @classmethod
    def _auxiliary_contents(cls, paragraphs: list[DocParagraph]) -> list[list[dict[str, Any]]]:
        """Remove duplicate header and footer paragraphs and filter pure page numbers."""

        result: list[list[dict[str, Any]]] = []
        seen: set[str] = set()
        for paragraph in paragraphs:
            content = cls._rich_text(paragraph.runs)
            plain = cls._plain_text(paragraph).strip()
            visible = inline_span_plain_text(content)
            if not content or plain.isdigit() or visible in seen:
                continue
            seen.add(visible)
            result.append(content)
        return result

    @classmethod
    def _section_page(cls, section: DocSection) -> list[dict[str, Any]]:
        """Convert a section and append the page auxiliary text stably to the end."""

        page: list[dict[str, Any]] = []
        list_stack: list[dict[str, Any]] = []
        list_identity: int | None = None
        index_stack: list[dict[str, Any]] = []
        for element in section.elements:
            if isinstance(element, DocParagraph) and element.is_toc:
                list_stack.clear()
                list_identity = None
                cls._append_index_item(page, index_stack, element)
                for payload in element.images:
                    if isinstance(payload, DocImagePayload) and payload.equation_latex:
                        continue
                    image = cls._image_block(payload)
                    if image is not None:
                        page.append(image)
                continue
            index_stack.clear()
            if (
                isinstance(element, DocParagraph)
                and element.list_info is not None
                and not (element.is_title or element.heading_level is not None)
            ):
                list_identity = cls._append_list_item(page, list_stack, list_identity, element)
                for payload in element.images:
                    if isinstance(payload, DocImagePayload) and payload.equation_latex:
                        continue
                    image = cls._image_block(payload)
                    if image is not None:
                        page.append(image)
                continue
            list_stack.clear()
            list_identity = None
            page.extend(cls._element_blocks(element))
        page.extend({"type": BlockType.HEADER, "content": content} for content in cls._auxiliary_contents(section.headers))
        page.extend({"type": BlockType.FOOTER, "content": content} for content in cls._auxiliary_contents(section.footers))
        page.extend(
            {"type": BlockType.PAGE_FOOTNOTE, "content": content} for content in cls._auxiliary_contents(section.footnotes)
        )
        return page

    @classmethod
    def _document_pages(cls, document: DocDocument) -> list[list[dict[str, Any]]]:
        """Convert the entire DOC and leave at least one empty section page."""

        pages = [cls._section_page(section) for section in document.sections]
        return pages or [[]]
