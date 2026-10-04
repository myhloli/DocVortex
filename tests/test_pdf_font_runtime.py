"""Verify ABI, exception isolation and process lifecycle of fixed fonts without contaminating the PDFium state of other tests."""

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

from docvortex.document.pdf.font_runtime import PdfiumFontError, _FontProvider, _cjk_charset, _load_font


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
    """Override explicit character sets, subset font names and aliases, and protect symbol/other language requests."""
    assert _cjk_charset(face, charset) == expected


class _DefaultFonts:
    """Log default provider calls and use independent fake handles to check delegates without modifying the native global interface."""

    def __init__(self) -> None:
        """Establish observable state for enum loading and resource release."""
        self.provider: _FontProvider | None = None
        self.mapped: list[int] = []
        self.deleted: list[int] = []
        self.freed = 0
        self.enum_face = b""

    def MapFont(self, info: Any, weight: int, italic: int, charset: int, pitch: int, face: Any, exact: Any) -> int:
        """Records the incoming character set and returns a system handle that does not coincide with the wrapper ID."""
        self.mapped.append(charset)
        return 0xABCD

    def GetFont(self, info: Any, face: Any) -> int:
        """Returns a system handle for the original name table query in the enumeration."""
        return 0xABCD

    def EnumFonts(self, info: Any, mapper: Any) -> None:
        """Simulate PDFium and callback to query the real font name when enumerating the localized CJK font."""
        assert self.provider is not None
        interface = self.provider.interface
        handle = interface.GetFont(ctypes.byref(interface), b"SimSun")
        buffer = ctypes.create_string_buffer(128)
        interface.GetFaceName(ctypes.byref(interface), handle, buffer, len(buffer))
        self.enum_face = buffer.value
        interface.DeleteFont(ctypes.byref(interface), handle)

    def GetFontData(self, info: Any, handle: int, table: int, buffer: Any, size: int) -> int:
        """Returns query results for identifiable system font data."""
        assert handle == 0xABCD
        return 17

    def GetFaceName(self, info: Any, handle: int, buffer: Any, size: int) -> int:
        """Returns the real system font name, confirming that the enumeration has not been replaced with Droid metadata."""
        value = b"Original System Font\0"
        if buffer and size >= len(value):
            ctypes.memmove(buffer, value, len(value))
        return len(value)

    def GetFontCharset(self, info: Any, handle: int) -> int:
        """Keep the original system font character set."""
        return 0

    def DeleteFont(self, info: Any, handle: int) -> None:
        """Records the handles that actually need to be released by the original provider."""
        self.deleted.append(handle)


def _provider() -> tuple[_FontProvider, _DefaultFonts]:
    """Borrow the current platform ABI declaration and install only the Python fake provider."""
    default = _DefaultFonts()
    pointer = SimpleNamespace(contents=default)

    def free_default(value: Any) -> None:
        """Record the number of default interface releases to avoid passing test doubles to native functions."""
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
    """The enumeration query uses the original name table, the actual CJK request uses fixed resources, and the system fonts are still released in the original way."""
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
    """Small buffers only return the required length, no partial writes or out-of-bounds occurrences occur."""
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
    """Invalid handles are converted to a ABI failure value in the callback, and subsequent access is prevented with an explicit exception."""
    provider, _ = _provider()
    assert provider.interface.GetFontData(ctypes.byref(provider.interface), 999999, 0, None, 0) == 0
    with pytest.raises(PdfiumFontError, match="_get_font_data"):
        provider.raise_if_failed()
    with pytest.raises(PdfiumFontError):
        provider.raise_if_failed()
    provider._release(None)


@pytest.mark.parametrize("missing", [False, True])
def test_font_resource_hash_is_enforced(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    """Corrupt distribution resources must fail explicitly and cannot be left to system fonts."""
    from docvortex.document.pdf import font_runtime

    class BrokenResource:
        """Only replace resource reads, leaving the test without modifying the installation files."""

        def joinpath(self, *parts: str) -> BrokenResource:
            """Returns the same resource double to simulate missing or corrupted fonts."""
            return self

        def read_text(self, *, encoding: str) -> str:
            """Provide the correct manifest so that the test reaches byte verification."""
            return json.dumps(
                {"policy": "droid-cjk-v1", "file": "DroidSansFallbackFull.ttf", "sha256": font_runtime._FONT_SHA256}
            )

        def read_bytes(self) -> bytes:
            """Returns content that does not match the fixed hash."""
            if missing:
                raise FileNotFoundError("bundled font missing")
            return b"broken font"

    monkeypatch.setattr(font_runtime.resources, "files", lambda package: BrokenResource())
    with pytest.raises(PdfiumFontError, match="missing" if missing else "SHA256"):
        _load_font()


def _run(code: str) -> str:
    """Check the global PDFium lifecycle in the new interpreter to avoid polluting the test process with callback states."""
    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_runtime_initialization_is_thread_safe_and_idempotent() -> None:
    """Concurrent first access installs the provider only once, returning the same immutable identity."""
    _run("""
from concurrent.futures import ThreadPoolExecutor
from docvortex.document.pdf import initialize_pdfium_runtime
from docvortex.document.pdf import pdfium
with ThreadPoolExecutor(max_workers=8) as pool:
    results=list(pool.map(lambda _:initialize_pdfium_runtime(),range(32)))
assert all(value is results[0] for value in results)
assert results[0].font_policy=='droid-cjk-v1'
assert pdfium._font_provider.pid > 0
""")


def test_cleanup_does_not_initialize_fonts() -> None:
    """Only close operations should not load fonts or install native callbacks."""
    _run("""
from docvortex.document.pdf import pdfium
class Child:
    def close(self):
        # 仅模拟关闭资源，不访问 PDFium。
        pass
pdfium.close_pdfium_document(Child())
pdfium.close_pdfium_child(Child())
assert pdfium._font_provider is None
""")


def test_import_does_not_read_font_resources() -> None:
    """The public PDF module can be imported, but the font is only loaded on the first actual access."""
    _run("""
from unittest.mock import patch
with patch('importlib.resources.files',side_effect=AssertionError('font read during import')):
    from docvortex.document.pdf import PDFDocument,initialize_pdfium_runtime
""")


def test_render_worker_initializes_its_own_font_runtime(tmp_path: Path) -> None:
    """Actually rebuild the spawn process pool and verify that each generation of worker has its own fixed font state."""
    script = tmp_path / "worker.py"
    script.write_text(
        '''
import json,os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from docvortex.document.pdf import initialize_pdfium_runtime
from docvortex.document.pdf.images import _initialize_pdf_render_worker

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
