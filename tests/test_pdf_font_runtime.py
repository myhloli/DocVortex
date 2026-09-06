"""验证固定字库的 ABI、异常隔离和进程生命周期，不污染其他测试的 PDFium 状态。"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
import pypdfium2.raw as raw

from docgale.document.pdf.font_runtime import PdfiumFontError, _FontProvider, _cjk_charset, _load_font


@pytest.mark.parametrize(
    ("face", "charset", "expected"),
    [
        (b"anything", 134, 134),
        (b"anything", 136, 136),
        (b"anything", 128, 128),
        (b"anything", 129, 129),
        (b"ABCDEF+SimSun-Bold", 0, 134),
        (b"@ABCDEF+SimSun", 1, 134),
        ("宋体".encode("gb18030"), 1, 134),
        (b"MingLiU", 0, 136),
        (b"MS-PGothic", 1, 128),
        (b"Malgun Gothic", 1, 129),
        (b"private_GBK", 1, 134),
        (b"Helvetica", 0, None),
        (b"UnknownFont", 1, None),
        (b"SimSun", 2, None),
        (b"SimSun", 178, None),
    ],
)
def test_cjk_request_classification(face: bytes, charset: int, expected: int | None) -> None:
    """覆盖明确字符集、子集字体名及别名，并保护符号/其他语言请求。"""
    assert _cjk_charset(face, charset) == expected


class _DefaultFonts:
    """记录默认提供器调用，使用独立假句柄检查委托而不修改原生全局接口。"""

    def __init__(self) -> None:
        """为枚举重入和资源释放建立可观察状态。"""
        self.provider: _FontProvider | None = None
        self.mapped: list[int] = []
        self.deleted: list[int] = []
        self.freed = 0
        self.enum_face = b""

    def MapFont(self, info: Any, weight: int, italic: int, charset: int, pitch: int, face: Any, exact: Any) -> int:
        """记录传入字符集并返回不会与包装 ID 重合的系统句柄。"""
        self.mapped.append(charset)
        return 0xABCD

    def GetFont(self, info: Any, face: Any) -> int:
        """为枚举中的原始名称表查询返回系统句柄。"""
        return 0xABCD

    def EnumFonts(self, info: Any, mapper: Any) -> None:
        """模拟 PDFium 枚举本地化 CJK 字体时回调查询真实字体名称。"""
        assert self.provider is not None
        interface = self.provider.interface
        handle = interface.GetFont(ctypes.byref(interface), b"SimSun")
        buffer = ctypes.create_string_buffer(128)
        interface.GetFaceName(ctypes.byref(interface), handle, buffer, len(buffer))
        self.enum_face = buffer.value
        interface.DeleteFont(ctypes.byref(interface), handle)

    def GetFontData(self, info: Any, handle: int, table: int, buffer: Any, size: int) -> int:
        """返回可识别的系统字体数据查询结果。"""
        assert handle == 0xABCD
        return 17

    def GetFaceName(self, info: Any, handle: int, buffer: Any, size: int) -> int:
        """返回真实系统字体名，确认枚举未被替成 Droid 元数据。"""
        value = b"Original System Font\0"
        if buffer and size >= len(value):
            ctypes.memmove(buffer, value, len(value))
        return len(value)

    def GetFontCharset(self, info: Any, handle: int) -> int:
        """保持原系统字体字符集。"""
        return 0

    def DeleteFont(self, info: Any, handle: int) -> None:
        """记录真正需要原提供器释放的句柄。"""
        self.deleted.append(handle)


def _provider() -> tuple[_FontProvider, _DefaultFonts]:
    """借用当前平台 ABI 声明，仅安装 Python 假提供器。"""
    default = _DefaultFonts()
    pointer = SimpleNamespace(contents=default)

    def free_default(value: Any) -> None:
        """记录默认接口释放次数，避免向原生函数传入测试替身。"""
        assert value is pointer
        default.freed += 1

    facade = SimpleNamespace(
        FPDF_SYSFONTINFO=raw.FPDF_SYSFONTINFO,
        FPDF_GetDefaultSystemFontInfo=lambda: pointer,
        FPDF_FreeDefaultSystemFontInfo=free_default,
    )
    provider = _FontProvider(facade, "test")
    default.provider = provider
    return provider, default


def test_font_provider_preserves_enumeration_and_delegation() -> None:
    """枚举查询使用原名称表，实际 CJK 请求使用固定资源，系统字体仍按原方式释放。"""
    provider, default = _provider()
    interface = provider.interface
    interface.EnumFonts(ctypes.byref(interface), None)
    assert default.enum_face == b"Original System Font"
    cjk = interface.MapFont(ctypes.byref(interface), 455, 0, 134, 0, b"SimSun", None)
    assert provider.handles[cjk].kind == "bundled"
    assert default.mapped == []
    assert interface.GetFontData(ctypes.byref(interface), cjk, 0, None, 0) == len(provider.data)
    name = ctypes.create_string_buffer(160)
    interface.GetFaceName(ctypes.byref(interface), cjk, name, len(name))
    assert b"droid-cjk-v1" in name.value and provider.info.font_sha256.encode() in name.value
    interface.DeleteFont(ctypes.byref(interface), cjk)
    symbol = interface.MapFont(ctypes.byref(interface), 400, 0, 2, 0, b"SimSun", None)
    assert provider.handles[symbol].kind == "system"
    fallback = interface.GetFont(ctypes.byref(interface), b"SimSun")
    assert provider.handles[fallback].kind == "system"
    interface.DeleteFont(ctypes.byref(interface), symbol)
    interface.DeleteFont(ctypes.byref(interface), fallback)
    assert default.mapped == [2]
    assert provider.handles == {}
    provider._release(None)
    provider._release(None)
    assert default.freed == 1 and default.deleted == [0xABCD] * 3


def test_font_table_queries_respect_buffer_boundaries() -> None:
    """小缓冲区只返回所需长度，不发生部分写入或越界。"""
    provider, _ = _provider()
    interface = provider.interface
    handle = interface.MapFont(ctypes.byref(interface), 400, 0, 134, 0, b"SimSun", None)
    tag = int.from_bytes(b"head", "big")
    offset, size = provider.tables[tag]
    small = (ctypes.c_ubyte * (size - 1))(*([0xA5] * (size - 1)))
    assert interface.GetFontData(ctypes.byref(interface), handle, tag, small, len(small)) == size
    assert bytes(small) == b"\xa5" * len(small)
    target = (ctypes.c_ubyte * size)()
    assert interface.GetFontData(ctypes.byref(interface), handle, tag, target, size) == size
    assert bytes(target) == provider.data[offset : offset + size]
    assert interface.GetFontData(ctypes.byref(interface), handle, int.from_bytes(b"ttcf", "big"), None, 0) == 0
    interface.DeleteFont(ctypes.byref(interface), handle)
    provider._release(None)


def test_callback_failure_is_reported_after_native_boundary() -> None:
    """无效句柄在回调里转为 ABI 失败值，随后以明确异常阻止继续访问。"""
    provider, _ = _provider()
    assert provider.interface.GetFontData(ctypes.byref(provider.interface), 999999, 0, None, 0) == 0
    with pytest.raises(PdfiumFontError, match="_get_font_data"):
        provider.raise_if_failed()
    with pytest.raises(PdfiumFontError):
        provider.raise_if_failed()
    provider._release(None)


@pytest.mark.parametrize("missing", [False, True])
def test_font_resource_hash_is_enforced(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    """损坏的发行资源必须显式失败，不能交由系统字体兜底。"""
    from docgale.document.pdf import font_runtime

    class BrokenResource:
        """仅替换资源读取，保持测试不修改安装文件。"""

        def joinpath(self, *parts: str) -> BrokenResource:
            """返回同一资源替身以模拟缺失或损坏的字体。"""
            return self

        def read_text(self, *, encoding: str) -> str:
            """提供正确清单，让测试到达字节校验。"""
            return json.dumps(
                {"policy": "droid-cjk-v1", "file": "DroidSansFallbackFull.ttf", "sha256": font_runtime._FONT_SHA256}
            )

        def read_bytes(self) -> bytes:
            """返回不匹配固定哈希的内容。"""
            if missing:
                raise FileNotFoundError("bundled font missing")
            return b"broken font"

    monkeypatch.setattr(font_runtime.resources, "files", lambda package: BrokenResource())
    with pytest.raises(PdfiumFontError, match="missing" if missing else "SHA256"):
        _load_font()


def _run(code: str) -> str:
    """在新解释器中检查全局 PDFium 生命周期，避免回调状态污染测试进程。"""
    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_runtime_initialization_is_thread_safe_and_idempotent() -> None:
    """并发首次访问只安装一次提供器，返回同一不可变身份。"""
    _run("""
