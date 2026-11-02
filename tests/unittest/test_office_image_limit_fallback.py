"""回归 #46：图片安全限制只替换当前图片，不丢失其余文档内容。"""

import base64
import hashlib
from io import BytesIO
import struct
from unittest.mock import Mock

from bs4 import BeautifulSoup
from docx import Document
from lxml import etree
from loguru import logger
from PIL import Image
import pytest

from _image_mtef_test_utils import build_gif_with_mtef, build_wmf, pre6_wmf_comment
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls, label_cell, _preview_bstore
from _metafile_test_utils import basic_wmf, build_placeable_wmf
from _mtef_test_utils import build_equation_doc, build_equation_object
from _mtef_v5_test_utils import v5_formula_corpus
from _office_image_mtef_test_utils import build_image_docx, build_image_pptx, build_image_xlsx
from test_docx_table_metafiles import _docx_with_image, _table_content

from docvortex.api import parse, render
from docvortex.analyzers.native.office import image as office_image
from docvortex.analyzers.native.office.doc import images as doc_images
from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter
from docvortex.analyzers.native.office.equation import image as image_equation
from docvortex.analyzers.native.office.equation.image import OfficeImageEquationDecoder
from docvortex.analyzers.native.office.errors import LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.legacy.officeart import OfficeImageBudget, OfficeImagePayload, decode_bstore
from docvortex.analyzers.native.office.ppt import parser as ppt_parser
from docvortex.analyzers.native.office.pptx.pptx_converter import PptxConverter
from docvortex.analyzers.native.office.rtf.converter import RtfConverter
from docvortex.analyzers.native.office.xls import parser as xls_parser
from docvortex.analyzers.native.office.xlsx.xlsx_converter import XlsxConverter
from docvortex.export.bundle import load_bundle
from docvortex.render.contracts import RenderFormat


def _many_records(count: int) -> bytes:
    """生成指定数量的无害 SETBKMODE，构造器额外添加一条 EOF。"""
    return build_placeable_wmf([struct.pack("<IHH", 4, 0x0102, 1)] * count)


def _placeholder_bytes() -> bytes:
    """解码统一占位图，验证产物与资源引用时使用真实字节。"""
    return base64.b64decode(office_image.get_image_limit_placeholder_data_uri().split(",", 1)[1])


def _rtf_picture(payload: bytes) -> bytes:
    """构造前后都有安全正文的 RTF WMF 图片。"""
    return b"{\\rtf1 SAFE BEFORE\\par {\\pict\\wmetafile8 " + payload.hex().encode() + b"}\\par SAFE AFTER}"


def _document(suffix: str, payload: bytes) -> bytes:
    """复用各格式的真实容器构造器，把同一原图放到不同图片路径。"""
    if suffix == "docx":
        return build_image_docx(payload, inline=True)
    if suffix == "pptx":
        return build_image_pptx(payload)
    if suffix == "xlsx":
        return build_image_xlsx(payload, cell_value="SAFE CELL")
    if suffix == "doc":
        return build_equation_doc([(1, b"invalid")], preview_storage_ids={1}, preview_payloads={1: payload})
    if suffix == "ppt":
        return build_equation_ppt([b"invalid"], preview_payload=payload)
    if suffix == "xls":
        return build_equation_xls([(1, b"invalid")], preview_payload=payload, cell_records=label_cell(0, 0, "SAFE CELL"))
    return _rtf_picture(payload)


@pytest.mark.parametrize("count,limited", [(99_998, False), (99_999, False), (100_000, True), (100_001, True)])
def test_wmf_record_limit_counts_eof_and_keeps_strict_decoder(count: int, limited: bool) -> None:
    """真实十万条边界包含 EOF；严格入口抛错，文档探测入口明确标记拒绝。"""
    payload = _many_records(count)
    decoder = OfficeImageEquationDecoder()
    if limited:
        with pytest.raises(LegacyOfficeResourceLimitError, match="max_picture_records=100000"):
            decoder.decode(payload)
    else:
        assert decoder.decode(payload) is None
    result = decoder.probe(payload)
    assert result.latex is None
    assert (result.limit_reason is not None) == limited


