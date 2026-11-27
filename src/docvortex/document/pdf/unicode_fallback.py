"""按实际 PDF 字体资源与原字符码恢复未映射字形，保留所有有效 Unicode 映射。"""

from __future__ import annotations

import logging
import re
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from copy import copy
from io import BytesIO
from typing import Any
from collections.abc import Iterator

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from .pdfium import pdfium_guard

logger = logging.getLogger(__name__)

# CMap 只读词法：注释、名称和字符串均作为整体跳过，绝不执行 PostScript。
_TOKENS = re.compile(
    rb"%[^\r\n]*|/[^\s<>{}\[\]()/%]+|\((?:\\[\s\S]|[^\\()])*\)|<<|>>|<[0-9A-Fa-f\s]*>|[^\s<>{}\[\]()/%]+|[^\s]"
)
_SECTIONS = {b"begincodespacerange": b"endcodespacerange", b"beginbfchar": b"endbfchar", b"beginbfrange": b"endbfrange"}
_MAX_CMAP_BYTES = 1024 * 1024
_MAX_TYPE1_BYTES = 2 * 1024 * 1024
# 这些 PostScript 字形名不在 AGL，单一字形的含义不依赖字体族。
_UNAMBIGUOUS_GLYPH_ALIASES = {"prime": "′", "parenleftbig": "(", "parenrightbig": ")"}
_ResourcePath = tuple[tuple[str, str], ...]


@dataclass(slots=True)
class _FontRepair:
    """保存实际字体对象的已知映射，以及可验证的原字符码和单字形语义。"""

    mapping: dict[int, bytes]
    codespaces: list[tuple[int, int]]
    slots: dict[int, tuple[int, int]]


def _hex(token: bytes) -> bytes:
    """严格读取偶数位十六进制，不猜测损坏的 CMap 字节。"""
    if not token.startswith(b"<") or not token.endswith(b">") or token.startswith(b"<<"):
        raise ValueError("invalid CMap hex string")
    return bytes.fromhex(token[1:-1].decode("ascii"))


def _simple_cmap(data: bytes) -> tuple[dict[int, bytes], list[tuple[int, int]]]:
    """读取有界单字节 bfchar/bfrange；继承、冲突或异常语法保持原样。"""
    if len(data) > _MAX_CMAP_BYTES:
        raise ValueError("simple-font CMap exceeds bound")
    tokens = [token for token in _TOKENS.findall(data) if not token.startswith(b"%")]
    if tokens.count(b"begincmap") != 1 or tokens.count(b"endcmap") != 1:
        raise ValueError("invalid CMap boundary")
    if any(token in {b"usecmap", b"begincidchar", b"begincidrange", b"(", b")"} for token in tokens):
        raise ValueError("unsupported inherited or malformed CMap")
    mapping: dict[int, bytes] = {}
    codespaces: list[tuple[int, int]] = []
    index = tokens.index(b"begincmap") + 1
    end = tokens.index(b"endcmap")
    while index < end:
        operator = tokens[index]
        if operator not in _SECTIONS:
            index += 1
            continue
        count = int(tokens[index - 1])
        if not 0 <= count <= 256:
            raise ValueError("invalid simple-font CMap count")
        index += 1
        for _ in range(count):
            first, second = _hex(tokens[index]), _hex(tokens[index + 1])
            index += 2
            if len(first) != 1:
                raise ValueError("not a single-byte font CMap")
            start = first[0]
            if operator == b"beginbfchar":
                pairs = [(start, second)]
            else:
                if len(second) != 1 or second[0] < start:
                    raise ValueError("invalid single-byte CMap range")
                stop = second[0]
                if operator == b"begincodespacerange":
                    codespaces.append((start, stop))
                    continue
                destination = tokens[index]
                index += 1
                if destination == b"[":
                    pairs = [(code, _hex(tokens[index + code - start])) for code in range(start, stop + 1)]
                    index += stop - start + 1
                    if tokens[index] != b"]":
                        raise ValueError("invalid CMap destination array")
                    index += 1
                else:
                    base = _hex(destination)
                    value = int.from_bytes(base, "big")
                    pairs = [(code, (value + code - start).to_bytes(len(base), "big")) for code in range(start, stop + 1)]
            for code, destination in pairs:
                if code in mapping or not destination or len(destination) % 2 or len(destination) > 512:
                    raise ValueError("ambiguous or malformed CMap destination")
                mapping[code] = destination
        if tokens[index] != _SECTIONS[operator]:
            raise ValueError("invalid CMap section boundary")
        index += 1
    if not codespaces or any(not any(lo <= code <= hi for lo, hi in codespaces) for code in mapping):
        raise ValueError("CMap code outside declared codespace")
    return mapping, codespaces


