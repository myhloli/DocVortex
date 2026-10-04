"""Body, auxiliary text for DocVortex table block and access rules for bbox."""

from __future__ import annotations

import math
from typing import Any

from bs4 import BeautifulSoup

from ...schema import BlockType

from .html import _build_front_cache, _scan_rows
from .models import MAX_HEADER_ROWS, BlockDict, CalculationBBox, TableMergeState
from .rules import is_table_continuation_text


def _bbox_for_calculation(bbox: Any) -> CalculationBBox | None:
    """The normalized bbox is copied and upscaled to thousandths integers, the original fields remain unchanged."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in bbox):
        return None

    values = tuple(float(value) for value in bbox)
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        return None

    x0, y0, x1, y1 = (int(round(value * 1000)) for value in values)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _table_children(table_block: BlockDict) -> list[BlockDict]:
    """Read the legal dict sub-block under the table root block."""
    content = table_block.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _find_table_body_block(table_block: BlockDict) -> BlockDict | None:
    """Find the body sub-block in dict table block."""
    for block in _table_children(table_block):
        if block.get("type") == BlockType.TABLE_BODY:
            return block
    return None


def _build_post_body_child_index(table_block: BlockDict, offset: int) -> int | None:
    """Generate security index after table body for footnote copied to previous table."""
    body_block = _find_table_body_block(table_block)
    if body_block is None:
        return None
    body_index = body_block.get("index")
    if not isinstance(body_index, int):
        return None

    child_indices = [block.get("index") for block in _table_children(table_block) if isinstance(block.get("index"), int)]
    return max([body_index, *child_indices]) + offset


def _block_text(block: BlockDict) -> str:
    """Recursively read the text content of dict block for use in table continuation mark judgment."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(_block_text(child) for child in content if isinstance(child, dict))


def _is_continuation_caption(caption_block: BlockDict) -> bool:
    """Determine whether the dict caption text has a table continuation mark."""
    return is_table_continuation_text(_block_text(caption_block))


def _is_post_table_non_continuation_caption(table_block: BlockDict, caption_block: BlockDict) -> bool:
    """Determine whether caption is mistakenly linked to a new paragraph title below the table.

    This type caption is located below table body and does not contain the continuation mark; it should not be used as
    The new title of the current table blocks cross-page relationship judgment.
    """
    if _is_continuation_caption(caption_block):
        return False

    body_block = _find_table_body_block(table_block)
    if body_block is None:
        return False

    body_bbox = _bbox_for_calculation(body_block.get("bbox"))
    caption_bbox = _bbox_for_calculation(caption_block.get("bbox"))
    if body_bbox is None or caption_bbox is None:
        return False

    return caption_bbox[1] >= body_bbox[3]


def _build_table_state(table_block: BlockDict, max_header_rows: int = MAX_HEADER_ROWS) -> TableMergeState | None:
    """Building structure cache from dict table block, illegal principal safety returns null."""
    body_block = _find_table_body_block(table_block)
    if body_block is None:
        return None

    html = body_block.get("content")
    if not isinstance(html, str) or not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody") or soup.find("table")
    rows = soup.find_all("tr")
    if tbody is None or not rows:
        return None

    scan = _scan_rows(rows)
    if scan.total_cols <= 0 or scan.last_nonempty_row_metrics is None:
        return None
    front_header_info, front_first_data_row_metrics = _build_front_cache(rows, max_header_rows=max_header_rows)

    return TableMergeState(
        owner_block=table_block,
        body_block=body_block,
        soup=soup,
        tbody=tbody,
        rows=rows,
        total_cols=scan.total_cols,
        front_header_info=front_header_info,
        front_first_data_row_metrics=front_first_data_row_metrics,
        last_data_row_metrics=scan.last_nonempty_row_metrics,
        row_effective_cols=scan.row_effective_cols,
        tail_occupied=scan.tail_occupied,
    )


def _get_or_create_table_state(
    table_block: BlockDict,
    state_cache: dict[int, TableMergeState],
    max_header_rows: int = MAX_HEADER_ROWS,
) -> TableMergeState | None:
    """Reuse HTML structure scan results by table dict object identity."""
    cache_key = id(table_block)
    state = state_cache.get(cache_key)
    if state is not None:
        return state

    try:
        state = _build_table_state(table_block, max_header_rows=max_header_rows)
    except (AssertionError, TypeError, ValueError):
        return None
    if state is not None:
        state_cache[cache_key] = state
    return state
