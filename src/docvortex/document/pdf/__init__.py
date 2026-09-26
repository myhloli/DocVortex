"""PDF 文档访问、显式分类及共享文本契约。"""

from ._document import PDFDocument, PDFPage, PDFPageTextGeometry, PDFPageVectorGeometry, get_lines_from_chars
from .pdfium import PdfiumFontError, PdfiumRuntimeInfo, initialize_pdfium_runtime
from .text import Bbox, Char, Line, Span
from .snapshot import PDFPageSnapshot

__all__ = [
    "PDFDocument",
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