def _font_differences(font: Any) -> dict[int, str]:
    """读取一个字体资源的显式单字节字模名表，遇到重码直接拒绝。"""
    encoding = font.get("/Encoding")
    encoding = encoding.get_object() if encoding is not None else None
    if not isinstance(encoding, dict) or not encoding.get("/Differences"):
        return {}
    code = -1
    names = {}
    for value in encoding["/Differences"]:
        if isinstance(value, int):
            code = int(value)
            continue
        if not 0 <= code <= 255 or code in names or not str(value).startswith("/"):
            raise ValueError("invalid font Differences")
        names[code] = str(value)[1:]
        code += 1
    return names


def _utf16_cmap(mapping: dict[int, bytes], codespaces: list[tuple[int, int]]) -> bytes:
    """编码临时单字节 ToUnicode 流，支持保留原有映射。"""
    lines = [
        b"/CIDInit /ProcSet findresource begin",
        b"12 dict begin",
        b"begincmap",
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        b"/CMapName /DocVortexGlyphFallback def",
        b"/CMapType 2 def",
        f"{len(codespaces)} begincodespacerange".encode(),
    ]
    lines.extend(f"<{lo:02x}><{hi:02x}>".encode() for lo, hi in codespaces)
    lines.append(b"endcodespacerange")
    entries = list(sorted(mapping.items()))
    for offset in range(0, len(entries), 100):
        batch = entries[offset : offset + 100]
        lines.append(f"{len(batch)} beginbfchar".encode())
        lines.extend(f"<{code:02x}><{text.hex()}>".encode() for code, text in batch)
        lines.append(b"endbfchar")
    lines.extend([b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"])
    return b"\n".join(lines)


def _type1_glyph_names(descriptor: Any) -> set[str]:
    """读取嵌入 Type1 或非 CID Type1C 程序，名称必须真实存在于字体。"""
    if stream := descriptor.get("/FontFile"):
        from fontTools.t1Lib import T1Font

        stream = stream.get_object()
        lengths = [int(stream.get(f"/Length{i}", 0)) for i in (1, 2, 3)]
        if not lengths[0] or not lengths[1] or min(lengths) < 0 or sum(lengths) > _MAX_TYPE1_BYTES:
            return set()
        data = stream.get_data()
        if len(data) > _MAX_TYPE1_BYTES:
            return set()
        embedded = object.__new__(T1Font)
        embedded.data = data
        embedded.encoding = "ascii"
        embedded.parse()
        return set(embedded["CharStrings"])

    stream = descriptor.get("/FontFile3")
    stream = stream.get_object() if stream is not None else None
    if stream is None or stream.get("/Subtype") != "/Type1C":
        return set()
    from fontTools.cffLib import CFFFontSet

    data = stream.get_data()
    if len(data) > _MAX_TYPE1_BYTES:
        return set()
    embedded = CFFFontSet()
    embedded.decompile(BytesIO(data), None)
    if len(embedded.fontNames) != 1:
        return set()
    top = embedded[embedded.fontNames[0]]
    return set(top.charset) if getattr(top, "ROS", None) is None else set()


def _embedded_glyph_names(font: Any, names: dict[int, str]) -> dict[int, tuple[int, int]]:
    """无 ToUnicode 时按标准 AGL 单字符语义及真实字模验证显式 Differences。"""
    descriptor = font.get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else {}
    if font.get("/Subtype") != "/Type1" or not int(descriptor.get("/Flags", 0)) & 4:
        return {}
    from fontTools.agl import toUnicode

    proposed = {}
    for code, glyph in names.items():
        # AGL 会静默跳过复合名称里的未知成分；这里要求能解释整个单字形名。
        if "_" in glyph.split(".", 1)[0]:
            continue
        value = _UNAMBIGUOUS_GLYPH_ALIASES.get(glyph) or toUnicode(glyph)
        if (
            len(value) == 1
            and len(unicodedata.normalize("NFKC", value)) == 1
            and value != "\ufffd"
            and not unicodedata.category(value).startswith("C")
        ):
            # 等值槽不参与探测，真实的 /zero 仍为 U+0030。
            if code != ord(value):
                proposed[code] = (code, ord(value))
    if not proposed:
        return {}
    available = _type1_glyph_names(descriptor)
    return {code: pair for code, pair in proposed.items() if names[code] in available}


def _repaired_cmap(font: Any) -> _FontRepair | None:
    """验证实际字体资源的恢复计划，任意有效 ToUnicode 映射均保持原值。"""
    if font.get("/Subtype") not in {"/Type1", "/TrueType"} or not (names := _font_differences(font)):
        return None
    if "/ToUnicode" not in font:
        slots = _embedded_glyph_names(font, names)
        return _FontRepair({}, [(0, 255)], slots) if slots else None

    from fontTools.agl import toUnicode

    data = font["/ToUnicode"].get_data()
    mapping, codespaces = _simple_cmap(data)
    slots = {}
    for code, glyph in names.items():
        if mapping.get(code) == b"\0\0":
            text = toUnicode(glyph)
            # 一对一恢复保证来源索引不变；未知名称、多字符 ligature 和控制码不作猜测。
            if len(text) == 1 and text != "\ufffd" and not unicodedata.category(text).startswith("C"):
                slots[code] = (0, ord(text))
    return _FontRepair(mapping, codespaces, slots) if slots else None


def _page_fonts(page: Any) -> Iterator[tuple[_ResourcePath, Any]]:
    """按页及嵌套 Form 的真实资源路径枚举字体，不把同名字体当同一个对象。"""
    visited: set[int] = set()

    def visit(resources: Any, path: _ResourcePath) -> Iterator[tuple[_ResourcePath, Any]]:
        """限制资源图的遍历深度，避免循环 Form。"""
        if resources is None or len(path) >= 32:
            return
        resources = resources.get_object()
        if not isinstance(resources, dict) or id(resources) in visited:
            return
        visited.add(id(resources))
        fonts = resources.get("/Font")
        if fonts is not None and isinstance(fonts.get_object(), dict):
            for name, ref in fonts.get_object().items():
                yield (*path, ("/Font", name)), ref
        objects = resources.get("/XObject")
        if objects is not None and isinstance(objects.get_object(), dict):
            for name, ref in objects.get_object().items():
                obj = ref.get_object()
                if isinstance(obj, dict) and obj.get("/Subtype") == "/Form":
                    yield from visit(obj.get("/Resources"), (*path, ("/XObject", name)))

    yield from visit(page.get("/Resources"), ())


def _known_page_repairs(page: Any, cache: dict[tuple[int, int], _FontRepair | None]) -> dict[_ResourcePath, _FontRepair]:
    """按实际间接对象缓存字体判断，避免给没有修复证据的页面克隆资源。"""
    available = {}
    for path, ref in _page_fonts(page):
        key = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else None
        if key is not None and key in cache:
            repaired = cache[key]
        else:
            try:
                font = ref.get_object()
                repaired = _repaired_cmap(font) if isinstance(font, dict) else None
            except Exception as exc:
                logger.debug("Skip unsupported PDF font CMap: %s", exc)
                repaired = None
            if key is not None:
                cache[key] = repaired
        if repaired is not None:
            available[path] = repaired
    return available


def _matching_char(textpage: pdfium.PdfTextPage, other: pdfium.PdfTextPage, index: int) -> bool:
    """确认恢复视图中的同一索引仍指向相同字形位置，拒绝重排或生成字符变化。"""
    return (
        textpage.get_charbox(index) == other.get_charbox(index)
        and textpage.get_charbox(index, loose=True) == other.get_charbox(index, loose=True)
        and raw.FPDFText_GetCharAngle(textpage.raw, index) == raw.FPDFText_GetCharAngle(other.raw, index)
        and raw.FPDFText_IsGenerated(textpage.raw, index) == raw.FPDFText_IsGenerated(other.raw, index) == 0
    )


def recover_page_unicode(
    textpage: pdfium.PdfTextPage,
    indices: list[int],
    reader_page: Any,
    font_cache: dict[tuple[int, int], _FontRepair | None],
) -> dict[int, int]:
    """只返回同一字符索引和字形位置的资源修复，原 PDF 与原字形保持不变。"""
    if reader_page is None or not indices:
        return {}
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    try:
        repairs = _known_page_repairs(reader_page, font_cache)
        if not repairs:
            return {}
        with pdfium_guard(), PdfWriter() as writer:
            # 仅克隆目标页；避免批注复制其它页，保持 reader 和用于正确渲染的文档不变。
            source_page = copy(reader_page)
            source_page.pop("/Annots", None)
            source_page.pop("/B", None)
            page = writer.add_page(source_page)
            markers = {}
            assigned = set()
            for path, font_ref in _page_fonts(page):
                if path not in repairs:
                    continue
                font = font_ref.get_object()
                if id(font) in assigned:
                    continue
                repair = repairs[path]
                mapping = repair.mapping.copy()
                for code, repair_slot in repair.slots.items():
                    # 临时编码只用来核实当前字形命中的资源和原码，标记永不进入正式输出。
                    marker = 0xE000 + len(markers)
                    if marker > 0xF8FF:
                        return {}
                    markers[marker] = repair_slot
                    mapping[code] = marker.to_bytes(2, "big")
                stream = DecodedStreamObject()
                stream.set_data(_utf16_cmap(mapping, repair.codespaces))
                font[NameObject("/ToUnicode")] = writer._add_object(stream)
                assigned.add(id(font))
            if not markers:
                return {}
            output = BytesIO()
            writer.write(output)
            with pdfium.PdfDocument(output.getvalue()) as document, closing(document[0]) as page:
                with closing(page.get_textpage()) as other:
                    count = textpage.count_chars()
                    if other.count_chars() != count:
                        return {}
                    original = [raw.FPDFText_GetUnicode(textpage.raw, i) for i in range(count)]
                    candidate = [raw.FPDFText_GetUnicode(other.raw, i) for i in range(count)]
                    changed = [i for i, (before, after) in enumerate(zip(original, candidate, strict=True)) if before != after]
                    # 若有与临时码无关的任何变化，无法确认原生索引仍正确，整页回退。
                    if any(candidate[i] not in markers for i in changed):
                        return {}
                    selected = set(indices).intersection(changed)
                    replacements = {}
                    for i in selected:
                        expected_byte, value = markers[candidate[i]]
                        # 缺失 CMap 也可能已被 PDFium 正确解码，合法的旧值优先。
                        if original[i] in (0, expected_byte) and original[i] != value and _matching_char(textpage, other, i):
                            replacements[i] = value
                    return replacements
    except Exception as exc:
        logger.debug("Skip PDF glyph recovery: %s", exc)
        return {}
