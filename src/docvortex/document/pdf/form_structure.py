"""Read the actual Form call and member source; the snapshot only saves the own number, not the PDFium address."""

from __future__ import annotations

import ctypes as ct
import math
from typing import Any

import pypdfium2.raw as raw
from pypdf.generic import ContentStream

from ..._compute_backend import get_native
from ...schema import BBox
from .native_contracts import DRAWING_FORM_MAX_DEPTH, _PDFFormInfo
from .native_coordinates import (
    _apply_pdf_matrix,
    _drawing_page_size,
    _get_raw_object_matrix,
    _multiply_pdf_matrices,
    _transform_drawing_point,
)
from .native_objects import (
    _clip_object_visual_bbox,
    _clipped_form_extent,
    _image_bbox_from_matrix,
    _intersect_object_bbox,
    _object_clip_bbox,
    _transform_object_bbox,
)

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _visual_bbox(bounds: BBox, matrix: tuple, page_bbox: BBox, rotation: int) -> BBox:
    """Transform the declaration box or parent coordinate box to page visual coordinates, retaining the uncropped page frame."""
    return _transform_object_bbox(
        bounds, lambda point: _transform_drawing_point(_apply_pdf_matrix(point, matrix), page_bbox, rotation)
    )


def _read_form_declarations(page: Any) -> dict[tuple[int, ...], tuple[BBox, tuple]]:
    """Expand Form only along the actual Do call, distinguishing duplicate calls and isolating corrupt resources and circular references."""
    output: dict[tuple[int, ...], tuple[BBox, tuple]] = {}

    def walk(stream, resources, matrix: tuple, occurrence: tuple, active: frozenset, depth: int) -> None:
        """Propagates q/Q and cm status, assigning an in-container sequence number to each valid Form call."""
        if depth >= DRAWING_FORM_MAX_DEPTH or stream is None:
            return
        stack = []
        form_index = 0
        for operands, operator in ContentStream(stream, page.pdf).operations:
            if operator == b"q":
                stack.append(matrix)
            elif operator == b"Q":
                if not stack:
                    raise ValueError("unbalanced PDF graphics state")
                matrix = stack.pop()
            elif operator == b"cm":
                matrix = _multiply_pdf_matrices(tuple(float(value) for value in operands), matrix)
            elif operator == b"Do":
                objects = resources.get("/XObject", {})
                obj_ref = objects.get(operands[0])
                if obj_ref is None:
                    raise ValueError("missing PDF XObject resource")
                obj = obj_ref.get_object()
                if obj is None:
                    raise ValueError("missing PDF XObject")
                if obj.get("/Subtype") != "/Form":
                    continue
                path = (*occurrence, form_index)
                form_index += 1
                key = (getattr(obj_ref, "idnum", None), getattr(obj_ref, "generation", None), id(obj))
                if key in active:
                    raise ValueError("cyclic PDF Form reference")
                combined = _multiply_pdf_matrices(tuple(float(v) for v in obj.get("/Matrix", _IDENTITY)), matrix)
                bounds = tuple(float(v) for v in obj["/BBox"])
                if len(bounds) != 4 or not all(math.isfinite(v) for v in (*bounds, *combined)):
                    raise ValueError("invalid PDF Form geometry")
                output[path] = (bounds, combined)
                walk(obj, obj.get("/Resources", resources), combined, path, active | {key}, depth + 1)

    walk(page.get_contents(), page.get("/Resources", {}), _IDENTITY, (), frozenset(), 0)
    return output


def _read_char_form_owners(textpage, owners: dict[int, int]) -> list[int | None]:
    """Relate original characters to stable Form numbers while textpage is alive; standard ABI uses Rust for batch reading."""
    getter = raw.FPDFText_GetTextObject
    count = textpage.count_chars()
    native = get_native()
    reader = getattr(native, "read_pdfium_char_form_owners", None)
    if (
        reader is not None
        and isinstance(getter, ct._CFuncPtr)
        and getter.restype is raw.FPDF_PAGEOBJECT
        and tuple(getter.argtypes or ()) == (raw.FPDF_TEXTPAGE, ct.c_int)
        and getattr(getter, "errcheck", None) is None
    ):
        return reader(ct.cast(getter, ct.c_void_p).value, ct.cast(textpage.raw, ct.c_void_p).value, count, owners)
    return [owners.get(ct.cast(getter(textpage.raw, index), ct.c_void_p).value) for index in range(count)]


