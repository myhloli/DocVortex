from loguru import logger
from PIL import Image
from pypdfium2 import PdfBitmap, PdfPage

from .pdfium import pdfium_guard

DEFAULT_PDF_IMAGE_DPI = 200
DEFAULT_MAX_RENDER_EDGE = 3500


def estimate_page_image_bytes(page_size: tuple[float, float], dpi: int = DEFAULT_PDF_IMAGE_DPI) -> int:
    """按现有渲染缩放估算四通道页图字节，用于限制批量驻留内存。"""
    import math

    width, height = page_size
    scale = min(dpi / 72, DEFAULT_MAX_RENDER_EDGE / max(width, height))
    return math.ceil(width * scale) * math.ceil(height * scale) * 4


def page_to_image(
    page: PdfPage,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    max_width_or_height: int = DEFAULT_MAX_RENDER_EDGE,
) -> tuple[Image.Image, float]:
    """按既有 DPI 与长边上限渲染页面，并独立持有返回图片的像素。"""
    with pdfium_guard():
        scale = dpi / 72

        long_side_length = max(*page.get_size())
        if (long_side_length * scale) > max_width_or_height:
            scale = max_width_or_height / long_side_length

        bitmap: PdfBitmap | None = None
        try:
            bitmap = page.render(scale=scale)  # type: ignore
            image = bitmap.to_pil().copy()
        finally:
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception as e:
                    logger.error(f"Failed to close bitmap: {e}")
    return image, scale


__all__ = ["page_to_image"]
