"""Restore ODF embedded chart preview with source data table."""

from __future__ import annotations

from collections.abc import Callable

from lxml import etree  # type: ignore[reportMissingImports]

from .....schema import BlockType
from .constants import qname
from .models import TableGrid
from .table import (
    OdfTableExpansionBudget,
    crop_table_grid,
    parse_cell_range_bounds,
    parse_table_grid,
    table_grid_to_html,
    union_bounds,
)


def _chart_range_bounds(chart: etree._Element) -> tuple[int, int, int, int] | None:
    """Collect the exact cell reference ranges for chart, series, label, and categories."""
    values: list[tuple[int, int, int, int]] = []
    attribute_names = {
        qname("chart", "values-cell-range-address"),
        qname("chart", "label-cell-address"),
        qname("table", "cell-range-address"),
    }
    for element in chart.iter():
        for attribute_name in attribute_names:
            if bounds := parse_cell_range_bounds(element.get(attribute_name, "")):
                values.append(bounds)
    return union_bounds(values)


def _single_nonempty_grid(grids: list[TableGrid]) -> TableGrid | None:
    """A safe fallback candidate is only returned if there is a unique non-empty table within the object."""
    nonempty = [grid for grid in grids if grid.rows]
    return nonempty[0] if len(nonempty) == 1 else None


def parse_chart_block(
    object_root: etree._Element,
    *,
    render_cell: Callable[[etree._Element], str],
    preview_data_uri: str | None,
    table_expansion_budget: OdfTableExpansionBudget | None = None,
) -> dict | None:
    """Construct the chart according to the rules of exact reference first, unique table fallback raw block."""
    chart = next(object_root.iter(qname("chart", "chart")), None)
    if chart is None:
        return None
    grids = [
        parse_table_grid(table, render_cell, expansion_budget=table_expansion_budget)
        for table in object_root.iter(qname("table", "table"))
    ]
    selected: TableGrid | None = None
    if grids and (bounds := _chart_range_bounds(chart)) is not None:
        selected = crop_table_grid(grids[0], bounds)
        if not selected.rows:
            selected = None
    if selected is None:
        selected = _single_nonempty_grid(grids)
    if selected is None:
        return None
    content = table_grid_to_html(selected)
    if not content:
        return None
    block: dict = {"type": BlockType.CHART, "content": content}
    if preview_data_uri:
        block["image_base64"] = preview_data_uri
    return block


__all__ = ["parse_chart_block"]
