"""PDF Portable page geometry extension required for block-level layout, does not save native handles."""

from __future__ import annotations

import math
import json
from copy import deepcopy
from typing import TYPE_CHECKING

from ...result import Diagnostic
from ...schema import MiddleJson

if TYPE_CHECKING:
    from ._document import PDFDocument


LAYOUT_EXTENSION = "docvortex_layout"


def remap_layout_geometry(extension: dict, page_indices: list[int] | None) -> dict:
    """Copy the local page geometry and map it to the source page number, retaining the blank page and image orientation fields."""
    copied = deepcopy(extension)
    if page_indices:
        for page in copied["pages"]:
            local_idx = page["page_idx"]
            if not 0 <= local_idx < len(page_indices):
                raise ValueError(f"Layout page_idx={local_idx} is outside page_index_map")
            page["page_idx"] = page_indices[local_idx]
    return copied


def merge_layout_extensions(previous: dict, current: dict, current_page_indices: list[int] | None = None) -> dict:
    """Merge homologous batch geometries; duplicate pages shall be subject to the later batch, and other product extensions must be consistent."""
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
    return left


def extract_layout_geometry(document: PDFDocument, page_indices: list[int] | None) -> tuple[dict, tuple[Diagnostic, ...]]:
    """Records the visible dimensions of the selected page and maps local page numbers back to the source document page numbers."""
    indices = list(range(document.page_count)) if page_indices is None else page_indices
    pages: list[dict] = []
    diagnostics: list[Diagnostic] = []
    for local_index, page_index in enumerate(indices):
        try:
            width, height = document.page_size(local_index)
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError("page dimensions must be finite and positive")
        except Exception as exc:
            diagnostics.append(Diagnostic("pdf_layout_geometry_unavailable", f"page_idx={page_index}: {exc}", page_index))
            continue
        pages.append({"page_idx": page_index, "width_pt": width, "height_pt": height})
    return {"version": 1, "pages": pages}, tuple(diagnostics)


def read_layout_geometry(middle: MiddleJson) -> dict[int, tuple[float, float]]:
    """Strictly read extensions and verify the size and top-level positioning of all output pages, without guessing historical page sizes."""
    extension = middle.extensions.get(LAYOUT_EXTENSION)
    if not isinstance(extension, dict):
        raise ValueError(f"Missing extensions.{LAYOUT_EXTENSION}; reparse the source PDF")
    if type(extension.get("version")) is not int or extension["version"] != 1:
        raise ValueError(f"Unsupported {LAYOUT_EXTENSION} version")
    pages = extension.get("pages")
    if not isinstance(pages, list):
        raise ValueError(f"{LAYOUT_EXTENSION}.pages must be a list")
    sizes: dict[int, tuple[float, float]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("Layout page geometry must be an object")
        page_idx = page.get("page_idx")
        if type(page_idx) is not int or page_idx < 0 or page_idx in sizes:
            raise ValueError(f"Invalid or duplicate layout page_idx={page_idx}")
        dimensions = (page.get("width_pt"), page.get("height_pt"))
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in dimensions):
            raise ValueError(f"Invalid layout page dimensions: page_idx={page_idx}")
        sizes[page_idx] = (float(dimensions[0]), float(dimensions[1]))
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
    """Record the angle used when converting an existing crop, so that the original layout can restore the image orientation without duplicating the material."""
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
