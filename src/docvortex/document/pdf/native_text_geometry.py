"""PDF 字符去重及原始文字几何提取，保持原生提取算法与资源语义。"""

from __future__ import annotations
import logging
import math
from typing import Any, Iterator, cast
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from .text.extract import get_chars, deduplicate_chars
from .text.contracts import Char
from .text.geometry import char_bbox_values as _char_bbox_values

from .native_contracts import (
    NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE,
    OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE,
    OFFSET_DUPLICATE_MIN_BBOX_OVERLAP_RATIO,
    OFFSET_DUPLICATE_TRANSLATION_TOLERANCE,
    PDFPageImage,
    PDFPageTextGeometry,
)
from .native_lifecycle import _try_close

logger = logging.getLogger("docvortex.document.pdf.document")


def _get_visible_char_signature(
    char: Char,
) -> tuple[str, tuple[Any, Any, Any, Any], float]:
    """生成可见字符去重签名，不把 bbox 放入签名以便单独做近重合判断。"""
    font = char.get("font") or {}
    font_key = (
        font.get("name"),
        font.get("flags"),
        font.get("size"),
        font.get("weight"),
    )
    rotation_key = round(float(char.get("rotation") or 0.0), 3)
    return str(char.get("char", "")), font_key, rotation_key


def _is_near_identical_bbox(
    bbox_a: tuple[float, float, float, float],
    bbox_b: tuple[float, float, float, float],
) -> bool:
    """判断两个字符 bbox 是否属于同一视觉位置的一点内抖动。"""
    return all(abs(coord_a - coord_b) <= NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE for coord_a, coord_b in zip(bbox_a, bbox_b))


def _calculate_bbox_overlap_in_smaller_area(
    bbox_a: tuple[float, float, float, float],
    bbox_b: tuple[float, float, float, float],
) -> float:
    """计算两个字符框交集占较小字符框面积的比例。"""
    intersection_width = max(
        0.0,
        min(bbox_a[2], bbox_b[2]) - max(bbox_a[0], bbox_b[0]),
    )
    intersection_height = max(
        0.0,
        min(bbox_a[3], bbox_b[3]) - max(bbox_a[1], bbox_b[1]),
    )
    bbox_a_area = max(0.0, bbox_a[2] - bbox_a[0]) * max(
        0.0,
        bbox_a[3] - bbox_a[1],
    )
    bbox_b_area = max(0.0, bbox_b[2] - bbox_b[0]) * max(
        0.0,
        bbox_b[3] - bbox_b[1],
    )
    smaller_area = min(bbox_a_area, bbox_b_area)
    if smaller_area == 0:
        return 0.0
    return intersection_width * intersection_height / smaller_area


def _is_adjacent_offset_duplicate_char(
    previous_char: Char,
    current_char: Char,
) -> bool:
    """识别相邻字符中由对角平移阴影产生的第二个重复字符。"""
    if _get_visible_char_signature(previous_char) != _get_visible_char_signature(current_char):
        return False

    previous_bbox = _char_bbox_values(previous_char.get("bbox"))
    current_bbox = _char_bbox_values(current_char.get("bbox"))
    if previous_bbox is None or current_bbox is None:
        return False

    x_start_offset = current_bbox[0] - previous_bbox[0]
    y_start_offset = current_bbox[1] - previous_bbox[1]
    x_end_offset = current_bbox[2] - previous_bbox[2]
    y_end_offset = current_bbox[3] - previous_bbox[3]

    # 阴影层应是同一字符框的刚性平移，避免把大小不同的相邻同字误判为重复。
    if (
        abs(x_start_offset - x_end_offset) > OFFSET_DUPLICATE_TRANSLATION_TOLERANCE
        or abs(y_start_offset - y_end_offset) > OFFSET_DUPLICATE_TRANSLATION_TOLERANCE
    ):
        return False

    if not (
        NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE < abs(x_start_offset) <= OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE
        and NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE < abs(y_start_offset) <= OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE
    ):
        return False

    return _calculate_bbox_overlap_in_smaller_area(previous_bbox, current_bbox) >= OFFSET_DUPLICATE_MIN_BBOX_OVERLAP_RATIO


