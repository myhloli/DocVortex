"""PDF 文档访问、显式分类及共享文本契约。"""

from .document import PDFDocument, PDFPage, PDFPageTextGeometry, get_lines_from_chars
from .text import Bbox, Char, Line, Span
from .pdfium import PdfiumFontError, PdfiumRuntimeInfo, initialize_pdfium_runtime

__all__ = [
    "PDFDocument",
    "PDFPage",
    "PDFPageTextGeometry",
    "get_lines_from_chars",
    "Bbox",
    "Char",
    "Line",
    "Span",
    "initialize_pdfium_runtime",
    "PdfiumRuntimeInfo",
    "PdfiumFontError",
]
