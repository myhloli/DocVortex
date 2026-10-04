"""Construct Equation Editor 3.x MTEF/OLE/DOC test objects with known results."""

from __future__ import annotations

import base64
import struct
from typing import cast
import uuid

_TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def mtef_char(value: str, *, embellishments: tuple[int, ...] = ()) -> bytes:
    """Constructs a list of Unicode CHAR and optionally embellishment."""

    if len(value) != 1 or ord(value) > 0xFFFF:
        raise ValueError("MTEF v3 CHAR fixture requires one BMP character")
    tag = 0x22 if embellishments else 0x02
    payload = bytes([tag, 131]) + struct.pack("<H", ord(value))
    if embellishments:
        payload += b"".join(bytes([0x06, embellishment]) for embellishment in embellishments)
        payload += b"\x00"
    return payload


def mtef_text(value: str) -> bytes:
    """Construct short text into contiguous CHAR records."""

    return b"".join(mtef_char(character) for character in value)


def mtef_line(*records: bytes) -> bytes:
    """Constructs a non-empty LINE slot ending in END."""

    return b"\x01" + b"".join(records) + b"\x00"


def mtef_null_line() -> bytes:
    """Construct NULL LINE slot without END."""

    return b"\x11"


def mtef_template(
    selector: int,
    *slots: bytes,
    variation: int = 0,
    options: int = 0,
) -> bytes:
    """Construct a TMPL and its LINE slots."""

    return bytes([0x03, selector, variation, options]) + b"".join(slots) + b"\x00"


