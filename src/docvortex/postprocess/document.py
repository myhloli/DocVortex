"""Convert normalized analysis results into an independent semantic document."""

from __future__ import annotations
from copy import deepcopy
from ..schema import MiddleJson, ModelJson
from .pages import model_json_to_pages


def model_json_to_middle_json(model_json: ModelJson) -> MiddleJson:
    """Run only deterministic postprocessing; callers perform intelligent enhancements separately."""
    return MiddleJson(
        pages=model_json_to_pages(model_json),
        is_full_document=model_json.is_full_document,
        metadata=model_json.metadata.model_copy(deep=True),
        extensions=deepcopy(model_json.extensions),
    )


__all__ = ["model_json_to_middle_json"]
