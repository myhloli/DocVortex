"""XLSX 合并单元格在第三方加载前的面积、记录数及实际 part 路径回归。"""

from __future__ import annotations

from io import BytesIO
import posixpath
import struct
from unittest.mock import Mock
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
import pytest

from docvortex.analyzers.native.office.xlsx import resource_budget as budget
from docvortex.analyzers.native.office.xlsx import xlsx_converter as converter_module
from docvortex.analyzers.native.office.xlsx.xlsx_converter import XlsxConverter
from docvortex.errors import DocumentError, ResourceLimitError
from docvortex.foundation.ooxml_zip import OoxmlZipFile

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx_package(
    sheet_ranges: list[list[str]], *, alternate_paths: bool = False, strict: bool = False, hidden: bool = False
) -> bytes:
    """写入原始 mergeCell XML，避免测试构造阶段先让 openpyxl 展开恶意范围。"""
    workbook = Workbook()
    for index in range(len(sheet_ranges)):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        assert sheet is not None
        sheet["A1"] = "visible text"
        if hidden and index:
            sheet.sheet_state = "hidden"
    original = BytesIO()
    workbook.save(original)
    relocated = {
        "xl/workbook.xml": "custom/book.xml",
        "xl/_rels/workbook.xml.rels": "custom/_rels/book.xml.rels",
        **{f"xl/worksheets/sheet{index + 1}.xml": f"custom/pages/page{index + 1}.xml" for index in range(len(sheet_ranges))},
    }
    output = BytesIO()
    with ZipFile(original) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.startswith("xl/worksheets/sheet") and info.filename.endswith(".xml"):
                index = int(info.filename.removeprefix("xl/worksheets/sheet").removesuffix(".xml")) - 1
                root = ET.fromstring(data)
                merges = ET.SubElement(root, f"{{{_SHEET_NS}}}mergeCells", count=str(len(sheet_ranges[index])))
                for reference in sheet_ranges[index]:
                    ET.SubElement(merges, f"{{{_SHEET_NS}}}mergeCell", ref=reference)
                data = ET.tostring(root)
            if alternate_paths and info.filename == "[Content_Types].xml":
                root = ET.fromstring(data)
                for part in root:
                    name = (part.get("PartName") or "").lstrip("/")
                    if name in relocated:
                        part.set("PartName", "/" + relocated[name])
                data = ET.tostring(root)
            if alternate_paths and info.filename in {"_rels/.rels", "xl/_rels/workbook.xml.rels"}:
                root = ET.fromstring(data)
                for relationship in root:
                    name = relationship.get("Target") or ""
                    if not name.startswith("/"):
                        base = "xl" if info.filename.startswith("xl/") else ""
                        name = posixpath.normpath(posixpath.join(base, name))
                    name = name.lstrip("/")
                    relationship.set("Target", "/" + relocated.get(name, name))
                data = ET.tostring(root)
            if strict and info.filename.endswith((".xml", ".rels")):
                data = data.replace(_SHEET_NS.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main").replace(
                    b"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                    b"http://purl.oclc.org/ooxml/officeDocument/relationships",
                )
            target.writestr(relocated.get(info.filename, info.filename) if alternate_paths else info.filename, data)
    return output.getvalue()


def _validate(package: bytes) -> None:
    """通过实际有界 ZIP 验证器执行工作簿合并预检。"""
    with OoxmlZipFile(BytesIO(package)) as source:
        budget.validate_xlsx_merge_budget(source)


@pytest.mark.parametrize("reference", ["A1:J10000", "$A$1:$J$10000", "xfd1048576", "A1", "A01:B02"])
def test_valid_ranges_accept_inclusive_limits(reference: str) -> None:
    """单区恰好十万格及 Excel 最后一个有效坐标均按有限整数运算接受。"""
    _validate(_xlsx_package([[reference]]))