def mtef_matrix(rows: list[list[bytes]]) -> bytes:
    """Construct MATRIX record without dividers."""

    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("matrix fixture must be rectangular")
    row_count = len(rows)
    col_count = len(rows[0])
    row_parts = b"\x00" * ((2 * (row_count + 1) + 7) // 8)
    col_parts = b"\x00" * ((2 * (col_count + 1) + 7) // 8)
    cells = b"".join(mtef_line(cell) for row in rows for cell in row)
    return bytes([0x05, 0, 0, 0, row_count, col_count]) + row_parts + col_parts + cells + b"\x00"


def mtef_equation(*records: bytes) -> bytes:
    """Constructed complete MTEF v3 header, FULL size and root LINE."""

    header = bytes([3, 1, 1, 3, 0])
    return header + b"\x0a" + mtef_line(*records) + b"\x00"


def equation_native(mtef: bytes) -> bytes:
    """Added 28 bytes EQNOLEFILEHDR for MTEF."""

    return (
        struct.pack(
            "<HIHI4I",
            28,
            0x0002_0000,
            0xC1C2,
            len(mtef),
            0,
            0,
            0,
            0,
        )
        + mtef
    )


def formula_corpus() -> list[tuple[str, bytes, str]]:
    """Returns names covering the common Equation, Editor structures, MTEF and the expected LaTeX."""

    fraction = mtef_template(
        14,
        mtef_line(mtef_text("a+b")),
        mtef_line(mtef_char("c")),
    )
    superscript = mtef_template(
        15,
        mtef_null_line(),
        mtef_line(mtef_char("2")),
        variation=0,
    )
    square_root = mtef_template(
        13,
        mtef_line(mtef_char("x"), superscript, mtef_text("+1")),
        variation=0,
    )
    nth_root = mtef_template(
        13,
        mtef_line(mtef_char("x")),
        mtef_line(mtef_char("3")),
        variation=1,
    )
    sub_sup = mtef_char("x") + mtef_template(
        15,
        mtef_line(mtef_char("i")),
        mtef_line(mtef_char("2")),
        variation=2,
    )
    fenced_fraction = mtef_template(1, mtef_line(fraction))
    summation = mtef_template(
        29,
        mtef_line(mtef_char("i")),
        mtef_line(mtef_char("n")),
        mtef_line(mtef_text("i=1")),
        mtef_line(mtef_char("∑")),
        variation=1,
    )
    integral = mtef_template(
        21,
        mtef_line(mtef_char("x")),
        mtef_line(mtef_char("1")),
        mtef_line(mtef_char("0")),
        mtef_line(mtef_char("∫")),
        variation=2,
    )
    matrix = mtef_matrix(
        [
            [mtef_char("a"), mtef_char("b")],
            [mtef_char("c"), mtef_char("d")],
        ]
    )
    return [
        ("linear", mtef_equation(mtef_text("x+y")), "x+y"),
        ("fraction", mtef_equation(fraction), r"\frac{a+b}{c}"),
        ("square_root", mtef_equation(square_root), r"\sqrt{x^{2}+1}"),
        ("nth_root", mtef_equation(nth_root), r"\sqrt[3]{x}"),
        ("sub_sup", mtef_equation(sub_sup), r"x_{i}^{2}"),
        ("fence", mtef_equation(fenced_fraction), r"\left(\frac{a+b}{c}\right)"),
        ("summation", mtef_equation(summation), r"\sum_{i=1}^{n}{i}"),
        ("integral", mtef_equation(integral), r"\int_{0}^{1}{x}"),
        ("matrix", mtef_equation(matrix), r"\begin{matrix}a&b\\c&d\end{matrix}"),
        ("embellishment", mtef_equation(mtef_char("x", embellishments=(9,))), r"\widehat{x}"),
        ("greek_relation", mtef_equation(mtef_char("α"), mtef_char("≤"), mtef_char("β")), r"\alpha \leq \beta"),
    ]


def _directory_entry(
    name: str,
    kind: int,
    *,
    left: int,
    right: int,
    child: int,
    start: int,
    size: int,
    clsid: bytes = b"\x00" * 16,
) -> bytes:
    """Constructs a CFB directory entry that supports the storage hierarchy."""

    raw_name = name.encode("utf-16le") + b"\x00\x00"
    if len(raw_name) > 64:
        raise ValueError("CFB fixture directory name is too long")
    return (
        raw_name
        + b"\x00" * (64 - len(raw_name))
        + struct.pack("<HBBIII", len(raw_name), kind, 1, left, right, child)
        + clsid
        + b"\x00" * 4
        + b"\x00" * 16
        + struct.pack("<IQ", start, size)
    )


def _build_nested_cfb(entries: list[dict[str, object]]) -> bytes:
    """Generate all stream hierarchies using the regular FAT sector CFB v3."""

    sector_size = 512
    end_of_chain = 0xFFFF_FFFE
    fat_sector = 0xFFFF_FFFD
    free_sector = 0xFFFF_FFFF
    fat: list[int] = []
    sectors: list[bytes] = []

    def add_chain(data: bytes) -> int:
        """Append FAT chain and return the first sector."""

        if not data:
            return end_of_chain
        padded = data + b"\x00" * (-len(data) % sector_size)
        first = len(sectors)
        count = len(padded) // sector_size
        for index in range(count):
            sectors.append(padded[index * sector_size : (index + 1) * sector_size])
            fat.append(first + index + 1 if index + 1 < count else end_of_chain)
        return first

    stream_locations: dict[int, tuple[int, int]] = {}
    for index, entry in enumerate(entries):
        if entry["kind"] != 2:
            continue
        payload = bytes(cast(bytes, entry.get("data", b"")))
        padded = payload + b"\x00" * (4096 - len(payload)) if 0 < len(payload) < 4096 else payload
        stream_locations[index] = (add_chain(padded), len(padded))

    directory_parts: list[bytes] = []
    for index, entry in enumerate(entries):
        start, size = stream_locations.get(index, (end_of_chain, 0))
        directory_parts.append(
            _directory_entry(
                str(entry["name"]),
                cast(int, entry["kind"]),
                left=cast(int, entry.get("left", free_sector)),
                right=cast(int, entry.get("right", free_sector)),
                child=cast(int, entry.get("child", free_sector)),
                start=start,
                size=size,
                clsid=bytes(cast(bytes, entry.get("clsid", b"\x00" * 16))),
            )
        )
    directory = b"".join(directory_parts)
    directory += b"\x00" * (-len(directory) % sector_size)
    directory_start = add_chain(directory)

    fat_count = 1
    while (len(fat) + fat_count + 127) // 128 > fat_count:
        fat_count += 1
    fat_start = len(sectors)
    fat_ids = list(range(fat_start, fat_start + fat_count))
    full_fat = fat + [fat_sector] * fat_count
    full_fat += [free_sector] * (fat_count * 128 - len(full_fat))
    for index in range(fat_count):
        sectors.append(struct.pack("<128I", *full_fat[index * 128 : (index + 1) * 128]))
    difat = fat_ids + [free_sector] * (109 - len(fat_ids))
    header = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + b"\x00" * 16
        + struct.pack(
            "<HHHHHHIIIIIIIII",
            0x003E,
            0x0003,
            0xFFFE,
            9,
            6,
            0,
            0,
            0,
            fat_count,
            directory_start,
            0,
            4096,
            end_of_chain,
            0,
            end_of_chain,
        )
        + struct.pack("<I", 0)
        + struct.pack("<109I", *difat)
    )
    return header + b"".join(sectors)


def build_equation_object(
    mtef: bytes,
    *,
    prog_id: str = "Equation.3",
) -> bytes:
    """Constructs a stand-alone formula OLE object containing Equation Native and the specified ProgID."""

    none = 0xFFFF_FFFF
    equation_clsid = uuid.UUID("0002CE02-0000-0000-C000-000000000046").bytes_le
    entries = [
        {"name": "Root Entry", "kind": 5, "child": 1, "clsid": equation_clsid},
        {
            "name": "\x01CompObj",
            "kind": 2,
            "right": 2,
            "data": prog_id.encode("ascii") + b"\x00",
        },
        {"name": "\x03ObjInfo", "kind": 2, "right": 3, "data": b"\x00" * 6},
        {"name": "Equation Native", "kind": 2, "right": none, "data": equation_native(mtef)},
    ]
    return _build_nested_cfb(entries)


def _piece_table(text_fc: int, cp_count: int) -> bytes:
    """Construct CLX of single Unicode piece."""

    plc = struct.pack("<II", 0, cp_count)
    plc += b"\x00\x00" + struct.pack("<I", text_fc) + b"\x00\x00"
    return b"\x02" + struct.pack("<I", len(plc)) + plc


def _chpx_fkp(
    text_fc: int,
    cp_count: int,
    anchors: list[tuple[int, int, bool]],
) -> tuple[bytes, int, int]:
    """Constructs ChpxFkp carrying OLE storage id only on the field delimiter."""

    end_fc = text_fc + cp_count * 2
    boundaries = [text_fc]
    location_by_start: dict[int, tuple[int, bool]] = {}
    for cp, location, is_ole in anchors:
        start = text_fc + cp * 2
        boundaries.extend([start, start + 2])
        location_by_start[start] = (location, is_ole)
    boundaries.append(end_fc)
    boundaries = sorted(set(boundaries))
    run_count = len(boundaries) - 1
    page = bytearray(512)
    struct.pack_into(f"<{len(boundaries)}I", page, 0, *boundaries)
    offsets_start = len(boundaries) * 4
    payload_cursor = offsets_start + run_count
    if payload_cursor % 2:
        payload_cursor += 1
    for index, start in enumerate(boundaries[:-1]):
        location_info = location_by_start.get(start)
        if location_info is None:
            continue
        location, is_ole = location_info
        grpprl = struct.pack("<HI", 0x6A03, location)
        if is_ole:
            grpprl += struct.pack("<HB", 0x080A, 1) + struct.pack("<HB", 0x0856, 1)
        page[offsets_start + index] = payload_cursor // 2
        page[payload_cursor] = len(grpprl)
        page[payload_cursor + 1 : payload_cursor + 1 + len(grpprl)] = grpprl
        payload_cursor += 1 + len(grpprl)
        if payload_cursor % 2:
            payload_cursor += 1
    page[511] = run_count
    page_number = 4
    bte = struct.pack("<III", text_fc, end_fc, page_number)
    return bytes(page), page_number, len(bte)


def _png_picf(png: bytes) -> bytes:
    """Packaging PNG into the smallest PICFAndOfficeArtData."""

    blip_body = b"\x00" * 16 + b"\x00" + png
    blip = struct.pack("<HHI", 0, 0xF01E, len(blip_body)) + blip_body
    header_size = 68
    header = bytearray(header_size)
    struct.pack_into("<IH", header, 0, header_size + len(blip), header_size)
    return bytes(header) + blip


def _raw_picf(payload: bytes) -> bytes:
    """Put the original picture recognized by magic fallback into the smallest PICF."""

    header_size = 68
    header = bytearray(header_size)
    struct.pack_into(
        "<IH",
        header,
        0,
        header_size + len(payload),
        header_size,
    )
    return bytes(header) + payload


def build_equation_doc(
    formulas: list[tuple[int, bytes]],
    *,
    preview_storage_ids: set[int] | None = None,
    preview_payloads: dict[int, bytes] | None = None,
    prog_id: str = "Equation.3",
) -> bytes:
    """Construct a minimal DOC integration fixture for multiple ObjectPool formula fields."""

    text_parts: list[str] = []
    anchors: list[tuple[int, int, bool]] = []
    data_stream = bytearray()
    previews = preview_storage_ids or set()
    custom_previews = preview_payloads or {}
    cp_cursor = 0
    for storage_id, _mtef in formulas:
        prefix = f"\x13 EMBED {prog_id} "
        field = prefix + "\x14\x01\x15\r"
        separator_cp = cp_cursor + len(prefix)
        anchors.append((separator_cp, storage_id, True))
        if storage_id in previews:
            preview_offset = len(data_stream)
            preview_payload = custom_previews.get(storage_id)
            data_stream.extend(_raw_picf(preview_payload) if preview_payload is not None else _png_picf(_TINY_PNG))
            anchors.append((separator_cp + 1, preview_offset, False))
        text_parts.append(field)
        cp_cursor += len(field)
    text = "".join(text_parts)
    encoded = text.encode("utf-16le")
    cp_count = len(encoded) // 2
    text_fc = 1024
    clx = _piece_table(text_fc, cp_count)
    section = struct.pack("<II", 0, cp_count) + b"\x00" * 12
    fkp, page_number, bte_size = _chpx_fkp(text_fc, cp_count, anchors)
    bte = struct.pack("<III", text_fc, text_fc + cp_count * 2, page_number)
    table = clx + section + bte

    pair_count = 75
    pairs = [(0, 0)] * pair_count
    pairs[6] = (len(clx), len(section))
    pairs[12] = (len(clx) + len(section), bte_size)
    pairs[33] = (0, len(clx))
    base = bytearray(32)
    struct.pack_into("<HH", base, 0, 0xA5EC, 0x00C1)
    struct.pack_into("<H", base, 6, 0x0409)
    struct.pack_into("<H", base, 10, 0x020C)
    struct.pack_into("<II", base, 24, text_fc, text_fc + len(encoded))
    rgw = [0] * 14
    rglw = [0] * 11
    rglw[0] = max(text_fc + len(encoded), (page_number + 1) * 512)
    rglw[3] = cp_count
    fib = bytes(base)
    fib += struct.pack("<H", len(rgw)) + struct.pack(f"<{len(rgw)}H", *rgw)
    fib += struct.pack("<H", len(rglw)) + struct.pack(f"<{len(rglw)}I", *rglw)
    fib += struct.pack("<H", pair_count)
    fib += b"".join(struct.pack("<II", offset, size) for offset, size in pairs)
    fib += struct.pack("<H", 0)
    word_document = bytearray(max((page_number + 1) * 512, text_fc + len(encoded)))
    word_document[: len(fib)] = fib
    word_document[text_fc : text_fc + len(encoded)] = encoded
    word_document[page_number * 512 : (page_number + 1) * 512] = fkp

    none = 0xFFFF_FFFF
    equation_clsid = uuid.UUID("0002CE02-0000-0000-C000-000000000046").bytes_le
    entries: list[dict[str, object]] = [
        {"name": "Root Entry", "kind": 5, "child": 1},
        {"name": "ObjectPool", "kind": 1},
    ]
    storage_indexes: list[int] = []
    for storage_id, mtef in formulas:
        storage_index = len(entries)
        storage_indexes.append(storage_index)
        entries.append(
            {
                "name": f"_{storage_id}",
                "kind": 1,
                "child": storage_index + 1,
                "clsid": equation_clsid,
            }
        )
        entries.extend(
            [
                {
                    "name": "Equation Native",
                    "kind": 2,
                    "right": storage_index + 2,
                    "data": equation_native(mtef),
                },
                {
                    "name": "\x01CompObj",
                    "kind": 2,
                    "right": storage_index + 3,
                    "data": prog_id.encode("ascii") + b"\x00",
                },
                {
                    "name": "\x03ObjInfo",
                    "kind": 2,
                    "right": none,
                    "data": b"\x00" * 6,
                },
            ]
        )
    for current, following in zip(storage_indexes, storage_indexes[1:]):
        entries[current]["right"] = following
    entries[1]["child"] = storage_indexes[0]
    table_index = len(entries)
    entries[1]["right"] = table_index
    entries.extend(
        [
            {"name": "1Table", "kind": 2, "right": table_index + 1, "data": table},
            {"name": "Data", "kind": 2, "right": table_index + 2, "data": bytes(data_stream)},
            {"name": "WordDocument", "kind": 2, "right": none, "data": bytes(word_document)},
        ]
    )
    return _build_nested_cfb(entries)
