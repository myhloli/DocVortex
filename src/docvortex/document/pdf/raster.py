from loguru import logger
from PIL import Image
from pypdfium2 import PdfBitmap, PdfPage

from .pdfium import pdfium_guard

DEFAULT_PDF_IMAGE_DPI = 200
DEFAULT_MAX_RENDER_EDGE = 3500


def _render_page_bitmap(page: PdfPage, dpi: int, max_width_or_height: int):
    """Using the exact same scaling rules within the caller's PDFium lock returns a bitmap that must be closed by the caller."""
    scale = dpi / 72
    long_side_length = max(*page.get_size())
    if (long_side_length * scale) > max_width_or_height:
        scale = max_width_or_height / long_side_length
    return page.render(scale=scale), scale


def page_to_pixel_file(page: PdfPage, path, dpi: int = DEFAULT_PDF_IMAGE_DPI):
    """Directly writes out the original bitmap buffer and decoded metadata, avoiding worker construction of PIL and full byte copies."""
    from pypdfium2.internal import BitmapTypeToStrReverse

    with pdfium_guard():
        bitmap = None
        try:
            bitmap, scale = _render_page_bitmap(page, dpi, DEFAULT_MAX_RENDER_EDGE)
            with open(path, "wb") as output:
                output.write(bitmap.buffer)
            return {
                "scale": scale,
                "mode": BitmapTypeToStrReverse[bitmap.format],
                "size": (bitmap.width, bitmap.height),
                "raw_mode": bitmap.mode,
                "stride": bitmap.stride,
                "path": path,
            }
        finally:
            if bitmap is not None:
                bitmap.close()


def estimate_page_image_bytes(page_size: tuple[float, float], dpi: int = DEFAULT_PDF_IMAGE_DPI) -> int:
    """Estimated four-channel page map bytes based on existing render scaling, used to limit batch-resident memory."""
    import math

    width, height = page_size
    scale = min(dpi / 72, DEFAULT_MAX_RENDER_EDGE / max(width, height))
    return math.ceil(width * scale) * math.ceil(height * scale) * 4


def page_to_image(
    page: PdfPage,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    max_width_or_height: int = DEFAULT_MAX_RENDER_EDGE,
) -> tuple[Image.Image, float]:
    """Renders the page as per DPI with long edge caps and holds the pixels of the returned image independently."""
    with pdfium_guard():
        bitmap: PdfBitmap | None = None
        try:
            bitmap, scale = _render_page_bitmap(page, dpi, max_width_or_height)
            image = bitmap.to_pil().copy()
        finally:
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception as e:
                    logger.error(f"Failed to close bitmap: {e}")
    return image, scale


__all__ = ["page_to_image"]


def page_to_owned_bitmap(page: PdfPage, dpi: int = DEFAULT_PDF_IMAGE_DPI):
    """Copies the PDFium bitmap to immutable bytes once, closing the handle for safe cropping and rotation by Rust."""
    with pdfium_guard():
        bitmap = None
        try:
            bitmap, _ = _render_page_bitmap(page, dpi, DEFAULT_MAX_RENDER_EDGE)
            return bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, bitmap.mode
        finally:
            if bitmap is not None:
                bitmap.close()