from concurrent.futures import ThreadPoolExecutor
from docgale.document.pdf import initialize_pdfium_runtime
from docgale.document.pdf import pdfium
with ThreadPoolExecutor(max_workers=8) as pool:
    results=list(pool.map(lambda _:initialize_pdfium_runtime(),range(32)))
assert all(value is results[0] for value in results)
assert results[0].font_policy=='droid-cjk-v1'
assert pdfium._font_provider.pid > 0
""")


def test_cleanup_does_not_initialize_fonts() -> None:
    """只有关闭操作时不应加载字库或安装原生回调。"""
    _run("""
from docgale.document.pdf import pdfium
class Child:
    def close(self):
        # 仅模拟关闭资源，不访问 PDFium。
        pass
pdfium.close_pdfium_document(Child())
pdfium.close_pdfium_child(Child())
assert pdfium._font_provider is None
""")


def test_import_does_not_read_font_resources() -> None:
    """公共 PDF 模块可导入，但字库只在首次实际访问时加载。"""
    _run("""
from unittest.mock import patch
with patch('importlib.resources.files',side_effect=AssertionError('font read during import')):
    from docgale.document.pdf import PDFDocument,initialize_pdfium_runtime
""")


def test_render_worker_initializes_its_own_font_runtime(tmp_path: Path) -> None:
    """实际重建 spawn 进程池，验证每代 worker 都有自己的固定字体状态。"""
    script = tmp_path / "worker.py"
    script.write_text(
        '''
import json,os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from docgale.document.pdf import initialize_pdfium_runtime
from docgale.document.pdf.images import _initialize_pdf_render_worker

def identity():
    """返回当前 worker 的运行时身份。"""
    return (os.getpid(), initialize_pdfium_runtime().font_sha256)

if __name__=='__main__':
    values=[]
    for _ in range(2):
        with ProcessPoolExecutor(max_workers=1,mp_context=get_context('spawn'),initializer=_initialize_pdf_render_worker) as pool:
            values.append(pool.submit(identity).result(timeout=30))
    assert values[0][0]!=values[1][0]
    assert values[0][1]==values[1][1]
    print(json.dumps(values))
''',
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr
    assert len(json.loads(completed.stdout)) == 2