def extract_form_structure(page, page_bbox: BBox, rotation: int, reader_page: Any | None) -> tuple[_PDFFormInfo, ...]:
    """Collects call trees and members within the current page lock; retains unknown Form if statement match fails, does not guess expansion."""
    forms: list[dict] = []
    text_objects: dict[int, int] = {}
    path_index = 0
    paint_order = 0

    def walk(container, parent_matrix: tuple, ancestor_ids: tuple[int, ...], occurrence: tuple, clip, depth: int) -> None:
        """Establish ancestry and visible boxes in the same depth-first order as existing Path extractions."""
        nonlocal path_index, paint_order
        if depth >= DRAWING_FORM_MAX_DEPTH:
            return
        is_form = bool(ancestor_ids)
        count = raw.FPDFFormObj_CountObjects(container) if is_form else raw.FPDFPage_CountObjects(container)
        form_index = 0
        for index in range(max(0, count)):
            obj = raw.FPDFFormObj_GetObject(container, index) if is_form else raw.FPDFPage_GetObject(container, index)
            matrix = _get_raw_object_matrix(obj) if obj else None
            if matrix is None:
                continue
            combined = _multiply_pdf_matrices(matrix, parent_matrix)
            effective_clip = _object_clip_bbox(obj, parent_matrix, clip)
            kind = raw.FPDFPageObj_GetType(obj)
            order = paint_order
            paint_order += 1
            if kind == raw.FPDF_PAGEOBJ_FORM:
                path = (*occurrence, form_index)
                form_index += 1
                values = [ct.c_float() for _ in range(4)]
                if not raw.FPDFPageObj_GetBounds(obj, *(ct.byref(v) for v in values)):
                    bounds = (0.0, 0.0, 0.0, 0.0)
                else:
                    bounds = tuple(v.value for v in values)
                bbox = _clip_object_visual_bbox(
                    _visual_bbox(bounds, parent_matrix, page_bbox, rotation), effective_clip, page_bbox, rotation
                )
                if not ancestor_ids:
                    clipped = _clipped_form_extent(obj, page_bbox, rotation)
                    if bbox is not None and clipped is not None:
                        bbox = _intersect_object_bbox(bbox, clipped)
                if bbox is not None:
                    bbox = _intersect_object_bbox(bbox, (0.0, 0.0, *_drawing_page_size(page_bbox, rotation)))
                    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                        bbox = None
                instance_id = len(forms)
                forms.append(
                    dict(
                        instance_id=instance_id,
                        parent_id=ancestor_ids[-1] if ancestor_ids else None,
                        occurrence=path,
                        bbox=bbox or (0.0, 0.0, 0.0, 0.0),
                        matrix=combined,
                        clip=effective_clip,
                        paint_order=order,
                        text_indices=set(),
                        path_indices=set(),
                        image_bboxes=[],
                    )
                )
                walk(obj, combined, (*ancestor_ids, instance_id), path, effective_clip, depth + 1)
            elif kind == raw.FPDF_PAGEOBJ_TEXT and ancestor_ids:
                text_objects[ct.cast(obj, ct.c_void_p).value] = ancestor_ids[-1]
            elif kind == raw.FPDF_PAGEOBJ_PATH:
                for ancestor in ancestor_ids:
                    forms[ancestor]["path_indices"].add(path_index)
                path_index += 1
            elif kind == raw.FPDF_PAGEOBJ_IMAGE:
                bbox = _image_bbox_from_matrix(combined, page_bbox, rotation)
                bbox = _clip_object_visual_bbox(bbox, effective_clip, page_bbox, rotation) if bbox is not None else None
                if bbox is not None:
                    for ancestor in ancestor_ids:
                        forms[ancestor]["image_bboxes"].append(bbox)

    walk(page.raw, _IDENTITY, (), (), None, 0)
    if not forms:
        return ()
    textpage = page.get_textpage()
    try:
        for index, owner in enumerate(_read_char_form_owners(textpage, text_objects)):
            while owner is not None:
                forms[owner]["text_indices"].add(index)
                owner = forms[owner]["parent_id"]
    finally:
        textpage.close()
    declarations = {}
    media_bbox = (0.0, 0.0, *page.get_size())
    if reader_page is not None:
        try:
            declarations = _read_form_declarations(reader_page)
            media_bbox = _visual_bbox(tuple(float(v) for v in reader_page.mediabox), _IDENTITY, page_bbox, rotation)
        except Exception:
            # PDF Resource damage only cancels the structure determination and does not affect the restored native content of PDFium.
            declarations = {}
    matched_tree = set(declarations) == {form["occurrence"] for form in forms}
    output = []
    for form in forms:
        declaration = declarations.get(form["occurrence"])
        valid = (
            matched_tree
            and declaration is not None
            and all(math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-3) for a, b in zip(form["matrix"], declaration[1], strict=True))
        )
        declared_bbox = _visual_bbox(declaration[0], declaration[1], page_bbox, rotation) if valid else None
        if declared_bbox is not None:
            # BBox of Form is also implicitly clipped. When upgrading the nested Form, the stroke outside the frame cannot be re-introduced.
            visible_clip = declared_bbox
            if form["clip"] is not None:
                visible_clip = _intersect_object_bbox(visible_clip, _visual_bbox(form["clip"], _IDENTITY, page_bbox, rotation))
            parent_id = form["parent_id"]
            if parent_id is not None and forms[parent_id]["clip"] is not None:
                visible_clip = _intersect_object_bbox(visible_clip, forms[parent_id]["clip"])
            form["clip"] = visible_clip
            form["bbox"] = _intersect_object_bbox(form["bbox"], visible_clip)
            form["image_bboxes"] = [
                clipped
                for bbox in form["image_bboxes"]
                if (clipped := _intersect_object_bbox(bbox, visible_clip))[2] > clipped[0] and clipped[3] > clipped[1]
            ]
        output.append(
            _PDFFormInfo(
                **{key: value for key, value in form.items() if key not in {"text_indices", "path_indices", "image_bboxes"}},
                declared_bbox=declared_bbox,
                media_bbox=media_bbox,
                text_indices=frozenset(form["text_indices"]),
                path_indices=frozenset(form["path_indices"]),
                image_bboxes=tuple(form["image_bboxes"]),
                structure_valid=valid,
            )
        )
    return tuple(output)
