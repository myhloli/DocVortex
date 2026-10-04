"""Bounded read Excel 97–2003 BIFF record stream."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterator

from loguru import logger

from ..errors import LegacyOfficeResourceLimitError
from ..limits import MAX_RECORDS

BOF = 0x0809
EOF = 0x000A
CONTINUE = 0x003C


@dataclass(frozen=True, slots=True)
class BiffRecord:
    """A BIFF record that has completed boundary verification."""

    offset: int
    record_type: int
    payload: bytes
    next_offset: int


@dataclass(slots=True)
class RecordBudget:
    """Record access budgets are shared across globals, worksheet and OfficeArt."""

    count: int = 0

    def charge(self) -> None:
        """Counts one record and hard fails when a fixed limit is exceeded."""

        self.count += 1
        if self.count > MAX_RECORDS:
            raise LegacyOfficeResourceLimitError(f"workbook stream exceeds max_records={MAX_RECORDS}")


def record_at(
    data: bytes,
    offset: int,
    *,
    budget: RecordBudget | None = None,
) -> BiffRecord | None:
    """Read the BIFF record at the specified offset, truncate header or body and return a null value."""

    if offset < 0 or offset + 4 > len(data):
        return None
    record_type, length = struct.unpack_from("<HH", data, offset)
    body_start = offset + 4
    body_end = body_start + int(length)
    if body_end < body_start or body_end > len(data):
        return None
    if budget is not None:
        budget.charge()
    return BiffRecord(
        offset=offset,
        record_type=int(record_type),
        payload=data[body_start:body_end],
        next_offset=body_end,
    )


def iter_records(
    data: bytes,
    *,
    start: int = 0,
    stop_at_eof: bool = False,
    budget: RecordBudget | None = None,
) -> Iterator[BiffRecord]:
    """Traverse the BIFF records sequentially, retaining completed records at the truncated tail."""

    cursor = start
    while cursor < len(data):
        if data[cursor:] and not any(data[cursor:]):
            return
        record = record_at(data, cursor, budget=budget)
        if record is None:
            logger.warning(
                "XLS_TRUNCATED_RECORD: workbook stream ends mid-record at byte {}",
                cursor,
            )
            return
        yield record
        cursor = record.next_offset
        if stop_at_eof and record.record_type == EOF:
            return


def collect_continues(
    data: bytes,
    base: BiffRecord,
    *,
    budget: RecordBudget,
) -> tuple[list[bytes], int]:
    """Collects CONTINUE bodies immediately following base and returns the next non-continuation record offset."""

    segments = [base.payload]
    cursor = base.next_offset
    while True:
        record = record_at(data, cursor)
        if record is None or record.record_type != CONTINUE:
            return segments, cursor
        budget.charge()
        segments.append(record.payload)
        cursor = record.next_offset


class SegmentReader:
    """Performs a bounded sequential read on the underlying record and its CONTINUE segments."""

    def __init__(self, segments: list[bytes]) -> None:
        """Save the segment list and place the cursor at the beginning of the first paragraph."""

        self.segments = segments
        self.segment_index = 0
        self.offset = 0

    def remaining_in_segment(self) -> int:
        """Returns the number of bytes that the current segment has not yet consumed."""

        if self.segment_index >= len(self.segments):
            return 0
        return len(self.segments[self.segment_index]) - self.offset

    def normalize(self) -> None:
        """Skip exhausted segments."""

        while self.segment_index < len(self.segments) and self.offset >= len(self.segments[self.segment_index]):
            self.segment_index += 1
            self.offset = 0

    def next_segment(self) -> bool:
        """Explicitly move to the next segment, or return False if it does not exist."""

        if self.segment_index + 1 >= len(self.segments):
            return False
        self.segment_index += 1
        self.offset = 0
        return True

    def read(self, size: int) -> bytes | None:
        """Only read fixed length fields within the current segment."""

        self.normalize()
        if size < 0 or self.segment_index >= len(self.segments):
            return None
        segment = self.segments[self.segment_index]
        end = self.offset + size
        if end > len(segment):
            return None
        output = segment[self.offset : end]
        self.offset = end
        return output

    def read_across(self, size: int) -> bytes | None:
        """Read plain non-character data across segment."""

        if size < 0:
            return None
        output = bytearray()
        remaining = size
        while remaining:
            self.normalize()
            available = self.remaining_in_segment()
            if available <= 0:
                return None
            take = min(available, remaining)
            chunk = self.read(take)
            if chunk is None:
                return None
            output.extend(chunk)
            remaining -= take
        return bytes(output)

    def skip(self, size: int) -> bool:
        """Skip the specified number of bytes across segments."""

        return self.read_across(size) is not None

    def u8(self) -> int | None:
        """Read an unsigned 8-bit integer."""

        value = self.read(1)
        return int(value[0]) if value is not None else None

    def u16(self) -> int | None:
        """Reads a little-endian unsigned 16-bit integer."""

        value = self.read(2)
        return int(struct.unpack("<H", value)[0]) if value is not None else None

    def u32(self) -> int | None:
        """Reads a little-endian unsigned 32-bit integer."""

        value = self.read(4)
        return int(struct.unpack("<I", value)[0]) if value is not None else None
