"""验证路径格式识别的读取上限、容器优先级及扩展名回退。"""

from __future__ import annotations

import builtins
import io
from pathlib import Path
import tracemalloc
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from docvortex.document import detection
from test_format_matrix import source_payload


class _ReadAudit:
    """包装真实文件句柄，记录返回的字节数并保留 seek 和关闭行为。"""

    def __init__(self, stream, reads: list[int]) -> None:
        """保存受审计句柄和本次识别共享的读取记录。"""
        self.stream = stream
        self.reads = reads

    def __getattr__(self, name: str):
        """把 seek、tell 等未拦截操作委托给真实文件。"""
        return getattr(self.stream, name)

    def __enter__(self):
        """进入文件上下文时继续返回审计包装。"""
        return self

    def __exit__(self, *args):
        """按原有异常语义关闭真实句柄。"""
        return self.stream.__exit__(*args)

    def read(self, size: int = -1) -> bytes:
        """记录实际返回量，允许 ZIP 从有界尾部位置使用 read(-1)。"""
        data = self.stream.read(size)
        self.reads.append(len(data))
        return data


def _audit_reads(monkeypatch: pytest.MonkeyPatch, source: Path) -> tuple[list[int], list[_ReadAudit]]:
    """同时拦截 open、Path.open 和 ZipFile 的目标文件读取。"""
    reads: list[int] = []
    streams: list[_ReadAudit] = []
    original_open = builtins.open

    def open_source(path, *args, **kwargs):
        """仅包装目标路径，其他库资源继续使用真实打开函数。"""
        stream = original_open(path, *args, **kwargs)
        if isinstance(path, (str, Path)) and Path(path) == source:
            audited = _ReadAudit(stream, reads)
            streams.append(audited)
            return audited
        return stream

    monkeypatch.setattr(builtins, "open", open_source)
    monkeypatch.setattr(io, "open", open_source)
    return reads, streams


@pytest.mark.parametrize("size", [2 * 1024**2, 16 * 1024**2])
def test_pdf_path_reads_are_bounded_by_headers_and_zip_tail(size, tmp_path, monkeypatch) -> None:
    """普通 PDF 的读入量不随正文增长，同时保留尾部 ZIP 身份检查。"""
    source = tmp_path / "large.csv"
    source.write_bytes(b"%PDF-1.7\n" + b" " * size + b"\n%%EOF\n")
    reads, streams = _audit_reads(monkeypatch, source)

    def forbidden():
        """强签名成功后不得加载通用识别模型。"""
        raise AssertionError("PDF signature should bypass Magika")

    monkeypatch.setattr(detection, "_magika", forbidden)
    assert detection.guess_suffix_by_path(source) == "pdf"
    assert reads and sum(reads) < 300_000
    assert max(reads) <= 65558
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("prefix", [b"%PDF-1.7", b"plain text", b"", detection.OLE2_SIG_BYTES[:-1]])
def test_non_ole_path_probe_reads_only_magic(prefix, tmp_path, monkeypatch) -> None:
    """非 OLE 文件只读八字节，不把完整载荷传入容器解析器。"""
    source = tmp_path / "candidate.doc"
    source.write_bytes(prefix + b"x" * 1024)
    reads, streams = _audit_reads(monkeypatch, source)

    def forbidden(_data):
        """非 OLE magic 必须在调用字节解析器前返回。"""
        raise AssertionError("non-OLE payload was read")

    monkeypatch.setattr(detection, "_guess_ole2_suffix_by_bytes", forbidden)
    assert detection._guess_ole2_suffix_by_path(source) is None
    assert reads == [8]
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("suffix", ["doc", "xls", "ppt"])
def test_real_ole_probe_rewinds_and_keeps_container_detection(suffix, tmp_path, monkeypatch) -> None:
    """实际旧 Office 容器通过 magic 后仍从文件起点解析，流身份优于错误后缀。"""
    data = source_payload(suffix)
    source = tmp_path / "disguised.pdf"
    source.write_bytes(data)
    reads, streams = _audit_reads(monkeypatch, source)
    assert detection._guess_ole2_suffix_by_path(source) == suffix
    assert reads == [8, len(data)]
    assert all(stream.closed for stream in streams)
    assert detection.guess_suffix_by_path(source) == detection.guess_suffix_by_bytes(data, str(source)) == suffix


def test_ole_magic_probe_does_not_duplicate_full_container(tmp_path, monkeypatch) -> None:
    """先读 magic 再整读时不能额外拼接完整载荷，避免 OLE 分配峰值翻倍。"""
    source = tmp_path / "large.doc"
    size = 4 * 1024**2
    source.write_bytes(detection.OLE2_SIG_BYTES + b"x" * (size - 8))

    def read_container(data):
        """隔离文件读取层，确认后续解析仍收到从 magic 开始的完整载荷。"""
        assert len(data) == size and data.startswith(detection.OLE2_SIG_BYTES)
        return "doc"

    monkeypatch.setattr(detection, "_guess_ole2_suffix_by_bytes", read_container)
    tracemalloc.start()
    try:
        assert detection._guess_ole2_suffix_by_path(source) == "doc"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < size + 128 * 1024


