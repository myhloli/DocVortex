"""OfficeArt logging and picture decoding for legacy Office binary format sharing."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import struct
import zlib

from loguru import logger

from ..image import ensure_bmp_header
from ..errors import LegacyOfficeResourceLimitError
from ..limits import MAX_ASSET_TOTAL_BYTES, MAX_ENTRY_BYTES, MAX_PICTURE_RECORDS, MAX_RECORD_DEPTH

OFFICEART_CONTAINER_VERSION = 0xF
OFFICEART_DGG_CONTAINER = 0xF000
OFFICEART_BSTORE_CONTAINER = 0xF001
OFFICEART_SP_CONTAINER = 0xF004
OFFICEART_BSE = 0xF007
OFFICEART_FSP = 0xF00A
OFFICEART_FOPT = 0xF00B
OFFICEART_CLIENT_ANCHOR = 0xF010
OFFICEART_TERTIARY_FOPT = 0xF122

FOPT_PIB = 0x0104
FOPT_GROUP_SHAPE = 0x03BF
F_HIDDEN = 0x0000_0002
F_USE_HIDDEN = 0x0002_0000


@dataclass(frozen=True, slots=True)
class OfficeArtRecord:
    """A OfficeArt record that has passed length boundary verification."""

    offset: int
    version: int
    instance: int
    record_type: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class OfficeImagePayload:
    """Original pictures and their media types recovered from BLIP."""

    data: bytes
    extension: str
    content_type: str
    render_size_emu: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class OfficeArtShape:
    """Excel Shape properties in drawing that can be bound to OBJ."""

    shape_id: int | None
    anchor: tuple[int, int, int, int] | None
    pib: int | None
    hidden: bool


def record_at(
    data: bytes,
    offset: int,
    *,
    end: int | None = None,
    charge: Callable[[], None] | None = None,
) -> OfficeArtRecord | None:
    """Reads a OfficeArt record from the specified offset, returns NULL for bad boundaries."""

    limit = len(data) if end is None else min(end, len(data))
    if offset < 0 or offset + 8 > limit:
        return None
    version_instance, record_type, length = struct.unpack_from("<HHI", data, offset)
    payload_start = offset + 8
    payload_end = payload_start + int(length)
    if payload_end < payload_start or payload_end > limit:
        return None
    if charge is not None:
        charge()
    return OfficeArtRecord(
        offset=offset,
        version=version_instance & 0xF,
        instance=version_instance >> 4,
        record_type=record_type,
        payload=data[payload_start:payload_end],
    )


def iter_records(
    data: bytes,
    *,
    start: int = 0,
    end: int | None = None,
    charge: Callable[[], None] | None = None,
) -> Iterator[OfficeArtRecord]:
    """Sequentially traverses direct child records within the same OfficeArt container."""

    limit = len(data) if end is None else min(end, len(data))
    cursor = start
    while cursor < limit:
        record = record_at(data, cursor, end=limit, charge=charge)
        if record is None:
            return
        yield record
        cursor += 8 + len(record.payload)


def iter_descendants(
    data: bytes,
    *,
    charge: Callable[[], None] | None = None,
) -> Iterator[OfficeArtRecord]:
    """Traverse the OfficeArt record tree with explicit stack depth first and limit the nesting depth."""

    stack: list[Iterator[OfficeArtRecord]] = [iter_records(data, charge=charge)]
    while stack:
        if len(stack) > MAX_RECORD_DEPTH:
            raise LegacyOfficeResourceLimitError(f"record nesting exceeds max_record_depth={MAX_RECORD_DEPTH}")
        try:
            record = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        yield record
        if record.version == OFFICEART_CONTAINER_VERSION:
            stack.append(iter_records(record.payload, charge=charge))


def _simple_properties(record: OfficeArtRecord) -> dict[int, int]:
    """Reads the FOPT simple attribute and allows subsequent occurrences of the same name to overwrite the previous value."""

    properties: dict[int, int] = {}
    for index in range(record.instance):
        offset = index * 6
        if offset + 6 > len(record.payload):
            break
        opid, value = struct.unpack_from("<HI", record.payload, offset)
        properties[opid & 0x3FFF] = int(value)
    return properties


def _excel_client_anchor(payload: bytes) -> tuple[int, int, int, int] | None:
    """Convert OfficeArtClientAnchorChart into start and end row and column coordinates."""

    if len(payload) < 18:
        return None
    start_col = struct.unpack_from("<H", payload, 2)[0]
    start_row = struct.unpack_from("<H", payload, 6)[0]
    end_col = struct.unpack_from("<H", payload, 10)[0]
    end_row = struct.unpack_from("<H", payload, 14)[0]
    return int(start_row), int(start_col), int(end_row), int(end_col)


def _shape_from_container(
    record: OfficeArtRecord,
    *,
    charge: Callable[[], None] | None = None,
) -> OfficeArtShape | None:
    """Extract shape id, anchor, pib with hidden state from a single SpContainer."""

    shape_id: int | None = None
    anchor: tuple[int, int, int, int] | None = None
    properties: dict[int, int] = {}
    for child in iter_records(record.payload, charge=charge):
        if child.record_type == OFFICEART_FSP and len(child.payload) >= 4:
            shape_id = int(struct.unpack_from("<I", child.payload, 0)[0])
        elif child.record_type in {OFFICEART_FOPT, OFFICEART_TERTIARY_FOPT}:
            properties.update(_simple_properties(child))
        elif child.record_type == OFFICEART_CLIENT_ANCHOR:
            anchor = _excel_client_anchor(child.payload)
    hidden_flags = properties.get(FOPT_GROUP_SHAPE, 0)
    hidden = bool(hidden_flags & F_USE_HIDDEN and hidden_flags & F_HIDDEN)
    pib = properties.get(FOPT_PIB)
    if anchor is None and pib is None:
        return None
    return OfficeArtShape(shape_id=shape_id, anchor=anchor, pib=pib, hidden=hidden)


def extract_excel_shapes(
    data: bytes,
    *,
    charge: Callable[[], None] | None = None,
) -> list[OfficeArtShape]:
    """Extract shapes that can be bound one by one to Excel OBJ in the order of drawing."""

    shapes: list[OfficeArtShape] = []
    for record in iter_descendants(data, charge=charge):
        if record.record_type != OFFICEART_SP_CONTAINER:
            continue
        shape = _shape_from_container(record, charge=charge)
        if shape is not None:
            shapes.append(shape)
    return shapes


def _bitmap_payload(body: bytes, instance: int) -> bytes | None:
    """Skips BLIP UID and tag, returning the bitmap original payload."""

    doubled = instance in {0x46B, 0x6E3, 0x6E1, 0x7A9}
    start = (32 if doubled else 16) + 1
    return body[start:] if start < len(body) else None


def decode_blip(record: OfficeArtRecord) -> OfficeImagePayload | None:
    """Decode common bitmaps and EMF/WMF BLIP, and limit vector decompression output."""

    instance = record.instance
    body = record.payload
    if record.record_type in {0xF01D, 0xF01E, 0xF01F, 0xF029}:
        data = _bitmap_payload(body, instance)
        if data is None:
            return None
        if len(data) > MAX_ENTRY_BYTES:
            raise LegacyOfficeResourceLimitError("bitmap BLIP exceeds max_entry_bytes")
        if record.record_type == 0xF01D:
            return OfficeImagePayload(data=data, extension="jpg", content_type="image/jpeg")
        if record.record_type == 0xF01E:
            return OfficeImagePayload(data=data, extension="png", content_type="image/png")
        if record.record_type == 0xF029:
            return OfficeImagePayload(data=data, extension="tiff", content_type="image/tiff")
        return OfficeImagePayload(data=ensure_bmp_header(data), extension="bmp", content_type="image/bmp")

    if record.record_type not in {0xF01A, 0xF01B}:
        return None
    doubled = instance in ({0x3D5} if record.record_type == 0xF01A else {0x217})
    header_offset = 32 if doubled else 16
    if header_offset + 34 > len(body):
        return None
    declared_size = int(struct.unpack_from("<I", body, header_offset)[0])
    render_width_emu, render_height_emu = struct.unpack_from("<ii", body, header_offset + 20)
    render_size_emu = (render_width_emu, render_height_emu) if render_width_emu > 0 and render_height_emu > 0 else None
    compressed_size = int(struct.unpack_from("<I", body, header_offset + 28)[0])
    compression = body[header_offset + 32]
    payload_start = header_offset + 34
    payload = body[payload_start : payload_start + compressed_size]
    if declared_size > MAX_ENTRY_BYTES:
        raise LegacyOfficeResourceLimitError("metafile BLIP exceeds max_entry_bytes")
    if compression == 0:
        data = b""
        reached_eof = False
        for window_bits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                inflater = zlib.decompressobj(window_bits)
                candidate = inflater.decompress(payload, MAX_ENTRY_BYTES + 1)
                candidate += inflater.flush(MAX_ENTRY_BYTES + 1 - len(candidate))
            except zlib.error:
                continue
            if inflater.eof:
                data = candidate
                reached_eof = True
                break
        if not reached_eof:
            return None
        if len(data) > MAX_ENTRY_BYTES:
            raise LegacyOfficeResourceLimitError("metafile BLIP decompression exceeded its limit")
    else:
        data = payload
    if record.record_type == 0xF01A:
        return OfficeImagePayload(
            data=data,
            extension="emf",
            content_type="image/emf",
            render_size_emu=render_size_emu,
        )
    # Keep legal placeable header so cross-platform renderers continue to get bbox and units-per-inch.
    return OfficeImagePayload(
        data=data,
        extension="wmf",
        content_type="image/wmf",
        render_size_emu=render_size_emu,
    )


def first_blip(
    data: bytes,
    *,
    charge: Callable[[], None] | None = None,
) -> OfficeImagePayload | None:
    """Depth first returns the first supported BLIP in a section of OfficeArt data."""

    for record in iter_descendants(data, charge=charge):
        if record.record_type == OFFICEART_BSE:
            decoded = _decode_bse_body(record.payload, charge=charge)
            if decoded is not None:
                return decoded
        decoded = decode_blip(record)
        if decoded is not None:
            return decoded
    return None


def extract_word_shapes(
    data: bytes,
    *,
    charge: Callable[[], None] | None = None,
) -> list[OfficeArtShape]:
    """Extract shape id, pib and hidden states in order Word drawing."""

    shapes: list[OfficeArtShape] = []
    for record in iter_descendants(data, charge=charge):
        if record.record_type != OFFICEART_SP_CONTAINER:
            continue
        shape = _shape_from_container(record, charge=charge)
        if shape is not None:
            shapes.append(shape)
    return shapes


def _decode_bse_body(
    body: bytes,
    *,
    charge: Callable[[], None] | None = None,
    delay_stream: bytes | None = None,
) -> OfficeImagePayload | None:
    """Recover pictures from FBSE body embedded or delayed BLIP."""

    if len(body) < 36:
        return None
    inner_offset = 36 + int(body[33])
    inner = record_at(body, inner_offset, charge=charge) if inner_offset < len(body) else None
    if inner is None and delay_stream is not None:
        delayed_size = int(struct.unpack_from("<I", body, 20)[0])
        reference_count = int(struct.unpack_from("<I", body, 24)[0])
        delayed_offset = int(struct.unpack_from("<I", body, 28)[0])
        if reference_count and delayed_offset != 0xFFFF_FFFF:
            delayed_end = delayed_offset + delayed_size
            if delayed_end >= delayed_offset and delayed_end <= len(delay_stream):
                inner = record_at(delay_stream, delayed_offset, end=delayed_end, charge=charge)
    return decode_blip(inner) if inner is not None else None


def decode_bstore(
    data: bytes,
    *,
    charge: Callable[[], None] | None = None,
    delay_stream: bytes | None = None,
) -> dict[int, OfficeImagePayload]:
    """Decode the image resources in drawing group according to a base BSE serial number."""

    bse_records = [record for record in iter_descendants(data, charge=charge) if record.record_type == OFFICEART_BSE]
    result: dict[int, OfficeImagePayload] = {}
    asset_total = 0
    for index, bse in enumerate(bse_records[:MAX_PICTURE_RECORDS], start=1):
        decoded = _decode_bse_body(
            bse.payload,
            charge=charge,
            delay_stream=delay_stream,
        )
        if decoded is None:
            continue
        asset_total += len(decoded.data)
        if asset_total > MAX_ASSET_TOTAL_BYTES:
            raise LegacyOfficeResourceLimitError(f"embedded assets exceed max_asset_total_bytes={MAX_ASSET_TOTAL_BYTES}")
        result[index] = decoded
    if len(bse_records) > MAX_PICTURE_RECORDS:
        logger.warning(
            "LEGACY_OFFICE_PICTURE_LIMIT: ignored BSE records after {}",
            MAX_PICTURE_RECORDS,
        )
    return result
