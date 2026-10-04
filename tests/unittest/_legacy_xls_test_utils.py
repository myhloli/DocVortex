"""Construct a minimal BIFF8 test file that does not depend on Excel/LibreOffice."""

from __future__ import annotations

from dataclasses import dataclass
import struct
import uuid
import zlib

from _mtef_test_utils import _TINY_PNG, _build_nested_cfb, equation_native
from _legacy_ppt_test_utils import _build_cfb


@dataclass(frozen=True, slots=True)
class SheetFixture:
    """The name, record, and visible status of a test worksheet."""

    name: str
    records: bytes = b""
    visible: bool = True


def biff_record(record_type: int, payload: bytes = b"") -> bytes:
    """Construct a BIFF record."""

    return struct.pack("<HH", record_type, len(payload)) + payload


def _officeart_record(
    record_type: int,
    payload: bytes,
    *,
    version: int = 0,
    instance: int = 0,
) -> bytes:
    """Construct a OfficeArt record."""

    return (
        struct.pack(
            "<HHI",
            (instance << 4) | version,
            record_type,
            len(payload),
        )
        + payload
    )


def _equation_shape(row: int, col: int, object_id: int, *, preview: bool) -> bytes:
    """Construct Excel shape with cell anchor, optional pib and ExObj in corresponding order."""

    fsp = _officeart_record(
        0xF00A,
        struct.pack("<II", object_id + 1024, 0),
        version=2,
        instance=1,
    )
    fopt = (
        _officeart_record(
            0xF00B,
            struct.pack("<HI", 0x0104, 1),
            version=3,
            instance=1,
        )
        if preview
        else b""
    )
    anchor = _officeart_record(
        0xF010,
        struct.pack(
            "<9H",
            0,
            col,
            0,
            row,
            0,
            min(col + 2, 255),
            0,
            min(row + 3, 65_535),
            0,
        ),
    )
    return _officeart_record(
        0xF004,
        fsp + fopt + anchor,
        version=0xF,
    )


def _equation_obj(location: int, object_id: int) -> bytes:
    """Construct picture OBJ with FtPictFmla pointing to MBD storage."""

    cmo = struct.pack("<HHH", 0x0008, object_id, 0) + b"\x00" * 12
    pict_flags = struct.pack("<H", 0)
    parsed_formula = struct.pack("<H", 5) + b"\x00" * 4 + b"\x02" + b"\x00" * 4
    embed_info = b"\x03\x00\x00"
    obj_formula = parsed_formula + embed_info
    pict_formula = struct.pack("<H", len(obj_formula)) + obj_formula + struct.pack("<I", location)
    payload = b"".join(
        (
            struct.pack("<HH", 0x0015, len(cmo)) + cmo,
            struct.pack("<HH", 0x0008, len(pict_flags)) + pict_flags,
            struct.pack("<HH", 0x0009, len(pict_formula)) + pict_formula,
            struct.pack("<HH", 0, 0),
        )
    )
    return biff_record(0x005D, payload)


def _preview_bstore(preview_payload: bytes | None = None) -> bytes:
    """Constructs a PNG/WMF BStore that can be read by the XLS/PPT shared decoder."""

    if preview_payload is None:
        blip = _officeart_record(0xF01E, b"\x00" * 17 + _TINY_PNG)
    else:
        compressed = zlib.compress(preview_payload)
        metafile_header = bytearray(34)
        struct.pack_into("<I", metafile_header, 0, len(preview_payload))
        struct.pack_into("<I", metafile_header, 28, len(compressed))
        metafile_header[32] = 0
        blip = _officeart_record(
            0xF01B,
            b"\x00" * 16 + bytes(metafile_header) + compressed,
        )
    bse = _officeart_record(0xF007, b"\x00" * 36 + blip, version=2)
    return _officeart_record(0xF001, bse, version=0xF, instance=1)


