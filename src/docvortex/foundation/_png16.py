"""按 PNG 规范恢复多通道十六位采样，避免 Pillow 的八位 RGB 量化。"""

from __future__ import annotations

import struct
import zlib

import numpy as np


def decode_png16(data: bytes) -> np.ndarray | None:
    """恢复 RGB、RGBA 或灰度 alpha 的十六位 PNG，返回原始通道顺序。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 33 or data[24] != 16 or data[25] not in (2, 4, 6):
        return None
    width, height, _, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if compression != 0 or filtering != 0 or interlace not in (0, 1):
        raise ValueError("Unsupported PNG format")
    channels = {2: 3, 4: 2, 6: 4}[color]
    compressed = bytearray()
    position = 8
    while position + 12 <= len(data):
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        if len(payload) != length:
            raise ValueError("Truncated PNG chunk")
        if kind == b"IDAT":
            compressed.extend(payload)
        position += length + 12
        if kind == b"IEND":
            break
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0
        else ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    )
    expected = sum(
        ((max(0, width - x) + dx - 1) // dx * channels * 2 + 1) * ((max(0, height - y) + dy - 1) // dy)
        for x, y, dx, dy in passes
        if x < width and y < height
    )
    decompressor = zlib.decompressobj()
    unpacked = decompressor.decompress(compressed, expected + 1)
    if len(unpacked) != expected or not decompressor.eof:
        raise ValueError("Invalid PNG scanline size")
    result = np.zeros((height, width, channels), np.uint16)
    position = 0
    pixel_bytes = channels * 2
    for x, y, dx, dy in passes:
        pw, ph = (max(0, width - x) + dx - 1) // dx, (max(0, height - y) + dy - 1) // dy
        if pw == 0 or ph == 0:
            continue
        row_bytes = pw * pixel_bytes
        previous = np.zeros(row_bytes, np.uint8)
        for row in range(ph):
            filter_type = unpacked[position]
            scanline = np.frombuffer(unpacked, np.uint8, row_bytes, position + 1).copy()
            position += row_bytes + 1
            if filter_type == 1:
                scanline = np.cumsum(scanline.reshape(pw, pixel_bytes), axis=0, dtype=np.uint64).astype(np.uint8).reshape(-1)
            elif filter_type == 2:
                scanline = (scanline.astype(np.uint16) + previous).astype(np.uint8)
            elif filter_type in (3, 4):
                for column in range(row_bytes):
                    left = int(scanline[column - pixel_bytes]) if column >= pixel_bytes else 0
                    up = int(previous[column])
                    upper_left = int(previous[column - pixel_bytes]) if column >= pixel_bytes else 0
                    if filter_type == 3:
                        predictor = (left + up) // 2
                    else:
                        p = left + up - upper_left
                        pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                        predictor = left if pa <= pb and pa <= pc else up if pb <= pc else upper_left
                    scanline[column] = (int(scanline[column]) + predictor) & 255
            elif filter_type != 0:
                raise ValueError("Invalid PNG filter")
            samples = scanline.view(">u2").astype(np.uint16).reshape(pw, channels)
            result[y + row * dy, x::dx] = samples
            previous = scanline
    return result


__all__ = ["decode_png16"]
