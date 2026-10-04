# Portions derived from pdftext 0.7.1, Copyright Vik Paruchuri, Apache-2.0.
# Changed in DocVortex: direct PDFium extraction collects character and extended geometry together.
"""Collects source code values, fonts, and optional geometry within a PDFium character pass."""

from __future__ import annotations

import math
from ctypes import byref, c_double, c_int, c_uint, c_void_p, cast, create_string_buffer
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ._contracts import Bbox, Char
from ...._compute_backend import get_native


def transform_point(
    point: tuple[float, float], page_bbox: tuple[float, float, float, float], rotation: int
) -> tuple[float, float]:
    """Preserve the floating point page box and convert PDF raw coordinates to visual page coordinates."""
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
    """Converts the original rectangle and returns null instead of fake coordinates when the geometry is missing or has zero area."""
    left, bottom, right, top = box
    points = [
        transform_point(point, page_bbox, rotation) for point in ((left, bottom), (left, top), (right, bottom), (right, top))
    ]
    result = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
    return result if all(math.isfinite(v) for v in result) and result[2] > result[0] and result[3] > result[1] else None


def _font_name(handle: Any, index: int, buffer: Any, flags: c_int) -> tuple[str, int]:
    """The font buffer is reused, and overlong names are re-read according to the return length of PDFium."""
    try:
        length = raw.FPDFText_GetFontInfo(handle, index, buffer, len(buffer), byref(flags))
        if length > len(buffer):
            buffer = create_string_buffer(length)
            raw.FPDFText_GetFontInfo(handle, index, buffer, length, byref(flags))
        return (buffer.value.decode("utf-8", errors="replace"), flags.value) if length > 0 else ("", 0)
    except pdfium.PdfiumError:
        return "", 0


def _assign_writing_angles(chars: list[Char]) -> None:
    """Advance the writing direction from the origin of the same object to avoid mistaking the italic shear angle for baseline rotation."""
    start = 0
    while start < len(chars):
        end = start + 1
        object_id = chars[start].get("text_object_id")
        if object_id is None:
            start = end
            continue
        while end < len(chars) and chars[end].get("text_object_id") == object_id:
            end += 1
        origin = chars[start].get("origin")
        if origin is not None:
            for index in range(start + 1, end):
                following = chars[index].get("origin")
                if following is None:
                    continue
                dx, dy = following[0] - origin[0], following[1] - origin[1]
                if math.hypot(dx, dy) > 0.001:
                    angle = math.atan2(dy, dx)
                    for char in chars[start:end]:
                        char["writing_angle"] = angle
                    break
        start = end


def _mark_visible_objects(chars: list[Char], handle: Any) -> None:
    """Only read transparency on pages with hidden text to prevent transparent text from becoming visible corresponding content."""
    if not any(char.get("text_render_mode") == 3 for char in chars):
        return
    visibility: dict[int | None, bool] = {None: False}
    red, green, blue, alpha = c_uint(), c_uint(), c_uint(), c_uint()
    for char in chars:
        object_id = char.get("text_object_id")
        if object_id not in visibility:
            mode = char.get("text_render_mode")
            visible = False
            readers = []
            if mode in (0, 2, 4, 6):
                readers.append(raw.FPDFText_GetFillColor)
            if mode in (1, 2, 5, 6):
                readers.append(raw.FPDFText_GetStrokeColor)
            for reader in readers:
                try:
                    if (
                        reader(handle, char["char_idx"], byref(red), byref(green), byref(blue), byref(alpha))
                        and alpha.value > 0
                    ):
                        visible = True
                except Exception:
                    pass
            visibility[object_id] = visible
        char["text_is_visible"] = visibility[object_id]


def _native_visibility_supported(visibility):
    """Only ordinary non-executable visibility data is rearranged, special mappings and values retain the access timing of the reference path."""
    if visibility is None:
        return True
    if type(visibility) is not dict:
        return False
    return all(
        (key is None or type(key) is int)
        and type(value) is tuple
        and len(value) == 2
        and type(value[0]) is bool
        and (
            value[1] is None
            or (
                type(value[1]) is tuple
                and len(value[1]) == 4
                and all(type(coordinate) is float and math.isfinite(coordinate) for coordinate in value[1])
            )
        )
        for key, value in visibility.items()
    )


def get_chars(
    textpage: pdfium.PdfTextPage,
    page_bbox: list[float],
    page_rotation: int,
    *,
    include_geometry: bool = False,
    visibility_by_object: dict[int, tuple[bool, tuple[float, float, float, float] | None]] | None = None,
) -> list[Char]:
    """Ordinary pages are read in batches using the same library characters, and the complete ctypes reference path is retained during special operations."""
    if (
        type(page_rotation) is int
        and page_rotation in (0, 90, 180, 270)
        and type(page_bbox) is list
        and len(page_bbox) == 4
        and all(type(value) is float and math.isfinite(value) for value in page_bbox)
        and get_native() is not None
        and _native_visibility_supported(visibility_by_object)
    ):
        result = _get_chars_native(textpage, page_bbox, page_rotation, include_geometry, visibility_by_object)
        if result is not None:
            return result
    return _get_chars_python(
        textpage,
        page_bbox,
        page_rotation,
        include_geometry=include_geometry,
        visibility_by_object=visibility_by_object,
    )


