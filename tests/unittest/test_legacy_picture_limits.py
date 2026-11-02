"""回归 #36/#46：图片超限保留占位图，文档结构安全限制仍然硬失败。"""

from io import BytesIO
from pathlib import Path
import struct
import zlib
from unittest.mock import Mock

import pytest
from loguru import logger
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls, label_cell
from _mtef_test_utils import build_equation_doc, formula_corpus
from _span_test_utils import inline_text

import docvortex
from docvortex.analyzers.native import DocModel, PptModel, XlsModel
from docvortex.analyzers.native.office.doc.images import ImageStore, inline_picture
from docvortex.analyzers.native.office.doc.records import DocBudget
from docvortex.analyzers.native.office.errors import LegacyOfficeImageLimitError, LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.legacy import officeart
from docvortex.analyzers.native.office.image import get_image_limit_placeholder_data_uri
from docvortex.analyzers.native.office.ppt import parser as ppt_parser
from docvortex.analyzers.native.office.ppt.records import PptRecord, RecordBudget
from docvortex.schema import BlockType


def _record(record_type: int, payload: bytes) -> bytes:
    """构造带长度边界的 OfficeArt 记录。"""
    return struct.pack("<HHI", 0, record_type, len(payload)) + payload


def _metafile(data: bytes, *, declared: int | None = None, compression: int = 0, raw: bool = False) -> bytes:
    """构造 WMF BLIP，独立控制声明长度和压缩封装。"""
    payload = data
    if compression == 0:
        compressor = zlib.compressobj(wbits=-15 if raw else 15)
        payload = compressor.compress(data) + compressor.flush()
    header = bytearray(34)
    struct.pack_into("<I", header, 0, len(data) if declared is None else declared)
    struct.pack_into("<I", header, 28, len(payload))
    header[32] = compression
    return _record(0xF01B, b"\x00" * 16 + header + payload)


def _collect(data: bytes, kind: str) -> dict[int, officeart.OfficeImagePayload]:
    """经共享 BStore 或 PPT 的真实收集边界读取图片。"""
    if kind == "shared":
        return officeart.decode_bstore(data)
    document = PptRecord(0, 0xF, 0, ppt_parser.RT_DOCUMENT, data)
    return ppt_parser._picture_map(document, b"", RecordBudget())


@pytest.mark.parametrize("compression,raw", [(0, False), (0, True), (0xFE, False)])
@pytest.mark.parametrize("size", [127, 128, 129])
def test_metafile_actual_output_limit(monkeypatch: pytest.MonkeyPatch, compression: int, raw: bool, size: int) -> None:
    """验证两种 deflate 和未压缩载荷在上限前后均检查实际大小。"""
    monkeypatch.setattr(officeart, "MAX_ENTRY_BYTES", 128)
    encoded = _metafile(b"x" * size, declared=1, compression=compression, raw=raw)
    record = officeart.record_at(encoded, 0)
    if size > 128:
        with pytest.raises(LegacyOfficeImageLimitError):
            officeart.decode_blip(record)
    else:
        assert officeart.decode_blip(record).data == b"x" * size


def test_declared_limit_prevents_decompression(monkeypatch: pytest.MonkeyPatch) -> None:
    """声明超限时在解压前拒绝，且保留资源错误的继承关系。"""
    encoded = _metafile(b"small", declared=officeart.MAX_ENTRY_BYTES + 1)
    monkeypatch.setattr(officeart.zlib, "decompressobj", Mock(side_effect=AssertionError("不应解压")))
    with pytest.raises(LegacyOfficeImageLimitError) as caught:
        officeart.decode_blip(officeart.record_at(encoded, 0))
    assert isinstance(caught.value, LegacyOfficeResourceLimitError)
    assert caught.value.code == "resource_limit"


def test_truncated_metafile_remains_unsupported() -> None:
    """截断压缩流仍返回不可用图片，避免接受未完整解码的内容。"""
    encoded = _metafile(b"normal")
    record = officeart.record_at(encoded, 0)
    truncated = officeart.OfficeArtRecord(0, 0, 0, 0xF01B, record.payload[:-2])
    assert officeart.decode_blip(truncated) is None


@pytest.mark.parametrize("record_type", [0xF01D, 0xF01E, 0xF01F, 0xF029])
def test_bitmap_limit_is_also_recoverable(monkeypatch: pytest.MonkeyPatch, record_type: int) -> None:
    """普通位图的单图大小限制使用同一种可恢复异常。"""
    monkeypatch.setattr(officeart, "MAX_ENTRY_BYTES", 8)
    record = officeart.record_at(_record(record_type, b"\x00" * 17 + b"x" * 9), 0)
    with pytest.raises(LegacyOfficeImageLimitError):
        officeart.decode_blip(record)


@pytest.mark.parametrize("kind", ["shared", "ppt"])
def test_bstore_replaces_bad_image_without_renumbering(kind: str) -> None:
    """坏图夹在正常图片之间时保留占位序号，并发出可定位告警。"""
    good = _record(0xF01E, b"\x00" * 17 + b"png-payload")
    bad = _metafile(b"small", declared=officeart.MAX_ENTRY_BYTES + 1)
    data = b"".join(_record(0xF007, b"\x00" * 36 + item) for item in (good, bad, good))
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        result = _collect(data, kind)
    finally:
        logger.remove(sink)
    assert list(result) == [1, 2, 3]
    assert result[1].data == result[3].data == b"png-payload"
    assert result[2].data == b"" and result[2].limit_reason is not None
    assert any("BSE 2" in str(message) for message in messages)


