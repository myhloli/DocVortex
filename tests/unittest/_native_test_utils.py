"""为迁入的原生阶段回归构造未外置图片的模型和语义文档。"""

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
    """真实执行原生分析与确定性后处理，保留待验证的内联图片载荷。"""
    model = analyze(data, file_suffix=file_suffix, source_context=source_context).model_json
    return model_json_to_middle_json(model), model