def _get_near_identical_bbox_bucket_key(
    bbox_coords: tuple[float, float, float, float],
) -> tuple[int, int]:
    """按字符 bbox 左上角生成空间桶 key，缩小近重合判断的候选范围。"""
    return (
        math.floor(bbox_coords[0] / NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE),
        math.floor(bbox_coords[1] / NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE),
    )


def _iter_neighbor_bbox_bucket_keys(
    bucket_key: tuple[int, int],
) -> Iterator[tuple[int, int]]:
    """遍历当前桶及周围 8 个邻近桶，覆盖 bbox 容差范围内的候选字符。"""
    bucket_x, bucket_y = bucket_key
    for offset_x in (-1, 0, 1):
        for offset_y in (-1, 0, 1):
            yield bucket_x + offset_x, bucket_y + offset_y


def _deduplicate_near_identical_chars(chars: list[Char]) -> list[Char]:
    """清理 PDFium 文本层边界处同字符、同位置及对角阴影重复字符。"""
    seen_visible_char_bboxes: dict[
        tuple[str, tuple[Any, Any, Any, Any], float],
        dict[tuple[int, int], list[tuple[float, float, float, float]]],
    ] = {}
    deduplicated_chars: list[Char] = []

    for char in chars:
        text = str(char.get("char", ""))
        if not text or text.isspace():
            deduplicated_chars.append(char)
            continue

        visible_char_key = _get_visible_char_signature(char)
        bbox_coords = _char_bbox_values(char.get("bbox"))
        if bbox_coords is None:
            deduplicated_chars.append(char)
            continue

        if deduplicated_chars and _is_adjacent_offset_duplicate_char(
            deduplicated_chars[-1],
            char,
        ):
            continue

        bbox_bucket_key = _get_near_identical_bbox_bucket_key(bbox_coords)
        visible_char_bbox_buckets = seen_visible_char_bboxes.setdefault(
            visible_char_key,
            {},
        )
        if any(
            _is_near_identical_bbox(bbox_coords, seen_bbox)
            for neighbor_bucket_key in _iter_neighbor_bbox_bucket_keys(bbox_bucket_key)
            for seen_bbox in visible_char_bbox_buckets.get(neighbor_bucket_key, [])
        ):
            continue

        visible_char_bbox_buckets.setdefault(bbox_bucket_key, []).append(bbox_coords)
        deduplicated_chars.append(char)

    return deduplicated_chars


