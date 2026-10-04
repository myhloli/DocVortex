"""Legacy Office Bounded little-endian read capability for binary format multiplexing."""

from __future__ import annotations

import struct


def get_u16(data: bytes, offset: int) -> int | None:
    """Bounded read little-endian unsigned 16-bit integer."""
    if offset < 0 or offset + 2 > len(data):
        return None
    return int(struct.unpack_from("<H", data, offset)[0])


def get_i16(data: bytes, offset: int) -> int | None:
    """Bounded read little-endian signed 16-bit integer."""
    if offset < 0 or offset + 2 > len(data):
        return None
    return int(struct.unpack_from("<h", data, offset)[0])


def get_u32(data: bytes, offset: int) -> int | None:
    """Bounded read little-endian unsigned 32-bit integer."""
    if offset < 0 or offset + 4 > len(data):
        return None
    return int(struct.unpack_from("<I", data, offset)[0])


def get_f64(data: bytes, offset: int) -> float | None:
    """Bounded read little endian IEEE-754 Double precision floating point number."""
    if offset < 0 or offset + 8 > len(data):
        return None
    return float(struct.unpack_from("<d", data, offset)[0])


def bounded_slice(data: bytes, offset: int, size: int) -> bytes | None:
    """Returns a byte range that is not out of bounds and does not allow negative offsets or negative lengths."""
    if offset < 0 or size < 0 or offset > len(data) - size:
        return None
    return data[offset : offset + size]


__all__ = ["bounded_slice", "get_f64", "get_i16", "get_u16", "get_u32"]
