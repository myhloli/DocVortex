"""固定 CJK 字体资源与 PDFium 字体提供器；导入时不读取字体或初始化 PDFium。"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
import json
import logging
import os
import re
import struct
from typing import Any

_FONT_FILE = "DroidSansFallbackFull.ttf"
_FONT_SHA256 = "8a4dea0899424438af25a6f1f6eb61e5d111d85367eece0a5d30170861ae6b2e"
_FONT_NAME = "Droid Sans Fallback Full"
_POLICY = "droid-cjk-v1"
_CJK_CHARSETS = frozenset({128, 129, 134, 136})
_STYLE_SUFFIXES = (
    "bolditalic",
    "boldoblique",
    "semibold",
    "demibold",
    "regular",
    "medium",
    "normal",
    "italic",
    "oblique",
    "light",
    "bold",
)
_FONT_ALIASES = {
    "stsong": 134,
    "simsun": 134,
    "nsimsun": 134,
    "simhei": 134,
    "fangsong": 134,
    "kaiti": 134,
    "microsoftyahei": 134,
    "songtisc": 134,
    "heitisc": 134,
    "droidsansfallbackfull": 134,
    "droidsansfallback": 134,
    "notosanscjksc": 134,
    "notoserifcjksc": 134,
    "宋体": 134,
    "新宋体": 134,
    "黑体": 134,
    "仿宋": 134,
    "楷体": 134,
    "微软雅黑": 134,
    "msung": 136,
    "mhei": 136,
    "mingliu": 136,
    "pmingliu": 136,
    "dfkaisb": 136,
    "microsoftjhenghei": 136,
    "songtitc": 136,
    "heititc": 136,
    "notosanscjktc": 136,
    "notoserifcjktc": 136,
    "新細明體": 136,
    "細明體": 136,
    "標楷體": 136,
    "微軟正黑體": 136,
    "heiseiminw3": 128,
    "heiseikakugow5": 128,
    "kozminpro": 128,
    "msmincho": 128,
    "mspmincho": 128,
    "msgothic": 128,
    "mspgothic": 128,
    "yumincho": 128,
    "yugothic": 128,
    "meiryo": 128,
    "notosanscjkjp": 128,
    "notoserifcjkjp": 128,
    "hygothic": 129,
    "hysmyeongjo": 129,
    "gulim": 129,
    "batang": 129,
    "dotum": 129,
    "malgungothic": 129,
    "notosanscjkkr": 129,
    "notoserifcjkkr": 129,
}
_logger = logging.getLogger(__name__)


class PdfiumFontError(RuntimeError):
    """固定字体资源或字体提供器失效，不能作为文档损坏而静默降级。"""


@dataclass(frozen=True, slots=True)
class PdfiumRuntimeInfo:
    """记录当前进程采用的 PDFium 与固定字体身份，不表示每份文档都发生了替代。"""

    pdfium_version: str
    font_policy: str
    font_name: str
    font_sha256: str


def _cjk_charset(face: bytes, charset: int) -> int | None:
    """优先按字符集识别 CJK，仅用明确族名和编码标记补充未指定字符集的请求。"""
    if charset in _CJK_CHARSETS:
        return charset
    if charset not in (0, 1):
        return None
    # 明确的符号/其他语言字符集不会被字体名覆盖；不做任意汉字或子串猜测。
    for encoding in ("utf-8", "gb18030", "big5", "cp932", "cp949"):
        try:
            name = face.decode(encoding)
        except UnicodeDecodeError:
            continue
        name = re.sub(r"^[A-Z]{6}\+", "", name.lstrip("@"))
        name = re.sub(r"[\s,_-]+", "", name).casefold()
        candidates = [name]
        for suffix in _STYLE_SUFFIXES:
            if name.endswith(suffix):
                candidates.append(name[: -len(suffix)])
        for candidate in candidates:
            if candidate in _FONT_ALIASES:
                return _FONT_ALIASES[candidate]
            for marker, value in (("gb2312", 134), ("gbk", 134), ("big5", 136)):
                if candidate.endswith(marker):
                    return value
    return None


def _load_font() -> tuple[bytes, dict[int, tuple[int, int]]]:
    """校验发行资源身份并读取 SFNT 索引；各表保留偏移，避免复制整份字库。"""
    try:
        root = resources.files("docvortex").joinpath("resources", "fonts")
        manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
        if (manifest["policy"], manifest["file"], manifest["sha256"]) != (_POLICY, _FONT_FILE, _FONT_SHA256):
            raise ValueError("font manifest does not match this runtime")
        data = root.joinpath(_FONT_FILE).read_bytes()
        if sha256(data).hexdigest() != _FONT_SHA256:
            raise ValueError("font SHA256 mismatch")
        if data[:4] != b"\x00\x01\x00\x00":
            raise ValueError("expected the pinned standalone TrueType font")
        count = struct.unpack_from(">H", data, 4)[0]
        tables = {}
        for index in range(count):
            tag, _checksum, offset, size = struct.unpack_from(">4sIII", data, 12 + index * 16)
            if offset + size > len(data):
                raise ValueError("invalid SFNT table bounds")
            tables[int.from_bytes(tag, "big")] = (offset, size)
        return data, tables
    except Exception as exc:
        raise PdfiumFontError(f"Unable to load bundled {_FONT_FILE}: {exc}") from exc


class _FontProvider:
    """核验同库 ABI 后创建唯一原生字体提供器。"""

    def __new__(cls, raw: Any, pdfium_version: str) -> Any:
        """保留旧构造入口，字体句柄与回调状态统一由 Rust 管理。"""
        native, functions, callback_type = _native_font_bridge(raw)
        return _NativeFontProvider(raw, pdfium_version, native, functions, callback_type)


_NATIVE_KEEPALIVE: list[Any] = []
_NATIVE_FONT_UNAVAILABLE_REASON: str | None = "not probed"
_NATIVE_FONT_INSTALLED = False


def native_font_runtime_info() -> dict[str, Any]:
    """区分扩展可用、ABI 已匹配与接口已安装，不能将扩展存在当作原生回调已启用。"""
    return {
        "native_font_provider_installed": _NATIVE_FONT_INSTALLED,
        "native_font_provider_unavailable_reason": _NATIVE_FONT_UNAVAILABLE_REASON,
    }


def _font_bridge_unavailable(reason: str) -> None:
    """记录 ABI 故障并立即失败，禁止切换到 Python 字体回调实现。"""
    global _NATIVE_FONT_UNAVAILABLE_REASON
    _NATIVE_FONT_UNAVAILABLE_REASON = reason
    raise PdfiumFontError(reason)


def _native_font_bridge(raw: Any) -> Any:
    """严格验证结构布局、每个回调和三个入口的 ABI 后才传递原生地址。"""
    from ..._compute_backend import get_native

    global _NATIVE_FONT_UNAVAILABLE_REASON
    native = get_native()
    pointer = ctypes.POINTER(raw.FPDF_SYSFONTINFO)
    void, integer = ctypes.c_void_p, ctypes.c_int
    char, byte = ctypes.POINTER(ctypes.c_char), ctypes.POINTER(ctypes.c_ubyte)
    callbacks = (
        ("Release", None, (pointer,)),
        ("EnumFonts", None, (pointer, void)),
        ("MapFont", void, (pointer, integer, integer, integer, integer, char, ctypes.POINTER(integer))),
        ("GetFont", void, (pointer, char)),
        ("GetFontData", ctypes.c_ulong, (pointer, void, ctypes.c_uint, byte, ctypes.c_ulong)),
        ("GetFaceName", ctypes.c_ulong, (pointer, void, char, ctypes.c_ulong)),
        ("GetFontCharset", integer, (pointer, void)),
        ("DeleteFont", None, (pointer, void)),
    )
    # FPDF_SYSFONTINFO 字段是普通 C 回调；库入口在支持的 Windows 64 位平台共用同一 ABI。
    callback_type = ctypes.CFUNCTYPE
    function_flags = {ctypes.CFUNCTYPE(None)._flags_}
    if os.name == "nt" and ctypes.sizeof(void) == 8:
        # pypdfium2 5.13 的 Windows 绑定使用 CDLL；WinDLL 包装在 64 位 ABI 下同样合法。
        function_flags.add(ctypes.WINFUNCTYPE(None)._flags_)
    fields = dict(raw.FPDF_SYSFONTINFO._fields_)
    alignment = ctypes.alignment(void)
    start = (ctypes.sizeof(integer) + alignment - 1) // alignment * alignment
    if fields.get("version") is not integer or raw.FPDF_SYSFONTINFO.version.offset != 0:
        return _font_bridge_unavailable("FPDF_SYSFONTINFO version ABI mismatch")
    if ctypes.sizeof(raw.FPDF_SYSFONTINFO) != start + 8 * ctypes.sizeof(void):
        return _font_bridge_unavailable("FPDF_SYSFONTINFO size ABI mismatch")
    for index, (name, result, arguments) in enumerate(callbacks):
        field = fields.get(name)
        expected = callback_type(result, *arguments)
        if (
            field is None
            or getattr(raw.FPDF_SYSFONTINFO, name).offset != start + index * ctypes.sizeof(void)
            or field._restype_ is not result
            or field._argtypes_ != arguments
        ):
            return _font_bridge_unavailable(f"FPDF_SYSFONTINFO.{name} signature/layout mismatch")
        if field._flags_ != expected._flags_:
            return _font_bridge_unavailable(f"FPDF_SYSFONTINFO.{name} calling convention mismatch")
    specs = (
        ("FPDF_GetDefaultSystemFontInfo", pointer, ()),
        ("FPDF_FreeDefaultSystemFontInfo", None, (pointer,)),
        ("FPDF_SetSystemFontInfo", None, (pointer,)),
    )
    functions = []
    for name, result, arguments in specs:
        function = getattr(raw, name, None)
        if (
            not isinstance(function, ctypes._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            return _font_bridge_unavailable(f"{name} unsupported function signature or wrapper")
        if function._flags_ not in function_flags:
            return _font_bridge_unavailable(f"{name} calling convention mismatch")
        functions.append(function)
    _NATIVE_FONT_UNAVAILABLE_REASON = None
    return native, functions, callback_type


class _NativeFontProvider:
    """保留运行时公开身份；资源校验在 Python，句柄与常见回调在 Rust。"""

    def __init__(self, raw: Any, pdfium_version: str, native: Any, functions: list[Any], callback_type: Any) -> None:
        """在原生接口取得资源前完成哈希校验并保活低频 Unicode 规范化回调。"""
        data, tables = _load_font()
        self.raw = raw
        self.pid = os.getpid()
        self.info = PdfiumRuntimeInfo(pdfium_version, _POLICY, _FONT_NAME, _FONT_SHA256)
        self.failure: tuple[str, BaseException] | None = None
        self.functions = functions
        self._legacy = callback_type(ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_int)(
            self._classify_legacy
        )
        try:
            self.native = native.NativeFontProvider(
                [ctypes.cast(function, ctypes.c_void_p).value for function in functions],
                data,
                tables,
                f"DocVortex-{_POLICY}-{_FONT_SHA256}".encode("ascii"),
                _FONT_ALIASES,
                _STYLE_SUFFIXES,
                ctypes.cast(self._legacy, ctypes.c_void_p).value,
            )
        except Exception as exc:
            raise PdfiumFontError(f"Unable to create native PDFium font runtime: {exc}") from exc

    def _classify_legacy(self, face: Any, size: int, charset: int) -> int:
        """少量非 ASCII 名称沿用精确 Python 编码语义，异常保留到退出 C 栈后处理。"""
        try:
            selected = _cjk_charset(ctypes.string_at(face, size), charset)
            return -1 if selected is None else selected
        except BaseException as exc:
            if self.failure is None:
                self.failure = ("_classify_legacy", exc)
            return -1

    def install(self) -> None:
        """全进程保留函数与低频回调，防止宿主提前回收仍被 PDFium 引用的资源。"""
        global _NATIVE_FONT_INSTALLED
        if self not in _NATIVE_KEEPALIVE:
            _NATIVE_KEEPALIVE.append(self)
        try:
            self.native.install()
        except Exception as exc:
            raise PdfiumFontError(str(exc)) from exc
        self.raise_if_failed()
        _NATIVE_FONT_INSTALLED = True

    def raise_if_failed(self) -> None:
        """将原生与低频规范化错误统一转换为现有永久故障契约。"""
        if self.failure is not None:
            name, error = self.failure
            if not isinstance(error, Exception):
                raise error
            raise PdfiumFontError(f"PDFium font callback {name} failed: {error}") from error
        try:
            self.native.raise_if_failed()
        except Exception as exc:
            raise PdfiumFontError(str(exc)) from exc

    @property
    def released(self) -> bool:
        """查询原生接口是否已由 PDFium 释放。"""
        return self.native.stats()[0]

    @property
    def bundled_requests(self) -> int:
        """返回真正发生的固定字库映射次数。"""
        return self.native.stats()[1]

    @property
    def full_font_copies(self) -> int:
        """返回完整字库复制次数，用于诊断 PDFium 缓存复用。"""
        return self.native.stats()[2]


__all__ = ["PdfiumFontError", "PdfiumRuntimeInfo"]
