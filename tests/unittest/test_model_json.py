from __future__ import annotations

import pytest
from _span_test_utils import inline
from pydantic import ValidationError

from docvortex.schema import ModelJson, Producer


def _model_json(
    *,
    pages: list[list[dict[str, object]]] | None = None,
    page_index_map: list[int] | None = None,
) -> ModelJson:
    """Constructs a strict ModelJson test object containing all required metadata."""
    return ModelJson(
        pages=pages if pages is not None else [[{"type": "text", "content": inline("正文")}]],
        page_index_map=page_index_map if page_index_map is not None else [],
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="3.4.0")},
        extensions={},
    )


def test_non_empty_page_index_map_represents_partial_input() -> None:
    """Verify that the non-null page number mapping represents an explicit page extraction and corresponds to raw pages one-to-one."""
    model_json = _model_json(pages=[[], []], page_index_map=[3, 5])

    assert model_json.page_index_map == [3, 5]
    assert model_json.is_full_document is False
    assert model_json.resolved_page_indices == [3, 5]


def test_empty_model_json_resolves_to_no_page_indices() -> None:
    """Verify that the empty document still belongs to the entire parsing and does not generate fictitious page numbers."""
    model_json = _model_json(pages=[])

    assert model_json.is_full_document is True
    assert model_json.resolved_page_indices == []


@pytest.mark.parametrize(
    ("page_index_map", "message"),
    [
        ([0], "length mismatch"),
        ([0, 0], "unique"),
        ([1, 0], "increasing"),
        ([0, -1], "non-negative"),
    ],
)
def test_model_json_rejects_invalid_page_index_map(page_index_map: list[int], message: str) -> None:
    """Verify that explicit paging mapping does not allow truncation, duplication, reversal, or negative numbers."""
    with pytest.raises(ValidationError, match=message):
        _model_json(pages=[[], []], page_index_map=page_index_map)


@pytest.mark.parametrize(
    "pages",
    ["[]", [{}], [[[]]]],
)
def test_model_json_rejects_invalid_page_structure(pages: object) -> None:
    """Verification pages must maintain a three-layer structure of page list, block list and block dictionary."""
    with pytest.raises(ValidationError):
        ModelJson(
            pages=pages,
            page_index_map=[],
            metadata={"file_suffix": "pdf", "producer": Producer(name="docvortex", version="3.4.0")},
            extensions={},
        )
