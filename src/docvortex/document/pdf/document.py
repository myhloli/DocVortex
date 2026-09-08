from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator, Literal, cast

import pypdfium2 as pdfium
from .native_annotations import pdfium_c as pdfium_c
from .text import extract as _text_extract
from .text.contracts import Char, Line
from PIL import Image, ImageOps

from ...schema import BBox, PageInfo
from ...foundation.image import crop_pil_image
from ...foundation.image_encoding import ImageArtifact, ImageFormat, encode_image
from .classify import classify
from .pdfium import _pdfium_lock, pdfium_guard
from .text import get_lines_from_chars as get_lines_from_chars


# See: pdfium.PdfDocument.METADATA_KEYS


# 原有符号由单一实现显式重导出，宿主导入路径与类型身份保持不变。
from .native_annotations import (
    _extract_page_link_annotations as _extract_page_link_annotations,
    _extract_page_signature_bboxes as _extract_page_signature_bboxes,
    _get_annotation_string as _get_annotation_string,
    _get_pdfium_uri_path as _get_pdfium_uri_path,
    _pdf_link_annotation_is_visible as _pdf_link_annotation_is_visible,
    _pdf_link_region_bboxes as _pdf_link_region_bboxes,
    _signature_bbox_from_annotation as _signature_bbox_from_annotation,
    _validate_pdf_external_link_target as _validate_pdf_external_link_target,
    _visual_bbox_from_pdf_points as _visual_bbox_from_pdf_points,
)
from .native_contracts import (
    DEFAULT_RENDER_DPI as DEFAULT_RENDER_DPI,
    DEFAULT_RENDER_MAX_EDGE as DEFAULT_RENDER_MAX_EDGE,
    DEFAULT_RENDER_SCALE as DEFAULT_RENDER_SCALE,
    DRAWING_FORM_MAX_DEPTH as DRAWING_FORM_MAX_DEPTH,
    DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE as DRAWING_LINE_AXIS_ABSOLUTE_TOLERANCE,
    DRAWING_LINE_AXIS_RATIO_TOLERANCE as DRAWING_LINE_AXIS_RATIO_TOLERANCE,
    DRAWING_LINE_MERGE_TOLERANCE as DRAWING_LINE_MERGE_TOLERANCE,
    DRAWING_LINE_MIN_LENGTH as DRAWING_LINE_MIN_LENGTH,
    DRAWING_THIN_RECT_MAX_THICKNESS as DRAWING_THIN_RECT_MAX_THICKNESS,
    DRAWING_THIN_RECT_MIN_ASPECT_RATIO as DRAWING_THIN_RECT_MIN_ASPECT_RATIO,
    NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE as NEAR_IDENTICAL_CHAR_BBOX_TOLERANCE,
    OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE as OFFSET_DUPLICATE_CHAR_BBOX_TOLERANCE,
    OFFSET_DUPLICATE_MIN_BBOX_OVERLAP_RATIO as OFFSET_DUPLICATE_MIN_BBOX_OVERLAP_RATIO,
    OFFSET_DUPLICATE_TRANSLATION_TOLERANCE as OFFSET_DUPLICATE_TRANSLATION_TOLERANCE,
    PDFDrawingLine as PDFDrawingLine,
    PDFImageInfo as PDFImageInfo,
    PDFLinkAnnotation as PDFLinkAnnotation,
    PDFMetadataKey as PDFMetadataKey,
    PDFPageImage as PDFPageImage,
    PDFPageTextGeometry as PDFPageTextGeometry,
    PDFPathInfo as PDFPathInfo,
    PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES as PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES,
    POINTS_PER_INCH as POINTS_PER_INCH,
    _PDFPageSnapshot as _PDFPageSnapshot,
    _PDF_EXTERNAL_LINK_SCHEMES as _PDF_EXTERNAL_LINK_SCHEMES,
    _PathSubpath as _PathSubpath,
)
from .native_coordinates import (
    _apply_pdf_matrix as _apply_pdf_matrix,
    _char_visual_bbox_from_pdfium as _char_visual_bbox_from_pdfium,
    _drawing_page_size as _drawing_page_size,
    _extract_page_char_extended_geometry as _extract_page_char_extended_geometry,
    _get_raw_object_matrix as _get_raw_object_matrix,
    _multiply_pdf_matrices as _multiply_pdf_matrices,
    _normalize_pdf_page_bbox as _normalize_pdf_page_bbox,
    _transform_drawing_point as _transform_drawing_point,
)
from .native_lifecycle import (
    _try_close as _try_close,
)
from .native_objects import (
    _combine_collinear_line_group as _combine_collinear_line_group,
    _extract_page_drawing_lines as _extract_page_drawing_lines,
    _extract_page_form_bboxes as _extract_page_form_bboxes,
    _extract_page_image_bboxes as _extract_page_image_bboxes,
    _extract_page_image_infos as _extract_page_image_infos,
    _extract_page_path_infos as _extract_page_path_infos,
    _extract_page_paths_and_lines as _extract_page_paths_and_lines,
    _extract_path_drawing_lines as _extract_path_drawing_lines,
    _form_bbox_from_object as _form_bbox_from_object,
    _get_path_visibility as _get_path_visibility,
    _get_raw_image_fingerprint as _get_raw_image_fingerprint,
    _get_raw_object_alpha as _get_raw_object_alpha,
    _get_raw_object_rgba as _get_raw_object_rgba,
    _get_raw_stroke_width as _get_raw_stroke_width,
    _get_segment_stroke_width as _get_segment_stroke_width,
    _get_thin_filled_subpath_line as _get_thin_filled_subpath_line,
    _image_bbox_from_matrix as _image_bbox_from_matrix,
    _iter_raw_image_objects as _iter_raw_image_objects,
    _iter_raw_path_objects as _iter_raw_path_objects,
    _iter_raw_path_objects_with_depth as _iter_raw_path_objects_with_depth,
    _iter_raw_root_form_objects as _iter_raw_root_form_objects,
    _line_axis_coordinate as _line_axis_coordinate,
    _line_main_interval as _line_main_interval,
    _make_axis_drawing_line as _make_axis_drawing_line,
    _merge_collinear_drawing_lines as _merge_collinear_drawing_lines,
    _merge_orientation_lines as _merge_orientation_lines,
    _path_info_from_object as _path_info_from_object,
    _read_raw_path_subpaths as _read_raw_path_subpaths,
    _transform_path_subpath as _transform_path_subpath,
    _walk_raw_page_objects as _walk_raw_page_objects,
    _walk_raw_page_objects_with_depth as _walk_raw_page_objects_with_depth,
    _walk_raw_path_objects as _walk_raw_path_objects,
)
from .native_text_geometry import (
    _calculate_bbox_overlap_in_smaller_area as _calculate_bbox_overlap_in_smaller_area,
    _deduplicate_near_identical_chars as _deduplicate_near_identical_chars,
    _extract_page_text_geometry as _extract_page_text_geometry,
    _get_near_identical_bbox_bucket_key as _get_near_identical_bbox_bucket_key,
    _get_visible_char_signature as _get_visible_char_signature,
    _is_adjacent_offset_duplicate_char as _is_adjacent_offset_duplicate_char,
    _is_near_identical_bbox as _is_near_identical_bbox,
    _iter_neighbor_bbox_bucket_keys as _iter_neighbor_bbox_bucket_keys,
    _page_to_image as _page_to_image,
    _restore_pdfium_surrogate_pairs as _restore_pdfium_surrogate_pairs,
)