def test_rejected_probe_is_cached_warned_once_and_never_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    """反复遇到同一超限图只扫描及告警一次，原图不能进入任何渲染器。"""
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 2)
    scan = Mock(wraps=image_equation._wmf_comments)
    monkeypatch.setattr(image_equation, "_wmf_comments", scan)
    decoder = OfficeImageEquationDecoder()
    payload = _many_records(3)
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        first = decoder.probe(payload, part_name="word/media/problem.wmf")
        assert decoder.probe(payload, content_type="image/x-wmf") is first
        forbidden = Mock(side_effect=AssertionError("超限原图不应渲染"))
        monkeypatch.setattr(office_image, "render_metafile", forbidden)
        monkeypatch.setattr(office_image.Image, "open", forbidden)
        source = office_image.serialize_office_image(payload, equation_decoder=decoder)
    finally:
        logger.remove(sink)
    assert source == office_image.get_image_limit_placeholder_data_uri()
    assert scan.call_count == 1
    forbidden.assert_not_called()
    warnings = [str(message) for message in messages if "OFFICE_IMAGE_LIMIT" in str(message)]
    assert len(warnings) == 1 and "word/media/problem.wmf" in warnings[0]
    assert decoder.candidate_total_bytes == 0


def test_probe_does_not_swallow_memory_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片资源降级不能将进程内存错误伪装成普通占位图。"""
    monkeypatch.setattr(image_equation, "_wmf_comments", Mock(side_effect=MemoryError("oom")))
    with pytest.raises(MemoryError):
        OfficeImageEquationDecoder().probe(_many_records(1))


def test_gif_candidate_limit_is_a_placeholder_not_a_failed_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享探测器的 GIF candidate 超限也停止图片解码并保留正文。"""
    mtef = v5_formula_corpus()[0][1]
    payload = build_gif_with_mtef(mtef)
    monkeypatch.setattr(image_equation, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", len(mtef) - 1)
    result = parse(build_image_docx(payload, inline=True), file_suffix="docx")
    assert _placeholder_bytes() in result.assets.values()
    assert "before" in str(result.to_dict()) and "after" in str(result.to_dict())


@pytest.mark.parametrize("suffix", ["doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf"])
def test_all_office_formats_keep_placeholder_and_safe_content(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    """七种真实公共解析入口均保留占位资源，且原始超限 WMF 不参与渲染。"""
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 2)
    forbidden = Mock(side_effect=AssertionError("超限 WMF 不应再次渲染"))
    monkeypatch.setattr(office_image, "render_metafile", forbidden)
    payload = _many_records(3)
    result = parse(_document(suffix, payload), file_suffix=suffix, keep_model_json=True)
    assert _placeholder_bytes() in result.assets.values()
    assert all(data != payload for data in result.assets.values())
    forbidden.assert_not_called()
    content = str(result.to_dict())
    if suffix in {"xls", "xlsx"}:
        assert "SAFE CELL" in content
    elif suffix == "docx":
        assert "before" in content and "after" in content
    elif suffix == "rtf":
        assert "SAFE BEFORE" in content and "SAFE AFTER" in content


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_docx_limited_table_preserves_cells_and_following_table(
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
    fallback: bool,
    tmp_path,
) -> None:
    """预解析、强制回退和嵌套表格均保留文字、alt、图片引用及后续表格。"""
    if fallback:
        monkeypatch.setattr(DocxConverter, "_preparse_tables_with_mammoth", lambda _self, _bytes: [])
    payload = _many_records(100_001)
    forbidden = Mock(side_effect=AssertionError("表格超限图不应渲染"))
    monkeypatch.setattr(office_image, "render_metafile", forbidden)
    package = _docx_with_image(payload, content_type="image/x-wmf", extension="wmf", nested=nested, next_table=True)
    result = parse(package, file_suffix="docx", keep_model_json=True)
    tables = _table_content(result)
    assert len(tables) == 2
    table = BeautifulSoup(tables[0], "html.parser")
    for expected in ("before", "after", "right cell"):
        assert expected in table.get_text()
    assert "later table" in tables[1]
    assert len(table.find_all("table")) == (2 if nested else 1)
    image = table.find("img")
    assert image is not None and image["alt"] == "Table logo"
    assert result.assets[image["src"]] == _placeholder_bytes()
    forbidden.assert_not_called()
    result.save_bundle(tmp_path / "bundle")
    restored = load_bundle(tmp_path / "bundle")
    for format_ in (RenderFormat.MARKDOWN, RenderFormat.HTML):
        exported = render(restored.middle_json, format_, assets=restored.assets)
        assert image["src"].encode() in exported.content
        assert b"later table" in exported.content
        assert b"right cell" in exported.content
    assert restored.assets[image["src"]] == _placeholder_bytes()
    assert all(data != payload for data in restored.assets.values())
    with Image.open(BytesIO(restored.assets[image["src"]])) as placeholder:
        assert placeholder.size == (320, 180)
        placeholder.verify()


def test_docx_body_keeps_safe_images_and_following_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """正文超限图片不妨碍后续正常位图和表格，公开结果只导出安全素材。"""
    payload = _many_records(100_001)
    document = Document(BytesIO(build_image_docx(payload, inline=True)))
    png = BytesIO()
    Image.new("RGB", (30, 20), "red").save(png, "PNG")
    document.add_picture(BytesIO(png.getvalue()))
    document.add_paragraph("SAFE AFTER")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "SAFE TABLE"
    output = BytesIO()
    document.save(output)
    forbidden = Mock(side_effect=AssertionError("超限 WMF 不应渲染"))
    monkeypatch.setattr(office_image, "render_metafile", forbidden)
    result = parse(output.getvalue(), file_suffix="docx")
    assert len(result.assets) == 2
    assert _placeholder_bytes() in result.assets.values()
    for text in ("before", "after", "SAFE AFTER", "SAFE TABLE"):
        assert text in str(result.to_dict())
    forbidden.assert_not_called()


def test_docx_mixed_formula_cell_keeps_limited_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一单元格的原生公式重建不能吞掉超限图片占位符和正常文字。"""
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 2)
    package = _docx_with_image(_many_records(3), content_type="image/x-wmf", extension="wmf", next_table=True)
    document = Document(BytesIO(package))
    document.tables[0].cell(0, 0).paragraphs[0]._p.append(
        etree.fromstring(
            b'<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x</m:t></m:r></m:oMath>'
        )
    )
    output = BytesIO()
    document.save(output)
    result = parse(output.getvalue(), file_suffix="docx")
    assert _placeholder_bytes() in result.assets.values()
    table = BeautifulSoup(_table_content(result)[0], "html.parser")
    assert table.find("img") is not None
    assert "before" in table.get_text() and "after" in table.get_text()
    assert "x" in table.get_text()


@pytest.mark.parametrize("cap,limited", [(4, False), (3, True)])
def test_asset_budget_exact_boundary_and_later_small_images(cap: int, limited: bool) -> None:
    """累计上限按唯一字节计算，拒绝的原图不入缓存且不阻塞后续小图。"""
    budget = OfficeImageBudget(max_bytes=cap)
    first = budget.admit(OfficeImagePayload(b"aa", "png", "image/png"), source="first.png")
    second = budget.admit(OfficeImagePayload(b"bb", "png", "image/png"), source="second.png")
    repeated = budget.admit(OfficeImagePayload(bytes(bytearray(b"aa")), "png", "image/png"), source="repeat.png")
    assert repeated.data is first.data
    assert (second.limit_reason is not None) == limited
    assert budget.total == (2 if limited else 4)
    if limited:
        assert second.data == b""
        assert hashlib.sha256(b"bb").digest() not in budget._accepted
        last = budget.admit(OfficeImagePayload(b"c", "png", "image/png"), source="last.png")
        assert last.data == b"c" and last.limit_reason is None
        assert budget.total == cap


def test_doc_bstore_and_inline_store_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 DOC 的 BStore 与 PICF 共用计费，重复引用不产生第二份原图缓存。"""
    payload = basic_wmf()
    monkeypatch.setattr(doc_images, "MAX_ASSET_TOTAL_BYTES", len(payload))
    store = doc_images.ImageStore()
    collected = decode_bstore(_preview_bstore(payload), image_budget=store.image_budget)
    floating = store.add(collected[1])
    inline = store.add(OfficeImagePayload(payload, "wmf", "image/wmf"))
    assert floating is inline
    assert store.total == len(payload)
    assert floating.limit_reason is None
    rejected = store.add(OfficeImagePayload(b"extra", "png", "image/png"))
    assert rejected.data == b"" and rejected.limit_reason is not None
    assert store.total == len(payload)


def test_record_limited_payload_never_enters_legacy_asset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """记录数超限图只有拒绝摘要，原图不占累计预算也不保存在任何图片缓存。"""
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 2)
    payload = _many_records(3)
    store = doc_images.ImageStore()
    first = store.add(OfficeImagePayload(payload, "wmf", "image/wmf"))
    repeated = store.add(OfficeImagePayload(payload, "wmf", "image/wmf"))
    assert repeated is first and first.data == b""
    assert first.limit_reason is not None
    assert store.total == 0 and not store.image_budget._accepted
    assert all(item.data == b"" for item in store.cache.values())


