"""Bounded read MS-PPT and OfficeArt record streams."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterator

from ..errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from ..limits import MAX_RECORD_DEPTH, MAX_RECORDS

CONTAINER_VERSION = 0xF
ROUNDTRIP_OPAQUE_MIN = 1053
ROUNDTRIP_OPAQUE_MAX = 1064


@dataclass(frozen=True, slots=True)
class PptRecord:
    """A PPT/OfficeArt record with verified boundaries."""

    offset: int
    version: int
    instance: int
    record_type: int
    payload: bytes


@dataclass(slots=True)
class RecordBudget:
    """The number of visits is recorded cumulatively across parsing stages."""

    count: int = 0

    def charge(self) -> None:
        """Counted as one record, hard failure occurs when a fixed upper limit is exceeded."""

        self.count += 1
        if self.count > MAX_RECORDS:
            raise LegacyOfficeResourceLimitError(f"record stream exceeds max_records={MAX_RECORDS}")


def record_at(
    data: bytes,
    offset: int,
    *,
    end: int | None = None,
    strict: bool = False,
    budget: RecordBudget | None = None,
) -> PptRecord | None:
    """Read a single record at the specified offset; convert bad boundaries into stable errors in strict mode."""

    limit = len(data) if end is None else min(end, len(data))
    if offset < 0 or offset + 8 > limit:
        if strict:
            raise LegacyOfficeMalformedError("PowerPoint record header is truncated")
        return None
    version_instance, record_type, length = struct.unpack_from("<HHI", data, offset)
    payload_start = offset + 8
    payload_end = payload_start + length
    if payload_end < payload_start or payload_end > limit:
        if strict:
            raise LegacyOfficeMalformedError("PowerPoint record extends beyond its container")
        return None
    if budget is not None:
        budget.charge()
    return PptRecord(
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
    budget: RecordBudget | None = None,
    strict_first: bool = False,
) -> Iterator[PptRecord]:
    """Traverse records within the same container sequentially, allowing the tail producer to fill bytes."""

    limit = len(data) if end is None else min(end, len(data))
    cursor = start
    first = True
    while cursor < limit:
        record = record_at(
            data,
            cursor,
            end=limit,
            strict=strict_first and first,
            budget=budget,
        )
        if record is None:
            return
        yield record
        cursor += 8 + len(record.payload)
        first = False


def iter_descendants(
    record: PptRecord,
    *,
    budget: RecordBudget | None = None,
) -> Iterator[PptRecord]:
    """Traverse the container using explicit stack depth first, skipping non-recursive round-trip blob."""

    if record.version != CONTAINER_VERSION:
        return
    stack: list[tuple[bytes, Iterator[PptRecord]]] = [(record.payload, iter_records(record.payload, budget=budget))]
    while stack:
        if len(stack) > MAX_RECORD_DEPTH:
            raise LegacyOfficeResourceLimitError(f"record nesting exceeds max_record_depth={MAX_RECORD_DEPTH}")
        _, iterator = stack[-1]
        try:
            child = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        yield child
        if child.version == CONTAINER_VERSION and not ROUNDTRIP_OPAQUE_MIN <= child.record_type <= ROUNDTRIP_OPAQUE_MAX:
            stack.append((child.payload, iter_records(child.payload, budget=budget)))


def utf16_text(payload: bytes) -> str:
    """Tolerantly decode UTF-16LE text and remove trailing NUL."""

    usable = payload[: len(payload) - (len(payload) % 2)]
    return usable.decode("utf-16le", "replace").rstrip("\x00")