@pytest.mark.parametrize("alternate_paths,strict", [(False, False), (True, False), (False, True), (True, True)])
def test_giant_range_fails_before_loader_or_normalizer(
    monkeypatch: pytest.MonkeyPatch, alternate_paths: bool, strict: bool
) -> None:
    """实际工作表路径及 Strict 命名空间中的超大范围均在 openpyxl 前拒绝。"""
    loader = Mock(side_effect=AssertionError("超限工作簿不能进入 openpyxl"))
    normalizer = Mock(side_effect=AssertionError("资源错误不能触发兼容重试"))
    monkeypatch.setattr(converter_module, "load_workbook", loader)
    monkeypatch.setattr(converter_module, "normalize_xlsx_package", normalizer)
    converter = XlsxConverter()
    with pytest.raises(ResourceLimitError, match="max_merge_range_cells=100000"):
        converter.convert(BytesIO(_xlsx_package([["A1:XFD1048576"]], alternate_paths=alternate_paths, strict=strict)))
    loader.assert_not_called()
    normalizer.assert_not_called()
    assert converter.zf is None
    assert not converter._projection_cache_enabled


def test_single_range_rejects_one_cell_over_limit() -> None:
    """十万格后一格即拒绝，不等到累计限额才阻断。"""
    with pytest.raises(ResourceLimitError, match="max_merge_range_cells"):
        _validate(_xlsx_package([["A1:A100001"]]))


def test_zip_preflight_failure_cannot_fall_through_to_openpyxl(monkeypatch: pytest.MonkeyPatch) -> None:
    """中央目录声明损坏时保持拒绝，不能因旧 ZIP 警告分支跳过安全扫描。"""
    package = bytearray(_xlsx_package([["A1:XFD1048576"]]))
    end_record = package.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", package, end_record + 8, 0, 0)
    loader = Mock(side_effect=AssertionError("ZIP 预检失败后不能进入 openpyxl"))
    monkeypatch.setattr(converter_module, "load_workbook", loader)
    with pytest.raises((BadZipFile, ValueError)):
        XlsxConverter().convert(BytesIO(package))
    loader.assert_not_called()


@pytest.mark.parametrize("hidden,alternate_paths", [(False, False), (True, False), (True, True)])
def test_workbook_budget_counts_all_sheets_duplicates_and_overlaps(hidden: bool, alternate_paths: bool) -> None:
    """每条原始记录均计费，跨工作表和隐藏表不能重置累计预算。"""
    _validate(_xlsx_package([["A1:A100000"] * 3, ["A1:A100000"] * 2], hidden=hidden, alternate_paths=alternate_paths))
    with pytest.raises(ResourceLimitError, match="max_merged_cells=500000"):
        _validate(
            _xlsx_package(
                [["A1:A100000"] * 3, ["A1:A100000", "A2:A100001", "A1"]],
                hidden=hidden,
                alternate_paths=alternate_paths,
            )
        )


def test_merge_record_count_is_separate_from_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """缩小记录预算验证边界，重复单格记录同样消耗一个名额。"""
    monkeypatch.setattr(budget, "MAX_MERGE_RANGES", 3)
    _validate(_xlsx_package([["A1"] * 3]))
    with pytest.raises(ResourceLimitError, match="max_merge_ranges=3"):
        _validate(_xlsx_package([["A1"] * 4]))


def test_default_record_budget_accepts_exactly_one_hundred_thousand() -> None:
    """真实十万条记录的流式扫描保持有界，并在下一条立即失败。"""
    _validate(_xlsx_package([["A1"] * 100_000]))
    with pytest.raises(ResourceLimitError, match="max_merge_ranges=100000"):
        _validate(_xlsx_package([["A1"] * 100_001]))


