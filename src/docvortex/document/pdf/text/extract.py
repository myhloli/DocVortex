# Portions derived from pdftext 0.7.1, Copyright Vik Paruchuri, Apache-2.0.
# Changed in DocVortex: direct PDFium extraction collects character and extended geometry together.
"""在一次 PDFium 字符遍历内收集原始码值、字体及可选几何。"""

from __future__ import annotations

from ctypes import byref, c_double, c_int, create_string_buffer
import math
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from .contracts import Bbox, Char


def transform_point(
    point: tuple[float, float], page_bbox: tuple[float, float, float, float], rotation: int
) -> tuple[float, float]:
    """保留浮点页面框，将 PDF 原始坐标转换到视觉页面坐标。"""
    left, bottom, right, top = page_bbox
    width, height = abs(right - left), abs(top - bottom)
    x, y = point[0] - min(left, right), max(bottom, top) - point[1]
    rotation %= 360
    if rotation == 90:
        return height - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, width - x
    return x, y


def visual_bbox(
    box: tuple[float, float, float, float], page_bbox: tuple[float, float, float, float], rotation: int
) -> tuple[float, float, float, float] | None:
    """转换原始矩形，几何缺失或零面积时返回空值而非伪造坐标。"""
    left, bottom, right, top = box
    points = [
        transform_point(point, page_bbox, rotation) for point in ((left, bottom), (left, top), (right, bottom), (right, top))
    ]
    result = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
    return result if all(math.isfinite(v) for v in result) and result[2] > result[0] and result[3] > result[1] else None


def _font_name(handle: Any, index: int, buffer: Any, flags: c_int) -> tuple[str, int]:
    """复用字体缓冲区，超长名称按 PDFium 返回长度重新读取。"""
    try:
        length = raw.FPDFText_GetFontInfo(handle, index, buffer, len(buffer), byref(flags))
        if length > len(buffer):
            buffer = create_string_buffer(length)
            raw.FPDFText_GetFontInfo(handle, index, buffer, length, byref(flags))
        return (buffer.value.decode("utf-8", errors="replace"), flags.value) if length > 0 else ("", 0)
    except pdfium.PdfiumError:
        return "", 0


def get_chars(
    textpage: pdfium.PdfTextPage, page_bbox: list[float], page_rotation: int, *, include_geometry: bool = False
) -> list[Char]:
    """读取原始字符记录；原始码值始终保留，随后统一解码和去重。"""
    handle = textpage.raw
    left, bottom, right, top = page_bbox
    width, height = math.ceil(abs(right - left)), math.ceil(abs(top - bottom))
    rect = raw.FS_RECTF()
    tight_left, tight_right, tight_bottom, tight_top = c_double(), c_double(), c_double(), c_double()
    origin_x, origin_y = c_double(), c_double()
    font_buffer, font_flags = create_string_buffer(256), c_int()
    fonts: dict[tuple[Any, ...], dict[str, Any]] = {}
    chars: list[Char] = []
    for index in range(textpage.count_chars()):
        code = int(raw.FPDFText_GetUnicode(handle, index))
        rotation = float(raw.FPDFText_GetCharAngle(handle, index))
        loose: tuple[float, float, float, float] | None = None
        tight: tuple[float, float, float, float] | None = None
        if rotation == 0 or include_geometry:
            try:
                if raw.FPDFText_GetLooseCharBox(handle, index, rect):
                    loose = (float(rect.left), float(rect.bottom), float(rect.right), float(rect.top))
            except Exception:
                if rotation == 0:
                    raise
        if rotation != 0 or include_geometry:
            try:
                if raw.FPDFText_GetCharBox(handle, index, tight_left, tight_right, tight_bottom, tight_top):
                    tight = (tight_left.value, tight_bottom.value, tight_right.value, tight_top.value)
            except Exception:
                if rotation != 0:
                    raise
        selected = loose if rotation == 0 else tight
        if selected is None:
            raise pdfium.PdfiumError("Failed to get charbox.")
        x0, y0, x1, y1 = selected
        # 布局框保留基线的整数页面高度，原始扩展几何另用浮点页面框。
        ys = (height - (y0 - bottom), height - (y1 - bottom))
        box = Bbox([min(x0, x1) - left, min(ys), max(x0, x1) - left, max(ys)])
        if page_rotation:
            box = box.rotate(width, height, page_rotation)
        name, flags = _font_name(handle, index, font_buffer, font_flags)
        size, weight = raw.FPDFText_GetFontSize(handle, index), raw.FPDFText_GetFontWeight(handle, index)
        font = fonts.setdefault((name, flags, size, weight), {"name": name, "flags": flags, "size": size, "weight": weight})
        char: Char = {
            "bbox": box,
            "char": chr(code) if not 0xD800 <= code <= 0xDFFF else "\ufffd",
            "rotation": rotation,
            "font": font,
            "char_idx": index,
            "source_indices": (index,),
            "raw_code": code,
        }
        if include_geometry:
            char["loose_bbox"] = visual_bbox(loose, tuple(page_bbox), page_rotation) if loose else None
            char["tight_bbox"] = visual_bbox(tight, tuple(page_bbox), page_rotation) if tight else None
            char["origin"] = None
            try:
                if raw.FPDFText_GetCharOrigin(handle, index, origin_x, origin_y):
                    origin = transform_point((origin_x.value, origin_y.value), tuple(page_bbox), page_rotation)
                    if all(math.isfinite(v) for v in origin):
                        char["origin"] = origin
            except Exception:
                pass
        chars.append(char)
    return chars


def deduplicate_chars(chars: list[Char]) -> list[Char]:
    """按词文本、字体、方向及取整位置去重，保留最早原始索引。"""
    if not chars:
        return []
    groups: list[list[Char]] = [[chars[0]]]
    for char in chars[1:]:
        previous = groups[-1][-1]
        if (
            previous["char"] in {"\x02", "\n", " "}
            or char["font"] != previous["font"]
            or char["rotation"] != previous["rotation"]
        ):
            groups.append([])
        groups[-1].append(char)
    seen: dict[tuple[Any, ...], list[Char]] = {}
    result: list[Char] = []
    for group in groups:
        box = group[0]["bbox"].copy()
        for char in group[1:]:
            box.merge_inplace(char["bbox"])
        font = group[0]["font"]
        key = (
            tuple(round(float(v), 0) for v in box.bbox),
            "".join(c["char"] for c in group),
            group[0]["rotation"],
            tuple(font.get(k) for k in ("name", "flags", "size", "weight")),
        )
        if key not in seen:
            seen[key] = group
            result.extend(group)
        else:
            for retained, duplicate in zip(seen[key], group):
                retained["source_indices"] = (
                    *retained.get("source_indices", (retained["char_idx"],)),
                    *duplicate.get("source_indices", (duplicate["char_idx"],)),
                )
    return result


__all__ = ["transform_point", "visual_bbox"]