logger = logging.getLogger(__name__)
get_chars = _text_extract.get_chars
deduplicate_chars = _text_extract.deduplicate_chars


class PDFPage:
    def __init__(self, pdf_doc: "PDFDocument", idx: int) -> None:
        self.pdf_doc = pdf_doc
        self._idx = idx

    @property
    def size(self) -> tuple[float, float]:
        return self.pdf_doc.page_size(self._idx)

    @property
    def rotation(self) -> Literal[0, 90, 180, 270]:
        """只读返回当前页声明的标准旋转角度。"""

        return self.pdf_doc.page_rotation(self._idx)

    def get_char_count(self) -> int:
        return self.pdf_doc.page_char_count(self._idx)

    def get_chars(self) -> list[Char]:
        return self.pdf_doc.get_page_chars(self._idx)

    def get_chars_with_geometry(self) -> PDFPageTextGeometry:
        """一次读取字符及 Hybrid TXT 匹配所需的额外几何。"""
        return self.pdf_doc.get_page_chars_with_geometry(self._idx)

    def get_drawing_lines(self) -> list[PDFDrawingLine]:
        """读取当前页已规范到视觉页面坐标的横竖 drawing。"""

        return self.pdf_doc.get_page_drawing_lines(self._idx)

    def get_path_infos(self) -> list[PDFPathInfo]:
        """读取当前页及嵌套 Form 中已规范到视觉页面坐标的 Path 摘要。"""

        return self.pdf_doc.get_page_path_infos(self._idx)

    def get_link_annotations(self) -> list[PDFLinkAnnotation]:
        """读取当前页已规范到视觉页面坐标的外部 URI Link 注解。"""

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
        self._classification: Literal["ocr", "txt"] | None = None
        if isinstance(pdf_bytes_or_path, bytes):
            self._pdf_bytes: bytes = pdf_bytes_or_path
        else:
            assert isinstance(pdf_bytes_or_path, str)
            with open(pdf_bytes_or_path, "rb") as f:
                self._pdf_bytes = f.read()

        self._pdf_doc_opened: pdfium.PdfDocument | None = None
        self._page_count: int | None = None
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

        # 根据 EXIF 信息自动转正（处理手机拍摄的带 Orientation 标记的图片）
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

        # 只在必要时转换
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
            # 第一张图保存为 PDF，其余追加
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

    def close(self) -> None:
        """关闭原生文档；清理路径不触发字体初始化。"""
        if self._pdf_doc_opened is not None:
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
        with self._open_page(page_idx) as page:
            # rect: (left, bottom, right, top)
            rect: tuple[float, float, float, float] = page.get_bbox()
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
        width = abs(rect[2] - rect[0])
        height = abs(rect[1] - rect[3])
        # PDFium 文本与渲染坐标已经应用页面旋转，页面尺寸必须使用相同视觉方向。
        return (height, width) if page_rotation in {90, 270} else (width, height)

    def page_rotation(self, page_idx: int) -> Literal[0, 90, 180, 270]:
        """在线程锁保护下返回 PDF 页面字典声明的标准旋转角度。"""

        with self._open_page(page_idx) as page:
            try:
                rotation = int(page.get_rotation()) % 360
            except Exception:
                rotation = 0
        return rotation if rotation in {0, 90, 180, 270} else 0

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
        """渲染整页或归一化区域并直接编码，内部图像在所有退出路径释放。"""
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
        """保留已有 JPEG 字节返回契约，复用公共区域图像输出。"""
        return self.render_image(page_idx, bbox=bbox, image_format="jpeg").data

    # ------------------------------------------------------------------ #
    #  Text
    # ------------------------------------------------------------------ #

    def page_char_count(self, page_idx: int) -> int:
        with self._open_page(page_idx) as page:
            textpage = None
            try:
                textpage = page.get_textpage()
                n_chars = textpage.count_chars()
            finally:
                _try_close(textpage)
        return cast(int, n_chars)

    def get_page_chars(self, page_idx: int) -> list[Char]:
        return self._get_page_text_geometry(page_idx, include_extended_geometry=False).chars

    def get_page_chars_with_geometry(self, page_idx: int) -> PDFPageTextGeometry:
        """一次读取字符、tight bbox 和字符原点，避免 Hybrid 重复打开 textpage。"""
        return self._get_page_text_geometry(page_idx, include_extended_geometry=True)

    def _extract_native_page(self, page_idx: int) -> _PDFPageSnapshot:
        """在一次加锁打开中收集 Flash 页面证据，并共享 Path 子路径解码。"""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                raw_rotation = int(page.get_rotation()) % 360
            except Exception:
                raw_rotation = 0
            rotation = raw_rotation if raw_rotation in {0, 90, 180, 270} else 0
            drawings, paths = _extract_page_paths_and_lines(page, page_bbox, raw_rotation)
            return _PDFPageSnapshot(
                page_size=_drawing_page_size(page_bbox, raw_rotation),
                rotation=cast(Literal[0, 90, 180, 270], rotation),
                text_geometry=_extract_page_text_geometry(page, include_extended_geometry=True),
                drawing_lines=drawings,
                path_infos=paths,
                image_infos=_extract_page_image_infos(page, page_bbox, raw_rotation),
                form_bboxes=_extract_page_form_bboxes(page, page_bbox, raw_rotation),
                signature_bboxes=_extract_page_signature_bboxes(
                    page,
                    page_bbox,
                    raw_rotation,
                    form_handle=getattr(self._pdf_doc, "formenv", None),
                ),
                link_annotations=_extract_page_link_annotations(page, self._pdf_doc.raw, page_bbox, raw_rotation),
            )

    def _get_page_text_geometry(
        self,
        page_idx: int,
        *,
        include_extended_geometry: bool,
    ) -> PDFPageTextGeometry:
        """在同一 PDFium textpage 生命周期内物化字符及可选扩展几何。"""
        with self._open_page(page_idx) as page:
            return _extract_page_text_geometry(page, include_extended_geometry=include_extended_geometry)

    def get_page_lines(self, page_idx: int) -> list[Line]:
        chars = self.get_page_chars(page_idx)
        return get_lines_from_chars(chars)

    # ------------------------------------------------------------------ #
    #  Drawing geometry
    # ------------------------------------------------------------------ #

    def get_page_drawing_lines(self, page_idx: int) -> list[PDFDrawingLine]:
        """提取页面中可见的水平、竖直绘图线，并转换为页面左上原点坐标。"""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_drawing_lines(page, page_bbox, page_rotation)

    def get_page_path_infos(self, page_idx: int) -> list[PDFPathInfo]:
        """提取页面及嵌套 Form 中的 Path 几何和绘制特征。"""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_path_infos(page, page_bbox, page_rotation)

    def get_page_image_bboxes(self, page_idx: int) -> list[BBox]:
        """提取页面及嵌套 Form 中的点阵图 bbox，并转换为页面左上原点坐标。"""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_image_bboxes(page, page_bbox, page_rotation)

    def get_page_image_infos(self, page_idx: int) -> list[PDFImageInfo]:
        """提取点阵图 bbox 与内容指纹，供 Flash 在认领正文前识别跨页重复图。"""

        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_image_infos(page, page_bbox, page_rotation)

    def get_page_form_bboxes(self, page_idx: int) -> list[BBox]:
        """提取页面顶层 Form XObject bbox，并转换为页面左上原点坐标。"""
        with self._open_page(page_idx) as page:
            page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
            try:
                page_rotation = int(page.get_rotation()) % 360
            except Exception:
                page_rotation = 0
            return _extract_page_form_bboxes(page, page_bbox, page_rotation)

    def get_page_signature_bboxes(self, page_idx: int) -> list[BBox]:
        """提取带可见正常外观的数字签名控件，并转换为页面左上原点坐标。"""

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
        """提取可见且目标安全的外部 URI Link 注解。"""

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
        """显式分类并在同一不可变文档实例内复用结果，不由文本提取隐式调用。"""
        if self._classification is None:
            self._classification = cast(Literal["ocr", "txt"], classify(self._pdf_doc, self.bytes))
        return self._classification

    # ------------------------------------------------------------------ #
    #  Visualization
    # ------------------------------------------------------------------ #

    def draw_layout_bbox(self, pages: list[PageInfo], output_path: str, *, page_indices: Sequence[int] | None = None) -> None:
        """写出带类型与原始编号标签的布局预览；抽页后的文档需传入原始页号映射。"""
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
                        # 表单环境必须在打开页面前初始化，签名字段类型才能沿 Parent 正确继承。
                        pdf_doc.init_forms()
                    except Exception:
                        # 异常表单不能阻断普通文本、绘图和渲染接口。
                        pass
                    self._pdf_doc_opened = pdf_doc
        return self._pdf_doc_opened

    @contextmanager
    def _open_page(self, page_idx: int) -> Iterator[pdfium.PdfPage]:
        """在统一字体运行时与共享锁内打开页面，离开时释放原生句柄。"""
        with pdfium_guard():
            page = None
            try:
                page = self._pdf_doc[page_idx]
                yield page
            finally:
                _try_close(page)
