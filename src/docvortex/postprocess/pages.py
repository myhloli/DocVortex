"""The sole conversion boundary from validated ModelJson to a list of PageInfo objects."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import TypeAdapter

from .lists import fix_office_index_title_blocks, fix_office_paragraph_titles
from .page_blocks import process_page_blocks
from .paragraphs import merge_para_text_blocks
from ..content.table import merge_table
from ..schema import ModelJson, PageInfo

PAGE_INFO_LIST_ADAPTER = TypeAdapter(list[PageInfo])


def _document_uses_bbox(model_list: list[list[dict[str, Any]]]) -> bool:
    """Distinguish PDF from Office input using bboxes anywhere in the document, avoiding misclassification on a blank first page."""
    return any(
        block.get("bbox") is not None for page_model_list in model_list for block in page_model_list if isinstance(block, dict)
    )


def _remove_private_block_metadata(block: dict[str, Any]) -> None:
    """Recursively remove temporary analysis fields before constructing typed objects."""
    for field_name in (
        "lines",
        "_lines",
        "angle",
        "score",
        "label",
        "_reference_start",
        "_paragraph_boundary",
        "_native_annotation_parent_bbox",
    ):
        block.pop(field_name, None)
    content = block.get("content")
    if isinstance(content, list):
        for child in content:
            if isinstance(child, dict):
                _remove_private_block_metadata(child)


def _blocks_to_raw_page_info(
    page_model_list: list[dict[str, Any]],
    *,
    page_idx: int,
    use_bbox: bool,
) -> dict[str, Any]:
    """Run single-page postprocessing and retain a raw dictionary for subsequent cross-page processing."""
    page_blocks = process_page_blocks(page_model_list, use_bbox=use_bbox)
    page_blocks.sort(key=lambda block: block["index"])
    return {"page_idx": page_idx, "blocks": page_blocks}


def blocks_to_page_info(
    page_model_list: list[dict[str, Any]],
    *,
    page_idx: int = 0,
    use_bbox: bool | None = None,
) -> PageInfo:
    """Convert a page's raw blocks into a validated PageInfo object without side effects."""
    copied_blocks = deepcopy(page_model_list)
    resolved_use_bbox = _document_uses_bbox([copied_blocks]) if use_bbox is None else use_bbox
    if not resolved_use_bbox:
        fix_office_paragraph_titles([copied_blocks])
        fix_office_index_title_blocks([copied_blocks])
    raw_page = _blocks_to_raw_page_info(
        copied_blocks,
        page_idx=page_idx,
        use_bbox=resolved_use_bbox,
    )
    for block in raw_page["blocks"]:
        _remove_private_block_metadata(block)
    return PageInfo.model_validate(raw_page)


def model_json_to_pages(model_json: ModelJson) -> list[PageInfo]:
    """Build recursively serializable PageInfo objects from validated ModelJson without side effects."""
    page_indices = model_json.resolved_page_indices
    copied_model_list = deepcopy(model_json.pages)
    use_bbox = _document_uses_bbox(copied_model_list)
    if not use_bbox:
        fix_office_paragraph_titles(copied_model_list)
        fix_office_index_title_blocks(copied_model_list)

    raw_pages = [
        _blocks_to_raw_page_info(
            page_model_list,
            page_idx=page_idx,
            use_bbox=use_bbox,
        )
        for page_model_list, page_idx in zip(copied_model_list, page_indices, strict=True)
    ]
    if use_bbox:
        merge_para_text_blocks(raw_pages)
        merge_table(raw_pages)

    for page in raw_pages:
        for block in page["blocks"]:
            _remove_private_block_metadata(block)
    return PAGE_INFO_LIST_ADAPTER.validate_python(raw_pages)
