"""验证真实 Office 样例的完整分页及公式语义。"""

from pathlib import Path

import pytest
from docvortex.api import analyze
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.schema import BlockType, FileSuffix, MiddleJson, ModelJson

_OFFICE_SAMPLE_DIR = Path(__file__).parents[2] / "demo/office_docs"


def analyze_sample(data: bytes, *, file_suffix: FileSuffix) -> tuple[MiddleJson, ModelJson]:
    """对真实样例执行引擎分析与确定性后处理。"""
    model = analyze(data, file_suffix=file_suffix).model_json
    return model_json_to_middle_json(model), model


@pytest.mark.parametrize(
    ("file_suffix", "expected_page_count"),
    [("docx", 3), ("pptx", 6), ("xlsx", 3)],
)
def test_native_office_real_samples(file_suffix: str, expected_page_count: int) -> None:
    """验证统一入口可直接分析三类真实 Office 样例并返回完整分页结果。"""
    sample_path = _OFFICE_SAMPLE_DIR / f"{file_suffix}_01.{file_suffix}"

    middle_json, model_json = analyze_sample(
        sample_path.read_bytes(),
        file_suffix=file_suffix,  # type: ignore[arg-type]
    )

    assert isinstance(middle_json, MiddleJson)
    assert isinstance(model_json, ModelJson)
    assert middle_json.is_full_document is True
    assert len(middle_json.pages) == expected_page_count
    assert all(page.page_idx == page_idx for page_idx, page in enumerate(middle_json.pages))
    assert len(model_json.pages) == expected_page_count
    assert all(isinstance(page, list) for page in model_json.pages)
    assert model_json.page_index_map == []
    assert model_json.file_suffix == file_suffix
    if file_suffix == "docx":
        model_equations = [
            block
            for page_model_list in model_json.pages
            for block in page_model_list
            if block.get("type") == BlockType.EQUATION
        ]
        middle_equations = [block for page in middle_json.pages for block in page.blocks if block.type == BlockType.EQUATION]
        assert model_equations
        assert len(middle_equations) == len(model_equations)
        assert "interline_equation" not in middle_json.to_json()
