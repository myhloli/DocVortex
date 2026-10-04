"""Construct a deterministic WMF/GIF picture carrying MathType MTEF comment."""

from __future__ import annotations

import base64
import struct

_TINY_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _wmf_record(function: int, payload: bytes = b"") -> bytes:
    """Construct WMF record aligned to WORD and declare the correct record size."""

    padded = payload + b"\x00" * (len(payload) % 2)
    return struct.pack("<IH", (6 + len(padded)) // 2, function) + padded


def _wmf_comment_record(comment: bytes) -> bytes:
    """Package comment as META_ESCAPE/MFCOMMENT record."""

    payload = struct.pack("<HH", 0x000F, len(comment)) + comment
    return _wmf_record(0x0626, payload)


def build_wmf(
    comments: list[bytes],
    *,
    placeable: bool = False,
) -> bytes:
    """Constructs standard WMF containing only comment records and EOF."""

    records = [_wmf_comment_record(comment) for comment in comments]
    records.append(_wmf_record(0))
    file_words = (18 + sum(len(record) for record in records)) // 2
    max_record_words = max(len(record) // 2 for record in records)
    header = struct.pack(
        "<HHHIHIH",
        1,
        9,
        0x0300,
        file_words,
        0,
        max_record_words,
        0,
    )
    metafile = header + b"".join(records)
    if not placeable:
        return metafile
    placeable_header = struct.pack(
        "<IHhhhhHIH",
        0x9AC6CDD7,
        0,
        0,
        0,
        100,
        100,
        1440,
        0,
        0,
    )
    return placeable_header + metafile


def pre6_wmf_comment(mtef: bytes) -> bytes:
    """Constructed MathType 6. Single comment MTEF header in front of b."""

    if len(mtef) > 0xFFFF:
        raise ValueError("pre-6 WMF fixture MTEF is too large")
    return b"MathType" + struct.pack("<HH", 0x5555, len(mtef)) + mtef


def baseline_wmf_comment(delta: int = 0) -> bytes:
    """The construct MathType baseline comment must be ignored."""

    return b"MathType" + struct.pack("<HH", 0, delta & 0xFFFF)


def apps_mfcc_comment(
    chunk: bytes,
    *,
    total_length: int,
    signature: str = "Design Science, Inc./MTEF",
) -> bytes:
    """Construct a AppsMFCC v1 chunk."""

    return b"AppsMFCC" + struct.pack("<HII", 1, total_length, len(chunk)) + signature.encode("ascii") + b"\x00" + chunk


def apps_mfcc_comments(
    mtef: bytes,
    *,
    chunk_size: int,
    signature: str = "Design Science, Inc./MTEF",
) -> list[bytes]:
    """Cut MTEF into multiple consecutive AppsMFCC comments."""

    if chunk_size <= 0:
        raise ValueError("AppsMFCC fixture chunk_size must be positive")
    return [
        apps_mfcc_comment(
            mtef[start : start + chunk_size],
            total_length=len(mtef),
            signature=signature,
        )
        for start in range(0, len(mtef), chunk_size)
    ]


def _gif_application_extension(
    payload: bytes,
    *,
    authentication: bytes,
    chunk_size: int,
) -> bytes:
    """Construct MathType GIF Application Extension and sub-blocks."""

    if len(authentication) != 3 or not 0 < chunk_size <= 255:
        raise ValueError("GIF application fixture parameters are invalid")
    subblocks = b"".join(
        bytes([len(payload[start : start + chunk_size])]) + payload[start : start + chunk_size]
        for start in range(0, len(payload), chunk_size)
    )
    return b"\x21\xff\x0bMathType" + authentication + subblocks + b"\x00"


def build_gif_with_extensions(extensions: list[bytes]) -> bytes:
    """Insert extensions before the first image block of a valid 1×1 GIF."""

    image_separator = _TINY_GIF.find(b"\x2c")
    if image_separator < 0:
        raise ValueError("tiny GIF fixture has no image descriptor")
    return _TINY_GIF[:image_separator] + b"".join(extensions) + _TINY_GIF[image_separator:]


def gif_mtef_extension(
    mtef: bytes,
    *,
    chunk_size: int = 255,
) -> bytes:
    """Constructs MathType/001 extension that can be combined into the same GIF."""

    return _gif_application_extension(
        mtef,
        authentication=b"001",
        chunk_size=chunk_size,
    )


def gif_baseline_extension(
    payload: bytes,
    *,
    chunk_size: int = 255,
) -> bytes:
    """Constructs baseline extension which can be combined into the same GIF without producing the formula candidate."""

    return _gif_application_extension(
        payload,
        authentication=b"002",
        chunk_size=chunk_size,
    )


def build_gif_with_mtef(
    mtef: bytes,
    *,
    chunk_size: int = 255,
    include_baseline: bool = False,
) -> bytes:
    """Constructs valid GIF with MathType/001 MTEF and optional 002 baseline."""

    extensions = [gif_mtef_extension(mtef, chunk_size=chunk_size)]
    if include_baseline:
        extensions.insert(
            0,
            _gif_application_extension(
                b"baseline",
                authentication=b"002",
                chunk_size=255,
            ),
        )
    return build_gif_with_extensions(extensions)


def build_baseline_only_gif() -> bytes:
    """Constructs a valid GIF with only MathType/002 baseline and no formula."""

    return build_gif_with_extensions(
        [
            _gif_application_extension(
                b"baseline",
                authentication=b"002",
                chunk_size=255,
            )
        ]
    )
