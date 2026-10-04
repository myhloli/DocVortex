"""PDF page coordinates, matrix and extended character geometry, maintaining the native extraction algorithm and resource semantics."""

from __future__ import annotations

import ctypes
import logging
import math
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ...schema import BBox
from .text._contracts import Char

logger = logging.getLogger("docvortex.document.pdf._document")


def _normalize_pdf_page_bbox(bbox: tuple[float, float, float, float]) -> BBox:
    """Standardized PDFium page frame, compatible with test or exception documents whose upper and lower coordinates are in reverse order."""
    x0, y0, x1, y1 = (float(value) for value in bbox)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _drawing_page_size(page_bbox: BBox, page_rotation: int) -> tuple[float, float]:
    """Calculate the page size in the upper-left coordinate system based on the unrotated page box and the page rotation angle."""
    width = page_bbox[2] - page_bbox[0]
    height = page_bbox[3] - page_bbox[1]
    if page_rotation in (90, 270):
        return height, width
    return width, height


def _transform_drawing_point(
    point: tuple[float, float],
    page_bbox: BBox,
    page_rotation: int,
) -> tuple[float, float]:
    """Convert the bottom left origin coordinates of PDF to the upper left origin coordinates after applying page rotation."""
    x, y = point
    left, bottom, right, top = page_bbox
    if page_rotation == 90:
        return y - bottom, x - left
    if page_rotation == 180:
        return right - x, y - bottom
    if page_rotation == 270:
        return top - y, right - x
    return x - left, top - y


def _char_visual_bbox_from_pdfium(
    left: float,
    right: float,
    bottom: float,
    top: float,
    page_bbox: BBox,
    page_rotation: int,
) -> BBox | None:
    """Convert PDFium character user-space box to legal visual page bbox."""
    points = [
        _transform_drawing_point(point, page_bbox, page_rotation)
        for point in (
            (left, bottom),
            (left, top),
            (right, bottom),
            (right, top),
        )
    ]
    bbox = (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    if not all(math.isfinite(value) for value in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox


def _extract_page_char_extended_geometry(
    textpage: pdfium.PdfTextPage,
    chars: list[Char],
    page_bbox: BBox,
    page_rotation: int,
) -> tuple[dict[int, BBox], dict[int, BBox], dict[int, tuple[float, float]]]:
    """Read loose/tight/origin character by character; failure of a single character does not affect the rest of the text."""
    loose_bboxes: dict[int, BBox] = {}
    tight_bboxes: dict[int, BBox] = {}
    origins: dict[int, tuple[float, float]] = {}
    textpage_raw = textpage.raw

    for char in chars:
        raw_char_idx = char.get("char_idx")
        if isinstance(raw_char_idx, bool) or not isinstance(raw_char_idx, int):
            continue

        try:
            char_rotation = float(char.get("rotation") or 0.0)
        except (TypeError, ValueError):
            char_rotation = math.nan
        # pdftext has reserved zero rotation in char["bbox"] loose; side-map only records
        # Explicit overwriting of non-zero rotation or characters of unknown origin to avoid repeated allocation of the entire book bbox tuple.
        if not math.isfinite(char_rotation) or abs(char_rotation) > 1e-9:
            loose_rect = pdfium_c.FS_RECTF()
            try:
                has_loose_bbox = pdfium_c.FPDFText_GetLooseCharBox(
                    textpage_raw,
                    raw_char_idx,
                    loose_rect,
                )
            except Exception:
                has_loose_bbox = False
            if has_loose_bbox:
                loose_bbox = _char_visual_bbox_from_pdfium(
                    float(loose_rect.left),
                    float(loose_rect.right),
                    float(loose_rect.bottom),
                    float(loose_rect.top),
                    page_bbox,
                    page_rotation,
                )
                if loose_bbox is not None:
                    loose_bboxes[raw_char_idx] = loose_bbox

        left = ctypes.c_double()
        right = ctypes.c_double()
        bottom = ctypes.c_double()
        top = ctypes.c_double()
        try:
            has_tight_bbox = pdfium_c.FPDFText_GetCharBox(
                textpage_raw,
                raw_char_idx,
                left,
                right,
                bottom,
                top,
            )
        except Exception:
            has_tight_bbox = False
        if has_tight_bbox:
            tight_bbox = _char_visual_bbox_from_pdfium(
                left.value,
                right.value,
                bottom.value,
                top.value,
                page_bbox,
                page_rotation,
            )
            if tight_bbox is not None:
                tight_bboxes[raw_char_idx] = tight_bbox

        origin_x = ctypes.c_double()
        origin_y = ctypes.c_double()
        try:
            has_origin = pdfium_c.FPDFText_GetCharOrigin(
                textpage_raw,
                raw_char_idx,
                origin_x,
                origin_y,
            )
        except Exception:
            has_origin = False
        if has_origin:
            origin = _transform_drawing_point(
                (origin_x.value, origin_y.value),
                page_bbox,
                page_rotation,
            )
            if all(math.isfinite(value) for value in origin):
                origins[raw_char_idx] = origin

    return loose_bboxes, tight_bboxes, origins


def _multiply_pdf_matrices(
    first: tuple[float, float, float, float, float, float],
    second: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    """Merges the object matrix with the parent Form matrix according to the PDF row vector convention."""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply_pdf_matrix(
    point: tuple[float, float],
    matrix: tuple[float, float, float, float, float, float],
) -> tuple[float, float]:
    """Applies the PDF affine matrix to a waypoint."""
    x, y = point
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _get_raw_object_matrix(raw_obj: Any) -> tuple[float, float, float, float, float, float] | None:
    """Read a raw PDFium page object matrix, and return None when the read fails."""
    matrix = pdfium_c.FS_MATRIX()
    try:
        ok = pdfium_c.FPDFPageObj_GetMatrix(raw_obj, ctypes.byref(matrix))
    except Exception:
        return None
    if not ok:
        return None
    return (
        float(matrix.a),
        float(matrix.b),
        float(matrix.c),
        float(matrix.d),
        float(matrix.e),
        float(matrix.f),
    )