def _restore_pdfium_surrogate_pairs(
    chars: list[Char],
    textpage: pdfium.PdfTextPage,
    *,
    raw_codes: dict[int, int] | None = None,
) -> list[Char]:
    """利用 PDFium 原始 UTF-16 code unit 恢复 pdftext 丢失的补充平面字符。"""
    if not any(
        len(text := str(char.get("char", ""))) == 1 and (text == "\ufffd" or 0xD800 <= ord(text) <= 0xDFFF) for char in chars
    ):
        return chars

    try:
        textpage_raw = textpage.raw
        char_count = int(textpage.count_chars())
    except Exception:
        textpage_raw = None
        char_count = 0

    restored_chars: list[Char] = []
    consumed_char_indices: set[int] = set()

    def get_unicode(handle: object, index: int) -> int:
        """优先复用首次读取的原始码值，仅为独立辅助调用读取原生接口。"""
        return raw_codes[index] if raw_codes is not None else int(pdfium_c.FPDFText_GetUnicode(handle, index))

    for char in chars:
        text = str(char.get("char", ""))
        raw_char_idx = char.get("char_idx")
        try:
            char_idx = int(raw_char_idx) if raw_char_idx is not None else -1
        except (TypeError, ValueError):
            char_idx = -1

        if char_idx in consumed_char_indices:
            continue

        raw_code = None
        if (
            textpage_raw is not None
            and 0 <= char_idx < char_count
            and len(text) == 1
            and (text == "\ufffd" or 0xD800 <= ord(text) <= 0xDFFF)
        ):
            raw_code = int(get_unicode(textpage_raw, char_idx))

        high_surrogate = None
        low_surrogate = None
        if raw_code is not None and 0xD800 <= raw_code <= 0xDBFF and char_idx + 1 < char_count:
            next_code = int(get_unicode(textpage_raw, char_idx + 1))
            if 0xDC00 <= next_code <= 0xDFFF:
                high_surrogate = raw_code
                low_surrogate = next_code
                consumed_char_indices.add(char_idx + 1)
        elif raw_code is not None and 0xDC00 <= raw_code <= 0xDFFF and char_idx > 0:
            previous_code = int(get_unicode(textpage_raw, char_idx - 1))
            if 0xD800 <= previous_code <= 0xDBFF:
                high_surrogate = previous_code
                low_surrogate = raw_code

        if high_surrogate is not None and low_surrogate is not None:
            restored_char = cast(Char, dict(char))
            restored_char["char"] = chr(0x10000 + ((high_surrogate - 0xD800) << 10) + (low_surrogate - 0xDC00))
            restored_char["source_indices"] = tuple(
                sorted(
                    set(
                        (
                            *char.get("source_indices", (char_idx,)),
                            char_idx + 1 if raw_code is not None and raw_code <= 0xDBFF else char_idx - 1,
                        )
                    )
                )
            )
            restored_chars.append(restored_char)
            continue

        if len(text) == 1 and 0xD800 <= ord(text) <= 0xDFFF:
            restored_char = cast(Char, dict(char))
            restored_char["char"] = "\ufffd"
            restored_chars.append(restored_char)
            continue

        restored_chars.append(char)

    return restored_chars


def _page_to_image(page: pdfium.PdfPage, scale: float, max_edge: int) -> PDFPageImage:
    long_edge_length = max(*page.get_size())
    if (long_edge_length * scale) > max_edge:
        scale = max_edge / long_edge_length

    bitmap = None
    try:
        bitmap = page.render(scale=scale)  # type: ignore
        bitmap = cast(pdfium.PdfBitmap, bitmap)
        pil_image = bitmap.to_pil()
    finally:
        _try_close(bitmap)

    return PDFPageImage(pil_image=pil_image, scale=scale)


def _extract_page_text_geometry(
    page: pdfium.PdfPage,
    *,
    include_extended_geometry: bool,
) -> PDFPageTextGeometry:
    """在调用方持有的页面和锁内读取字符，使批量提取与独立接口共用实现。"""
    textpage = None
    try:
        textpage = page.get_textpage()
        raw_page_bbox: list[float] = list(page.get_bbox())
        page_rotation: int = 0
        try:
            page_rotation = page.get_rotation()
        except Exception:
            pass
        chars = get_chars(textpage, raw_page_bbox, page_rotation, include_geometry=include_extended_geometry)
        raw_codes = {char["char_idx"]: char["raw_code"] for char in chars}
        chars = deduplicate_chars(chars)
        chars = _restore_pdfium_surrogate_pairs(chars, textpage, raw_codes=raw_codes)
        chars = _deduplicate_near_identical_chars(chars)
        if include_extended_geometry:
            loose_bboxes = {
                char["char_idx"]: char["loose_bbox"]
                for char in chars
                if char.get("loose_bbox") is not None and abs(char["rotation"]) > 1e-9
            }
            tight_bboxes = {char["char_idx"]: char["tight_bbox"] for char in chars if char.get("tight_bbox") is not None}
            origins = {char["char_idx"]: char["origin"] for char in chars if char.get("origin") is not None}
        else:
            loose_bboxes, tight_bboxes, origins = {}, {}, {}
    finally:
        _try_close(textpage)
    return PDFPageTextGeometry(
        chars=chars,
        tight_bboxes=tight_bboxes,
        origins=origins,
        loose_bboxes=loose_bboxes,
    )
