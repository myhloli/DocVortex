"""验证区域分析公开接口的证据所有权、缓存与输出契约。"""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import MagicMock

import pytest

from docvortex.analyzers.pdf import (
    PDFTablePage,
    PDFTableRecoveryError,
    PDFTableResult,
    PDFTextEvidence,
    PDFTextScriptLine,
    PDFTextScriptRange,
    apply_text_evidence,
    prepare_table_page,
    prepare_text_evidence,
    recover_table_region,
)
from docvortex.document.pdf import PDFPageTextGeometry
from docvortex.public_api import PUBLIC_API


def test_public_api_manifest_exports_exist() -> None:
    """静态清单中的模块和符号必须真实可用，不接受过期或仅存在于测试的声明。"""
    for module, names in PUBLIC_API.items():
        imported = import_module(module)
        for name in names:
            if f"{module}.{name}" in PUBLIC_API:
                assert import_module(f"{module}.{name}")
            else:
                assert hasattr(imported, name), f"{module}.{name}"


def test_new_api_annotations_resolve() -> None:
    """公共类型和函数不依赖调用方预加载旧模块才能解析注解。"""
    for value in (
        PDFTablePage,
        PDFTableResult,
        PDFTextEvidence,
        prepare_text_evidence,
        apply_text_evidence,
        prepare_table_page,
        recover_table_region,
    ):
        assert get_type_hints(value)


@pytest.mark.parametrize(
    "module",
    [
        "docvortex.document.pdf.geometry",
        "docvortex.document.pdf.visual_geometry",
        "docvortex.document.pdf.document",
        "docvortex.document.pdf.text.contracts",
        "docvortex.analyzers.native.pdf.shared",
        "docvortex.analyzers.native.pdf.text_styles",
        "docvortex.analyzers.native.html.contracts",
        "docvortex.foundation.geometry",
        "docvortex.foundation.text",
        "docvortex.foundation.hyperlink",
        "docvortex.foundation.image",
        "docvortex.foundation.image_payload",
        "docvortex.foundation.platform",
    ],
)
def test_replaced_modules_are_removed(module: str) -> None:
    """被替代的 Python 入口本轮直接删除，不保留静态或动态兼容转发。"""
    assert find_spec(module) is None


def test_text_evidence_excludes_formula_regions_and_preserves_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """区域中心排除规则保留边界语义，并且不修改源字符与原脚本区间。"""
    from docvortex.analyzers.native.pdf.inline import scripts

    line = PDFTextScriptLine(
        bbox=(0.0, 0.0, 40.0, 20.0),
        text="x2y3",
        script_ranges=(
            PDFTextScriptRange(1, 2, "superscript", (10.0, 2.0, 15.0, 8.0), 2, True),
            PDFTextScriptRange(3, 4, "subscript", (30.0, 12.0, 35.0, 18.0), 2, False),
        ),
        source_index=0,
        angle=0,
    )
    monkeypatch.setattr(scripts, "detect_pdf_text_script_lines", lambda *_args, **_kwargs: [line])
    geometry = PDFPageTextGeometry([], {}, {})
    original = deepcopy(geometry)
    page = MagicMock(size=(100.0, 100.0), rotation=0)
    page.get_drawing_lines.return_value = []
    page.get_link_annotations.return_value = []
    evidence = prepare_text_evidence(page, geometry=geometry, excluded_script_regions=[(8.0, 0.0, 18.0, 10.0)])
    assert evidence.geometry is geometry
    assert geometry == original
    assert [(item.start, item.end, item.style) for item in evidence.scripts[0].script_ranges] == [(3, 4, "subscript")]
    assert len(line.script_ranges) == 2
    page.get_chars_with_geometry.assert_not_called()
    page.get_drawing_lines.assert_called_once()
    page.get_link_annotations.assert_called_once()


def test_table_page_is_reused_and_returns_materialized_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个区域共享同页原语，结果直接携带最终 HTML 且不包含页面句柄。"""
    from docvortex.analyzers.native.pdf import table_materialization

    page = MagicMock(size=(100.0, 200.0))
    page.get_drawing_lines.return_value = []
    page.get_path_infos.return_value = []
    geometry = PDFPageTextGeometry([], {}, {})
    prepared = prepare_table_page(page, geometry=geometry)
    recover = MagicMock(
        return_value=(
            "<table><tr><td>x<sup>2</sup></td></tr></table>",
            SimpleNamespace(source="vector_grid", confidence=1.0, diagnostics=()),
        )
    )
    monkeypatch.setattr(table_materialization, "recover_table_result", recover)
    results = [recover_table_region(prepared, (10, 20, 90, 80), angle=angle) for angle in (0, 90)]
    assert all(result.html == "<table><tr><td>x<sup>2</sup></td></tr></table>" for result in results)
    assert [call.args[0].angle for call in recover.call_args_list] == [0, 90]
    page.get_chars_with_geometry.assert_not_called()
    page.get_drawing_lines.assert_called_once()
    page.get_path_infos.assert_called_once()
    assert prepared.geometry is geometry
    monkeypatch.setattr(table_materialization, "recover_table_result", lambda *_args: None)
    assert recover_table_region(prepared, (10, 20, 90, 80)) is None


def test_apply_evidence_does_not_modify_its_inputs() -> None:
    """最终物化仅更新目标 blocks，允许同一证据安全用于另一个结果。"""
    evidence = PDFTextEvidence(
        (100.0, 100.0),
        scripts=(
            PDFTextScriptLine(
                (10, 10, 30, 20),
                "x2",
                (PDFTextScriptRange(1, 2, "superscript", (20, 10, 25, 15), 1, False),),
                0,
                0,
            ),
        ),
    )
    before = deepcopy(evidence)
    blocks = [{"type": "text", "bbox": [0.1, 0.1, 0.3, 0.2], "content": "x2"}]
    apply_text_evidence(blocks, evidence)
    assert evidence == before
    assert blocks[0]["content"] == [
        {"type": "text", "content": "x"},
        {"type": "text", "content": "2", "styles": ["superscript"]},
    ]


def test_table_failure_stages_remain_distinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构恢复异常带原始原因，HTML 物化异常保持原异常类型向外传播。"""
    from docvortex.analyzers.native.pdf import table_materialization

    page = PDFTablePage((100.0, 100.0), PDFPageTextGeometry([], {}, {}))
    monkeypatch.setattr(table_materialization, "recover_native_pdf_table", MagicMock(side_effect=ValueError("recovery")))
    with pytest.raises(PDFTableRecoveryError) as caught:
        recover_table_region(page, (10, 10, 90, 90))
    assert isinstance(caught.value.__cause__, ValueError)
    monkeypatch.setattr(table_materialization, "recover_native_pdf_table", MagicMock(return_value=object()))
    monkeypatch.setattr(
        table_materialization, "render_native_table_html_with_scripts", MagicMock(side_effect=ValueError("materialization"))
    )
    with pytest.raises(ValueError, match="materialization"):
        recover_table_region(page, (10, 10, 90, 90))