def test_unrelated_id_namespace_cannot_hide_the_real_sheet_relation() -> None:
    """伪造的其他命名空间 id 属性不能覆盖第三方实际读取的 r:id。"""
    package = _xlsx_package([["A1:XFD1048576"]])
    output = BytesIO()
    with ZipFile(BytesIO(package)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "xl/workbook.xml":
                root = ET.fromstring(data)
                for sheet in root.iter(f"{{{_SHEET_NS}}}sheet"):
                    original_attributes = dict(sheet.attrib)
                    sheet.attrib.clear()
                    sheet.set("{urn:unrelated}id", "missing-relation")
                    sheet.attrib.update(original_attributes)
                data = ET.tostring(root)
            target.writestr(info, data)
    with pytest.raises(ResourceLimitError, match="max_merge_range_cells"):
        _validate(output.getvalue())


@pytest.mark.parametrize(
    "part_name,kind",
    [("[Content_Types].xml", "dtd"), ("xl/worksheets/sheet1.xml", "dtd"), ("xl/worksheets/sheet1.xml", "depth")],
)
def test_preflight_xml_does_not_expand_entities_or_accept_excessive_depth(
    monkeypatch: pytest.MonkeyPatch, part_name: str, kind: str
) -> None:
    """新预检拒绝 DTD 和过深 XML，不能先于第三方引入实体或树深度放大。"""
    output = BytesIO()
    with ZipFile(BytesIO(_xlsx_package([["A1:B2"]]))) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == part_name:
                if kind == "dtd":
                    data = b'<!DOCTYPE root [<!ENTITY marker "INERT_ENTITY">]>' + data
                    data = data.replace(b"visible text", b"&marker;")
                else:
                    closing_root = data.rfind(b"</")
                    data = data[:closing_root] + b"<nested>" * 300 + b"</nested>" * 300 + data[closing_root:]
            target.writestr(info, data)
    loader = Mock(side_effect=AssertionError("非法 XML 不能进入 openpyxl"))
    normalizer = Mock(side_effect=AssertionError("非法预检 XML 不能进入兼容重试"))
    monkeypatch.setattr(converter_module, "load_workbook", loader)
    monkeypatch.setattr(converter_module, "normalize_xlsx_package", normalizer)
    with pytest.raises(DocumentError) as caught:
        XlsxConverter().convert(BytesIO(output.getvalue()))
    assert caught.value.code == "open_failed"
    loader.assert_not_called()
    normalizer.assert_not_called()


@pytest.mark.parametrize("reference", ["A0:B1", "A1:XFE1", "A1:A1048577", "B2:A1", "A:A", "1:5", "", "A1 B2"])
def test_invalid_merge_coordinates_fail_before_loading(reference: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """非法坐标使用稳定输入错误，防止第三方把缺失或越界边界展开。"""
    loader = Mock(side_effect=AssertionError("非法合并区不能进入 openpyxl"))
    monkeypatch.setattr(converter_module, "load_workbook", loader)
    with pytest.raises(DocumentError) as caught:
        XlsxConverter().convert(BytesIO(_xlsx_package([[reference]])))
    assert caught.value.code == "open_failed"
    loader.assert_not_called()


def test_ole_stripped_copy_is_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """OLE 兼容副本经过同样预检，不能绕过原始包已完成的检查。"""
    monkeypatch.setattr(converter_module, "package_has_sheet_ole_objects", Mock(return_value=True))
    monkeypatch.setattr(
        converter_module, "strip_xlsx_ole_objects_for_openpyxl", Mock(return_value=_xlsx_package([["A1:XFD1048576"]]))
    )
    loader = Mock(side_effect=AssertionError("超限 OLE 副本不能进入 openpyxl"))
    monkeypatch.setattr(converter_module, "load_workbook", loader)
    with pytest.raises(ResourceLimitError):
        XlsxConverter().convert(BytesIO(_xlsx_package([["A1:B2"]])))
    loader.assert_not_called()


def test_normal_merge_and_rich_text_reach_real_loader() -> None:
    """正常合并、富文本及多工作表继续经过真实 openpyxl 并保留输出内容。"""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet["A1"] = CellRichText("plain ", TextBlock(InlineFont(b=True), "bold"))
    sheet.merge_cells("A1:B2")
    workbook.create_sheet("Following")["A1"] = "next sheet"
    stream = BytesIO()
    workbook.save(stream)
    converter = XlsxConverter()
    converter.convert(BytesIO(stream.getvalue()))
    assert len(converter.pages) == 2
    assert "plain " in str(converter.pages) and "bold" in str(converter.pages)
    assert "next sheet" in str(converter.pages)
    assert converter.workbook.active.merged_cells.ranges
    assert isinstance(converter.workbook.active["A1"].value, CellRichText)
