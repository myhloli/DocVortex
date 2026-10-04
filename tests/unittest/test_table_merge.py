from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from bs4 import BeautifulSoup

from docvortex.content.table import merge_table, merge_table_content
from docvortex.schema import BlockType

DEFAULT_HTML = "<table><tr><td>A</td><td>B</td></tr></table>"


def _table_body(
    html: str = DEFAULT_HTML,
    *,
    index: int = 0,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """Construct table body test block of normalized bbox."""
    body: dict[str, Any] = {
        "type": BlockType.TABLE_BODY,
        "index": index,
        "bbox": deepcopy(bbox or [0.1, 0.1, 0.9, 0.9]),
        "content": html,
    }
    return body


def _caption(
    text: str,
    *,
    index: int = 0,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """Construct the table caption test block."""
    return {
        "type": BlockType.TABLE_CAPTION,
        "index": index,
        "bbox": deepcopy(bbox or [0.1, 0.05, 0.9, 0.08]),
        "content": text,
    }


def _footnote(text: str, *, index: int = 1) -> dict[str, Any]:
    """Construct the table footnote test block."""
    return {
        "type": BlockType.TABLE_FOOTNOTE,
        "index": index,
        "bbox": [0.1, 0.91, 0.9, 0.95],
        "content": text,
    }


def _table(
    index: int,
    html: str = DEFAULT_HTML,
    *,
    bbox: list[float] | None = None,
    children: list[dict[str, Any]] | None = None,
    cell_merge: list[int] | None = None,
) -> dict[str, Any]:
    """Construct a two-layer dict table test block."""
    table_bbox = deepcopy(bbox or [0.1, 0.1, 0.9, 0.9])
    body = _table_body(
        html,
        index=index,
        bbox=table_bbox,
    )
    table = {
        "type": BlockType.TABLE,
        "index": index,
        "bbox": table_bbox,
        "content": children if children is not None else [body],
    }
    if cell_merge is not None:
        table["cell_merge"] = cell_merge
    return table


def _page(page_idx: int, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Constructs the raw page used by the ModelJson post-processing stage."""
    return {"page_idx": page_idx, "blocks": blocks}


def _noise(block_type: str) -> dict[str, Any]:
    """Construct blocks of negligible noise at page boundaries."""
    return {
        "type": block_type,
        "index": 99,
        "bbox": [0.1, 0.01, 0.9, 0.03],
        "content": "noise",
    }


def _merged_soup(table: dict[str, Any]) -> BeautifulSoup:
    """Read table body HTML in merged results."""
    body = next(child for child in table["content"] if child["type"] == BlockType.TABLE_BODY)
    return BeautifulSoup(body["content"], "html.parser")


def _row_texts(table: dict[str, Any]) -> list[list[str]]:
    """Extract line-by-line cell text of merged HTML."""
    return [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in _merged_soup(table).find_all("tr")]


def test_merge_table_marks_reverse_multi_page_chain_and_cleans_stale_markers() -> None:
    """Verify the reverse order recognition of multi-page chains and clean up the expiration marks of root blocks and sub-blocks."""
    tables = [_table(index) for index in range(3)]
    tables[0]["continues_prev"] = True
    tables[0]["content"][0]["continues_prev"] = True
    pages = [_page(index, [table]) for index, table in enumerate(tables)]

    merge_table(pages)

    assert "continues_prev" not in tables[0]
    assert "continues_prev" not in tables[0]["content"][0]
    assert tables[1]["continues_prev"] is True
    assert tables[2]["continues_prev"] is True

    merge_table(pages)
    assert tables[1]["continues_prev"] is True
    assert tables[2]["continues_prev"] is True


@pytest.mark.parametrize("page_indices", [(0, 2), (2, 1)])
def test_merge_table_requires_consecutive_increasing_page_indices(
    page_indices: tuple[int, int],
) -> None:
    """Verify that neither page jump nor reverse order page_idx will establish a cross-page relationship."""
    previous_table = _table(0)
    current_table = _table(1)

    merge_table(
        [
            _page(page_indices[0], [previous_table]),
            _page(page_indices[1], [current_table]),
        ]
    )

    assert "continues_prev" not in current_table


@pytest.mark.parametrize(
    "block_type",
    [
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.PAGE_FOOTNOTE,
        BlockType.ASIDE_TEXT,
    ],
)
def test_merge_table_skips_boundary_noise(block_type: str) -> None:
    """Verify that specifying header and footer class blocks does not block boundary table scanning."""
    previous_table = _table(0)
    current_table = _table(1)
    pages = [
        _page(0, [previous_table, _noise(block_type)]),
        _page(1, [_noise(block_type), current_table]),
    ]

    merge_table(pages)

    assert current_table["continues_prev"] is True


@pytest.mark.parametrize("barrier_side", ["previous", "current"])
def test_merge_table_is_blocked_by_boundary_semantic_block(barrier_side: str) -> None:
    """Validating that normal text is on a page boundary blocks the table relationship."""
    previous_table = _table(0)
    current_table = _table(1)
    text = _noise(BlockType.TEXT)
    previous_blocks = [previous_table, text] if barrier_side == "previous" else [previous_table]
    current_blocks = [text, current_table] if barrier_side == "current" else [current_table]

    merge_table([_page(0, previous_blocks), _page(1, current_blocks)])

    assert "continues_prev" not in current_table


@pytest.mark.parametrize(
    ("previous_footnotes", "current_caption", "expected"),
    [
        (0, None, True),
        (0, "Table 1", False),
        (0, "Table 1 (continued)", True),
        (1, None, False),
        (1, "Table 1 (continued)", True),
        (2, "Table 1 (continued)", False),
    ],
)
def test_merge_table_applies_caption_and_footnote_rules(
    previous_footnotes: int,
    current_caption: str | None,
    expected: bool,
) -> None:
    """Verify the rules for combining the continuation table caption with the previous table footnote."""
    previous_children = [_table_body()]
    previous_children.extend(_footnote(f"old-{index}", index=index + 1) for index in range(previous_footnotes))
    current_children = [_table_body(index=1)]
    if current_caption is not None:
        current_children.insert(0, _caption(current_caption))
    previous_table = _table(0, children=previous_children)
    current_table = _table(1, children=current_children)

    merge_table([_page(0, [previous_table]), _page(1, [current_table])])

    assert current_table.get("continues_prev", False) is expected


def test_post_table_non_continuation_caption_does_not_block_merge() -> None:
    """Verify that the non-continuation table caption below table body does not participate in blocking judgment."""
    current_children = [
        _table_body(index=1, bbox=[0.1, 0.1, 0.9, 0.7]),
        _caption("Next section", index=2, bbox=[0.1, 0.75, 0.9, 0.8]),
    ]
    current_table = _table(1, children=current_children)

    merge_table([_page(0, [_table(0)]), _page(1, [current_table])])

    assert current_table["continues_prev"] is True


def test_merge_table_enforces_strict_ten_percent_width_threshold() -> None:
    """Reject if the verification width difference is exactly ten percent, and accept if it is less than the threshold."""
    previous_table = _table(0, bbox=[0.1, 0.1, 0.9, 0.9])
    exact_threshold = _table(1, bbox=[0.06, 0.1, 0.94, 0.9])
    pages = [_page(0, [previous_table]), _page(1, [exact_threshold])]

    merge_table(pages)
    assert "continues_prev" not in exact_threshold

    exact_threshold["bbox"] = [0.08, 0.1, 0.92, 0.9]
    exact_threshold["content"][0]["bbox"] = [0.08, 0.1, 0.92, 0.9]
    merge_table(pages)
    assert exact_threshold["continues_prev"] is True


def test_merge_table_uses_boundary_row_metrics_when_total_columns_differ() -> None:
    """After verifying that the total number of columns is different, the judgment is made based on the boundary row effective/actual/number of rendering segments."""
    matching_previous = _table(
        0,
        "<table><tr><td colspan='3'>H</td></tr><tr><td>A</td><td>B</td></tr></table>",
    )
    matching_current = _table(1, "<table><tr><td>C</td><td>D</td></tr></table>")
    merge_table([_page(0, [matching_previous]), _page(1, [matching_current])])
    assert matching_current["continues_prev"] is True

    mismatching_previous = _table(
        0,
        "<table><tr><td colspan='4'>H</td></tr><tr><td colspan='2'>A</td><td>B</td></tr></table>",
    )
    mismatching_current = _table(1, "<table><tr><td colspan='2'>C</td></tr></table>")
    merge_table([_page(0, [mismatching_previous]), _page(1, [mismatching_current])])
    assert "continues_prev" not in mismatching_current


@pytest.mark.parametrize(
    "current_table",
    [
        _table(1, bbox=[10.0, 10.0, 90.0, 90.0]),
        {"type": BlockType.TABLE, "index": 1, "bbox": [0.1, 0.1, 0.9, 0.9], "content": []},
        _table(1, "<table></table>"),
        _table(1, "<table><tr><td colspan='bad'>X</td></tr></table>"),
    ],
)
def test_merge_table_safely_skips_invalid_bbox_body_or_html(current_table: dict[str, Any]) -> None:
    """Verification illegal bbox, table body and HTML are all safely downgraded."""
    current_table = deepcopy(current_table)
    merge_table([_page(0, [_table(0)]), _page(1, [current_table])])
    assert "continues_prev" not in current_table


def test_merge_table_only_changes_markers_and_preserves_normalized_bboxes() -> None:
    """Verify that the thousandth calculation will not write back bbox, and the detection will not modify any table content."""
    previous_table = _table(0)
    current_table = _table(1)
    pages = [_page(0, [previous_table]), _page(1, [current_table])]
    original_pages = deepcopy(pages)

    merge_table(pages)

    assert current_table.pop("continues_prev") is True
    assert pages == original_pages


def test_merge_table_content_removes_repeated_rowspan_header_and_appends_data() -> None:
    """Verify that duplicate headers are removed by rowspan coverage and the current data row is appended."""
    previous_html = (
        "<table><tr><th rowspan='2'>H1</th><th>H2</th></tr><tr><th>H3</th></tr><tr><td>A</td><td>B</td></tr></table>"
    )
    current_html = "<table><tr><th rowspan='2'>H1</th><th>H2</th></tr><tr><th>H3</th></tr><tr><td>C</td><td>D</td></tr></table>"

    merged = merge_table_content(_table(0, previous_html), _table(1, current_html))

    assert merged is not None
    assert _row_texts(merged) == [["H1", "H2"], ["H3"], ["A", "B"], ["C", "D"]]


def test_merge_table_content_repairs_colspan_on_narrower_current_rows() -> None:
    """Verify that the narrower current page data row fills the last cell colspan."""
    previous = _table(0, "<table><tr><td>A</td><td colspan='2'>B</td></tr></table>")
    current = _table(1, "<table><tr><td>C</td><td>D</td></tr></table>")

    merged = merge_table_content(previous, current)

    assert merged is not None
    appended_cells = _merged_soup(merged).find_all("tr")[-1].find_all(["td", "th"])
    assert [cell.get_text() for cell in appended_cells] == ["C", "D"]
    assert appended_cells[-1]["colspan"] == "2"


def test_merge_table_content_clips_overlapped_blank_rowspan_placeholder() -> None:
    """Verify that the current page is repeatedly blank. The rowspan placeholder will be cropped according to the remaining span of the previous page."""
    previous = _table(0, "<table><tr><td rowspan='2'>A</td><td>X</td></tr></table>")
    current = _table(
        1,
        "<table><tr><td rowspan='2'></td><td>Y</td></tr><tr><td>Z</td></tr></table>",
    )

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert _row_texts(merged) == [["A", "X"], ["Y"], ["", "Z"]]
    moved_blank_cell = _merged_soup(merged).find_all("tr")[-1].find_all(["td", "th"])[0]
    assert "rowspan" not in moved_blank_cell.attrs


def test_merge_table_content_replaces_footnote_and_preserves_previous_payloads() -> None:
    """Verify that a pure merge preserves the previous table load, ignores the current caption, and replaces footnote."""
    previous_body = _table_body(
        "<table><tr><th>H</th></tr><tr><td>A</td></tr></table>",
        index=5,
    )
    previous_body["image_base64"] = "previous-image"
    previous = _table(
        5,
        children=[_caption("Original caption", index=4), previous_body, _footnote("old", index=6)],
    )
    previous["image_base64"] = "outer-image"
    current = _table(
        8,
        children=[
            _caption("Table 1 (continued)", index=7),
            _table_body("<table><tr><th>H</th></tr><tr><td>B</td></tr></table>", index=8),
            {**_footnote("new", index=9), "_cross_page": True},
        ],
    )
    previous_original = deepcopy(previous)
    current_original = deepcopy(current)

    merged = merge_table_content(previous, current)

    assert merged is not None
    assert previous == previous_original
    assert current == current_original
    assert merged["index"] == previous["index"]
    assert merged["bbox"] == previous["bbox"]
    assert merged["image_base64"] == "outer-image"
    merged_body = next(child for child in merged["content"] if child["type"] == BlockType.TABLE_BODY)
    assert merged_body["image_base64"] == "previous-image"
    assert _row_texts(merged) == [["H"], ["A"], ["B"]]
    captions = [child["content"] for child in merged["content"] if child["type"] == BlockType.TABLE_CAPTION]
    footnotes = [child for child in merged["content"] if child["type"] == BlockType.TABLE_FOOTNOTE]
    assert captions == ["Original caption"]
    assert [footnote["content"] for footnote in footnotes] == ["new"]
    assert "_cross_page" not in footnotes[0]


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (_table(0, "<table></table>"), _table(1)),
        (_table(0), _table(1, "<table><tr><td colspan='bad'>X</td></tr></table>")),
        (_table(0), {"type": BlockType.TABLE, "bbox": [0.1, 0.1, 0.9, 0.9], "content": []}),
        (_table(0), _table(1, bbox=[0.0, 0.0, 100.0, 100.0])),
    ],
)
def test_merge_table_content_returns_none_for_invalid_input(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> None:
    """Validate content-only operation returns null when encountering an illegal HTML, body, or bbox."""
    previous_original = deepcopy(previous)
    current_original = deepcopy(current)

    assert merge_table_content(previous, current) is None
    assert previous == previous_original
    assert current == current_original
