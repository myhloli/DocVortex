"""PDF 文档访问、显式分类及共享文本契约。"""
from .document import PDFDocument, PDFPage, PDFPageTextGeometry, get_lines_from_chars
from .text import Bbox, Char, Line, Span

__all__ = ["PDFDocument", "PDFPage", "PDFPageTextGeometry", "get_lines_from_chars", "Bbox", "Char", "Line", "Span"]