@pytest.mark.parametrize("suffix", ["docx", "pptx", "xlsx", "odt", "ods", "odp", "epub", "ofd"])
@pytest.mark.parametrize("prefix,byte_suffix,path_suffix", [
    (b"%PDF-1.7\n%%EOF\n", "pdf", None),
    (b"{\\rtf1 body}\n", "rtf", "rtf"),
    (b'Content-Type: multipart/related; boundary="x"\r\n\r\n', "mhtml", None),
    (b"arbitrary self-extracting prefix\n", None, None),
    (detection.OLE2_SIG_BYTES + b"\0" * 512, None, None),
])
def test_prefixed_zip_keeps_existing_entrypoint_priorities(suffix, prefix, byte_suffix, path_suffix, tmp_path) -> None:
    """锁定 PDF/MHTML 与尾部 ZIP 的既有入口差异，不把性能修改变成判型迁移。"""
    data = prefix + source_payload(suffix)
    source = tmp_path / "mixed.csv"
    source.write_bytes(data)
    assert detection.guess_suffix_by_path(source) == (path_suffix or suffix)
    assert detection.guess_suffix_by_bytes(data, str(source)) == (byte_suffix or suffix)


@pytest.mark.parametrize("suffix", [
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf", "csv", "tsv", "html",
    "mhtml", "epub", "ofd", "odt", "ods", "odp",
])
def test_supported_formats_keep_bytes_and_path_results(suffix, tmp_path) -> None:
    """真实样例及有效合成容器在路径和字节入口得到既有格式结论。"""
    data = source_payload(suffix)
    source = tmp_path / f"sample.{suffix}"
    source.write_bytes(data)
    assert detection.guess_suffix_by_path(str(source)) == detection.guess_suffix_by_bytes(data, str(source)) == suffix


@pytest.mark.parametrize("data", [
    detection.OLE2_SIG_BYTES + b"\0" * 512,
    b"PK\x03\x04truncated zip",
    b"%PD fake signature",
    b"not an office container",
    b"",
])
@pytest.mark.parametrize("extension,expected", [("doc", "unknown"), ("docx", "unknown"), ("csv", "csv"), ("htm", "html")])
def test_invalid_headers_and_packages_keep_verified_fallbacks(data, extension, expected, tmp_path, monkeypatch) -> None:
    """伪造头部与损坏包不能绕过 Office 身份验证，文本扩展名回退保持不变。"""
    result = SimpleNamespace(prediction=SimpleNamespace(output=SimpleNamespace(label="unknown")))

    def classify(_source):
        """固定弱识别结果，单独检查容器校验和扩展名回退合同。"""
        return result

    def classifier():
        """为两个入口返回相同的通用识别器替身。"""
        return SimpleNamespace(identify_bytes=classify, identify_path=classify)

    monkeypatch.setattr(detection, "_magika", classifier)
    source = tmp_path / f"invalid.{extension}"
    source.write_bytes(data)
    assert detection.guess_suffix_by_path(source) == detection.guess_suffix_by_bytes(data, str(source)) == expected


def test_pdf_with_unrecognized_zip_still_uses_pdf_signature(tmp_path) -> None:
    """普通 ZIP 尾包没有文档身份时仍回退到 PDF 头，不仅凭 ZIP 存在改变格式。"""
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("note.txt", "not a document package")
    data = b"%PDF-1.7\n%%EOF\n" + stream.getvalue()
    source = tmp_path / "mixed.pdf"
    source.write_bytes(data)
    assert detection.guess_suffix_by_path(source) == detection.guess_suffix_by_bytes(data) == "pdf"


@pytest.mark.parametrize("data", [b"%PDF", b"%PDFnot-a-parseable-document"])
def test_pdf_signature_detection_does_not_add_document_validation(data, tmp_path) -> None:
    """格式探测继续只认四字节 PDF 签名，正文是否可解析仍由后续入口验证。"""
    source = tmp_path / "spoofed.docx"
    source.write_bytes(data)
    assert detection.guess_suffix_by_path(source) == detection.guess_suffix_by_bytes(data, str(source)) == "pdf"


def test_missing_and_directory_inputs_keep_io_errors(tmp_path) -> None:
    """头部探测的容错不吞掉公开入口最终的缺失文件或目录错误。"""
    with pytest.raises(FileNotFoundError):
        detection.guess_suffix_by_path(tmp_path / "missing.pdf")
    with pytest.raises(IsADirectoryError):
        detection.guess_suffix_by_path(tmp_path)
