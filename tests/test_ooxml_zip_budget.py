"""用小型 ZIP 验证 Office 解压预算和第三方重新打开原包的边界。"""

from __future__ import annotations

from io import BytesIO, UnsupportedOperation
from pathlib import Path
import random
import struct
from zipfile import BadZipFile, ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
import zlib

import pytest

from docvortex.errors import DocumentError, ResourceLimitError
from docvortex.foundation import ooxml_zip
from docvortex.foundation.ooxml_zip import OoxmlZipFile, validate_ooxml_package


def _package(members: dict[str, bytes], compression: int = ZIP_DEFLATED) -> bytes:
    """生成固定时间戳的小 ZIP，避免测试依赖平台时钟。"""
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, data in members.items():
            info = ZipInfo(name)
            info.compress_type = compression
            archive.writestr(info, data)
    return stream.getvalue()


def _small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """缩小资源预算，全部拒绝测试只需几 KiB 数据。"""
    monkeypatch.setattr(ooxml_zip, "MAX_ENTRY_BYTES", 1024)
    monkeypatch.setattr(ooxml_zip, "MAX_TOTAL_BYTES", 2048)
    monkeypatch.setattr(ooxml_zip, "MAX_CONTROL_XML_BYTES", 256)


def _forge_uncompressed_size(payload: bytes, size: int, crc: int | None = None) -> bytes:
    """同步篡改中央目录与局部头，模拟长度声明小于实际压缩流。"""
    data = bytearray(payload)
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, local + 22, size)
    struct.pack_into("<I", data, central + 24, size)
    if crc is not None:
        struct.pack_into("<I", data, local + 14, crc)
        struct.pack_into("<I", data, central + 16, crc)
    return bytes(data)


@pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
def test_valid_package_preserves_original_reader(compression: int, tmp_path: Path) -> None:
    """有效包可按原始字节交给第三方，路径与流均能读取。"""
    payload = _package({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<document/>"}, compression)
    source = tmp_path / "document.docx"
    source.write_bytes(payload)
    validate_ooxml_package(payload)
    with OoxmlZipFile(source) as archive:
        assert archive.is_ooxml
        assert archive.read("word/document.xml") == b"<document/>"
    with ZipFile(source) as third_party:
        assert third_party.read("word/document.xml") == b"<document/>"


def test_validation_restores_borrowed_stream_position() -> None:
    """验证成功与失败均恢复调用者流位置并保持流打开。"""
    for payload in (_package({"word/document.xml": b"x"}), b"not a zip"):
        source = BytesIO(payload)
        source.seek(2)
        try:
            validate_ooxml_package(source)
        except BadZipFile:
            pass
        assert source.tell() == 2
        assert not source.closed


@pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
def test_entry_limit_accepts_boundary_and_rejects_overflow(monkeypatch: pytest.MonkeyPatch, compression: int) -> None:
    """成员大小恰好等于预算时成功，超出一字节时在解压前拒绝。"""
    _small_limits(monkeypatch)
    validate_ooxml_package(_package({"word/document.xml": b"x" * 1024}, compression))
    with pytest.raises(ResourceLimitError) as error:
        validate_ooxml_package(_package({"word/document.xml": b"x" * 1025}, compression))
    assert error.value.code == "resource_limit"


def test_total_limit_covers_all_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """无引用成员同样计入全包预算，防止多个小成员累加绕过。"""
    _small_limits(monkeypatch)
    validate_ooxml_package(_package({"a": b"x" * 1024, "b": b"x" * 1024}))
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(_package({"a": b"x" * 1024, "b": b"x" * 1024, "c": b"x"}))


@pytest.mark.parametrize("name", ["[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels"])
def test_control_xml_uses_smaller_limit(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """控制 XML 使用较小预算，工作表 XML 继续使用普通成员预算。"""
    _small_limits(monkeypatch)
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(_package({name: b"x" * 257}))
    validate_ooxml_package(_package({"xl/worksheets/sheet1.xml": b"x" * 1024}))


def test_forged_small_file_size_cannot_hide_inflation(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接检查原始压缩流，拒绝 ZipExtFile 会提前截断的伪造长度。"""
    _small_limits(monkeypatch)
    payload = _forge_uncompressed_size(_package({"word/document.xml": b"x" * 4096}), 1, zlib.crc32(b"x"))
    with ZipFile(BytesIO(payload)) as ordinary:
        assert ordinary.read("word/document.xml") == b"x"
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(payload)


def test_size_mismatch_inside_budget_is_malformed() -> None:
    """实际体积未超预算但声明不一致时拒绝交给第三方重新打开。"""
    payload = _forge_uncompressed_size(_package({"word/document.xml": b"x" * 20}), 1, zlib.crc32(b"x"))
    with pytest.raises(DocumentError) as error:
        validate_ooxml_package(payload)
    assert error.value.code == "open_failed"


def _empty_block_package(names: list[str], empty_blocks: int) -> bytes:
    """构造大量合法空 Deflate 块，每个成员最终只展开一个字节。"""
    compressed = b"\x00\x00\x00\xff\xff" * empty_blocks + b"\x01\x01\x00\xfe\xffx"
    local_records = bytearray()
    central_records = bytearray()
    for name in names:
        filename = name.encode("utf-8")
        offset = len(local_records)
        local_records.extend(
            struct.pack("<4s5H3I2H", b"PK\x03\x04", 20, 0, 8, 0, 33, zlib.crc32(b"x"), len(compressed), 1, len(filename), 0)
        )
        local_records.extend(filename)
        local_records.extend(compressed)
        central_records.extend(
            struct.pack(
                "<4s6H3I5H2I",
                b"PK\x01\x02",
                20,
                20,
                0,
                8,
                0,
                33,
                zlib.crc32(b"x"),
                len(compressed),
                1,
                len(filename),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
        )
        central_records.extend(filename)
    ending = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, len(names), len(names), len(central_records), len(local_records), 0)
    return bytes(local_records + central_records + ending)


def test_compressed_input_budget_rejects_empty_block_amplification(monkeypatch: pytest.MonkeyPatch) -> None:
    """解压量虽小，过长压缩输入仍会造成第三方 read 的内存分配。"""
    _small_limits(monkeypatch)
    payload = _empty_block_package(["word/document.xml"], 14_000)
    with ZipFile(BytesIO(payload)) as ordinary:
        assert ordinary.read("word/document.xml") == b"x"
    with pytest.raises(ResourceLimitError, match="compressed member"):
        validate_ooxml_package(payload)
    with pytest.raises(ResourceLimitError, match="compressed package"):
        validate_ooxml_package(_empty_block_package(["a", "b"], 10_000))


def test_incompressible_member_has_encoding_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    """允许刚好达到解压预算的普通不可压缩数据所需的少量编码开销。"""
    _small_limits(monkeypatch)
    payload = _package({"word/document.xml": random.Random(0).randbytes(1024)})
    with ZipFile(BytesIO(payload)) as ordinary:
        assert ordinary.infolist()[0].compress_size > 1024
    validate_ooxml_package(payload)


def test_local_encryption_flag_does_not_bypass_actual_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """只相信第三方实际使用的中央目录标志，避免局部加密位绕过验证。"""
    _small_limits(monkeypatch)
    payload = bytearray(_forge_uncompressed_size(_package({"word/document.xml": b"x" * 4096}), 1))
    struct.pack_into("<H", payload, 6, 1)
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(bytes(payload))


def test_member_count_checked_before_zipfile_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOCD 声明和实际目录条目都必须在创建 ZipInfo 列表前受限。"""
    payload = _package({"a": b"", "b": b"", "c": b""})
    monkeypatch.setattr(ooxml_zip, "MAX_MEMBERS", 2)
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(payload)
    forged = bytearray(payload)
    eocd = forged.rindex(b"PK\x05\x06")
    struct.pack_into("<2H", forged, eocd + 8, 1, 1)
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(bytes(forged))


def test_central_directory_size_checked_before_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """中央目录尺寸过大时只读取尾部元数据便拒绝。"""
    payload = _package({"a": b""})
    monkeypatch.setattr(ooxml_zip, "MAX_CENTRAL_DIRECTORY_BYTES", 16)
    with pytest.raises(ResourceLimitError):
        validate_ooxml_package(payload)


def test_classic_zip_accepts_exactly_65535_directory_entries() -> None:
    """经典 ZIP 可恰好包含 65535 项，此数量本身不要求 ZIP64 locator。"""
    count = 65535
    record = b"PK\x01\x02" + b"\x00" * 42
    directory = record * count
    ending = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, count, count, len(directory), 0, 0)
    # 只验证目录预算，避免本边界测试额外创建六万多个 ZipInfo 对象。
    ooxml_zip._check_directory_before_open(BytesIO(directory + ending))


def test_non_ooxml_candidate_does_not_apply_member_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """格式探测时无 OOXML 标记的包保留其他格式的成员策略。"""
    _small_limits(monkeypatch)
    with OoxmlZipFile(BytesIO(_package({"unrelated.bin": b"x" * 4096}, ZIP_BZIP2)), ooxml_only=True) as archive:
        assert not archive.is_ooxml
    with pytest.raises(DocumentError, match="Stored and Deflate"):
        OoxmlZipFile(BytesIO(_package({"word/document.xml": b"x"}, ZIP_BZIP2)))


def test_duplicate_members_are_rejected() -> None:
    """重复名称会造成不同读取方选择不同成员，必须拒绝。"""
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(ZipInfo("word/document.xml"), b"a")
        with pytest.warns(UserWarning):
            archive.writestr(ZipInfo("word/document.xml"), b"b")
    with pytest.raises(DocumentError, match="Duplicate"):
        validate_ooxml_package(stream.getvalue())


def test_crc_corruption_remains_available_to_normalizer() -> None:
    """预检只核资源，不提前阻断 normalizer 对坏可选成员的 CRC 容错。"""
    payload = bytearray(_package({"word/media/optional.bin": b"a"}, ZIP_STORED))
    payload[30 + len("word/media/optional.bin")] = ord("b")
    with OoxmlZipFile(BytesIO(payload)) as archive:
        with pytest.raises(BadZipFile, match="CRC"):
            archive.read("word/media/optional.bin")


class _NonSeekable(BytesIO):
    """生成带 data descriptor 的标准 ZIP 测试流。"""

    def seek(self, offset: int, whence: int = 0) -> int:
        """写入时拒绝回跳，使 zipfile 选择 data descriptor。"""
        raise UnsupportedOperation("not seekable")


@pytest.mark.parametrize("zip64", [False, True])
def test_data_descriptor_and_zip64_local_headers(zip64: bool) -> None:
    """局部头尺寸尚未回填和 ZIP64 局部 extra 均使用实际中央目录定位。"""
    stream = _NonSeekable()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        with archive.open("word/document.xml", "w", force_zip64=zip64) as member:
            member.write(b"content")
    validate_ooxml_package(stream.getvalue())


@pytest.mark.parametrize("prefix", [b"", b"self extracting prefix"])
def test_zip64_end_record_and_prefixed_package(prefix: bytes) -> None:
    """支持 ZIP64 结束记录和相对偏移保持原样的自解压前缀。"""
    payload = _package({"word/document.xml": b"content"})
    eocd = payload.rindex(b"PK\x05\x06")
    _, disk, directory_disk, disk_count, count, size, offset, comment = struct.unpack("<4s4H2IH", payload[eocd:])
    record = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, disk, directory_disk, disk_count, count, size, offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd, 1)
    ending = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, comment)
    validate_ooxml_package(prefix + payload[:eocd] + record + locator + ending)
