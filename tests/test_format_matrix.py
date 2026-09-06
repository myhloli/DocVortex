"""覆盖全部十五种原生文档输入与七种输出的独立端到端矩阵。"""

from __future__ import annotations

from pathlib import Path

import pytest

import docvortex
from docvortex.render.contracts import RenderFormat
from _epub_test_utils import build_epub_fixture
from _odf_test_utils import build_odt_fixture, build_odp_fixture, build_ods_fixture
from _ofd_test_utils import build_multi_document_ofd


def source_payload(suffix: str) -> bytes:
    """使用迁移的公共样例和合成容器构造原生输入。"""
    root = Path(__file__).resolve().parents[1]
    builders = {
        "epub": build_epub_fixture,
        "ofd": build_multi_document_ofd,
        "odt": build_odt_fixture,
        "odp": build_odp_fixture,
        "ods": build_ods_fixture,
    }
    if suffix in builders:
        return builders[suffix]()
    if suffix == "csv":
        return b"name,value\nalpha,1\nbeta,2\n"
    if suffix == "html":
        return b"<html><h1>Title</h1><p>Native conversion</p></html>"
    if suffix == "pdf":
        return (root / "demo/pdfs/mixed_elements_pages_39_40.pdf").read_bytes()
    return next((root / "demo/office_docs").glob(f"*.{suffix}")).read_bytes()


@pytest.mark.parametrize(
    "suffix", ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf", "csv", "html", "epub", "ofd", "odt", "ods", "odp"]
)
def test_all_native_formats_render_all_targets(suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """每种原生格式完成分析、后处理和七种目标编码，不导入宿主。"""
    if suffix != "pdf":
        from docvortex import content

        def forbidden(_pages: object) -> None:
            """非 PDF 格式不应调用 PDF 专用文字清洗。"""
            raise AssertionError("PDF normalization used for another input format")

        monkeypatch.setattr(content, "normalize_pdf_model_text", forbidden)
    result = docvortex.parse(source_payload(suffix), file_suffix=suffix, keep_model_json=True)
    assert result.middle_json.pages
    assert result.model_json is not None
    before = result.to_dict()
    for target in RenderFormat:
        artifact = docvortex.render_artifact(result.middle_json, target, assets=result.assets)
        assert artifact.content, (suffix, target)
    assert result.to_dict() == before