def _build_xls_with_embeddings(
    workbook: bytes,
    equations: dict[int, bytes],
    *,
    prog_id: str,
) -> bytes:
    """Write Workbook with multiple MBD Equation Native storage to the same CFB."""

    none = 0xFFFF_FFFF
    entries: list[dict[str, object]] = [
        {"name": "Root Entry", "kind": 5},
    ]
    storage_indexes: list[int] = []
    for location, mtef in equations.items():
        storage_index = len(entries)
        storage_indexes.append(storage_index)
        entries.append(
            {
                "name": f"MBD{location:08X}",
                "kind": 1,
                "child": storage_index + 1,
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
    workbook_index = len(entries)
    entries.append(
        {
            "name": "Workbook",
            "kind": 2,
            "right": none,
            "data": workbook,
        }
    )
    root_children = storage_indexes + [workbook_index]
    for current, following in zip(root_children, root_children[1:]):
        entries[current]["right"] = following
    entries[0]["child"] = root_children[0]
    return _build_nested_cfb(entries)


def biff_bof(substream_type: int, *, version: int = 0x0600) -> bytes:
    """Construct BIFF8 BOF record."""

    return biff_record(
        0x0809,
        struct.pack("<HHHHII", version, substream_type, 0x4F5A, 0x07CD, 0x000200C1, 0x00000608),
    )


def label_cell(row: int, col: int, text: str, *, xf_index: int = 0) -> bytes:
    """Construct BIFF8 LABEL cell."""

    encoded = text.encode("utf-16le")
    string = struct.pack("<HB", len(encoded) // 2, 1) + encoded
    return biff_record(0x0204, struct.pack("<HHH", row, col, xf_index) + string)


def number_cell(row: int, col: int, value: float, *, xf_index: int = 0) -> bytes:
    """Construct the NUMBER cell."""

    return biff_record(0x0203, struct.pack("<HHHd", row, col, xf_index, value))


def formula_number_cell(row: int, col: int, value: float, *, xf_index: int = 0) -> bytes:
    """Constructs a FORMULA cell that relies only on cached values."""

    body = struct.pack("<HHH", row, col, xf_index) + struct.pack("<d", value) + b"\x00" * 6
    return biff_record(0x0006, body)


def formula_string_cell(row: int, col: int, value: str, *, xf_index: int = 0) -> bytes:
    """Construct cache results from FORMULA provided immediately following the STRING record."""

    cached = bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF])
    formula = biff_record(
        0x0006,
        struct.pack("<HHH", row, col, xf_index) + cached + b"\x00" * 6,
    )
    encoded = value.encode("utf-16le")
    string = biff_record(0x0207, struct.pack("<HB", len(encoded) // 2, 1) + encoded)
    return formula + string


def merged_cells(*ranges: tuple[int, int, int, int]) -> bytes:
    """Construct MERGEDCELLS record."""

    payload = struct.pack("<H", len(ranges))
    for row_first, col_first, row_last, col_last in ranges:
        payload += struct.pack("<4H", row_first, row_last, col_first, col_last)
    return biff_record(0x00E5, payload)


def url_hyperlink(
    row: int,
    col: int,
    label: str,
    target: str,
) -> bytes:
    """Construct HLink with display name and URL Moniker."""

    ref = struct.pack("<4H", row, row, col, col)
    hyperlink_clsid = uuid.UUID("79eac9d0-baf9-11ce-8c82-00aa004ba90b").bytes_le
    url_clsid = uuid.UUID("79eac9e0-baf9-11ce-8c82-00aa004ba90b").bytes_le
    display = (label + "\x00").encode("utf-16le")
    url = (target + "\x00").encode("utf-16le")
    body = (
        ref
        + hyperlink_clsid
        + struct.pack("<II", 2, 0x17)
        + struct.pack("<I", len(display) // 2)
        + display
        + url_clsid
        + struct.pack("<I", len(url))
        + url
    )
    return biff_record(0x01B8, body)


def font_record(*, bold: bool = False, italic: bool = False) -> bytes:
    """Constructs a FONT record carrying only the bold/italic attributes."""

    flags = 0x0002 if italic else 0
    weight = 700 if bold else 400
    body = struct.pack("<HHHHHBBBB", 220, flags, 0, weight, 0, 0, 0, 0, 0)
    body += bytes([5, 1]) + "Arial".encode("utf-16le")
    return biff_record(0x0031, body)


def rich_sst(strings: list[tuple[str, list[tuple[int, int]]]]) -> bytes:
    """Construct SST with font switching points."""

    payload = struct.pack("<II", len(strings), len(strings))
    for text, starts in strings:
        encoded = text.encode("utf-16le")
        flags = 0x01 | (0x08 if starts else 0)
        payload += struct.pack("<HB", len(encoded) // 2, flags)
        if starts:
            payload += struct.pack("<H", len(starts))
        payload += encoded
        for character_index, font_index in starts:
            payload += struct.pack("<HH", character_index, font_index)
    return biff_record(0x00FC, payload)


def continued_rich_sst(text: str, starts: list[tuple[int, int]]) -> bytes:
    """Use CONTINUE to split a rich text SST entry in the middle of character data."""

    encoded = text.encode("utf-16le")
    unit_count = len(encoded) // 2
    first_character = encoded[:2]
    remaining_characters = encoded[2:]
    base = struct.pack("<IIHBH", 1, 1, unit_count, 0x09, len(starts))
    base += first_character
    continuation = bytes([0x01]) + remaining_characters
    continuation += b"".join(struct.pack("<HH", character_index, font_index) for character_index, font_index in starts)
    return biff_record(0x00FC, base) + biff_record(0x003C, continuation)


def labelsst_cell(row: int, col: int, string_index: int, *, xf_index: int = 0) -> bytes:
    """Constructs the LABELSST cell that references SST."""

    return biff_record(0x00FD, struct.pack("<HHHI", row, col, xf_index, string_index))


def build_xls(
    sheets: list[SheetFixture],
    *,
    globals_records: bytes = b"",
    corrupt_first_offset: bool = False,
    encrypted: bool = False,
    equations: dict[int, bytes] | None = None,
    equation_prog_id: str = "Equation.3",
) -> bytes:
    """Construct a OLE file containing Workbook Globals and multiple worksheet substreams."""

    prefix = biff_bof(0x0005)
    prefix += biff_record(0x0042, struct.pack("<H", 1200))
    prefix += font_record()
    prefix += biff_record(0x00E0, struct.pack("<HH", 0, 0) + b"\x00" * 16)
    if encrypted:
        prefix += biff_record(0x002F, b"\x00\x00")
    prefix += globals_records

    def boundsheet(offset: int, sheet: SheetFixture) -> bytes:
        """Constructs the BoundSheet8 directory entry."""

        name = sheet.name.encode("utf-16le")
        body = struct.pack("<IBB", offset, 0 if sheet.visible else 1, 0)
        body += struct.pack("<BB", len(name) // 2, 1) + name
        return biff_record(0x0085, body)

    placeholders = b"".join(boundsheet(0, sheet) for sheet in sheets)
    globals_length = len(prefix) + len(placeholders) + len(biff_record(0x000A))
    sheet_streams = [biff_bof(0x0010) + sheet.records + biff_record(0x000A) for sheet in sheets]
    offsets: list[int] = []
    cursor = globals_length
    for stream in sheet_streams:
        offsets.append(cursor)
        cursor += len(stream)
    if corrupt_first_offset and offsets:
        offsets[0] = 0x7FFF_FFF0
    directory = b"".join(boundsheet(offset, sheet) for offset, sheet in zip(offsets, sheets, strict=True))
    workbook = prefix + directory + biff_record(0x000A) + b"".join(sheet_streams)
    if equations:
        return _build_xls_with_embeddings(
            workbook,
            equations,
            prog_id=equation_prog_id,
        )
    return _build_cfb([("Workbook", workbook)])


def build_equation_xls(
    formulas: list[tuple[int, bytes]],
    *,
    cell_records: bytes = b"",
    preview: bool = True,
    prog_id: str = "Equation.3",
    preview_payload: bytes | None = None,
) -> bytes:
    """Construct XLS with formulas picture OBJ, MBD storages and optional preview."""

    drawing_records = b""
    for index, (location, _mtef) in enumerate(formulas, start=1):
        drawing_records += biff_record(
            0x00EC,
            _equation_shape(index - 1, 0, index, preview=preview),
        )
        drawing_records += _equation_obj(location, index)
    globals_records = biff_record(0x00EB, _preview_bstore(preview_payload)) if preview else b""
    return build_xls(
        [SheetFixture("Equations", cell_records + drawing_records)],
        globals_records=globals_records,
        equations=dict(formulas),
        equation_prog_id=prog_id,
    )


def build_biff5_xls(text: str) -> bytes:
    """Construct BIFF5 files using Book stream and cp1252 LABEL."""

    encoded = text.encode("cp1252")
    label = biff_record(
        0x0204,
        struct.pack("<HHHH", 0, 0, 0, len(encoded)) + encoded,
    )
    sheet_stream = biff_bof(0x0010, version=0x0500) + label + biff_record(0x000A)
    prefix = biff_bof(0x0005, version=0x0500)
    prefix += biff_record(0x0042, struct.pack("<H", 1252))
    prefix += biff_record(0x00E0, struct.pack("<HH", 0, 0) + b"\x00" * 12)
    name = b"Legacy"
    placeholder = biff_record(
        0x0085,
        struct.pack("<IBBB", 0, 0, 0, len(name)) + name,
    )
    offset = len(prefix) + len(placeholder) + len(biff_record(0x000A))
    boundsheet = biff_record(
        0x0085,
        struct.pack("<IBBB", offset, 0, 0, len(name)) + name,
    )
    workbook = prefix + boundsheet + biff_record(0x000A) + sheet_stream
    return _build_cfb([("Book", workbook)])
