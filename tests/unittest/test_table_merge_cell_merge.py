from __future__ import annotations

from copy import deepcopy
from typing import Any

from bs4 import BeautifulSoup

from docvortex.content.table import merge_table_content
from docvortex.schema import BlockType


def _build_table_block(index: int, html: str, cell_merge: list[int] | None = None) -> dict[str, Any]:
    """Constructs a two-tier dict table block used for content-only merge testing."""
    body: dict[str, Any] = {
        "index": index,
        "type": BlockType.TABLE_BODY,
        "bbox": [0.1, 0.1, 0.9, 0.9],
        "content": html,
    }
    table = {
        "index": index,
        "type": BlockType.TABLE,
        "bbox": [0.1, 0.1, 0.9, 0.9],
        "content": [body],
    }
    if cell_merge is not None:
        table["cell_merge"] = cell_merge
    return table


def _row_texts(table: dict[str, Any]) -> list[list[str]]:
    """Extract the line-by-line cell text of the merged table body."""
    body = table["content"][0]
    soup = BeautifulSoup(body["content"], "html.parser")
    return [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in soup.find_all("tr")]


def test_merge_table_content_applies_partial_cell_merge_from_table() -> None:
    """Verify that after some visual columns are continued, the current first row is retained and the migrated cells are cleared."""
    previous_table = _build_table_block(
        0,
        "<table><tr><td>A</td><td>X</td></tr></table>",
    )
    current_table = _build_table_block(
        1,
        "<table><tr><td>B</td><td>Y</td></tr></table>",
        [1, 0],
    )
    previous_original = deepcopy(previous_table)
    current_original = deepcopy(current_table)

    merged = merge_table_content(previous_table, current_table)

    assert merged is not None
    assert _row_texts(merged) == [["AB", "X"], ["", "Y"]]
    assert previous_table == previous_original
    assert current_table == current_original


def test_merge_table_content_applies_full_cell_merge_and_removes_consumed_row() -> None:
    """After verifying that all visual columns are continued, delete the consumed rows and continue to append subsequent data rows."""
    previous_table = _build_table_block(
        0,
        "<table><tr><td>A</td><td>X</td></tr></table>",
    )
    current_table = _build_table_block(
        1,
        "<table><tr><td>B</td><td>Y</td></tr><tr><td>C</td><td>Z</td></tr></table>",
        [1, 1],
    )

    merged = merge_table_content(previous_table, current_table)

    assert merged is not None
    assert _row_texts(merged) == [["AB", "XY"], ["C", "Z"]]


def test_merge_table_content_supports_fully_consumed_only_current_row() -> None:
    """Verify that the current table has only one row and the merged result is still returned when it is fully consumed by cell_merge."""
    previous_table = _build_table_block(
        0,
        "<table><tr><td>A</td><td>X</td></tr></table>",
    )
    current_table = _build_table_block(
        1,
        "<table><tr><td>B</td><td>Y</td></tr></table>",
        [1, 1],
    )

    merged = merge_table_content(previous_table, current_table)

    assert merged is not None
    assert _row_texts(merged) == [["AB", "XY"]]
