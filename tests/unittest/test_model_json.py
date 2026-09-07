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
    """构造包含全部必填元数据的严格 ModelJson 测试对象。"""
    return ModelJson(
        pages=pages if pages is not None else [[{"type": "text", "content": inline("正文")}]],
        page_index_map=page_index_map if page_index_map is not None else [],
        file_suffix="docx",
        producer=Producer(name="docvortex", version="3.4.0"),
        extensions={},
    )


def test_non_empty_page_index_map_represents_partial_input() -> None:
    """验证非空页号映射表示显式抽页并与 raw pages 一一对应。"""
    model_json = _model_json(pages=[[], []], page_index_map=[3, 5])

    assert model_json.page_index_map == [3, 5]
    assert model_json.is_full_document is False
    assert model_json.resolved_page_indices == [3, 5]


def test_empty_model_json_resolves_to_no_page_indices() -> None:
    """验证空文档仍属于整本解析且不会产生虚构页号。"""
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
    """验证显式抽页映射不允许截断、重复、逆序或负数。"""
    with pytest.raises(ValidationError, match=message):
        _model_json(pages=[[], []], page_index_map=page_index_map)


@pytest.mark.parametrize(
    "pages",
    ["[]", [{}], [[[]]]],
)
def test_model_json_rejects_invalid_page_structure(pages: object) -> None:
    """验证 pages 必须保持页列表、块列表和块字典三层结构。"""
    with pytest.raises(ValidationError):
        ModelJson(
            pages=pages,
            page_index_map=[],
            file_suffix="pdf",
            producer=Producer(name="docvortex", version="3.4.0"),
            extensions={},
        )
