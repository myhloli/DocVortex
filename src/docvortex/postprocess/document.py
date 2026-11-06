"""将规范化分析结果转换为独立的语义文档。"""

from __future__ import annotations
from copy import deepcopy
from ..schema import MiddleJson, ModelJson
from .pages import model_json_to_pages


def model_json_to_middle_json(model_json: ModelJson) -> MiddleJson:
    """只执行确定性后处理，智能增强由调用方另行执行。"""
    extensions = deepcopy(model_json.extensions)
    return MiddleJson(
        pages=model_json_to_pages(model_json, structure_extension=extensions.get("docvortex_pdf_structure")),
        is_full_document=model_json.is_full_document,
        metadata=model_json.metadata.model_copy(deep=True),
        extensions=extensions,
    )


__all__ = ["model_json_to_middle_json"]
