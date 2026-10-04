"""HTML table Secure materialization of native tables to pageable ReportLab."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol

from bs4 import NavigableString, Tag
from pydantic import ValidationError
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable, Image, LongTable, Paragraph, Table, TableStyle

from ....foundation._hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from ....schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, InlineStyle, TextSpan, parse_inline_spans
from ..common.html_table import (
    MAX_NESTED_TABLE_DEPTH,
    HtmlTableCell,
    HtmlTableError,
    HtmlTableGrid,
    HtmlTableSource,
)
from ..common.html_table import (
    parse_html_tables as _parse_common_html_tables,
)
from .styles import BORDER_COLOR, SURFACE_COLOR, PdfStyleSet
from .table_content import PdfTableContent

_BLOCK_TAGS = {"address", "article", "blockquote", "div", "figcaption", "footer", "header", "li", "p", "section"}
_SKIPPED_TAGS = {"script", "style", "template", "noscript"}
_NATURAL_MEASURE_WIDTH = 1e6
_SPATIAL_VERTICAL_PADDING = 1


def _spatial_leading(font_size: float) -> float:
    """Let actual paragraphs and conservative line-height lower bounds use the same leading rules and floating-point order."""
    return font_size * 11 / 8.5


class PdfTableError(HtmlTableError):
    """Indicates that the HTML table structure or PDF table geometry cannot be safely materialized."""


@dataclass(frozen=True, slots=True)
class SpatialTableOptions:
    """Compact table parameters used only by the original layout and do not change public rendering options."""

    font_size: float = 8.5


@dataclass(frozen=True, slots=True)
class _ColumnPlan:
    """Also saves the final column width and content intrinsic width for correct measurement by the parent nested table."""

    widths: list[float]
    minimum: float
    preferred: float


class _SpatialTable(Table):
    """The fixed area table retains its inherent width to avoid mistaking the allocated width as the minimum width when nesting."""

    def __init__(self, data: list[list[object]], plan: _ColumnPlan, repeat_rows: int) -> None:
        """Construct native tables with determined column widths and record size constraints for reuse by outer layers."""
        self.column_plan = plan
        super().__init__(data, colWidths=plan.widths, repeatRows=repeat_rows, splitByRow=1, splitInRow=1, hAlign="LEFT")


class _PdfLongTable(LongTable):
    """Prioritize pagination by entire rows, and only split super-tall rows when the new page cannot fit."""

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        """First disable in-line splitting and try paging, and then enable the original bottom-up capability after the top of the page fails."""
        split_in_row = self.splitInRow
        self.splitInRow = 0
        try:
            parts = super().split(availWidth, availHeight)
        finally:
            self.splitInRow = split_in_row
        for part in parts:
            part.splitInRow = split_in_row
        frame = getattr(self, "_frame", None)
        if not parts and (frame is None or frame._atTop):
            parts = super().split(availWidth, availHeight)
        return parts


class ParagraphBuilder(Protocol):
    """Define the callback for creating rich text in table cells Paragraph."""

    def __call__(self, spans: list[InlineSpan], style: object, max_width: float) -> Paragraph:
        """Construct span in the cell row into Paragraph with the specified width."""
        ...


class HtmlImageBuilder(Protocol):
    """Define the callback for table cells to create offline pictures or placeholder Flowable."""

    def __call__(self, source: str, max_width: float, alt_text: str) -> Flowable:
        """Convert HTML img source to a picture or loose placeholder."""
        ...


def parse_html_tables(source: HtmlTableSource) -> tuple[HtmlTableGrid, ...]:
    """Reuse the common grid parsing and keep the PDF private exception type unchanged."""
    try:
        return _parse_common_html_tables(source)
    except HtmlTableError as exc:
        raise PdfTableError(str(exc)) from exc


def build_pdf_tables(
    source: HtmlTableSource,
    *,
    available_width: float,
    styles: PdfStyleSet,
    build_paragraph: ParagraphBuilder,
    build_image: HtmlImageBuilder,
    depth: int = 1,
    spatial: SpatialTableOptions | None = None,
    prepared: PdfTableContent | None = None,
) -> tuple[Table, ...]:
    """Recursively convert the HTML table into a PDF table that supports merged cells and repeated headers."""
    if depth > MAX_NESTED_TABLE_DEPTH:
        raise PdfTableError(f"Nested table depth exceeds {MAX_NESTED_TABLE_DEPTH}")
    if available_width <= 0:
        raise PdfTableError("available_width must be positive")
    if spatial is not None and depth == 1:
        styles = replace(
            styles,
            table_cell=styles.table_cell.clone(
                "Spatial Table Cell", fontSize=spatial.font_size, leading=_spatial_leading(spatial.font_size), autoLeading="max"
            ),
            table_header=styles.table_header.clone(
                "Spatial Table Header",
                fontSize=spatial.font_size,
                leading=_spatial_leading(spatial.font_size),
                autoLeading="max",
            ),
        )
    prepared = prepared if prepared is not None else PdfTableContent(source)
    prepared.begin_build()
    grids = prepared.grids(source)
    return tuple(
        _build_pdf_table(
            grid,
            available_width=available_width,
            styles=styles,
            build_paragraph=build_paragraph,
            build_image=build_image,
            depth=depth,
            spatial=spatial,
            prepared=prepared,
        )
        for grid in grids
    )


def _build_pdf_table(
    grid: HtmlTableGrid,
    *,
    available_width: float,
    styles: PdfStyleSet,
    build_paragraph: ParagraphBuilder,
    build_image: HtmlImageBuilder,
    depth: int,
    spatial: SpatialTableOptions | None = None,
    prepared: PdfTableContent,
) -> Table:
    """Materialize a grid, write cell contents, merge regions and fix print styles."""
    column_plan = (
        _content_column_widths(grid, available_width, styles, build_paragraph, build_image, depth, spatial, prepared)
        if spatial is not None
        else None
    )
    column_widths = column_plan.widths if column_plan is not None else _column_widths(available_width, grid.column_count)
    horizontal_padding, vertical_padding = (2, _SPATIAL_VERTICAL_PADDING) if spatial is not None else (5, 4)
    data: list[list[object]] = [["" for _ in range(grid.column_count)] for _ in range(grid.row_count)]
    commands: list[tuple[object, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), horizontal_padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), horizontal_padding),
        ("TOPPADDING", (0, 0), (-1, -1), vertical_padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), vertical_padding),
    ]
    for row_index in grid.header_rows:
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), SURFACE_COLOR))
    for placement in grid.cells:
        cell_width = sum(column_widths[placement.column : placement.column + placement.colspan]) - horizontal_padding * 2
        style = styles.table_header if placement.is_header else styles.table_cell
        data[placement.row][placement.column] = _cell_flowables(
            placement.tag,
            max_width=max(1.0, cell_width),
            style=style,
            styles=styles,
            build_paragraph=build_paragraph,
            build_image=build_image,
            depth=depth,
            spatial=spatial,
            prepared=prepared,
        )
        if placement.rowspan > 1 or placement.colspan > 1:
            commands.append(
                (
                    "SPAN",
                    (placement.column, placement.row),
                    (placement.end_column, placement.end_row),
                )
            )
    repeat_rows = 0
    while repeat_rows in grid.header_rows:
        repeat_rows += 1
    if column_plan is not None:
        table = _SpatialTable(data, column_plan, repeat_rows)
    else:
        table_class = _PdfLongTable if depth == 1 else Table
        table = table_class(data, colWidths=column_widths, repeatRows=repeat_rows, splitByRow=1, splitInRow=1, hAlign="LEFT")
    table.setStyle(TableStyle(commands))
    return table


def _cell_flowables(
    cell: Tag,
    *,
    max_width: float,
    style: object,
    styles: PdfStyleSet,
    build_paragraph: ParagraphBuilder,
    build_image: HtmlImageBuilder,
    depth: int,
    spatial: SpatialTableOptions | None = None,
    prepared: PdfTableContent,
    measure: bool = False,
) -> list[Flowable]:
    """Convert cell text, images, and directly nested tables to Flowable in safe order."""
    flowables: list[Flowable] = []
    content = prepared.cell(cell)
    if content.spans:
        flowables.append(
            content.paragraph(
                build_paragraph,
                style,
                max_width,
                prepared.style_key(style),
                consume=not measure,
                fragment_key=prepared.fragment_key(style) if spatial is not None else None,
            )
        )
    for source, alt in content.images:
        flowables.append(build_image(source, max_width, alt))
    for nested in content.nested:
        flowables.extend(
            build_pdf_tables(
                nested,
                available_width=max_width,
                styles=styles,
                build_paragraph=build_paragraph,
                build_image=build_image,
                depth=depth + 1,
                spatial=spatial,
                prepared=prepared,
            )
        )
    if not flowables:
        flowables.append(
            content.paragraph(
                build_paragraph,
                style,
                max_width,
                prepared.style_key(style),
                consume=not measure,
                fragment_key=prepared.fragment_key(style) if spatial is not None else None,
            )
        )
    return flowables


def _content_column_widths(
    grid: HtmlTableGrid,
    width: float,
    styles: PdfStyleSet,
    build_paragraph: ParagraphBuilder,
    build_image: HtmlImageBuilder,
    depth: int,
    spatial: SpatialTableOptions,
    prepared: PdfTableContent,
) -> _ColumnPlan:
    """Allocate column widths according to the minimum and natural width of real cells, and use merged cells as cross-column constraints."""
    minimum = [spatial.font_size + 4 for _ in range(grid.column_count)]
    preferred = minimum.copy()
    for cell in sorted(grid.cells, key=lambda item: item.colspan):
        style = styles.table_header if cell.is_header else styles.table_cell
        key = (id(cell.tag), prepared.style_key(style))
        # The inherent width of normal text is independent of the allocated width; resources, formulas, and nested tables are still measured according to this constraint.
        cached = prepared.widths.get(key) if prepared.cell(cell.tag).plain else None
        if cached is None:
            cached = _cell_widths(cell, width, style, styles, build_paragraph, build_image, depth, spatial, prepared)
            if prepared.cell(cell.tag).plain:
                prepared.remember_widths(key, cached)
        min_width, natural_width = cached
        for widths, required in ((minimum, min_width + 4), (preferred, natural_width + 4)):
            columns = range(cell.column, cell.column + cell.colspan)
            deficit = max(0.0, required - sum(widths[index] for index in columns)) / cell.colspan
            for index in columns:
                widths[index] += deficit
    preferred = [max(low, high) for low, high in zip(minimum, preferred)]
    low_sum, high_sum = sum(minimum), sum(preferred)
    if width <= low_sum:
        # The extremely narrow area retains the true minimum width, which is reconstructed from the outer layer and then scaled, and the padding cannot be squeezed out of the cell.
        return _ColumnPlan(minimum, low_sum, high_sum)
    if width >= high_sum:
        return _ColumnPlan([value + (width - high_sum) / grid.column_count for value in preferred], low_sum, high_sum)
    ratio = (width - low_sum) / (high_sum - low_sum)
    return _ColumnPlan([low + ratio * (high - low) for low, high in zip(minimum, preferred)], low_sum, high_sum)


def _cell_widths(cell, width, style, styles, build_paragraph, build_image, depth, spatial, prepared) -> tuple[float, float]:
    """Measures the true minimum and natural width of a cell, without saving variable objects participating in wrap."""
    flows = _cell_flowables(
        cell.tag,
        max_width=max(1.0, width - 4),
        style=style,
        styles=styles,
        build_paragraph=build_paragraph,
        build_image=build_image,
        depth=depth,
        spatial=spatial,
        prepared=prepared,
        measure=True,
    )
    if len(flows) == 1 and isinstance(flows[0], Paragraph) and prepared.cell(cell.tag).unstyled:
        measured = _simple_cjk_widths(flows[0], prepared)
        if measured is not None:
            return measured
    min_width = natural_width = 0.0
    for flow in flows:
        if isinstance(flow, Paragraph):
            measured_minimum = _paragraph_minimum_width(flow)
            flow.wrap(_NATURAL_MEASURE_WIDTH, 1e9)
            min_width = max(min_width, measured_minimum)
            natural_width = max([natural_width, *flow.getActualLineWidths0()])
        elif isinstance(flow, _SpatialTable):
            min_width = max(min_width, flow.column_plan.minimum)
            natural_width = max(natural_width, flow.column_plan.preferred)
        else:
            measured_width, _ = flow.wrap(width, 1e9)
            min_width = max(min_width, measured_width if isinstance(flow, Image) else flow.minWidth())
            natural_width = max(natural_width, measured_width)
    return min_width, natural_width


def _simple_cjk_widths(paragraph: Paragraph, prepared: PdfTableContent) -> tuple[float, float] | None:
    """Directly measure the width of simple CJK content that does not cause line breaks, and retain the character-by-character accumulation and margin restoration of ReportLab."""
    style = paragraph.style
    if (
        style.wordWrap != "CJK"
        or paragraph.bulletText
        or style.endDots
        or any(getattr(style, name, 0) for name in ("leftIndent", "rightIndent", "firstLineIndent"))
        or getattr(paragraph, "_splitpara", False)
        or any(
            hasattr(fragment, "cbDefn") or hasattr(fragment, "lineBreak") or type(getattr(fragment, "text", None)) is not str
            for fragment in paragraph.frags
        )
    ):
        return None
    minimum = total = 0.0
    for fragment in paragraph.frags:
        for character in fragment.text:
            width = prepared.character_width(fragment.fontName, fragment.fontSize, character)
            if not isfinite(width) or width < 0:
                return None
            total += width
            if total > _NATURAL_MEASURE_WIDTH:
                return None
            if not character.isspace():
                minimum = max(minimum, width)
    # getActualLineWidths0 uses width - extraSpace and cannot directly return total to change the floating point rounding.
    return minimum, _NATURAL_MEASURE_WIDTH - (_NATURAL_MEASURE_WIDTH - total)


def _paragraph_minimum_width(paragraph: Paragraph) -> float:
    """CJK Paragraphs have minimum column widths based on true breakable characters while retaining the full width of inline formulas."""
    if paragraph.style.wordWrap != "CJK":
        return paragraph.minWidth()
    widths = [0.0]
    for fragment in paragraph.frags:
        image = getattr(fragment, "cbDefn", None)
        if image is not None:
            widths.append(float(image.width))
        text = getattr(fragment, "text", "")
        widths.extend(pdfmetrics.stringWidth(char, fragment.fontName, fragment.fontSize) for char in text if not char.isspace())
    return max(widths)


def _html_cell_spans(cell: Tag) -> list[InlineSpan]:
    """Convert non-image, non-nested table content in cells to strict InlineSpan."""
    spans: list[InlineSpan] = []
    for child in cell.children:
        spans.extend(_html_node_spans(child, styles=(), allow_links=True))
    if not spans:
        return []
    try:
        return parse_inline_spans(spans)
    except (TypeError, ValidationError, ValueError) as exc:
        raise PdfTableError("HTML table cell inline content is invalid") from exc


def _html_node_spans(node: object, *, styles: tuple[InlineStyle, ...], allow_links: bool) -> list[InlineSpan]:
    """Recursively parse safe HTML rich text tags, ignoring active content and independent visual nodes."""
    if isinstance(node, NavigableString):
        text = str(node)
        return [TextSpan(type="text", content=text, styles=list(styles))] if text else []
    if not isinstance(node, Tag):
        return []
    name = (node.name or "").lower()
    if name in _SKIPPED_TAGS or name in {"img", "table"}:
        return []
    if name == "br":
        return [TextSpan(type="text", content="\n", styles=list(styles))]
    if name == "eq":
        content = node.get_text()
        return [EquationInlineSpan(type="equation_inline", content=content)] if content.strip() else []
    if name == "code":
        content = node.get_text()
        return [CodeInlineSpan(type="code_inline", content=content)] if content else []
    style_name: InlineStyle | None = {
        "b": "bold",
        "strong": "bold",
        "i": "italic",
        "em": "italic",
        "u": "underline",
        "s": "strikethrough",
        "del": "strikethrough",
        "sup": "superscript",
        "sub": "subscript",
    }.get(name)
    child_styles = tuple(dict.fromkeys((*styles, style_name))) if style_name is not None else styles
    if name == "a" and allow_links:
        children = [
            span
            for child in node.children
            for span in _html_node_spans(child, styles=child_styles, allow_links=False)
            if not isinstance(span, HyperlinkSpan)
        ]
        target = sanitize_hyperlink_target(
            node.get("href"),
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        if target is not None and children:
            try:
                return [HyperlinkSpan(type="hyperlink", url=target, content=children)]  # type: ignore[arg-type]
            except ValidationError:
                pass
        return children
    spans = [span for child in node.children for span in _html_node_spans(child, styles=child_styles, allow_links=allow_links)]
    if name in _BLOCK_TAGS and spans and not _spans_end_with_newline(spans):
        spans.append(TextSpan(type="text", content="\n"))
    return spans


def _spans_end_with_newline(spans: list[InlineSpan]) -> bool:
    """Determine whether the current span sequence has ended with a normal text line break."""
    return bool(spans and isinstance(spans[-1], TextSpan) and spans[-1].content.endswith("\n"))


def _column_widths(total_width: float, column_count: int) -> list[float]:
    """Deterministically divide the available width evenly among all logical columns."""
    if column_count <= 0:
        raise PdfTableError("Table must contain at least one column")
    base = total_width / column_count
    return [base for _ in range(column_count)]


__all__ = [
    "HtmlTableCell",
    "HtmlTableGrid",
    "MAX_NESTED_TABLE_DEPTH",
    "PdfTableError",
    "build_pdf_tables",
    "parse_html_tables",
]
