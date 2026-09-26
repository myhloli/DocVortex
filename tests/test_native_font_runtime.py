"""在独立进程验证原生字体回调，避免污染其它测试的 PDFium 全局状态。"""
import os
import subprocess
import sys

import pytest

from docvortex._compute_backend import get_native


def _run_native_font_check(source: str) -> None:
    """仅在兼容原生扩展可用时启动独立解释器执行字体生命周期场景。"""
    native = get_native()
    if native is None or not hasattr(native, "NativeFontProvider"):
        pytest.skip("native font provider extension unavailable")
    import pypdfium2.raw as raw
    from docvortex.document.pdf.font_runtime import _native_font_bridge, native_font_runtime_info

    if _native_font_bridge(raw) is None:
        pytest.skip(native_font_runtime_info()["native_font_provider_unavailable_reason"])
    environment = dict(os.environ, DOCVORTEX_COMPUTE_BACKEND="rust")
    subprocess.run([sys.executable, "-c", source], env=environment, check=True, timeout=60)


def test_native_font_names_and_table_bytes() -> None:
    """比较全部别名、五编码、子集前缀和 Unicode 变体，验证表复制与错误隔离。"""
    _run_native_font_check(r'''
import ctypes as c
import pypdfium2.raw as raw
from docvortex.document.pdf.font_runtime import (
    _FontProvider, _NativeFontProvider, _FONT_ALIASES, _STYLE_SUFFIXES,
    _cjk_charset, _load_font, PdfiumFontError,
)
p = _FontProvider(raw, "test")
assert isinstance(p, _NativeFontProvider)
for name in list(_FONT_ALIASES) + ["UnknownFont", "private_GBK", "ſimſun", "Sim\u001csun", "ＳimSun"]:
    for variant in [name, "@ABCDEF+" + name, name + "-Bold", name.upper()]:
        for encoding in ("utf-8", "gb18030", "big5", "cp932", "cp949"):
            try:
                face = variant.encode(encoding)
            except UnicodeEncodeError:
                continue
            for charset in (0, 1, 2, 128, 129, 134, 136, 178):
                assert p.native.classify(face, charset) == _cjk_charset(face, charset), (face, charset)
info = c.cast(p.native._interface_address(), c.POINTER(raw.FPDF_SYSFONTINFO))
font = info.contents.MapFont(info, 400, 0, 134, 0, b"SimSun", None)
data, tables = _load_font()
for tag, (offset, size) in tables.items():
    assert info.contents.GetFontData(info, font, tag, None, 0) == size
    target = (c.c_ubyte * size)()
    assert info.contents.GetFontData(info, font, tag, target, size) == size
    assert bytes(target) == data[offset:offset + size]
    if size:
        small = (c.c_ubyte * (size - 1))(*([165] * (size - 1)))
        info.contents.GetFontData(info, font, tag, small, size - 1)
        assert bytes(small) == b"\xa5" * (size - 1)
info.contents.DeleteFont(info, font)
assert info.contents.GetFontData(info, 999999, 0, None, 0) == 0
try:
    p.raise_if_failed()
except PdfiumFontError as exc:
    assert "_get_font_data" in str(exc)
else:
    raise AssertionError("invalid handle was not reported")
info.contents.Release(info)
info.contents.Release(info)
assert p.released
''')


def test_native_font_runtime_allows_serial_thread_transfer() -> None:
    """全局锁允许调用线程变化，不应被 PyO3 的 unsendable 线程检查阻断。"""
    _run_native_font_check(r'''
from concurrent.futures import ThreadPoolExecutor
from docvortex.document.pdf.pdfium import initialize_pdfium_runtime, pdfium_guard
from docvortex.document.pdf import pdfium
from docvortex.document.pdf.font_runtime import _NativeFontProvider
initialize_pdfium_runtime()
assert isinstance(pdfium._font_provider, _NativeFontProvider)
def check():
    """在不同线程持锁检查同一个字体运行时。"""
    with pdfium_guard():
        pdfium._font_provider.raise_if_failed()
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(lambda _: check(), range(10)))
''')