@pytest.mark.parametrize("kind", ["shared", "ppt"])
def test_bstore_asset_total_rejects_only_excess_images(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """累计预算拒绝大图后仍可接收剩余空间内的小图，重复图不重复计费。"""
    module = officeart if kind == "shared" else ppt_parser
    monkeypatch.setattr(module, "MAX_ASSET_TOTAL_BYTES", 3)
    good = _record(0xF007, b"\x00" * 36 + _record(0xF01E, b"\x00" * 17 + b"xx"))
    excess = _record(0xF007, b"\x00" * 36 + _record(0xF01E, b"\x00" * 17 + b"yy"))
    small = _record(0xF007, b"\x00" * 36 + _record(0xF01E, b"\x00" * 17 + b"z"))
    result = _collect(good + excess + good + small, kind)
    assert [item.data for item in result.values()] == [b"xx", b"", b"xx", b"z"]
    assert "max_asset_total_bytes=3" in result[2].limit_reason
    assert result[1].data is result[3].data


@pytest.mark.parametrize("kind", ["shared", "ppt"])
@pytest.mark.parametrize("error", [LegacyOfficeResourceLimitError("records"), MemoryError("memory")])
def test_bstore_does_not_swallow_document_errors(monkeypatch: pytest.MonkeyPatch, kind: str, error: Exception) -> None:
    """图片解码期间发生记录预算或内存错误时必须继续向上传播。"""
    module = officeart if kind == "shared" else ppt_parser
    monkeypatch.setattr(module, "_decode_bse_body", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        _collect(_record(0xF007, b"\x00" * 36), kind)
    assert caught.value is error


def test_doc_limit_does_not_fall_back_to_magic_scan() -> None:
    """超限 BLIP 中的 PNG magic 不能触发原始字节恢复。"""
    art = _metafile(b"\x89PNG\r\n\x1a\n", declared=officeart.MAX_ENTRY_BYTES + 1, compression=0xFE)
    picf = struct.pack("<IH", 6 + len(art), 6) + art
    store = ImageStore()
    result = inline_picture(picf, offset=0, store=store, budget=DocBudget())
    assert result.data == b"" and result.limit_reason is not None
    assert store.total == 0


def test_doc_keeps_body_and_normal_images_around_bad_inline_picture(tmp_path: Path) -> None:
    """真实 DOC 内嵌坏图前后的正文和两张正常 PNG 均应完整输出。"""
    data = build_equation_doc(
        [(1, b"invalid"), (2, b"invalid"), (3, b"invalid")],
        preview_storage_ids={1, 2, 3},
        preview_payloads={2: _metafile(b"small", declared=officeart.MAX_ENTRY_BYTES + 1)},
    )
    # 等长替换字段控制字符，让同一套 PICF/CHPX 绑定用于普通正文图片。
    data = data.replace("\x13 EMBED Equation.3 ".encode("utf-16le"), "前 EMBED Equation.3 ".encode("utf-16le"))
    data = data.replace("\x14\x01\x15\r".encode("utf-16le"), "中\x01后\r".encode("utf-16le"))
    pages = DocModel().predict(BytesIO(data))
    blocks = [block for page in pages for block in page]
    images = [block for block in blocks if block["type"] == BlockType.IMAGE]
    assert len(images) == 3
    assert images[1]["image_base64"] == get_image_limit_placeholder_data_uri()
    texts = [inline_text(block["content"]) for block in blocks if block["type"] == BlockType.TEXT]
    assert len(texts) == 3
    assert all("前 EMBED Equation.3 中后" in text for text in texts)
    output = tmp_path / "oversized-picture.md"
    docvortex.convert(data, output, file_suffix="doc")
    markdown = output.read_text(encoding="utf-8")
    assert markdown.count("![") == 3
    assert markdown.count("前 EMBED Equation.3 中后") == 3


@pytest.mark.parametrize("suffix", ["doc", "ppt", "xls"])
def test_native_document_survives_oversized_preview(suffix: str) -> None:
    """在真实 CFB 解析入口验证超限预览不会阻止前后正常公式或单元格输出。"""
    _, mtef, expected = formula_corpus()[0]
    oversized = officeart.MAX_ENTRY_BYTES + 1
    marker = b"unique preview"
    if suffix == "doc":
        data = build_equation_doc(
            [(1, mtef), (2, b"invalid"), (3, mtef)],
            preview_storage_ids={2},
            preview_payloads={2: _metafile(marker, declared=oversized)},
        )
        pages = DocModel().predict(BytesIO(data))
    else:
        if suffix == "ppt":
            data = build_equation_ppt([mtef, b"invalid", mtef], preview_payload=marker)
            model = PptModel()
        else:
            data = build_equation_xls([(1, b"invalid")], preview_payload=marker, cell_records=label_cell(0, 0, "正文"))
            model = XlsModel()
        header = bytearray(34)
        struct.pack_into("<I", header, 0, len(marker))
        struct.pack_into("<I", header, 28, len(zlib.compress(marker)))
        needle = bytes(header) + zlib.compress(marker)
        assert data.count(needle) == 1
        data = data.replace(needle, struct.pack("<I", oversized) + needle[4:])
        pages = model.predict(BytesIO(data))
    if suffix != "xls":
        images = [block for page in pages for block in page if block["type"] == BlockType.IMAGE]
        assert len(images) == 1
        assert images[0]["image_base64"] == get_image_limit_placeholder_data_uri()
    if suffix == "xls":
        assert "正文" in str(pages)
    else:
        assert [block["content"] for page in pages for block in page if block["type"] == BlockType.EQUATION] == [
            expected,
            expected,
        ]
