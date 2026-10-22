"""PDF 字符去重及原始文字几何提取，保持原生提取算法与资源语义。"""

from __future__ import annotations

import logging
from typing import cast

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .native_contracts import (
    PDFPageImage,
    PDFPageTextGeometry,
)
from .native_lifecycle import _try_close
from .native_objects import _text_object_visibility
from .text._contracts import Char
from .text.dedup import deduplicate_chars
from .text.extract import get_chars

logger = logging.getLogger("docvortex.document.pdf._document")


def _restore_pdfium_surrogate_pairs(
    chars: list[Char],
    textpage: pdfium.PdfTextPage,
    *,
    raw_codes: dict[int, int] | None = None,
) -> list[Char]:
    """准备原始 UTF-16 码元后调用共享 Rust 恢复规则，保持代表字符和来源成员。"""
    from ..._compute_backend import get_native

    if not any(
        len(text := str(char.get("char", ""))) == 1 and (text == "\ufffd" or 0xD800 <= ord(text) <= 0xDFFF) for char in chars
    ):
        return chars
    try:
        handle, count = textpage.raw, int(textpage.count_chars())
    except Exception:
        handle, count = None, 0
    if raw_codes is None:
        raw_codes = (
            {index: int(pdfium_c.FPDFText_GetUnicode(handle, index)) for index in range(count)} if handle is not None else {}
        )
    return get_native().restore_surrogate_chars(chars, raw_codes, count)


def _page_to_image(page: pdfium.PdfPage, scale: float, max_edge: int) -> PDFPageImage:
    """按原缩放与长边上限复制页面像素，并返回独立持有的图片。"""
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
    visible_only: bool = False,
    compact: bool = False,
    compact_only: bool = False,
    paint_page_reader=None,
) -> PDFPageTextGeometry | None:
    """在调用方持有的页面和锁内读取字符，使批量提取与独立接口共用实现。"""
    textpage = None
    try:
        raw_page_bbox: list[float] = list(page.get_bbox())
        page_rotation: int = 0
        try:
            page_rotation = page.get_rotation()
        except Exception:
            pass
        visibility = (
            _text_object_visibility(page, tuple(raw_page_bbox), page_rotation, paint_page_reader) if visible_only else None
        )
        textpage = page.get_textpage()
        if compact:
            from .snapshot_bridge import read_text_snapshot

            # 消融未显示稳定收益，恢复由 Python 包装持有 textpage 的原快照入口。
            snapshot = read_text_snapshot(textpage, raw_page_bbox, page_rotation, include_extended_geometry, visibility)
            if snapshot is not None:
                return snapshot
            if compact_only:
                return None
        options = {"visibility_by_object": visibility} if visible_only else {}
        chars = get_chars(textpage, raw_page_bbox, page_rotation, include_geometry=include_extended_geometry, **options)
        raw_codes = {char["char_idx"]: char["raw_code"] for char in chars}
        chars = _restore_pdfium_surrogate_pairs(chars, textpage, raw_codes=raw_codes)
        chars = deduplicate_chars(chars)
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
