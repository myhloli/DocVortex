"""PDF 块级布局所需的可移植页面几何扩展，不保存原生句柄。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING

from ...foundation._pdf_layout import LAYOUT_EXTENSION, normalize_layout_dimensions, round_layout_dimension
from ...result import Diagnostic
from ...schema import MiddleJson

if TYPE_CHECKING:
    from ._document import PDFDocument


def remap_layout_geometry(extension: dict, page_indices: list[int] | None) -> dict:
    """复制局部页几何并映射为源页号，保留空白页与图片方向字段。"""
    copied = deepcopy(extension)
    if page_indices:
        for page in copied["pages"]:
            local_idx = page["page_idx"]
            if not 0 <= local_idx < len(page_indices):
                raise ValueError(f"Layout page_idx={local_idx} is outside page_index_map")
            page["page_idx"] = page_indices[local_idx]
    normalize_layout_dimensions(copied)
    return copied


def merge_layout_extensions(previous: dict, current: dict, current_page_indices: list[int] | None = None) -> dict:
    """合并同源批次几何；重复页以后一批为准，其余产品扩展必须一致。"""
    left, right = deepcopy(previous), deepcopy(current)
    left_layout, right_layout = left.pop(LAYOUT_EXTENSION, None), right.pop(LAYOUT_EXTENSION, None)
    if json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
        raise ValueError("Incompatible document extensions")
    layouts = [item for item in (left_layout, right_layout) if item is not None]
    if layouts:
        pages = {}
        for layout in layouts:
            if not isinstance(layout, dict) or type(layout.get("version")) is not int or layout["version"] != 1:
                raise ValueError("Unsupported layout extension in batch")
            if layout is right_layout and current_page_indices is not None:
                for idx in current_page_indices:
                    pages.pop(idx, None)
            for page in layout["pages"]:
                if layout is not right_layout or current_page_indices is None or page["page_idx"] in current_page_indices:
                    pages[page["page_idx"]] = page
        if right_layout is None and current_page_indices is not None:
            for idx in current_page_indices:
                pages.pop(idx, None)
        left[LAYOUT_EXTENSION] = {"version": 1, "pages": [pages[idx] for idx in sorted(pages)]}
        normalize_layout_dimensions(left[LAYOUT_EXTENSION])
    return left


def extract_layout_geometry(document: PDFDocument, page_indices: list[int] | None) -> tuple[dict, tuple[Diagnostic, ...]]:
    """记录已选页面的可见尺寸，并把局部页号映射回源文档页号。"""
    indices = list(range(document.page_count)) if page_indices is None else page_indices
    pages: list[dict] = []
    diagnostics: list[Diagnostic] = []
    for local_index, page_index in enumerate(indices):
        try:
            raw_width, raw_height = document.page_size(local_index)
            width = round_layout_dimension(raw_width)
            height = round_layout_dimension(raw_height)
            if not width or not height:
                raise ValueError("page dimensions must round to positive integer points")
        except Exception as exc:
            diagnostics.append(Diagnostic("pdf_layout_geometry_unavailable", f"page_idx={page_index}: {exc}", page_index))
            continue
        pages.append({"page_idx": page_index, "width_pt": width, "height_pt": height})
    return {"version": 1, "pages": pages}, tuple(diagnostics)


def read_layout_geometry(middle: MiddleJson) -> dict[int, tuple[int, int]]:
    """严格读取扩展并验证全部输出页的尺寸及顶层定位，不猜测历史页尺寸。"""
    extension = middle.extensions.get(LAYOUT_EXTENSION)
    if not isinstance(extension, dict):
        raise ValueError(f"Missing extensions.{LAYOUT_EXTENSION}; reparse the source PDF")
    if type(extension.get("version")) is not int or extension["version"] != 1:
        raise ValueError(f"Unsupported {LAYOUT_EXTENSION} version")
    pages = extension.get("pages")
    if not isinstance(pages, list):
        raise ValueError(f"{LAYOUT_EXTENSION}.pages must be a list")
    sizes: dict[int, tuple[int, int]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("Layout page geometry must be an object")
        page_idx = page.get("page_idx")
        if type(page_idx) is not int or page_idx < 0 or page_idx in sizes:
            raise ValueError(f"Invalid or duplicate layout page_idx={page_idx}")
        width = round_layout_dimension(page.get("width_pt"))
        height = round_layout_dimension(page.get("height_pt"))
        if not width or not height:
            raise ValueError(f"Invalid layout page dimensions: page_idx={page_idx}")
        sizes[page_idx] = (width, height)
        rotations = page.get("image_rotations", {})
        if not isinstance(rotations, dict) or any(
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or type(angle) is not int
            or angle not in (0, 90, 180, 270)
            for key, angle in rotations.items()
        ):
            raise ValueError(f"Invalid image rotations: page_idx={page_idx}")
    for page in middle.pages:
        if page.page_idx not in sizes:
            raise ValueError(f"Missing layout page dimensions: page_idx={page.page_idx}")
        for block in page.blocks:
            if block.bbox is None:
                raise ValueError(f"Missing layout bbox: page_idx={page.page_idx}, block_index={block.index}")
    return sizes


def attach_layout_image_rotations(extension: dict, model_pages: list[list[dict]], page_indices: list[int] | None) -> None:
    """记录现有裁图转正时使用的角度，使原布局能恢复图片方向而不复制素材。"""
    from .constants import MODEL_JSON_VISUAL_BLOCK_TYPES

    geometry_pages = {page["page_idx"]: page for page in extension["pages"]}
    indices = list(range(len(model_pages))) if page_indices is None else page_indices
    for page_idx, blocks in zip(indices, model_pages, strict=True):
        rotations = {
            str(index): block["angle"]
            for index, block in enumerate(blocks)
            if block.get("type") in MODEL_JSON_VISUAL_BLOCK_TYPES
            and block.get("image_base64")
            and block.get("angle") in (90, 180, 270)
        }
        if rotations and page_idx in geometry_pages:
            geometry_pages[page_idx]["image_rotations"] = rotations


__all__ = [
    "LAYOUT_EXTENSION",
    "extract_layout_geometry",
    "attach_layout_image_rotations",
    "remap_layout_geometry",
    "merge_layout_extensions",
    "read_layout_geometry",
]