def _get_chars_native(textpage, page_bbox, page_rotation, include_geometry, visibility_by_object):
    """Materialize records that have been read in batches, preserving font sharing, in-page object numbering, clipping, and write direction semantics."""
    from ._pdfium_bridge import read_native_chars

    batch = read_native_chars(textpage, include_geometry, frame=page_bbox, rotation=page_rotation)
    if batch is None:
        return None
    records, raw_fonts = batch
    # Empty snapshots no longer construct font maps or call empty geometry batches, and still maintain the normal list return contract.
    if type(records) in (tuple, list) and not records and type(raw_fonts) in (tuple, list) and not raw_fonts:
        return []
    decoded_fonts = [(bytes(name).decode("utf-8", errors="replace"), flags) for name, flags in raw_fonts]
    fonts, objects = {}, {}
    retained = []
    writing_rotation = math.radians(page_rotation)
    last_font_index = last_size = last_weight = last_font = None
    last_address = last_object_id = None
    for index, (code, rotation, font_index, size, weight, address, mode, layout, loose, tight, origin) in enumerate(records):
        # The reading bridge materializes records batch by batch according to a fixed upper limit, and the consumed batches do not overlap with the final characters of the entire page for a long time.
        if (
            type(font_index) is int
            and type(size) is float
            and type(weight) is int
            and font_index == last_font_index
            and size == last_size
            and weight == last_weight
        ):
            font = last_font
        else:
            name, flags = decoded_fonts[font_index]
            key = (name, flags, size, weight)
            font = fonts.get(key)
            if font is None:
                font = fonts[key] = {"name": name, "flags": flags, "size": size, "weight": weight}
            if type(font_index) is int and type(size) is float and type(weight) is int:
                last_font_index, last_size, last_weight, last_font = font_index, size, weight, font
            else:
                last_font_index = None
        object_id = None
        if address:
            if type(address) is int and address == last_address:
                object_id = last_object_id
            else:
                object_id = objects.get(address)
                if object_id is None:
                    object_id = objects[address] = len(objects)
                if type(address) is int:
                    last_address, last_object_id = address, object_id
                else:
                    last_address = None
        clip = None
        if visibility_by_object is not None and (address or None) in visibility_by_object:
            visible, clip = visibility_by_object[address or None]
            if not visible:
                continue
        char = {
            "bbox": Bbox(layout),
            "char": chr(code) if not 0xD800 <= code <= 0xDFFF else "\ufffd",
            "rotation": rotation,
            "font": font,
            "char_idx": index,
            "source_indices": (index,),
            "raw_code": code,
            "text_object_id": object_id,
            "text_render_mode": mode,
            "writing_angle": writing_rotation - rotation,
            "origin": origin,
        }
        if include_geometry:
            char["loose_bbox"], char["tight_bbox"] = loose, tight
        if clip is None or _clip_visible_character(char, clip):
            retained.append(char)
    del batch, records, raw_fonts
    _assign_writing_angles(retained)
    _mark_visible_objects(retained, textpage.raw)
    return retained


def _materialize_native_char_batch(chars, raw_geometry, pending_clips, page_bbox, page_size, page_rotation, include_geometry):
    """Bounded batches complete independent coordinate transformation and cropping; fonts, source numbers and writing directions are still processed within the entire page."""
    prepared = get_native().materialize_geometry(raw_geometry, page_bbox, page_size, page_rotation)
    retained = []
    for char, (layout, loose, tight, origin), clip in zip(chars, prepared, pending_clips, strict=True):
        char["bbox"] = Bbox(layout)
        char["origin"] = origin
        if include_geometry:
            char["loose_bbox"], char["tight_bbox"] = loose, tight
        if clip is None or _clip_visible_character(char, clip):
            retained.append(char)
    return retained


