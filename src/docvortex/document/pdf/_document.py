from __future__ import annotations

import logging
import math
import time
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator, Literal, cast
from weakref import WeakValueDictionary

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from ...foundation._image import crop_pil_image
from ...foundation.image_encoding import ImageArtifact, ImageFormat, encode_image
from ...schema import BBox, PageInfo
from .classify import classify
from .snapshot import PDFPageSnapshot

# See: pdfium.PdfDocument.METADATA_KEYS
# Original symbols are explicitly re-exported by a single implementation, and host import paths and type identities remain unchanged.
from .native_annotations import (
    _extract_page_link_annotations as _extract_page_link_annotations,
)
from .native_annotations import (
    _extract_page_signature_bboxes as _extract_page_signature_bboxes,
)
from .native_annotations import (
    _get_annotation_string as _get_annotation_string,
)
from .native_annotations import (
    _get_pdfium_uri_path as _get_pdfium_uri_path,
)
from .native_annotations import (
    _pdf_link_annotation_is_visible as _pdf_link_annotation_is_visible,
)
from .native_annotations import (
    _pdf_link_region_bboxes as _pdf_link_region_bboxes,
)
from .native_annotations import (
    _signature_bbox_from_annotation as _signature_bbox_from_annotation,
)
from .native_annotations import (
    _validate_pdf_external_link_target as _validate_pdf_external_link_target,
)
from .native_annotations import (
    _visual_bbox_from_pdf_points as _visual_bbox_from_pdf_points,
)
from .native_annotations import pdfium_c as pdfium_c
from .native_contracts import (
    _PDF_EXTERNAL_LINK_SCHEMES as _PDF_EXTERNAL_LINK_SCHEMES,
)
from .native_contracts import (
    DEFAULT_RENDER_DPI as DEFAULT_RENDER_DPI,
)
from .native_contracts import (
    DEFAULT_RENDER_MAX_EDGE as DEFAULT_RENDER_MAX_EDGE,
)
from .native_contracts import (
    DEFAULT_RENDER_SCALE as DEFAULT_RENDER_SCALE,
)
from .native_contracts import (
    DRAWING_FORM_MAX_DEPTH as DRAWING_FORM_MAX_DEPTH,
)
from .native_contracts import (
    DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE as DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE,
)
from .native_contracts import (
    DRAWING_LINE_AXIS_RATIO_TOLERANCE as DRAWING_LINE_AXIS_RATIO_TOLERANCE,
)
from .native_contracts import (
    DRAWING_LINE_MERGE_TOLERANCE as DRAWING_LINE_MERGE_TOLERANCE,
)
from .native_contracts import (
    DRAWING_LINE_MIN_LENGTH as DRAWING_LINE_MIN_LENGTH,
)
from .native_contracts import (
    DRAWING_THIN_RECT_MAX_THICKNESS as DRAWING_THIN_RECT_MAX_THICKNESS,
)
from .native_contracts import (
    DRAWING_THIN_RECT_MIN_ASPECT_RATIO as DRAWING_THIN_RECT_MIN_ASPECT_RATIO,
)
from .native_contracts import (
    PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES as PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES,
)
from .native_contracts import (
    POINTS_PER_INCH as POINTS_PER_INCH,
)
from .native_contracts import (
    PDFDrawingLine as PDFDrawingLine,
)
from .native_contracts import (
    PDFImageInfo as PDFImageInfo,
)
from .native_contracts import (
    PDFLinkAnnotation as PDFLinkAnnotation,
)
from .native_contracts import (
    PDFMetadataKey as PDFMetadataKey,
)
from .native_contracts import (
    PDFPageImage as PDFPageImage,
)
from .native_contracts import (
    PDFPageTextGeometry as PDFPageTextGeometry,
)
from .native_contracts import PDFPageVectorGeometry as PDFPageVectorGeometry
from .native_contracts import (
    PDFPathInfo as PDFPathInfo,
)
from .native_contracts import (
    _PathSubpath as _PathSubpath,
)
from .native_contracts import (
    _PDFPageSnapshot as _PDFPageSnapshot,
)
from .native_coordinates import (
    _apply_pdf_matrix as _apply_pdf_matrix,
)
from .native_coordinates import (
    _char_visual_bbox_from_pdfium as _char_visual_bbox_from_pdfium,
)
from .native_coordinates import (
    _drawing_page_size as _drawing_page_size,
)
from .native_coordinates import (
    _extract_page_char_extended_geometry as _extract_page_char_extended_geometry,
)
from .native_coordinates import (
    _get_raw_object_matrix as _get_raw_object_matrix,
)
from .native_coordinates import (
    _multiply_pdf_matrices as _multiply_pdf_matrices,
)
from .native_coordinates import (
    _normalize_pdf_page_bbox as _normalize_pdf_page_bbox,
)
from .native_coordinates import (
    _transform_drawing_point as _transform_drawing_point,
)
from .native_lifecycle import (
    _try_close as _try_close,
)
from .native_objects import (
    _combine_collinear_line_group as _combine_collinear_line_group,
)
from .native_objects import (
    _extract_page_drawing_lines as _extract_page_drawing_lines,
)
from .native_objects import (
    _extract_page_form_bboxes as _extract_page_form_bboxes,
)
from .native_objects import (
    _extract_page_image_bboxes as _extract_page_image_bboxes,
)
from .native_objects import (
    _extract_page_image_infos as _extract_page_image_infos,
)
from .native_objects import (
    _extract_page_path_infos as _extract_page_path_infos,
)
from .native_objects import (
    _extract_page_paths_and_lines as _extract_page_paths_and_lines,
)
from .native_objects import (
    _extract_path_drawing_lines as _extract_path_drawing_lines,
)
from .native_objects import (
    _form_bbox_from_object as _form_bbox_from_object,
)
from .native_objects import (
    _get_path_visibility as _get_path_visibility,
)
from .native_objects import (
    _get_raw_image_fingerprint as _get_raw_image_fingerprint,
)
from .native_objects import (
    _get_raw_object_alpha as _get_raw_object_alpha,
)
from .native_objects import (
    _get_raw_object_rgba as _get_raw_object_rgba,
)
from .native_objects import (
    _get_raw_stroke_width as _get_raw_stroke_width,
)
from .native_objects import (
    _get_segment_stroke_width as _get_segment_stroke_width,
)
from .native_objects import (
    _get_thin_filled_subpath_line as _get_thin_filled_subpath_line,
)
from .native_objects import (
    _image_bbox_from_matrix as _image_bbox_from_matrix,
)
from .native_objects import (
    _iter_raw_image_objects as _iter_raw_image_objects,
)
from .native_objects import (
    _iter_raw_path_objects as _iter_raw_path_objects,
)
from .native_objects import (
    _iter_raw_path_objects_with_depth as _iter_raw_path_objects_with_depth,
)
from .native_objects import (
    _iter_raw_root_form_objects as _iter_raw_root_form_objects,
)
from .native_objects import (
    _line_axis_coordinate as _line_axis_coordinate,
)
from .native_objects import (
    _line_main_interval as _line_main_interval,
)
from .native_objects import (
    _make_axis_drawing_line as _make_axis_drawing_line,
)
from .native_objects import (
    _merge_collinear_drawing_lines as _merge_collinear_drawing_lines,
)
from .native_objects import (
    _merge_orientation_lines as _merge_orientation_lines,
)
from .native_objects import (
    _path_info_from_object as _path_info_from_object,
)
from .native_objects import (
    _read_raw_path_subpaths as _read_raw_path_subpaths,
)
from .native_objects import (
    _transform_path_subpath as _transform_path_subpath,
)
from .native_objects import (
    _walk_raw_page_objects as _walk_raw_page_objects,
)
from .native_objects import (
    _walk_raw_page_objects_with_depth as _walk_raw_page_objects_with_depth,
)
from .native_objects import (
    _walk_raw_path_objects as _walk_raw_path_objects,
)
from .native_text_geometry import (
    _extract_page_text_geometry as _extract_page_text_geometry,
)
from .native_text_geometry import (
    _page_to_image as _page_to_image,
)
from .native_text_geometry import (
    _restore_pdfium_surrogate_pairs as _restore_pdfium_surrogate_pairs,
)
from .pdfium import _pdfium_lock, pdfium_guard
from .text import extract as _text_extract
from .text import get_lines_from_chars as get_lines_from_chars
from .text._contracts import Char, Line

