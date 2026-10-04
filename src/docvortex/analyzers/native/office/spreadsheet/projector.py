"""XLS Neutral worksheet projector multiplexed with XLSX."""

from __future__ import annotations

import collections
import html
from collections.abc import Callable, Iterator
from typing import Any

from loguru import logger
from openpyxl.cell.rich_text import CellRichText
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .....schema import BlockType
from ..._shared.hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from .....content.spans import text_spans
from .html import EQUATION_BOOKENDS, render_spreadsheet_table
from .models import AnchoredBlock, DataRegion, ExcelCell, ExcelTable, FormulaMap, SheetImage
from .region_discovery import (
    ConnectedRegion,
    discover_connected_regions,
    keep_maximal_by_semantic_sets,
    select_best_gap_candidate,
)


class _MergedCellLookup:
    """Cache the merged cell range by row to avoid repeatedly scanning the openpyxl merged area during parsing."""

    def __init__(self, sheet: Worksheet):
        """Build the 0-based coordinate index from the merged area of the worksheet."""
        self._merged_row_intervals: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        self._hidden_row_intervals: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        self._anchor_spans: dict[tuple[int, int], tuple[int, int]] = {}

        for merged in sheet.merged_cells.ranges:
            min_row = merged.min_row - 1
            max_row = merged.max_row - 1
            min_col = merged.min_col - 1
            max_col = merged.max_col - 1

            self._anchor_spans[(min_row, min_col)] = (
                max_row - min_row + 1,
                max_col - min_col + 1,
            )

            for row in range(min_row, max_row + 1):
                self._merged_row_intervals[row].append((min_col, max_col))
                hidden_start_col = min_col + 1 if row == min_row else min_col
                if hidden_start_col <= max_col:
                    self._hidden_row_intervals[row].append((hidden_start_col, max_col))

        for intervals in self._merged_row_intervals.values():
            intervals.sort()
        for intervals in self._hidden_row_intervals.values():
            intervals.sort()

    @staticmethod
    def _contains_interval(
        row_intervals: dict[int, list[tuple[int, int]]],
        row: int,
        col: int,
    ) -> bool:
        """Determine whether the coordinates of 0-based fall into any column range of the specified row."""
        for start_col, end_col in row_intervals.get(row, []):
            if start_col <= col <= end_col:
                return True
            if start_col > col:
                break
        return False

    def contains_merged_cell(self, row: int, col: int) -> bool:
        """Determine whether the coordinates of 0-based belong to any merged area."""
        return self._contains_interval(self._merged_row_intervals, row, col)

    def is_hidden_merged_cell(self, row: int, col: int) -> bool:
        """Determine whether the 0-based coordinate is a hidden grid in the merged area other than the upper left corner."""
        return self._contains_interval(self._hidden_row_intervals, row, col)

    def get_anchor_span(self, row: int, col: int) -> tuple[int, int]:
        """Returns rowspan/colspan corresponding to the coordinates of the upper left corner of the merged area, and returns 1x1 for non-merged anchor points."""
        return self._anchor_spans.get((row, col), (1, 1))


