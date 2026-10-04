"""HTML table to TeX Live longtable/tabular Secure materialization of source code."""

from __future__ import annotations

from typing import TypeAlias

from bs4 import NavigableString, Tag
from pydantic import ValidationError

from ....foundation._hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from ....schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, InlineStyle, TextSpan, parse_inline_spans
from ..common.html_table import (
    MAX_NESTED_TABLE_DEPTH,
    HtmlTableCell,
    HtmlTableError,
    HtmlTableGrid,
    HtmlTableSource,
    parse_html_tables,
)
from .assets import resolve_html_image_path, tex_image_path
from .inline import LatexAnchorRegistry, escape_latex_text, escape_latex_url, render_inline_spans

_BLOCK_TAGS = {"address", "article", "blockquote", "div", "figcaption", "footer", "header", "li", "p", "section"}
_SKIPPED_TAGS = {"script", "style", "template", "noscript"}
_HtmlCellToken: TypeAlias = InlineSpan | Tag


class LatexTableError(HtmlTableError):
    """Indicates that HTML table cannot be safely materialized into the LaTeX form."""


class LatexTableRenderer:
    """Recursive LaTeX table renderer holding image path and anchor context."""

    def __init__(self, *, asset_base_path: str, anchors: LatexAnchorRegistry) -> None:
        """Saves the document-level context required for table cell rendering."""
        self.asset_base_path = asset_base_path
        self.anchors = anchors

    def render(self, source: HtmlTableSource) -> str:
        """Converts one or more top-level HTML table to pageable LaTeX tables."""
        return self._render_source(source, depth=1)

    def _render_source(self, source: HtmlTableSource, *, depth: int) -> str:
        """Recursively parse the table of the current level and perform depth upper limit verification."""
        if depth > MAX_NESTED_TABLE_DEPTH:
            raise LatexTableError(f"Nested table depth exceeds {MAX_NESTED_TABLE_DEPTH}")
        try:
            grids = parse_html_tables(source)
        except HtmlTableError as exc:
            raise LatexTableError(str(exc)) from exc
        return "\n\n".join(self._render_grid(grid, depth=depth) for grid in grids)

    def _render_grid(self, grid: HtmlTableGrid, *, depth: int) -> str:
        """Convert strict placeholder grid to longtable or nested tabular."""
        environment = "longtable" if depth == 1 else "tabular"
        column_spec = self._column_spec(grid.column_count)
        occupied = {
            (row, column): placement
            for placement in grid.cells
            for row in range(placement.row, placement.end_row + 1)
            for column in range(placement.column, placement.end_column + 1)
        }
        lines = [rf"\begin{{{environment}}}{{{column_spec}}}", r"\hline"]
        for row_index in range(grid.row_count):
            segments: list[str] = []
            column_index = 0
            while column_index < grid.column_count:
                placement = occupied[(row_index, column_index)]
                if placement.column != column_index:
                    column_index += 1
                    continue
                if placement.row == row_index:
                    segment = self._render_cell(
                        placement.tag,
                        is_header=placement.is_header,
                        rowspan=placement.rowspan,
                        colspan=placement.colspan,
                        column_count=grid.column_count,
                        depth=depth,
                    )
                else:
                    segment = self._continued_rowspan_placeholder(placement.colspan)
                segments.append(segment)
                column_index += placement.colspan
            lines.append(" & ".join(segments) + r" \\")
            lines.extend(_row_rules(occupied, row_index=row_index, column_count=grid.column_count))
        lines.append(rf"\end{{{environment}}}")
        return "\n".join(lines)

    def _render_cell(
        self,
        cell: Tag,
        *,
        is_header: bool,
        rowspan: int,
        colspan: int,
        column_count: int,
        depth: int,
    ) -> str:
        """Renders a origin cell, combined with the rowspan/colspan declaration."""
        parts = self._render_cell_parts(cell, depth=depth)
        content = r"\par ".join(part for part in parts if part) or "~"
        if is_header:
            content = rf"\cellcolor{{DocVortexTableHeader}}\textbf{{{content}}}"
        width = _cell_width(colspan, column_count)
        if rowspan > 1:
            content = rf"\multirow{{{rowspan}}}{{*}}{{\parbox[t]{{{width}}}{{{content}}}}}"
        if colspan > 1:
            content = rf"\multicolumn{{{colspan}}}{{|p{{{width}}}|}}{{{content}}}"
        return content

    def _render_cell_parts(self, cell: Tag, *, depth: int) -> list[str]:
        """Alternately render inline content, images, and nested tables in the order of the HTML source."""

        parts: list[str] = []
        inline_buffer: list[InlineSpan] = []

        def flush_inline_buffer() -> None:
            """Verify and render token in the current continuous line into a LaTeX fragment."""

            if not inline_buffer:
                return
            spans = _parse_cell_inline_spans(inline_buffer)
            rendered = render_inline_spans(spans, self.anchors)
            if rendered:
                parts.append(rendered)
            inline_buffer.clear()

        for token in _html_cell_tokens(cell):
            if not isinstance(token, Tag):
                inline_buffer.append(token)
                continue
            flush_inline_buffer()
            if token.name == "img":
                parts.append(self._render_image_tag(token))
            elif token.name == "table":
                parts.append(self._render_source(token, depth=depth + 1))
        flush_inline_buffer()
        return parts

    def _render_image_tag(self, image: Tag) -> str:
        """Reads a cell image label and renders it as a LaTeX image or visible fallback."""

        source = image.get("src", "")
        if isinstance(source, list):
            source = ""
        alt = image.get("alt", "image")
        if isinstance(alt, list):
            alt = " ".join(str(item) for item in alt)
        return self._render_image(str(source), str(alt))

    def _render_image(self, source: str, alt_text: str) -> str:
        """Rendering cell image; non-native sidecar degrades to visible link or placeholder."""
        path = resolve_html_image_path(source, self.asset_base_path)
        if path is not None:
            return rf"\includegraphics[width=.85\linewidth,height=.25\textheight,keepaspectratio]{{{tex_image_path(path)}}}"
        label = rf"\textit{{{escape_latex_text(f'image unavailable: {alt_text}')}}}"
        normalized = source.strip()
        if normalized.startswith(("http://", "https://")):
            return rf"\href{{{escape_latex_url(normalized)}}}{{{label}}}"
        return label

    @staticmethod
    def _column_spec(column_count: int) -> str:
        """Generate deterministic equal-width paragraph column declarations in logical column numbers."""
        if column_count <= 0:
            raise LatexTableError("Table must contain at least one column")
        width = _cell_width(1, column_count)
        column = rf">{{\raggedright\arraybackslash}}p{{{width}}}"
        return "|" + "|".join(column for _ in range(column_count)) + "|"

    @staticmethod
    def _continued_rowspan_placeholder(colspan: int) -> str:
        """Write equal column width space for rowspan that is continued from the previous row."""
        return rf"\multicolumn{{{colspan}}}{{|l|}}{{}}" if colspan > 1 else ""