logger = logging.getLogger(__name__)
get_chars = _text_extract.get_chars


_STANDARD_TEXT_GEOMETRY_EXTRACTOR = _extract_page_text_geometry


class PDFPage:
    def __init__(self, pdf_doc: "PDFDocument", idx: int) -> None:
        self.pdf_doc = pdf_doc
        self._idx = idx

    @property
    def size(self) -> tuple[float, float]:
        return self.pdf_doc.page_size(self._idx)

    @property
    def rotation(self) -> Literal[0, 90, 180, 270]:
        """Read-only returns the standard rotation angle declared on the current page."""

        return self.pdf_doc.page_rotation(self._idx)

    def get_char_count(self) -> int:
        return self.pdf_doc.page_char_count(self._idx)

    def get_chars(self) -> list[Char]:
        return self.pdf_doc.get_page_chars(self._idx)

    def get_chars_with_geometry(self) -> PDFPageTextGeometry:
        """Read the characters at once and Hybrid TXT match the additional geometry required."""
        return self.pdf_doc.get_page_chars_with_geometry(self._idx)

    def get_text_snapshot(self):
        """Only obtain independent native text evidence, without parsing paths or links in advance; refer to the backend and return None."""
        from ..._compute_backend import get_native

        native = get_native()
        if native is None or not hasattr(native, "NativeTextSnapshot"):
            return None
        text = self.pdf_doc._get_owned_text_snapshot(self._idx, visible_only=False)
        return text if isinstance(text, native.NativeTextSnapshot) else None

    def get_snapshot(self) -> PDFPageSnapshot:
        """Open the page once to get shared evidence, and the snapshot can be used after the document is closed."""
        return self.pdf_doc.get_page_snapshot(self._idx)

    def get_drawing_lines(self) -> list[PDFDrawingLine]:
        """Read the horizontal and vertical drawing of the current page that has been normalized to the visual page coordinates."""

        return self.pdf_doc.get_page_drawing_lines(self._idx)

    def get_path_infos(self) -> list[PDFPathInfo]:
        """Read the Path digest in the current page and nested Form that has been normalized to the visual page coordinates."""

        return self.pdf_doc.get_page_path_infos(self._idx)

    def get_vector_geometry(self) -> PDFPageVectorGeometry:
        """Read the drawing line and path summary of the current page once, and the caller explicitly manages the reuse range."""
        return self.pdf_doc.get_page_vector_geometry(self._idx)

    def get_link_annotations(self) -> list[PDFLinkAnnotation]:
        """Reads the external URI Link annotation that the current page has normalized to visual page coordinates."""

        return self.pdf_doc.get_page_link_annotations(self._idx)


