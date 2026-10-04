"""Fixed CJK font resources with PDFium font provider; fonts are not read or initialized by PDFium on import."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
import json
import logging
import os
import re
import struct
from typing import Any, Literal

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
    """Fixed font resource or font provider failure not being silently downgraded as document corruption."""


@dataclass(frozen=True, slots=True)
class PdfiumRuntimeInfo:
    """Recording the PDFium and fixed font identity adopted by the current process does not mean that every document has been replaced."""

    pdfium_version: str
    font_policy: str
    font_name: str
    font_sha256: str


def _cjk_charset(face: bytes, charset: int) -> int | None:
    """Prefer CJK to be identified by character set, supplementing requests without character set only with explicit family name and encoding tags."""
    if charset in _CJK_CHARSETS:
        return charset
    if charset not in (0, 1):
        return None
    # Explicit symbols/other language character sets are not overridden by font names; no arbitrary Chinese character or substring guessing is done.
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
    """Verify the identity of the issued resource and read the SFNT index; each table retains offsets to avoid duplicating the entire font."""
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


@dataclass(frozen=True, slots=True)
class _FontHandle:
    """Distinguish between the fixed font handle managed by Python and the native handle of the default provider."""

    kind: Literal["bundled", "system"]
    value: int


class _FontProvider:
    """Wraps the native default provider, forcing CJK mapping and maintaining process-level ownership of ABI callbacks and bytes."""

    def __new__(cls, raw: Any, pdfium_version: str) -> Any:
        """The standard library ABI uses native callbacks, and the test double and Python backend retain the reference implementation."""
        bridge = _native_font_bridge(raw)
        if bridge is not None:
            native, functions, callback_type = bridge
            return _NativeFontProvider(raw, pdfium_version, native, functions, callback_type)
        return super().__new__(cls)

    def __init__(self, raw: Any, pdfium_version: str) -> None:
        """Completely verify resources before assigning default providers and callbacks to avoid semi-initialization states."""
        self.data, self.tables = _load_font()
        self.raw = raw
        self.pid = os.getpid()
        self.info = PdfiumRuntimeInfo(pdfium_version, _POLICY, _FONT_NAME, _FONT_SHA256)
        self.cache_name = f"DocVortex-{_POLICY}-{_FONT_SHA256}".encode("ascii")
        self.handles: dict[int, _FontHandle] = {}
        self.next_handle = 0
        self.enum_depth = 0
        self.released = False
        self.failure: tuple[str, BaseException] | None = None
        self.reported_delegations: set[tuple[bytes, int]] = set()
        self.request_charsets: dict[bytes, int] = {}
        self.default: Any = None
        self.bundled_requests = 0
        self.full_font_copies = 0
        types = dict(raw.FPDF_SYSFONTINFO._fields_)
        self.callbacks = (
            self._bind(types["Release"], self._release, None),
            self._bind(types["EnumFonts"], self._enum_fonts, None),
            self._bind(types["MapFont"], self._map_font, None),
            self._bind(types["GetFont"], self._get_font, None),
            self._bind(types["GetFontData"], self._get_font_data, 0),
            self._bind(types["GetFaceName"], self._get_face_name, 0),
            self._bind(types["GetFontCharset"], self._get_font_charset, 1),
            self._bind(types["DeleteFont"], self._delete_font, None),
        )
        self.interface = raw.FPDF_SYSFONTINFO(1, *self.callbacks)
        self.default = raw.FPDF_GetDefaultSystemFontInfo()
        if not self.default:
            raise PdfiumFontError("PDFium did not provide its default system font interface")

    def _bind(self, callback_type: Any, function: Callable[..., Any], failure_value: Any) -> Any:
        """Use the ABI declared by the current platform binding and leave the exception to be handled after the native call returns."""

        def invoke(*args: Any) -> Any:
            """The C callback can only return the value agreed by ABI, and the exception must not cross the native stack."""
            try:
                return function(*args)
            except BaseException as exc:
                if self.failure is None:
                    self.failure = (function.__name__, exc)
                return failure_value

        return callback_type(invoke)

    def install(self) -> None:
        """Register the interface before first font use without rebuilding PDFium or modifying system fonts."""
        try:
            self.raw.FPDF_SetSystemFontInfo(ctypes.byref(self.interface))
        except BaseException:
            self._release(None)
            raise
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        """Report a permanent failure after native calls return to avoid reusing potentially degraded font caches."""
        if self.failure is not None:
            name, error = self.failure
            if not isinstance(error, Exception):
                raise error
            raise PdfiumFontError(f"PDFium font callback {name} failed: {error}") from error
        if self.released:
            raise PdfiumFontError("The PDFium font runtime has already been released")

    def _allocate(self, kind: Literal["bundled", "system"], value: int) -> int:
        """Allocate opaque ID that is not confused with the system address, and hand it over to DeleteFont after use."""
        self.next_handle += 1
        self.handles[self.next_handle] = _FontHandle(kind, value)
        return self.next_handle

    def _release(self, _info: Any) -> None:
        """By PDFium Release the default provider when cleaning up the interface without reinstalling or destroying the entire runtime."""
        if self.released:
            return
        self.released = True
        try:
            for key in list(self.handles):
                self._delete_font(None, key)
        finally:
            if self.default:
                self.raw.FPDF_FreeDefaultSystemFontInfo(self.default)
                self.default = None

    def _enum_fonts(self, _info: Any, mapper: Any) -> None:
        """The default enumeration is responsible for initializing the Linux font; the original name table is read in the enumeration and its metadata is not replaced."""
        if not self.default or not self.default.contents.EnumFonts:
            return
        self.enum_depth += 1
        try:
            self.default.contents.EnumFonts(self.default, mapper)
        finally:
            self.enum_depth -= 1

    def _map_font(self, _info: Any, weight: int, italic: int, charset: int, pitch: int, face: Any, exact: Any) -> int | None:
        """Actual CJK font requests hit the fixed font first, and other requests maintain default provider semantics."""
        name = ctypes.string_at(face) if face else b""
        if not self.enum_depth:
            self.request_charsets[name] = charset
        selected = _cjk_charset(name, charset) if not self.enum_depth else None
        if selected is not None:
            self.bundled_requests += 1
            return self._allocate("bundled", selected)
        if not self.enum_depth and len(self.reported_delegations) < 128 and (name, charset) not in self.reported_delegations:
            self.reported_delegations.add((name, charset))
            _logger.debug("Delegating PDF font request: charset=%d face=%r", charset, name)
        if self.default and self.default.contents.MapFont:
            handle = self.default.contents.MapFont(self.default, weight, italic, charset, pitch, face, exact)
            if handle:
                return self._allocate("system", int(handle))
        return None

    def _get_font(self, _info: Any, face: Any) -> int | None:
        """Explicit font name queries use the same substitution rules, and the original system font is always read during enumeration."""
        name = ctypes.string_at(face) if face else b""
        selected = _cjk_charset(name, self.request_charsets.get(name, 1)) if not self.enum_depth else None
        if selected is not None or (not self.enum_depth and name == self.cache_name):
            self.bundled_requests += 1
            return self._allocate("bundled", selected if selected is not None else 134)
        if self.default and self.default.contents.GetFont:
            handle = self.default.contents.GetFont(self.default, face)
            if handle:
                return self._allocate("system", int(handle))
        return None

    def _get_font_data(self, _info: Any, handle: int, table: int, buffer: Any, size: int) -> int:
        """Follows interface convention for query length/copy bytes; unknown SFNT table returns zero."""
        font = self.handles[handle]
        if font.kind == "system":
            return self.default.contents.GetFontData(self.default, font.value, table, buffer, size)
        offset, length = (0, len(self.data)) if table == 0 else self.tables.get(table, (0, 0))
        if buffer and size >= length and length:
            ctypes.memmove(buffer, self.data[offset : offset + length], length)
            if table == 0:
                self.full_font_copies += 1
        return length

    def _get_face_name(self, _info: Any, handle: int, buffer: Any, size: int) -> int:
        """Reuse Droid with a hashed stable cache identity to avoid hitting different versions of fonts with the same name in the system."""
        font = self.handles[handle]
        if font.kind == "system":
            if self.default.contents.GetFaceName:
                return self.default.contents.GetFaceName(self.default, font.value, buffer, size)
            return 0
        value = self.cache_name + b"\0"
        if buffer and size >= len(value):
            ctypes.memmove(buffer, value, len(value))
        return len(value)

    def _get_font_charset(self, _info: Any, handle: int) -> int:
        """The CJK character set corresponding to the request is returned, and the system font is delegated to the original query."""
        font = self.handles[handle]
        if font.kind == "system":
            if self.default.contents.GetFontCharset:
                return self.default.contents.GetFontCharset(self.default, font.value)
            return 1
        return font.value

    def _delete_font(self, _info: Any, handle: int) -> None:
        """Only the system handle is released to the original provider, and the fixed font bytes are retained until the end of the process."""
        font = self.handles.pop(handle)
        if font.kind == "system" and self.default and self.default.contents.DeleteFont:
            self.default.contents.DeleteFont(self.default, font.value)


_NATIVE_KEEPALIVE: list[Any] = []
_NATIVE_FONT_UNAVAILABLE_REASON: str | None = "not probed"
_NATIVE_FONT_INSTALLED = False


def native_font_runtime_info() -> dict[str, Any]:
    """To distinguish between extensions being available, ABI matched and interfaces installed, the existence of extensions cannot be regarded as native callbacks being enabled."""
    return {
        "native_font_provider_installed": _NATIVE_FONT_INSTALLED,
        "native_font_provider_unavailable_reason": _NATIVE_FONT_UNAVAILABLE_REASON,
    }


def _font_bridge_unavailable(reason: str) -> None:
    """Document the exact reference path cause, specifically retaining the Windows calling convention mismatch message."""
    global _NATIVE_FONT_UNAVAILABLE_REASON
    _NATIVE_FONT_UNAVAILABLE_REASON = reason
    return None


def _native_font_bridge(raw: Any) -> Any:
    """Strictly verify the structure layout, ABI of each callback and three entries before passing the native address."""
    from ..._compute_backend import get_native

    global _NATIVE_FONT_UNAVAILABLE_REASON
    native = get_native()
    if native is None:
        return _font_bridge_unavailable("Python compute backend or native extension unavailable")
    if not hasattr(native, "NativeFontProvider"):
        return _font_bridge_unavailable("NativeFontProvider missing from extension")
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
    callback_type = ctypes.WINFUNCTYPE if os.name == "nt" else ctypes.CFUNCTYPE
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
        if function._flags_ != callback_type(result, *arguments)._flags_:
            return _font_bridge_unavailable(f"{name} calling convention mismatch")
        functions.append(function)
    _NATIVE_FONT_UNAVAILABLE_REASON = None
    return native, functions, callback_type


class _NativeFontProvider:
    """Preserve runtime public identity; resource verification is in Python, handles and common callbacks are in Rust."""

    def __init__(self, raw: Any, pdfium_version: str, native: Any, functions: list[Any], callback_type: Any) -> None:
        """Complete hash verification and keep alive the low-frequency Unicode standardized callback before obtaining resources through the native interface."""
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
        """A small number of non-ASCII names follow the exact Python encoding semantics, and exceptions are retained until exiting the C stack post-processing."""
        try:
            selected = _cjk_charset(ctypes.string_at(face, size), charset)
            return -1 if selected is None else selected
        except BaseException as exc:
            if self.failure is None:
                self.failure = ("_classify_legacy", exc)
            return -1

    def install(self) -> None:
        """The whole process retains functions and low-frequency callbacks to prevent the host from early recycling of resources that are still referenced by PDFium."""
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
        """Unified conversion of native and low-frequency normalized errors into existing permanent fault contracts."""
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
        """Query whether the native interface has been released by PDFium."""
        return self.native.stats()[0]

    @property
    def bundled_requests(self) -> int:
        """Returns the actual number of fixed font mappings that occurred."""
        return self.native.stats()[1]

    @property
    def full_font_copies(self) -> int:
        """Returns the number of complete font copy times, used to diagnose PDFium cache reuse."""
        return self.native.stats()[2]


__all__ = ["PdfiumFontError", "PdfiumRuntimeInfo"]