def _get_chars_python(
    textpage: pdfium.PdfTextPage,
    page_bbox: list[float],
    page_rotation: int,
    *,
    include_geometry: bool = False,
    visibility_by_object: dict[int, tuple[bool, tuple[float, float, float, float] | None]] | None = None,
) -> list[Char]:
    """Read the original character record; the original code value is always retained, and is subsequently decoded and deduplicated uniformly."""
    handle = textpage.raw
    left, bottom, right, top = page_bbox
    width, height = math.ceil(abs(right - left)), math.ceil(abs(top - bottom))
    rect = raw.FS_RECTF()
    tight_left, tight_right, tight_bottom, tight_top = c_double(), c_double(), c_double(), c_double()
    origin_x, origin_y = c_double(), c_double()
    font_buffer, font_flags = create_string_buffer(256), c_int()
    fonts: dict[tuple[Any, ...], dict[str, Any]] = {}
    objects: dict[int, tuple[int, int]] = {}
    chars: list[Char] = []
    native = get_native() if page_rotation in (0, 90, 180, 270) else None
    raw_geometry = []
    pending_clips = []
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
        box = None
        if native is None:
            x0, y0, x1, y1 = selected
            # The layout box retains the integer page height of the baseline, and the original expanded geometry uses a floating point page box.
            ys = (height - (y0 - bottom), height - (y1 - bottom))
            box = Bbox([min(x0, x1) - left, min(ys), max(x0, x1) - left, max(ys)])
            if page_rotation:
                box = box.rotate(width, height, page_rotation)
        name, flags = _font_name(handle, index, font_buffer, font_flags)
        size, weight = raw.FPDFText_GetFontSize(handle, index), raw.FPDFText_GetFontWeight(handle, index)
        key = (name, flags, size, weight)
        font = fonts.get(key)
        if font is None:
            font = fonts[key] = {"name": name, "flags": flags, "size": size, "weight": weight}
        char: Char = {
            "bbox": box,
            "char": chr(code) if not 0xD800 <= code <= 0xDFFF else "\ufffd",
            "rotation": rotation,
            "font": font,
            "char_idx": index,
            "source_indices": (index,),
            "raw_code": code,
            "text_object_id": None,
            "text_render_mode": None,
            "writing_angle": math.radians(page_rotation) - rotation,
            "origin": None,
        }
        # The address key is only held during the current fetch, the output uses integer numbers within the page, and the native handle is not saved.
        address = None
        try:
            obj = raw.FPDFText_GetTextObject(handle, index)
            address = cast(obj, c_void_p).value
            if address:
                if address not in objects:
                    objects[address] = (len(objects), int(raw.FPDFTextObj_GetTextRenderMode(obj)))
                char["text_object_id"], char["text_render_mode"] = objects[address]
        except Exception:
            pass
        # Both extraction entries require an origin to distinguish between multi-coded values of a single glyph and independent repeated drawings.
        raw_origin = None
        try:
            if raw.FPDFText_GetCharOrigin(handle, index, origin_x, origin_y):
                raw_origin = (origin_x.value, origin_y.value)
                if native is None:
                    origin = transform_point(raw_origin, tuple(page_bbox), page_rotation)
                    if all(math.isfinite(v) for v in origin):
                        char["origin"] = origin
        except Exception:
            pass
        if include_geometry and native is None:
            char["loose_bbox"] = visual_bbox(loose, tuple(page_bbox), page_rotation) if loose else None
            char["tight_bbox"] = visual_bbox(tight, tuple(page_bbox), page_rotation) if tight else None
        clip = None
        if visibility_by_object is not None and address in visibility_by_object:
            visible, clip = visibility_by_object[address]
            if not visible:
                continue
            if native is None and clip is not None and not _clip_visible_character(char, clip):
                continue
        if native is not None:
            # Only values are temporarily stored; materialization and clipping are still completed within the original textpage and lock scope.
            raw_geometry.append(
                (selected, loose if include_geometry else None, tight if include_geometry else None, raw_origin)
            )
            pending_clips.append(clip)
        chars.append(char)
    if native is not None:
        prepared = native.materialize_geometry(raw_geometry, page_bbox, (width, height), page_rotation)
        del raw_geometry
        retained = []
        for char, (layout, loose, tight, origin), clip in zip(chars, prepared, pending_clips, strict=True):
            char["bbox"] = Bbox(layout)
            char["origin"] = origin
            if include_geometry:
                char["loose_bbox"], char["tight_bbox"] = loose, tight
            if clip is None or _clip_visible_character(char, clip):
                retained.append(char)
        chars = retained
    _assign_writing_angles(chars)
    _mark_visible_objects(chars, handle)
    return chars


def _clip_visible_character(char: Char, clip: tuple[float, float, float, float]) -> bool:
    """Only the actual truncated glyphs are cropped; the origin and character index remain unchanged, and text completely outside the cropping area does not enter Flash."""
    ink = char.get("tight_bbox") or char["bbox"]
    if char["char"].isspace():
        # PDFium Spaces may not have inked areas, and the original separators between visible words must still be retained.
        return clip[0] <= (ink[0] + ink[2]) / 2 <= clip[2] and clip[1] <= (ink[1] + ink[3]) / 2 <= clip[3]
    visible = (max(ink[0], clip[0]), max(ink[1], clip[1]), min(ink[2], clip[2]), min(ink[3], clip[3]))
    if visible[2] <= visible[0] or visible[3] <= visible[1]:
        return False
    if tuple(ink) != visible:
        for key in ("bbox", "loose_bbox", "tight_bbox"):
            bbox = char.get(key)
            if bbox is not None:
                clipped = (max(bbox[0], clip[0]), max(bbox[1], clip[1]), min(bbox[2], clip[2]), min(bbox[3], clip[3]))
                if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                    # Broken loose box cannot override valid ink evidence, using confirmed visible glyph range.
                    clipped = visible
                char[key] = Bbox(list(clipped)) if key == "bbox" else clipped
    return True


__all__ = ["transform_point", "visual_bbox"]
