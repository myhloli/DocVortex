"""Construct models and semantic documents without external images for the imported native stage regression."""

from docvortex.api import analyze
from docvortex.document.source import HtmlSourceContext
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.schema import FileSuffix, MiddleJson, ModelJson


def analyze_native_test_document(
    data: bytes,
    *,
    file_suffix: FileSuffix,
    source_context: HtmlSourceContext | None = None,
) -> tuple[MiddleJson, ModelJson]:
    """Really perform native analysis and deterministic post-processing, retaining inline image payloads to be verified."""
    model = analyze(data, file_suffix=file_suffix, source_context=source_context).model_json
    return model_json_to_middle_json(model), model
