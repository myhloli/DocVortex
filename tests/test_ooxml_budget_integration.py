"""验证共享 OOXML 预算覆盖判型、属性、正文和兼容重试入口。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from docvortex import api
from docvortex.analyzers.native.office.docx import docx_converter
from docvortex.analyzers.native.office.pptx import pptx_converter
from docvortex.analyzers.native.office.xlsx import xlsx_converter
from docvortex.document import detection
from docvortex.errors import DocumentError, ResourceLimitError
from docvortex.foundation import ooxml_zip


def _office_payload(suffix: str, padding: int = 0) -> bytes:
    """构造能正常打开的 Office 文档，并追加不被正文引用的压缩成员。"""
    stream = BytesIO()
    if suffix == "docx":
        from docx import Document

        document = Document()
        document.add_paragraph("Budget regression")
    elif suffix == "pptx":
        from pptx import Presentation

        document = Presentation()
        document.slides.add_slide(document.slide_layouts[0]).shapes.title.text = "Budget regression"
    else:
        from openpyxl import Workbook

        document = Workbook()
        document.active["A1"] = "Budget regression"
    document.save(stream)
    if padding:
        with ZipFile(stream, "a", ZIP_DEFLATED) as package:
            package.writestr("unreferenced-padding.bin", b"x" * padding)
    return stream.getvalue()


@pytest.mark.parametrize("suffix", ["docx", "pptx", "xlsx"])
@pytest.mark.parametrize("entrypoint", ["bytes", "path", "metadata", "parse"])
def test_public_office_entries_enforce_unreferenced_member_budget(
    suffix: str, entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真正的 Office 包在所有公开路径拒绝未引用成员的解压放大。"""
    limit = 512 * 1024
    payload = _office_payload(suffix, limit + 1)
    path = tmp_path / f"sample.{suffix}"
    path.write_bytes(payload)
    monkeypatch.setattr(ooxml_zip, "MAX_ENTRY_BYTES", limit)
    with pytest.raises(ResourceLimitError) as caught:
        if entrypoint == "bytes":
            detection.guess_suffix_by_bytes(payload)
        elif entrypoint == "path":
            detection.guess_suffix_by_path(path)
        elif entrypoint == "metadata":
            api.extract_metadata(payload, file_suffix=suffix)
        else:
            api.parse(payload, file_suffix=suffix)
    assert caught.value.code == "resource_limit"


@pytest.mark.parametrize("by_path", [False, True])
def test_ooxml_budget_precedes_other_zip_detectors(by_path: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超限输入在其他 ZIP 身份检测器创建目录对象或读取成员前终止。"""
    payload = _office_payload("docx", 512 * 1024 + 1)
    path = tmp_path / "input.docx"
    path.write_bytes(payload)
    monkeypatch.setattr(ooxml_zip, "MAX_ENTRY_BYTES", 512 * 1024)
    detector = Mock(side_effect=AssertionError("不能在预算拒绝前调用其他判型器"))
    monkeypatch.setattr(detection, "_guess_ofd_suffix_by_path" if by_path else "_guess_ofd_suffix_by_bytes", detector)
    with pytest.raises(ResourceLimitError):
        (detection.guess_suffix_by_path if by_path else detection.guess_suffix_by_bytes)(path if by_path else payload)
    detector.assert_not_called()


@pytest.mark.parametrize("suffix", ["docx", "pptx", "xlsx"])
def test_converter_rejects_before_third_party_loader(suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """直接模型入口也必须拒绝超限包，且不触发第三方内存分配。"""
    payload = _office_payload(suffix, 512 * 1024 + 1)
    monkeypatch.setattr(ooxml_zip, "MAX_ENTRY_BYTES", 512 * 1024)
    module, converter, loader_name = {
        "docx": (docx_converter, docx_converter.DocxConverter, "Document"),
        "pptx": (pptx_converter, pptx_converter.PptxConverter, "Presentation"),
        "xlsx": (xlsx_converter, xlsx_converter.XlsxConverter, "load_workbook"),
    }[suffix]
    loader = Mock(side_effect=AssertionError("预算失败时不能调用第三方 loader"))
    monkeypatch.setattr(module, loader_name, loader)
    with pytest.raises(ResourceLimitError):
        converter().convert(BytesIO(payload))
    loader.assert_not_called()


def test_metadata_resource_limit_is_not_downgraded_to_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享属性资源错误必须在正文分析之前直接终止公开 analyze。"""
    payload = _office_payload("docx")
    failure = DocumentError("resource_limit", "metadata budget")
    monkeypatch.setattr(api, "extract_metadata", Mock(side_effect=failure))
    with pytest.raises(DocumentError) as caught:
        api.analyze(payload, file_suffix="docx")
    assert caught.value is failure


def test_embedded_workbook_keeps_resource_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """嵌入图表工作簿不能在可选图表容错中消化解压预算错误。"""
    from docvortex.analyzers.native.office import ooxml_chart

    payload = _office_payload("xlsx", 512 * 1024 + 1)
    monkeypatch.setattr(ooxml_zip, "MAX_ENTRY_BYTES", 512 * 1024)
    loader = Mock(side_effect=AssertionError("嵌入工作簿应在解压预算处拒绝"))
    monkeypatch.setattr(ooxml_chart, "load_workbook", loader)
    with pytest.raises(ResourceLimitError):
        ooxml_chart.html_table_from_excel_bytes(payload)
    loader.assert_not_called()