class PDFDocument:
    """A PDF file loaded in memory, with lazy pypdfium2 access.

    This object is responsible for all access to, and lifecycle management of,
    the associated PDFium document/page objects.

    All pypdfium2 operations are serialized under a module-level lock for
    thread safety. Call ``close()`` when done, or use as a context manager.

    The class does not expose raw PDFium objects or methods without wrapping
    them first. Callers may use this class to read or operate on the
    underlying PDFium state, but they must not directly access PDFium objects
    through this API.
    """

    def __init__(
        self,
        pdf_bytes_or_path: bytes | str,
        render_scale: float = DEFAULT_RENDER_SCALE,
        render_max_edge: int = DEFAULT_RENDER_MAX_EDGE,
    ) -> None:
        self._render_session = None
        self._render_session_lock = threading.Lock()
        self._classification: Literal["ocr", "txt"] | None = None
        self._snapshot_owner = object()
        self._page_snapshots: WeakValueDictionary[int, PDFPageSnapshot] = WeakValueDictionary()
        self._owned_text_snapshots: WeakValueDictionary = WeakValueDictionary()
        if isinstance(pdf_bytes_or_path, bytes):
            self._pdf_bytes: bytes = pdf_bytes_or_path
        else:
            assert isinstance(pdf_bytes_or_path, str)
            with open(pdf_bytes_or_path, "rb") as f:
                self._pdf_bytes = f.read()

        self._pdf_doc_opened: pdfium.PdfDocument | None = None
        self._page_count: int | None = None
        self._page_char_counts: dict[int, int] = {}
        self._page_sizes: dict[int, tuple[float, float]] = {}
        self._page_rotations: dict[int, Literal[0, 90, 180, 270]] = {}
        self._form_reader = None
        self._form_reader_ready = False
        self.render_scale = render_scale
        self.render_max_edge = render_max_edge

    # ------------------------------------------------------------------ #
    #  Factory
    # ------------------------------------------------------------------ #

    @staticmethod
    def from_image(
        image_bytes: bytes,
        render_scale: float = DEFAULT_RENDER_SCALE,
        render_max_edge: int = DEFAULT_RENDER_MAX_EDGE,
    ) -> "PDFDocument":
        open_started_at = time.perf_counter()
        logger.debug("Pillow image open started input_bytes=%d", len(image_bytes))
        image = Image.open(BytesIO(image_bytes))
        logger.debug(
            "Pillow image open completed format=%s mode=%s size=%sx%s elapsed_ms=%d",
            image.format,
            image.mode,
            image.width,
            image.height,
            round((time.perf_counter() - open_started_at) * 1000),
        )

        # Automatically correct according to EXIF information (process pictures taken with mobile phones marked with Orientation)
        transpose_started_at = time.perf_counter()
        logger.debug("Pillow EXIF transpose started")
        image = ImageOps.exif_transpose(image) or image
        logger.debug(
            "Pillow EXIF transpose completed mode=%s size=%sx%s elapsed_ms=%d",
            image.mode,
            image.width,
            image.height,
            round((time.perf_counter() - transpose_started_at) * 1000),
        )

        # Convert only when necessary
        if image.mode != "RGB":
            source_mode = image.mode
            conversion_started_at = time.perf_counter()
            logger.debug("Pillow RGB conversion started source_mode=%s", source_mode)
            image = image.convert("RGB")
            logger.debug(
                "Pillow RGB conversion completed source_mode=%s elapsed_ms=%d",
                source_mode,
                round((time.perf_counter() - conversion_started_at) * 1000),
            )

        render_dpi = max(1, int(round(render_scale * POINTS_PER_INCH)))

        with BytesIO() as pdf_buffer:
            # The first picture is saved as PDF, and the rest are appended
            save_started_at = time.perf_counter()
            logger.debug(
                "Pillow PDF encoding started mode=%s size=%sx%s dpi=%d",
                image.mode,
                image.width,
                image.height,
                render_dpi,
            )
            image.save(
                pdf_buffer,
                format="PDF",
                resolution=render_dpi,
                quality=95,
                subsampling=0,
            )
            pdf_bytes = pdf_buffer.getvalue()
            logger.debug(
                "Pillow PDF encoding completed output_bytes=%d elapsed_ms=%d",
                len(pdf_bytes),
                round((time.perf_counter() - save_started_at) * 1000),
            )

        return PDFDocument(
            pdf_bytes,
            render_scale=render_scale,
            render_max_edge=render_max_edge,
        )

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def get_render_session(self, *, threads: int | None = None, timeout: float | None = None):
        """Lazily create a document-owned rendering session and reuse it for subsequent windows.

        ``threads`` and the default ``timeout`` apply only when the session is first created; later calls
        do not modify the existing session even if different values are supplied. Override individual task deadlines in ``session.render``
        via its ``timeout`` parameter. The document releases the session in ``close()``.
        """
        from .render_session import PDFRenderSession

        with self._render_session_lock:
            if self._render_session is None:
                self._render_session = PDFRenderSession(self._pdf_bytes, threads=threads, timeout=timeout)
            return self._render_session

    def close(self) -> None:
        """Turn off rendering leases and native documents; cleanup paths do not trigger font initialization."""
        try:
            lock = getattr(self, "_render_session_lock", None)
            if lock is not None:
                with lock:
                    session = self._render_session
                    self._render_session = None
                    if session is not None:
                        _try_close(session)
        finally:
            self._form_reader = None
            self._form_reader_ready = False
            if getattr(self, "_pdf_doc_opened", None) is not None:
                with _pdfium_lock:
                    _try_close(self._pdf_doc_opened)
                    self._pdf_doc_opened = None

    def __enter__(self) -> "PDFDocument":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    #  Properties
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return self.page_count

    def __getitem__(self, idx: int) -> PDFPage:
        return PDFPage(self, idx)

    @property
    def page_count(self) -> int:
        with pdfium_guard():
            return len(self._pdf_doc)

    @property
    def metadata(self) -> dict[PDFMetadataKey, str]:
        with pdfium_guard():
            metadata = self._pdf_doc.get_metadata_dict()
        return cast(dict[PDFMetadataKey, str], metadata)

    @property
    def bytes(self) -> bytes:
        # TODO: some invoker expected PDF bytes even if input bytes is an Image.
        return self._pdf_bytes

    # ------------------------------------------------------------------ #
    #  Metadata
    # ------------------------------------------------------------------ #

    def page_size(self, page_idx: int) -> tuple[float, float]:
        """Reuse the page size of the immutable source document to avoid opening pages repeatedly for text and image stages."""
        cached = self._page_sizes.get(page_idx) if type(page_idx) is int else None
        if cached is not None:
            return cached
        with self._open_page(page_idx) as page:
            # rect: (left, bottom, right, top)
            rect: tuple[float, float, float, float] = page.get_bbox()
            rotation_read = True
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
                rotation_read = False
        width = abs(rect[2] - rect[0])
        height = abs(rect[1] - rect[3])
        # PDFium The text and rendering coordinates have page rotation applied and the page size must use the same visual orientation.
        size = (height, width) if page_rotation in {90, 270} else (width, height)
        if rotation_read and type(page_idx) is int:
            self._page_sizes[page_idx] = size
            self._page_rotations[page_idx] = page_rotation if page_rotation in {0, 90, 180, 270} else 0
        return size

    def page_rotation(self, page_idx: int) -> Literal[0, 90, 180, 270]:
        """Returns the standard rotation angle declared by the PDF page dictionary under thread lock protection."""

        cached = self._page_rotations.get(page_idx) if type(page_idx) is int else None
        if cached is not None:
            return cached
        with self._open_page(page_idx) as page:
            try:
                rotation = int(page.get_rotation()) % 360
            except Exception:
                return 0
        rotation = rotation if rotation in {0, 90, 180, 270} else 0
        if type(page_idx) is int:
            self._page_rotations[page_idx] = rotation
        return rotation

    # ------------------------------------------------------------------ #
    #  Rendering
    # ------------------------------------------------------------------ #

    def render_page(self, page_idx: int, *, scale: float | None = None) -> PDFPageImage:
        if scale is None:
            scale = self.render_scale
        if scale <= 0:
            raise ValueError("scale must be greater than 0")
        with self._open_page(page_idx) as page:
            return _page_to_image(page, scale, self.render_max_edge)

    def render_image(
        self,
        page_idx: int,
        *,
        bbox: BBox | None = None,
        image_format: ImageFormat = "jpeg",
        scale: float | None = None,
    ) -> ImageArtifact:
        """Render a full page or normalized region and encode it directly, with the inner image released on all exit paths."""
        if bbox is not None and (
            len(bbox) != 4 or not all(math.isfinite(value) for value in bbox) or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]
        ):
            raise ValueError("bbox must be a finite, non-empty normalized rectangle")
        image = self.render_page(page_idx, scale=scale)
        crop = None
        try:
            if bbox is not None:
                crop = crop_pil_image(bbox, image.pil_image)
                if crop.width <= 0 or crop.height <= 0:
                    raise ValueError("bbox does not intersect the rendered page")
            return encode_image(crop if crop is not None else image.pil_image, image_format=image_format)
        finally:
            if crop is not None:
                crop.close()
            image.pil_image.close()

    def crop_image(self, bbox: BBox, page_idx: int) -> bytes:
        """Keep the existing JPEG byte return contract and reuse the common area image output."""
        return self.render_image(page_idx, bbox=bbox, image_format="jpeg").data

    # ------------------------------------------------------------------ #
    #  Text
    # ------------------------------------------------------------------ #

    def page_char_count(self, page_idx: int) -> int:
        """Cache the original number of characters successfully read and prioritize reusing the count from snapshot extraction to avoid rebuilding textpage."""
        cached = self._page_char_counts.get(page_idx) if type(page_idx) is int else None
        if cached is not None:
            return cached
        with pdfium_guard():
            cached = self._page_char_counts.get(page_idx) if type(page_idx) is int else None
            if cached is not None:
                return cached
            with self._open_page(page_idx) as page:
                textpage = None
                try:
                    textpage = page.get_textpage()
                    n_chars = textpage.count_chars()
                finally:
                    _try_close(textpage)
            if type(page_idx) is int and type(n_chars) is int and n_chars >= 0:
                self._page_char_counts[page_idx] = n_chars
        return cast(int, n_chars)

    def get_page_chars(self, page_idx: int) -> list[Char]:
        return self._get_page_text_geometry(page_idx, include_extended_geometry=False).chars

    def get_page_chars_with_geometry(self, page_idx: int) -> PDFPageTextGeometry:
        """Read characters, tight bbox and character origin at one time to avoid Hybrid and textpage from being opened repeatedly."""
        return self._get_page_text_geometry(page_idx, include_extended_geometry=True)

    def _get_page_text_paint(self, page_idx: int, char_indices: Sequence[int]) -> dict[int, tuple[int, int, int, int]]:
        """Only native draw colors are read for suspiciously short text; page and text handles are promptly released within shared locks without changing characters or exposing output."""
        from ctypes import byref, c_uint
        import pypdfium2.raw as raw

        result = {}
        with self._open_page(page_idx) as page:
            text = page.get_textpage()
            try:
                for index in dict.fromkeys(char_indices):
                    obj = raw.FPDFText_GetTextObject(text.raw, index)
                    if not obj or raw.FPDFTextObj_GetTextRenderMode(obj) != 0:
                        # Text with strokes or only cropping cannot be used to determine light noise based on color filling alone.
                        continue
                    values = [c_uint() for _ in range(4)]
                    if raw.FPDFText_GetFillColor(text.raw, index, *(byref(value) for value in values)):
                        result[index] = tuple(value.value for value in values)
            finally:
                text.close()
        return result

    def _extract_owned_text_snapshot(self, page_idx: int, page, *, visible_only: bool, reference: bool = True):
        """Only the own text is extracted and weakly cached, and the full page snapshot reuses it without introducing additional vector or link dependencies."""
        key = (page_idx, visible_only)
        cached = self._owned_text_snapshots.get(key)
        if cached is not None:
            return cached
        if _extract_page_text_geometry is not _STANDARD_TEXT_GEOMETRY_EXTRACTOR:
            # The non-standard replacement entry retains the original signature and exception, and does not pass new parameters to the old extension or failure injector.
            return (
                _extract_page_text_geometry(page, include_extended_geometry=True, visible_only=visible_only)
                if reference
                else None
            )
        text = _extract_page_text_geometry(
            page,
            include_extended_geometry=True,
            visible_only=visible_only,
            compact=True,
            compact_only=not reference,
            paint_page_reader=(lambda: self._get_form_resource_page(page_idx)) if visible_only else None,
        )
        if text is not None and not isinstance(text, PDFPageTextGeometry):
            self._owned_text_snapshots[key] = text
            if type(page_idx) is int and callable(getattr(text, "raw_char_count", None)):
                self._page_char_counts[page_idx] = text.raw_char_count()
        return text

    def _get_owned_text_snapshot(self, page_idx: int, *, visible_only: bool = False):
        """If the capability is missing, None is returned immediately; unsupported original values are only reported for reference selection, and the fallback characters are not materialized in advance."""
        from ..._compute_backend import get_native

        native = get_native()
        if native is None or not hasattr(native, "NativeTextSnapshot"):
            return None
        with pdfium_guard():
            cached = self._owned_text_snapshots.get((page_idx, visible_only))
            if cached is not None:
                return cached
            with self._open_page(page_idx) as page:
                return self._extract_owned_text_snapshot(page_idx, page, visible_only=visible_only, reference=False)

    def get_page_snapshot(self, page_idx: int) -> PDFPageSnapshot:
        """Explicit snapshots of the same page are reused during the lifetime of the consumer, and weak caching does not extend the residence time of full-text evidence."""
        with pdfium_guard():
            cached = self._page_snapshots.get(page_idx)
            if cached is not None:
                return cached
            with self._open_page(page_idx) as page:
                bbox = _normalize_pdf_page_bbox(page.get_bbox())
                try:
                    rotation = int(page.get_rotation()) % 360
                except Exception:
                    rotation = 0
                rotation = rotation if rotation in {0, 90, 180, 270} else 0
                drawings, paths = _extract_page_paths_and_lines(page, bbox, rotation)
                text = self._extract_owned_text_snapshot(page_idx, page, visible_only=False)
                native_text = None if isinstance(text, PDFPageTextGeometry) else text
                snapshot = PDFPageSnapshot(
                    page_idx,
                    _drawing_page_size(bbox, rotation),
                    rotation,
                    text if native_text is None else None,
                    PDFPageVectorGeometry(tuple(drawings), tuple(paths)),
                    tuple(_extract_page_link_annotations(page, self._pdf_doc.raw, bbox, rotation)),
                    self._snapshot_owner,
                    native_text,
                )
            self._page_snapshots[page_idx] = snapshot
            return snapshot

    def _extract_native_page(self, page_idx: int) -> _PDFPageSnapshot:
        """Collect Flash page evidence in a locked open and share Path subpath decoding."""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                raw_rotation = int(page.get_rotation()) % 360
            except Exception:
                raw_rotation = 0
            rotation = raw_rotation if raw_rotation in {0, 90, 180, 270} else 0
            drawings, paths = _extract_page_paths_and_lines(page, page_bbox, raw_rotation)
            form_bboxes = _extract_page_form_bboxes(page, page_bbox, raw_rotation)
            form_infos = ()
            if form_bboxes:
                from .form_structure import extract_form_structure

                form_infos = extract_form_structure(page, page_bbox, rotation, self._get_form_resource_page(page_idx))
            text = self._extract_owned_text_snapshot(page_idx, page, visible_only=True)
            native_text = None if isinstance(text, PDFPageTextGeometry) else text
            return _PDFPageSnapshot(
                page_size=_drawing_page_size(page_bbox, raw_rotation),
                rotation=cast(Literal[0, 90, 180, 270], rotation),
                text_geometry=text if native_text is None else None,
                native_text=native_text,
                drawing_lines=drawings,
                path_infos=paths,
                image_infos=_extract_page_image_infos(page, page_bbox, raw_rotation),
                form_bboxes=form_bboxes,
                form_infos=form_infos,
                signature_bboxes=_extract_page_signature_bboxes(
                    page,
                    page_bbox,
                    raw_rotation,
                    form_handle=getattr(self._pdf_doc, "formenv", None),
                ),
                link_annotations=_extract_page_link_annotations(page, self._pdf_doc.raw, page_bbox, raw_rotation),
            )

    def _get_form_resource_page(self, page_idx: int):
        """Lazy reading of resource dictionaries only for documents containing Form, reusing reader and isolating corrupted PDF."""
        if not self._form_reader_ready:
            self._form_reader_ready = True
            try:
                from pypdf import PdfReader

                self._form_reader = PdfReader(BytesIO(self._pdf_bytes))
            except Exception:
                self._form_reader = None
        try:
            return self._form_reader.pages[page_idx] if self._form_reader is not None else None
        except Exception:
            return None

    def _get_page_text_geometry(
        self,
        page_idx: int,
        *,
        include_extended_geometry: bool,
    ) -> PDFPageTextGeometry:
        """Materialize characters and optionally extended geometry within the same PDFium textpage lifetime."""
        with self._open_page(page_idx) as page:
            return _extract_page_text_geometry(page, include_extended_geometry=include_extended_geometry)

    def get_page_lines(self, page_idx: int) -> list[Line]:
        chars = self.get_page_chars(page_idx)
        return get_lines_from_chars(chars)

    # ------------------------------------------------------------------ #
    #  Drawing geometry
    # ------------------------------------------------------------------ #

    def get_page_drawing_lines(self, page_idx: int) -> list[PDFDrawingLine]:
        """Extract the visible horizontal and vertical drawing lines in the page and convert them to the coordinates of the upper left origin of the page."""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_drawing_lines(page, page_bbox, page_rotation)

    def get_page_vector_geometry(self, page_idx: int) -> PDFPageVectorGeometry:
        """Iterate over Path once within the same page and shared lock, returning independently materialized vector geometries."""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            drawings, paths = _extract_page_paths_and_lines(page, page_bbox, page_rotation)
            return PDFPageVectorGeometry(tuple(drawings), tuple(paths))

    def get_page_path_infos(self, page_idx: int) -> list[PDFPathInfo]:
        """Extracts Path geometry and drawing features from the page and nested Form."""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_path_infos(page, page_bbox, page_rotation)

    def get_page_image_bboxes(self, page_idx: int) -> list[BBox]:
        """Extract the bitmap bbox in the page and nested Form, and convert it to the coordinates of the upper left origin of the page."""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_image_bboxes(page, page_bbox, page_rotation)

    def get_page_image_infos(self, page_idx: int) -> list[PDFImageInfo]:
        """Extract bitmap bbox and content fingerprint for Flash to identify cross-page duplicate images before claiming the main text."""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_image_infos(page, page_bbox, page_rotation)

    def get_page_form_bboxes(self, page_idx: int) -> list[BBox]:
        """Extract the top layer of the page Form XObject bbox and convert it to the coordinates of the upper left origin of the page."""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_form_bboxes(page, page_bbox, page_rotation)

    def get_page_signature_bboxes(self, page_idx: int) -> list[BBox]:
        """Extract the digital signature control with a visible normal appearance and convert it to the coordinates of the upper left origin of the page."""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_signature_bboxes(
                page,
                page_bbox,
                page_rotation,
                form_handle=getattr(self._pdf_doc, "formenv", None),
            )

    def get_page_link_annotations(self, page_idx: int) -> list[PDFLinkAnnotation]:
        """Extract external URI Link annotations that are visible and target-safe."""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_link_annotations(
                page,
                self._pdf_doc.raw,
                page_bbox,
                page_rotation,
            )

    # ------------------------------------------------------------------ #
    #  Classification
    # ------------------------------------------------------------------ #

    def classify(self) -> Literal["ocr", "txt"]:
        """Explicitly classify and reuse results within the same immutable document instance, not implicitly called by text extraction."""
        if self._classification is None:
            self._classification = cast(Literal["ocr", "txt"], classify(self._pdf_doc, self.bytes))
        return self._classification

    # ------------------------------------------------------------------ #
    #  Visualization
    # ------------------------------------------------------------------ #

    def draw_layout_bbox(self, pages: list[PageInfo], output_path: str, *, page_indices: Sequence[int] | None = None) -> None:
        """Write a layout preview with type and original number tags; the document after page extraction needs to pass in the original page number mapping."""
        from ...visualization import render_layout_pdf

        pdf_bytes = render_layout_pdf(self._pdf_bytes, pages, page_indices=page_indices)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(pdf_bytes)

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    @property
    def _pdf_doc(self) -> pdfium.PdfDocument:
        if self._pdf_doc_opened is None:
            with pdfium_guard():
                if self._pdf_doc_opened is None:
                    pdf_doc = pdfium.PdfDocument(self._pdf_bytes)
                    try:
                        # The form environment must be initialized before opening the page for signature field types to be inherited correctly along Parent.
                        pdf_doc.init_forms()
                    except Exception:
                        # Exception forms cannot block normal text, drawing, and rendering interfaces.
                        pass
                    self._pdf_doc_opened = pdf_doc
        return self._pdf_doc_opened

    @contextmanager
    def _open_page(self, page_idx: int) -> Iterator[pdfium.PdfPage]:
        """Open the page within the unified font runtime and shared lock, and release the native handle when leaving."""
        with pdfium_guard():
            page = None
            try:
                page = self._pdf_doc[page_idx]
                yield page
            finally:
                _try_close(page)
