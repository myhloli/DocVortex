"""Parse DOC CLX piece table and restore the global UTF-16 CP text stream."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import struct

from ..errors import LegacyOfficeMalformedError
from ..legacy.binary import bounded_slice, get_u16, get_u32

from .records import DocBudget


@dataclass(frozen=True, slots=True)
class Piece:
    """A piece that maps the logical CP range to WordDocument FC."""

    cp_start: int
    cp_end: int
    fc: int
    compressed: bool
    prm: bytes = b""


@dataclass(slots=True)
class TextStream:
    """Character scalars and their CP, FC, and piece mappings."""

    chars: list[str]
    cps: list[int]
    fcs: list[int]
    piece_indexes: list[int]

    def index_of_cp(self, cp: int) -> int:
        """Return the first character index where CP is not less than the target value."""

        return bisect_left(self.cps, max(cp, 0))

    def text_between(self, cp_start: int, cp_end: int) -> str:
        """Returns the Unicode text specifying the CP half-open range."""

        return "".join(self.chars[self.index_of_cp(cp_start) : self.index_of_cp(cp_end)])


def _prm0_grpprl(prm: int) -> bytes:
    """Restore individual attributes already supported in Prm0 to grpprl."""

    isprm = (prm >> 1) & 0x7F
    value = (prm >> 8) & 0xFF
    opcode = {
        0x0C: 0x260A,  # sprmPIlvl
        0x18: 0x2416,  # sprmPFInTable
        0x19: 0x2417,  # sprmPFTtp
        0x55: 0x0835,  # sprmCFBold
        0x56: 0x0836,  # sprmCFItalic
        0x57: 0x0837,  # sprmCFStrike
        0x78: 0x2640,  # sprmPOutLvl
    }.get(isprm)
    if opcode is None:
        return b""
    return struct.pack("<HB", opcode, value)


def _parse_plc_pcd(plc: bytes, prcs: list[bytes], budget: DocBudget) -> list[Piece]:
    """Parse PlcPcd in Pcdt and bind piece to Prm."""

    if len(plc) < 16 or (len(plc) - 4) % 12:
        raise LegacyOfficeMalformedError("DOC piece table is empty or malformed")
    count = (len(plc) - 4) // 12
    budget.charge(count)
    cp_bytes = (count + 1) * 4
    pieces: list[Piece] = []
    previous_cp = -1
    for index in range(count):
        cp_start = get_u32(plc, index * 4)
        cp_end = get_u32(plc, (index + 1) * 4)
        pcd_offset = cp_bytes + index * 8
        fc_raw = get_u32(plc, pcd_offset + 2)
        prm = get_u16(plc, pcd_offset + 6) or 0
        if cp_start is None or cp_end is None or fc_raw is None:
            raise LegacyOfficeMalformedError("DOC piece table is truncated")
        if cp_start < previous_cp or cp_end < cp_start:
            raise LegacyOfficeMalformedError("DOC piece CP values are not ordered")
        previous_cp = cp_end
        compressed = bool(fc_raw & 0x4000_0000)
        fc = fc_raw & 0x3FFF_FFFF
        if compressed:
            fc //= 2
        grpprl = b""
        if prm & 1:
            prc_index = prm >> 1
            if prc_index < len(prcs):
                grpprl = prcs[prc_index]
        elif prm:
            grpprl = _prm0_grpprl(prm)
        pieces.append(
            Piece(
                cp_start=int(cp_start),
                cp_end=int(cp_end),
                fc=int(fc),
                compressed=compressed,
                prm=grpprl,
            )
        )
    return pieces


def parse_clx(table_stream: bytes, *, offset: int, size: int, budget: DocBudget) -> list[Piece]:
    """Parse the Prc array and final Pcdt in CLX."""

    clx = bounded_slice(table_stream, offset, size)
    if clx is None:
        raise LegacyOfficeMalformedError("DOC CLX range is out of bounds")
    prcs: list[bytes] = []
    cursor = 0
    while cursor < len(clx):
        kind = clx[cursor]
        if kind == 1:
            length = get_u16(clx, cursor + 1)
            if length is None:
                raise LegacyOfficeMalformedError("DOC CLX Prc is truncated")
            payload = bounded_slice(clx, cursor + 3, length)
            if payload is None:
                raise LegacyOfficeMalformedError("DOC CLX Prc exceeds its range")
            budget.charge()
            prcs.append(payload)
            cursor += 3 + length
            continue
        if kind == 2:
            length = get_u32(clx, cursor + 1)
            if length is None:
                raise LegacyOfficeMalformedError("DOC CLX Pcdt is truncated")
            plc = bounded_slice(clx, cursor + 5, length)
            if plc is None:
                raise LegacyOfficeMalformedError("DOC PlcPcd exceeds CLX")
            return _parse_plc_pcd(plc, prcs, budget)
        raise LegacyOfficeMalformedError("DOC CLX contains an unknown record")
    raise LegacyOfficeMalformedError("DOC CLX does not contain a Pcdt")


def legacy_single_piece(*, fc_min: int, fc_mac: int, ccp_text: int) -> list[Piece]:
    """Construct conservative single piece for non-complex documents without CLX."""

    if fc_min < 0 or fc_mac <= fc_min:
        return []
    length = min(fc_mac - fc_min, max(ccp_text, 0))
    if length <= 0:
        return []
    return [Piece(cp_start=0, cp_end=length, fc=fc_min, compressed=True)]


def codec_for_lid(lid: int) -> str:
    """Map Word LID to ANSI/DBCS codec available in Python."""

    primary = lid & 0x03FF
    if primary == 0x11:
        return "cp932"
    if primary == 0x12:
        return "cp949"
    if primary == 0x04:
        return "cp950" if lid in {0x0404, 0x0C04, 0x1404, 0x7C04} else "cp936"
    if primary in {0x01, 0x20, 0x29}:
        return "cp1256"
    if primary in {0x02, 0x19, 0x22, 0x23}:
        return "cp1251"
    if primary in {0x05, 0x0E, 0x15, 0x18, 0x1A, 0x1B, 0x24}:
        return "cp1250"
    if primary == 0x08:
        return "cp1253"
    if primary == 0x0D:
        return "cp1255"
    if primary == 0x1E:
        return "cp874"
    if primary in {0x1F, 0x2C}:
        return "cp1254"
    if primary in {0x25, 0x26, 0x27}:
        return "cp1257"
    if primary == 0x2A:
        return "cp1258"
    return "cp1252"


def _lead_byte(codec: str, value: int) -> bool:
    """Determine whether a compressed piece byte is the first byte of DBCS."""

    if codec == "cp932":
        return 0x81 <= value <= 0x9F or 0xE0 <= value <= 0xFC
    if codec in {"cp936", "cp949", "cp950"}:
        return 0x81 <= value <= 0xFE
    return False


def extract_text(
    word_document: bytes,
    pieces: list[Piece],
    *,
    total_cp: int,
    codec: str,
    budget: DocBudget,
) -> TextStream:
    """Restore characters in piece order and preserve the reverse mapping of characters to CP/FC."""

    chars: list[str] = []
    cps: list[int] = []
    fcs: list[int] = []
    piece_indexes: list[int] = []
    for piece_index, piece in enumerate(pieces):
        if piece.cp_start >= total_cp:
            break
        cp_cursor = piece.cp_start
        logical_length = min(piece.cp_end, total_cp) - piece.cp_start
        if logical_length <= 0:
            continue
        if piece.compressed:
            payload = bounded_slice(word_document, piece.fc, logical_length)
            if payload is None:
                continue
            cursor = 0
            while cursor < len(payload):
                width = 2 if _lead_byte(codec, payload[cursor]) and cursor + 1 < len(payload) else 1
                decoded = payload[cursor : cursor + width].decode(codec, errors="replace")
                for char in decoded:
                    chars.append(char)
                    cps.append(cp_cursor)
                    fcs.append(piece.fc + cursor)
                    piece_indexes.append(piece_index)
                cp_cursor += width
                cursor += width
                budget.charge(width)
        else:
            byte_length = logical_length * 2
            payload = bounded_slice(word_document, piece.fc, byte_length)
            if payload is None:
                continue
            cursor = 0
            while cursor + 2 <= len(payload):
                first = int(struct.unpack_from("<H", payload, cursor)[0])
                width = 2
                units = [first]
                if 0xD800 <= first <= 0xDBFF and cursor + 4 <= len(payload):
                    second = int(struct.unpack_from("<H", payload, cursor + 2)[0])
                    if 0xDC00 <= second <= 0xDFFF:
                        units.append(second)
                        width = 4
                raw = struct.pack(f"<{len(units)}H", *units)
                char = raw.decode("utf-16le", errors="replace")
                chars.append(char)
                cps.append(cp_cursor)
                fcs.append(piece.fc + cursor)
                piece_indexes.append(piece_index)
                unit_count = width // 2
                cp_cursor += unit_count
                cursor += width
                budget.charge(unit_count)
    return TextStream(chars=chars, cps=cps, fcs=fcs, piece_indexes=piece_indexes)