class SpreadsheetProjector:
    """Project openpyxl worksheet to stable paged model-list."""

    def __init__(
        self,
        *,
        treat_singleton_as_text: bool = True,
        gap_tolerance: int | None = None,
        include_hidden_sheets: bool = False,
    ) -> None:
        """Saves the worksheet projection configuration and initializes the running state of unformatted proprietary dependencies."""
        self.treat_singleton_as_text = treat_singleton_as_text
        self.gap_tolerance = gap_tolerance
        self.include_hidden_sheets = include_hidden_sheets
        self._reset_projection_state()

    def _reset_projection_state(self) -> None:
        """Reset workbook, pagination, and shared projection state by sheet."""
        self.workbook: Workbook | None = None
        self.pages: list[list[dict[str, Any]]] = []
        self.cur_page: list[dict[str, Any]] = []
        self.math_map: FormulaMap = {}
        self.sheet_images: list[SheetImage] = []
        self.table_image_map: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
        self._merged_cell_lookup_cache: dict[int, _MergedCellLookup] = {}

    def _prepare_sheet_assets(self, sheet: Worksheet) -> None:
        """Prepare common formulas and images, and build media maps used by table cells."""
        self.math_map = self._map_math_formulas_to_cells(sheet)
        self.sheet_images = self._collect_sheet_images(sheet)
        self.table_image_map = collections.defaultdict(list)
        for image in self.sheet_images:
            row, col = image.anchor
            if row is None or col is None:
                continue
            if image.latex:
                self.table_image_map[(row, col)].append(EQUATION_BOOKENDS.format(EQ=image.latex))
            elif image.image_base64:
                self.table_image_map[(row, col)].append(f'<img src="{image.image_base64}" />')

    def _convert_sheet(self, sheet: Worksheet) -> None:
        """Project a worksheet in a stable sequence of tables, charts, additional assets, and stand-alone images."""
        self._prepare_sheet_assets(sheet)
        used_cells, visual_artifacts = self._find_tables_in_sheet(sheet)
        visual_artifacts.extend(self._find_charts_in_sheet(sheet))
        visual_artifacts.extend(self._find_additional_visual_artifacts(used_cells))
        for _, _, block in sorted(
            visual_artifacts,
            key=lambda item: (item[0][0], item[0][1], item[1]),
        ):
            self.cur_page.append(block)
        self._find_images_in_sheet(used_cells)

    def _map_math_formulas_to_cells(self, sheet: Worksheet) -> FormulaMap:
        """Returns formulas for the current worksheet grouped by 0-based cell anchor."""
        return {}

    def _collect_sheet_images(self, sheet: Worksheet) -> list[SheetImage]:
        """Returns the picture or picture formula of the current worksheet sorted by anchor."""
        return []

    def _find_charts_in_sheet(self, sheet: Worksheet) -> list[AnchoredBlock]:
        """Returns the format-specific chart blocks for the current worksheet."""
        return []

    def _find_additional_visual_artifacts(
        self,
        used_cells: set[tuple[int, int]],
    ) -> list[AnchoredBlock]:
        """Returns a format-specific formula or image that has not been absorbed by the table blocks."""
        return []

    def _resolve_cell_image(self, raw_cell_text: str) -> str:
        """Parse format-specific cell image function, which does not generate media by default."""
        return ""

    def _iter_sheets_to_convert(self) -> Iterator[Worksheet]:
        """Traverse visible worksheets in workbook order allowing output."""
        if self.workbook is None:
            return

        for sheet in self.workbook.worksheets:
            if not self.include_hidden_sheets and sheet.sheet_state != Worksheet.SHEETSTATE_VISIBLE:
                logger.debug(f"跳过隐藏工作表：{sheet.title}")
                continue
            yield sheet

    @staticmethod
    def _build_sheet_title_block(sheet_title: str) -> dict:
        """Construct the worksheet title block and reuse the Office title rendering link to output the Markdown title."""
        return {
            "type": BlockType.PARAGRAPH_TITLE,
            "level": 2,
            "content": text_spans(sheet_title),
        }

    @staticmethod
    def _should_emit_sheet_titles(pages: list[list[dict]]) -> bool:
        """Add header only if there are multiple non-empty outputs sheet to avoid single table or empty table noise."""
        return sum(1 for page in pages if page) > 1

    def _prepend_sheet_titles(self, sheet_pages: list[tuple[str, list[dict]]]) -> None:
        """Inserts the sheet title at the beginning of each non-empty page and does not participate in the visual ordering of the Table/Graph."""
        for sheet_title, page in sheet_pages:
            if not page:
                continue
            page.insert(0, self._build_sheet_title_block(sheet_title))

    def _get_block_sort_anchor(self, row: int | None, col: int | None) -> tuple[int, int]:
        """Place missing anchor stable after all valid worksheet coordinates."""
        if row is None or col is None:
            return (10**9, 10**9)
        return row, col

    def _build_block_from_excel_table(self, excel_table: ExcelTable) -> dict:
        """Project table IR to text or table block according to singleton rules."""
        if self.treat_singleton_as_text and len(excel_table.data) == 1 and self._can_render_singleton_as_text(excel_table):
            return {
                "type": BlockType.TEXT,
                "content": text_spans(excel_table.data[0].text),
            }

        return {
            "type": BlockType.TABLE,
            "content": render_spreadsheet_table(excel_table),
        }

    def _find_tables_in_sheet(self, sheet: Worksheet) -> tuple[set[tuple[int, int]], list[tuple[tuple[int, int], int, dict]]]:
        """Discovers the current sheet table and returns the absorbed cell and anchored blocks."""
        used_cells = set()
        visual_artifacts = []
        if self.workbook is not None:
            tables = self._find_data_tables(sheet)  # Detect all data tables in a worksheet

            for order, excel_table in enumerate(tables):
                # Record used cells
                anchor_c, anchor_r = excel_table.anchor
                for cell in excel_table.data:
                    source_row, source_col = self._resolve_excel_cell_source_position(
                        excel_table.anchor,
                        cell,
                    )
                    used_cells.add((source_row, source_col))

                visual_artifacts.append(
                    (
                        self._get_block_sort_anchor(anchor_r, anchor_c),
                        order,
                        self._build_block_from_excel_table(excel_table),
                    )
                )

        return used_cells, visual_artifacts

    def _build_excel_cell(
        self,
        sheet: Worksheet,
        display_row: int,
        display_col: int,
        source_row: int,
        source_col: int,
        row_span: int = 1,
        col_span: int = 1,
    ) -> ExcelCell:
        """Materialize the source worksheet cell completely to neutral ExcelCell."""
        cell = sheet.cell(row=source_row + 1, column=source_col + 1)
        raw_cell_text = str(cell.value) if cell.value is not None else ""
        cell_text = ""
        text_is_html = False
        media_content = []
        if "DISPIMG" in raw_cell_text:
            cell_image = self._resolve_cell_image(raw_cell_text)
            if cell_image:
                media_content.append(cell_image)
        else:
            cell_text, text_is_html = self._cell_value_to_html(cell)
        media_content.extend(self.table_image_map.get((source_row, source_col), []))

        return ExcelCell(
            row=display_row,
            col=display_col,
            text=cell_text,
            row_span=row_span,
            col_span=col_span,
            styles=self._extract_cell_style(cell),
            media=media_content,
            equations=list(self.math_map.get((source_row, source_col), [])),
            text_is_html=text_is_html,
            source_row=source_row,
            source_col=source_col,
        )

    def _build_synthetic_table_from_sheet_selection(self, sheet: Worksheet, rows: list[int], cols: list[int]) -> ExcelTable:
        """Materializes the specified source row and column selection into a compact table IR."""
        selected_coords = {(row, col) for row in rows for col in cols}
        hidden_merge_cells = set()
        merge_spans = {}

        for mr in sheet.merged_cells.ranges:
            top_left = (mr.min_row - 1, mr.min_col - 1)
            if top_left not in selected_coords:
                continue

            selected_rows = [row for row in rows if mr.min_row - 1 <= row <= mr.max_row - 1]
            selected_cols = [col for col in cols if mr.min_col - 1 <= col <= mr.max_col - 1]
            if not selected_rows or not selected_cols:
                continue

            merge_spans[top_left] = (len(selected_rows), len(selected_cols))
            for row in selected_rows:
                for col in selected_cols:
                    if (row, col) != top_left:
                        hidden_merge_cells.add((row, col))

        data = []
        for display_row, source_row in enumerate(rows):
            for display_col, source_col in enumerate(cols):
                if (source_row, source_col) in hidden_merge_cells:
                    continue

                row_span, col_span = merge_spans.get((source_row, source_col), (1, 1))
                data.append(
                    self._build_excel_cell(
                        sheet,
                        display_row,
                        display_col,
                        source_row,
                        source_col,
                        row_span=row_span,
                        col_span=col_span,
                    )
                )

        return ExcelTable(
            anchor=(cols[0], rows[0]),
            num_rows=len(rows),
            num_cols=len(cols),
            data=data,
        )

    def _resolve_excel_cell_source_position(
        self,
        table_anchor: tuple[int, int],
        excel_cell: ExcelCell | None,
        row: int | None = None,
        col: int | None = None,
    ) -> tuple[int, int]:
        """Explicit source coordinates are preferred, otherwise source coordinates are restored via table anchor."""
        if excel_cell is not None:
            if excel_cell.source_row is not None and excel_cell.source_col is not None:
                return excel_cell.source_row, excel_cell.source_col
            row = excel_cell.row
            col = excel_cell.col

        if row is None or col is None:
            raise ValueError("row and col must be provided when excel_cell is None")

        return table_anchor[1] + row, table_anchor[0] + col

    def _can_render_singleton_as_text(self, excel_table: ExcelTable) -> bool:
        """Determine whether a single-cell table can be safely downgraded to plain text block."""
        cell = excel_table.data[0]
        return cell.row_span == 1 and cell.col_span == 1 and not cell.media and not cell.text_is_html and not cell.equations

    def _cell_has_semantic_content(self, excel_table: ExcelTable, cell: ExcelCell) -> bool:
        """Determine whether the cell contains text, media or formula semantics."""
        return bool(cell.text.strip() or any(media.strip() for media in cell.media) or cell.equations)

    def _get_table_semantic_positions(self, excel_table: ExcelTable) -> set[tuple[int, int]]:
        """Returns the coordinates of the source worksheet with semantic content within the table."""
        semantic_positions = set()
        for cell in excel_table.data:
            if not self._cell_has_semantic_content(excel_table, cell):
                continue
            semantic_positions.add(
                self._resolve_excel_cell_source_position(
                    excel_table.anchor,
                    excel_cell=cell,
                )
            )
        return semantic_positions

    def _filter_semantic_subset_tables(self, tables: list[ExcelTable]) -> list[ExcelTable]:
        """Remove duplicate tables whose semantic coordinates are strictly contained in other candidates."""
        semantic_sets = [self._get_table_semantic_positions(table) for table in tables]
        return [tables[index] for index in keep_maximal_by_semantic_sets(semantic_sets)]

    def _sheet_semantic_predicates(
        self, sheet: Worksheet
    ) -> tuple[Callable[[int, int], bool], Callable[[int, int], tuple[int, int]]]:
        """Construct gap score using semantic content and merge span query.

        The semantic judgment is consistent with the materialization result of _build_excel_cell: the ordinary value is non-blank text,
        The DISPIMG formula only counts the content when parsing out the media, and the anchor image and formula mapping are also superimposed.
        """
        merged_lookup = self._get_merged_cell_lookup(sheet)

        def has_semantic_content(row: int, col: int) -> bool:
            """Determine whether the source coordinates contain text, media or formula semantics."""
            cell = sheet._cells.get((row + 1, col + 1))
            if cell is not None and cell.value is not None:
                raw_text = str(cell.value)
                if "DISPIMG" in raw_text:
                    resolved_image = self._resolve_cell_image(raw_text)
                    if resolved_image and resolved_image.strip():
                        return True
                elif raw_text.strip():
                    return True
            if any(media.strip() for media in self.table_image_map.get((row, col), [])):
                return True
            return bool(self.math_map.get((row, col)))

        return has_semantic_content, merged_lookup.get_anchor_span

    def _select_best_gap_candidate(self, sheet: Worksheet) -> tuple[int, float, list[ExcelTable]]:
        """Select the most stable gap tolerance in fixed candidate and preference order."""
        bounds: DataRegion = self._find_true_data_bounds(sheet)
        has_semantic_content, span_at = self._sheet_semantic_predicates(sheet)
        gap_tolerance, penalty, best_regions = select_best_gap_candidate(
            lambda tolerance: self._discover_sheet_regions(sheet, bounds, tolerance),
            has_semantic_content,
            span_at,
        )
        merged_lookup = self._get_merged_cell_lookup(sheet)
        tables = self._filter_semantic_subset_tables(
            [self._materialize_region(sheet, region, merged_lookup) for region in best_regions]
        )
        return gap_tolerance, penalty, tables

    def _select_best_tables(self, sheet: Worksheet) -> list[ExcelTable]:
        """Selects and records the best set of table candidates for the current worksheet."""
        gap_tolerance, penalty, tables = self._select_best_gap_candidate(sheet)
        logger.debug(
            "Selected gap_tolerance={} for sheet '{}' with penalty={:.4f}",
            gap_tolerance,
            sheet.title,
            penalty,
        )
        return tables

    def _find_images_in_sheet(self, used_cells: set[tuple[int, int]] | None = None) -> None:
        """Outputs independent images that are not absorbed by the table and are not formula carriers."""
        if self.workbook is not None:
            for image in self.sheet_images:
                r, c = image.anchor
                if used_cells and r is not None and c is not None and (r, c) in used_cells:
                    continue

                if image.latex:
                    continue
                if image.image_base64:
                    self.cur_page.append(
                        {
                            "type": BlockType.IMAGE,
                            "image_base64": image.image_base64,
                        }
                    )

    def _find_data_tables(self, sheet: Worksheet) -> list[ExcelTable]:
        """Find all compact rectangular data tables in the Excel worksheet.

        parameter:
            sheet: Excel worksheet to be parsed.

        return:
            A list of ExcelTable objects representing all data tables.
        """
        if self.gap_tolerance is None:
            return self._select_best_tables(sheet)
        return self._find_data_tables_with_gap(sheet, self.gap_tolerance)

    def _find_data_tables_with_gap(self, sheet: Worksheet, gap_tolerance: int) -> list[ExcelTable]:
        """Discover tables and remove semantic subset candidates by fixed gap."""
        return self._filter_semantic_subset_tables(self._find_data_tables_with_gap_raw(sheet, gap_tolerance))

    def _find_data_tables_with_gap_raw(self, sheet: Worksheet, gap_tolerance: int) -> list[ExcelTable]:
        """Find all data tables in the worksheet under fixed gap_tolerance."""
        bounds: DataRegion = self._find_true_data_bounds(sheet)  # Get real data boundaries
        merged_lookup = self._get_merged_cell_lookup(sheet)
        return [
            self._materialize_region(sheet, region, merged_lookup)
            for region in self._discover_sheet_regions(sheet, bounds, gap_tolerance)
        ]

    def _discover_sheet_regions(
        self,
        sheet: Worksheet,
        bounds: DataRegion,
        gap_tolerance: int,
    ) -> list[ConnectedRegion]:
        """Floods the discovered connected data region by gap_tolerance on the current worksheet."""
        merged_lookup = self._get_merged_cell_lookup(sheet)
        max_row, max_col = bounds.max_row - 1, bounds.max_col - 1

        def has_content(row: int, col: int) -> bool:
            """Check whether the specified cell (0-based index) has content (has a value or belongs to the merged area)."""
            cell = sheet._cells.get((row + 1, col + 1))
            if cell is not None and cell.value is not None:
                return True
            return merged_lookup.contains_merged_cell(row, col)

        # Iterate only over cells that already exist and have values to avoid iter_rows creating a large number of empty cells on large sparse tables.
        return discover_connected_regions(
            has_content,
            self._get_non_empty_cell_positions(sheet, bounds),
            max_row,
            max_col,
            gap_tolerance,
        )

    def _materialize_region(
        self,
        sheet: Worksheet,
        region: ConnectedRegion,
        merged_lookup: _MergedCellLookup,
    ) -> ExcelTable:
        """Materialize the connected area bounding box into a compact table IR, and correctly handle merged cells.

        Traverse the bounding box of the discovery area (the spaces inside bbox are retained as empty cells, maintaining the rectangular layout),
        Skips coordinates obscured by merged cells and hooks the span to the merge anchor point.
        """
        data: list[ExcelCell] = []
        for source_row in range(region.row_start, region.row_end + 1):
            for source_col in range(region.col_start, region.col_end + 1):
                # Skip cells obscured by merged cells (not the upper left corner).
                if merged_lookup.is_hidden_merged_cell(source_row, source_col):
                    continue

                # Calculate merge span (default is 1x1).
                row_span, col_span = merged_lookup.get_anchor_span(source_row, source_col)

                data.append(
                    self._build_excel_cell(
                        sheet,
                        source_row - region.row_start,
                        source_col - region.col_start,
                        source_row,
                        source_col,
                        row_span=row_span,
                        col_span=col_span,
                    )
                )

        return ExcelTable(
            anchor=(region.col_start, region.row_start),
            num_rows=region.num_rows,
            num_cols=region.num_cols,
            data=data,
        )

    def _get_non_empty_cell_positions(
        self,
        sheet: Worksheet,
        bounds: DataRegion,
    ) -> list[tuple[int, int]]:
        """Returns the 0-based coordinates of cells with existing values within the true bounds in row and column order."""
        positions = []
        for cell in sheet._cells.values():
            if cell.value is None:
                continue
            if not (bounds.min_row <= cell.row <= bounds.max_row and bounds.min_col <= cell.column <= bounds.max_col):
                continue
            positions.append((cell.row - 1, cell.column - 1))
        return sorted(positions)

    def _find_true_data_bounds(self, sheet: Worksheet) -> DataRegion:
        """Find the true data boundaries (min/max rows and columns) in a worksheet.

        This function scans all cells and finds all non-empty cells or merged cell ranges.
        Minimum rectangular range, returns the row and column index of the boundary.

        parameter:
            sheet: Worksheet to be analyzed.

        return:
            Minimum rectangular area covering all data and merged cells DataRegion.
            If the worksheet is empty, (1, 1, 1, 1) is returned by default.
        """
        min_row, min_col = None, None
        max_row, max_col = 0, 0

        # Traverse all cells with values and dynamically update the boundaries
        for cell in sheet._cells.values():
            if cell.value is not None:
                r, c = cell.row, cell.column
                min_row = r if min_row is None else min(min_row, r)
                min_col = c if min_col is None else min(min_col, c)
                max_row = max(max_row, r)
                max_col = max(max_col, c)

        # Include the range of merged cells into boundary calculations
        for merged in sheet.merged_cells.ranges:
            min_row = merged.min_row if min_row is None else min(min_row, merged.min_row)
            min_col = merged.min_col if min_col is None else min(min_col, merged.min_col)
            max_row = max(max_row, merged.max_row)
            max_col = max(max_col, merged.max_col)

        # If there is no data in the worksheet, the default value is (1, 1, 1, 1)
        if min_row is None or min_col is None:
            min_row = min_col = max_row = max_col = 1

        return DataRegion(min_row, max_row, min_col, max_col)

    def _get_merged_cell_lookup(self, sheet: Worksheet) -> _MergedCellLookup:
        """Get the worksheet merged cell cache, which is only built once for each sheet in the same round of conversion."""
        cache_key = id(sheet)
        lookup = self._merged_cell_lookup_cache.get(cache_key)
        if lookup is None:
            lookup = _MergedCellLookup(sheet)
            self._merged_cell_lookup_cache[cache_key] = lookup
        return lookup

    @staticmethod
    def _escape_text_with_line_breaks(text: str) -> str:
        """Escape text and uniformly project platform newlines to HTML newlines."""
        return html.escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    @staticmethod
    def _get_cell_hyperlink_target(cell: Any) -> str:
        """Read location outside the cell or within the workbook."""
        hyperlink = getattr(cell, "hyperlink", None)
        if not hyperlink:
            return ""

        target = getattr(hyperlink, "target", None)
        if target:
            return str(target)

        location = getattr(hyperlink, "location", None)
        if location:
            return f"#{location}"

        return ""

    @staticmethod
    def _apply_inline_font_tags(text_html: str, inline_font: Any) -> str:
        """Wrap visible HTML labels in openpyxl inline font order."""
        if not text_html or inline_font is None:
            return text_html

        wrapped = text_html
        if getattr(inline_font, "strike", False) or getattr(inline_font, "u", None):
            wrapped = wrapped.replace(" ", "&nbsp;")
        vert_align = getattr(inline_font, "vertAlign", None)
        if vert_align == "superscript":
            wrapped = f"<sup>{wrapped}</sup>"
        elif vert_align == "subscript":
            wrapped = f"<sub>{wrapped}</sub>"

        if getattr(inline_font, "strike", False):
            wrapped = f"<s>{wrapped}</s>"
        if getattr(inline_font, "u", None):
            wrapped = f"<u>{wrapped}</u>"
        if getattr(inline_font, "i", False):
            wrapped = f"<em>{wrapped}</em>"
        if getattr(inline_font, "b", False):
            wrapped = f"<strong>{wrapped}</strong>"

        return wrapped

    def _cell_value_to_html(self, cell: Any) -> tuple[str, bool]:
        """Convert normal or rich text cells to safe HTML with content-type tag."""
        if cell.value is None:
            return "", False

        safe_target = sanitize_hyperlink_target(
            self._get_cell_hyperlink_target(cell),
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        link_target = html.escape(safe_target, quote=True) if safe_target else ""

        if isinstance(cell.value, CellRichText):
            html_parts = []
            for part in cell.value:
                if hasattr(part, "text"):
                    part_text = self._escape_text_with_line_breaks(str(getattr(part, "text", "")))
                    html_parts.append(
                        self._apply_inline_font_tags(
                            part_text,
                            getattr(part, "font", None),
                        )
                    )
                else:
                    html_parts.append(self._escape_text_with_line_breaks(str(part)))

            rich_text_html = "".join(html_parts)
            if link_target and rich_text_html:
                rich_text_html = f'<a href="{link_target}">{rich_text_html}</a>'
            return rich_text_html, True

        plain_text = str(cell.value)
        if link_target and plain_text:
            escaped_text = self._escape_text_with_line_breaks(plain_text)
            return f'<a href="{link_target}">{escaped_text}</a>', True

        return plain_text, False

    def _extract_cell_style(self, cell: Any) -> dict[str, Any]:
        """Extracts the visible styles currently reserved for IR from the openpyxl cell."""
        style: dict[str, Any] = {}
        if cell.font:
            if cell.font.b:
                style["font-weight"] = "bold"
            if cell.font.i:
                style["font-style"] = "italic"
            if cell.font.u:
                style["text-decoration"] = "underline"
            if cell.font.strike:
                style["text-decoration"] = "line-through"
            if cell.font.color and hasattr(cell.font.color, "rgb") and cell.font.color.rgb:
                # Color might be ARGB "FF000000"
                color = cell.font.color.rgb
                if isinstance(color, str) and len(color) == 8:
                    style["color"] = "#" + color[2:]
                elif isinstance(color, str):
                    style["color"] = "#" + color

        if cell.alignment:
            if cell.alignment.horizontal:
                style["text-align"] = cell.alignment.horizontal
            if cell.alignment.vertical:
                style["vertical-align"] = cell.alignment.vertical

        # Gradient fills do not have patternType; only the existing solid color background is extracted, other fills retain content and the rest of the style.
        fill = cell.fill
        if getattr(fill, "patternType", None) == "solid" and fill.fgColor:
            color = fill.fgColor.rgb
            if hasattr(fill.fgColor, "type") and fill.fgColor.type == "rgb" and color:
                if isinstance(color, str) and len(color) == 8:
                    style["background-color"] = "#" + color[2:]
        return style


__all__ = ["SpreadsheetProjector"]