def _cell_width(colspan: int, column_count: int) -> str:
    """Calculate the proportion of linewidth occupied by the current cell based on the total number of columns in the table."""
    ratio = 0.94 * colspan / column_count
    return f"{ratio:.6f}\\linewidth"


def _row_rules(
    occupied: dict[tuple[int, int], HtmlTableCell],
    *,
    row_index: int,
    column_count: int,
) -> list[str]:
    """Only draw lines under the cell column at the end of the current row to avoid horizontal lines cutting through rowspan."""
    ending_columns = [column for column in range(column_count) if occupied[(row_index, column)].end_row == row_index]
    if len(ending_columns) == column_count:
        return [r"\hline"]
    rules: list[str] = []
    range_start: int | None = None
    previous: int | None = None
    for column in ending_columns:
        if range_start is None:
            range_start = column
        elif previous is not None and column != previous + 1:
            rules.append(rf"\cline{{{range_start + 1}-{previous + 1}}}")
            range_start = column
        previous = column
    if range_start is not None and previous is not None:
        rules.append(rf"\cline{{{range_start + 1}-{previous + 1}}}")
    return rules


def _parse_cell_inline_spans(spans: list[InlineSpan]) -> list[InlineSpan]:
    """Verify that a contiguous inline segment of cells does not span images or nested tables."""

    try:
        return parse_inline_spans(spans)
    except (TypeError, ValidationError, ValueError) as exc:
        raise LatexTableError("HTML table cell inline content is invalid") from exc


def _html_cell_tokens(cell: Tag) -> list[_HtmlCellToken]:
    """Returns in-row, picture, and nested tables in cells in order of source token."""

    return [
        token
        for child in cell.children
        for token in _html_node_tokens(
            child,
            styles=(),
            allow_links=True,
        )
    ]


def _html_node_tokens(
    node: object,
    *,
    styles: tuple[InlineStyle, ...],
    allow_links: bool,
) -> list[_HtmlCellToken]:
    """Parse HTML nodes recursively while preserving images and nested tables as order barriers."""

    if isinstance(node, NavigableString):
        text = str(node)
        return [TextSpan(type="text", content=text, styles=list(styles))] if text else []
    if not isinstance(node, Tag):
        return []
    name = (node.name or "").lower()
    if name in _SKIPPED_TAGS:
        return []
    if name in {"img", "table"}:
        return [node]
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
            token
            for child in node.children
            for token in _html_node_tokens(
                child,
                styles=child_styles,
                allow_links=False,
            )
            if not isinstance(token, HyperlinkSpan)
        ]
        target = sanitize_hyperlink_target(
            node.get("href"),
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        return _wrap_hyperlink_tokens(children, target)
    tokens = [
        token
        for child in node.children
        for token in _html_node_tokens(
            child,
            styles=child_styles,
            allow_links=allow_links,
        )
    ]
    if name in _BLOCK_TAGS and tokens and not _tokens_end_with_newline(tokens):
        tokens.append(TextSpan(type="text", content="\n"))
    return tokens


def _wrap_hyperlink_tokens(
    tokens: list[_HtmlCellToken],
    target: str | None,
) -> list[_HtmlCellToken]:
    """Only wrap contiguous InlineSpan within a link, leaving images and nested tables in place."""

    if target is None:
        return tokens
    output: list[_HtmlCellToken] = []
    inline_buffer: list[InlineSpan] = []

    def flush_inline_buffer() -> None:
        """Wrap the current linked inline fragment into a HyperlinkSpan."""

        if not inline_buffer:
            return
        try:
            output.append(
                HyperlinkSpan(
                    type="hyperlink",
                    url=target,
                    content=list(inline_buffer),
                )
            )
        except ValidationError:
            output.extend(inline_buffer)
        inline_buffer.clear()

    for token in tokens:
        if isinstance(token, Tag):
            flush_inline_buffer()
            output.append(token)
        else:
            inline_buffer.append(token)
    flush_inline_buffer()
    return output


def _tokens_end_with_newline(tokens: list[_HtmlCellToken]) -> bool:
    """Determine whether the current token sequence has ended with a normal text line break."""

    return bool(tokens and isinstance(tokens[-1], TextSpan) and tokens[-1].content.endswith("\n"))


__all__ = ["LatexTableError", "LatexTableRenderer"]