def test_doc_cumulative_limit_preserves_earlier_and_later_small_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 DOC 中间大图超限后，前后正文及剩余预算内的小图继续输出。"""
    first, excess, last = basic_wmf(), _many_records(100), _many_records(1)
    monkeypatch.setattr(doc_images, "MAX_ASSET_TOTAL_BYTES", len(first) + len(last))
    package = build_equation_doc(
        [(1, b"invalid"), (2, b"invalid"), (3, b"invalid")],
        preview_storage_ids={1, 2, 3},
        preview_payloads={1: first, 2: excess, 3: last},
    )
    package = package.replace("\x13 EMBED Equation.3 ".encode("utf-16le"), "前 EMBED Equation.3 ".encode("utf-16le"))
    package = package.replace("\x14\x01\x15\r".encode("utf-16le"), "中\x01后\r".encode("utf-16le"))
    result = parse(package, file_suffix="doc")
    assert len(result.assets) == 3 and _placeholder_bytes() in result.assets.values()
    assert str(result.to_dict()).count("前 EMBED Equation.3 中后") == 3
    assert all(data != excess for data in result.assets.values())


@pytest.mark.parametrize("suffix,module", [("doc", doc_images), ("ppt", ppt_parser), ("xls", xls_parser)])
def test_legacy_document_survives_cumulative_image_limit(monkeypatch: pytest.MonkeyPatch, suffix: str, module) -> None:
    """旧三格式的公共入口在累计预算不足时保留占位图而非整篇失败。"""
    # 保证图片比 fixture 的 OLE storage 大，只触发图片预算，不触发 OLE 对象预算。
    payload = _many_records(len(build_equation_object(b"invalid")) // 8 + 1)
    monkeypatch.setattr(module, "MAX_ASSET_TOTAL_BYTES", len(payload) - 1)
    forbidden = Mock(side_effect=AssertionError("累计超限图不应渲染"))
    monkeypatch.setattr(office_image, "render_metafile", forbidden)
    result = parse(_document(suffix, payload), file_suffix=suffix)
    assert _placeholder_bytes() in result.assets.values()
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "suffix,converter_class",
    [
        ("docx", DocxConverter),
        ("pptx", PptxConverter),
        ("xlsx", XlsxConverter),
        ("rtf", RtfConverter),
    ],
)
def test_converter_reuse_clears_rejected_images(monkeypatch: pytest.MonkeyPatch, suffix: str, converter_class) -> None:
    """复用转换器时重新建立探测状态；上一份文档的拒绝不得污染新文档。"""
    _name, mtef, expected = v5_formula_corpus()[0]
    payload = build_wmf([pre6_wmf_comment(mtef)], placeable=True)
    converter = converter_class()
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 1)
    converter.convert(BytesIO(_document(suffix, payload)))
    assert office_image.get_image_limit_placeholder_data_uri() in str(converter.pages)
    monkeypatch.setattr(image_equation, "MAX_PICTURE_RECORDS", 100_000)
    converter.convert(BytesIO(_document(suffix, payload)))
    assert expected in str(converter.pages)
    assert office_image.get_image_limit_placeholder_data_uri() not in str(converter.pages)
