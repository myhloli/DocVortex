"""PDF Document access, explicit classification, and shared text contracts."""

from ._document import PDFDocument, PDFPage, PDFPageTextGeometry, PDFPageVectorGeometry, get_lines_from_chars
from .pdfium import PdfiumFontError, PdfiumRuntimeInfo, initialize_pdfium_runtime
from .text import Bbox, Char, Line, Span
from .snapshot import PDFPageSnapshot

__all__ = [
    "PDFDocument",
    "PDFRenderSession",
    "PDFPage",
    "PDFPageSnapshot",
    "PDFPageTextGeometry",
    "PDFPageVectorGeometry",
    "get_lines_from_chars",
    "Bbox",
    "Char",
    "Line",
    "Span",
    "initialize_pdfium_runtime",
    "PdfiumRuntimeInfo",
    "PdfiumFontError",
]


def __getattr__(name: str):
    """The process orchestration module is only loaded when a render session is explicitly requested, maintaining document import boundaries."""
    if name == "PDFRenderSession":
        from .render_session import PDFRenderSession

        return PDFRenderSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