def test_native_font_default_delegation_and_enumeration() -> None:
    """以同 ABI 假默认接口验证重入枚举、系统句柄及单次释放。"""
    _run_native_font_check(r'''
import ctypes as c
import os
import pypdfium2.raw as raw
from docvortex._compute_backend import get_native
from docvortex.document.pdf.font_runtime import _load_font, _FONT_ALIASES, _STYLE_SUFFIXES
factory = c.WINFUNCTYPE if os.name == "nt" else c.CFUNCTYPE
fields = dict(raw.FPDF_SYSFONTINFO._fields_)
ptr = c.POINTER(raw.FPDF_SYSFONTINFO)
deleted, freed, mapped, enum_names = [], [], [], []

def get_font(default, face):
    """返回可识别的系统句柄。"""
    return 0xABCD

def map_font(default, weight, italic, charset, pitch, face, exact):
    """记录系统请求，符号字符集必须继续委托。"""
    mapped.append(charset)
    return 0xABCD

def get_name(default, font, buffer, size):
    """返回系统名称，枚举不得变成固定字库名。"""
    value = b"SystemFont\0"
    if buffer and size >= len(value):
        c.memmove(buffer, value, len(value))
    return len(value)

def delete_font(default, font):
    """记录原生系统句柄回收。"""
    deleted.append(font)

def enumerate_fonts(default, mapper):
    """从默认枚举重入包装接口，检查未持有不可重入内部借用。"""
    font = info.contents.GetFont(info, b"SimSun")
    buffer = c.create_string_buffer(64)
    info.contents.GetFaceName(info, font, buffer, len(buffer))
    enum_names.append(buffer.value)
    info.contents.DeleteFont(info, font)

callbacks = {
    "EnumFonts": fields["EnumFonts"](enumerate_fonts),
    "MapFont": fields["MapFont"](map_font),
    "GetFont": fields["GetFont"](get_font),
    "GetFaceName": fields["GetFaceName"](get_name),
    "DeleteFont": fields["DeleteFont"](delete_font),
}
default = raw.FPDF_SYSFONTINFO(version=1, **callbacks)
get_default = factory(c.c_void_p)(lambda: c.addressof(default))
free_default = factory(None, ptr)(lambda value: freed.append(c.addressof(value.contents)))
set_default = factory(None, ptr)(lambda value: None)
legacy = factory(c.c_int, c.POINTER(c.c_ubyte), c.c_size_t, c.c_int)(lambda face, size, charset: -1)
data, tables = _load_font()
provider = get_native().NativeFontProvider(
    [c.cast(f, c.c_void_p).value for f in (get_default, free_default, set_default)],
    data, tables, b"fixed-cache", _FONT_ALIASES, _STYLE_SUFFIXES, c.cast(legacy, c.c_void_p).value,
)
info = c.cast(provider._interface_address(), ptr)
provider.install()
info.contents.EnumFonts(info, None)
symbol = info.contents.MapFont(info, 400, 0, 2, 0, b"SimSun", None)
fallback = info.contents.GetFont(info, b"SimSun")
provider.raise_if_failed()
assert mapped == [2]
assert enum_names == [b"SystemFont"]
info.contents.Release(info)
info.contents.Release(info)
assert deleted == [0xABCD] * 3
assert freed == [c.addressof(default)]
assert provider.stats()[0]
''')


def test_native_font_callback_and_diagnostics_do_not_deadlock() -> None:
    """原生回调等待 Python 时，另一线程查询诊断必须让出 GIL 后等状态锁。"""
    _run_native_font_check(r'''
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pypdfium2.raw as raw
from docvortex.document.pdf import font_runtime as fonts
provider = fonts._FontProvider(raw, "test")
entered, proceed, reading = Event(), Event(), Event()
original = fonts._cjk_charset

def classify(face, charset):
    """暂停低频 Python 规范化以覆盖锁与 GIL 的相反获取顺序。"""
    entered.set()
    assert proceed.wait(10)
    return original(face, charset)

def read_stats():
    """标记诊断即将进入原生锁等待。"""
    reading.set()
    return provider.native.stats()

fonts._cjk_charset = classify
with ThreadPoolExecutor(max_workers=2) as pool:
    request = pool.submit(provider.native.classify, "宋体".encode(), 1)
    assert entered.wait(10)
    stats = pool.submit(read_stats)
    assert reading.wait(10)
    proceed.set()
    assert request.result(10) == 134
    assert stats.result(10) == (False, 0, 0)
''')


def test_native_font_abi_fallback_reports_concrete_reason() -> None:
    """非标准调用标志必须保留参考路径，并明确记录拒绝的符号。"""
    _run_native_font_check(r'''
import ctypes as c
from types import SimpleNamespace
import pypdfium2.raw as raw
from docvortex.document.pdf.font_runtime import _native_font_bridge, native_font_runtime_info
wrapped = c.CFUNCTYPE(c.POINTER(raw.FPDF_SYSFONTINFO), use_errno=True)(c.cast(raw.FPDF_GetDefaultSystemFontInfo, c.c_void_p).value)
facade = SimpleNamespace(
    FPDF_SYSFONTINFO=raw.FPDF_SYSFONTINFO,
    FPDF_GetDefaultSystemFontInfo=wrapped,
    FPDF_FreeDefaultSystemFontInfo=raw.FPDF_FreeDefaultSystemFontInfo,
    FPDF_SetSystemFontInfo=raw.FPDF_SetSystemFontInfo,
)
assert _native_font_bridge(facade) is None
reason = native_font_runtime_info()["native_font_provider_unavailable_reason"]
assert reason == "FPDF_GetDefaultSystemFontInfo calling convention mismatch", reason
assert not native_font_runtime_info()["native_font_provider_installed"]
''')
