"""对未嵌入系统字体的真实 PDF 验证完整流程，精确布局金标使用统一 Droid 策略验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from docgale.api import parse, render
from docgale.export.bundle import load_bundle
from docgale.render import RenderFormat


@pytest.mark.parametrize(("file_name", "page_count"), [("中文论文3.pdf", 4), ("中文论文4.pdf", 5)])
def test_system_font_pdf_converts_and_restores_offline(file_name: str, page_count: int, tmp_path: Path) -> None:
    """缺失嵌入字体时仍须产生完整、可重复渲染且可离线恢复的有效文档。"""
    source = Path(__file__).parents[1] / "demo/pdfs" / file_name
    result = parse(source, keep_model_json=True)
    assert len(result.middle_json.pages) == page_count
    assert [page.page_idx for page in result.middle_json.pages] == list(range(page_count))
    assert all(page.blocks for page in result.middle_json.pages)
    before = result.to_dict()
    for target in RenderFormat:
        artifact = render(result.middle_json, target, assets=result.assets)
        assert artifact.content, target
    assert result.to_dict() == before
    bundle = tmp_path / "bundle"
    result.save_bundle(bundle)
    restored = load_bundle(bundle)
    assert restored.to_dict() == before
    assert restored.model_json is not None
    # 结果包会将 raw 图片载荷外置为素材引用，模型字典不要求逐字节等于原始内嵌载荷。
    assert restored.model_json.file_suffix == "pdf"
    assert restored.model_json.resolved_page_indices == list(range(page_count))
    assert restored.export(tmp_path / "restored.html", output_format="html").path.is_file()
