"""文件输入准备与有效页范围，不包含档位或 OCR 路由。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from docvortex.document.contracts import HtmlSourceContext

from ..errors import InvalidRequestError
from ..schema import FILE_SUFFIXES, DocumentProperties, FileSuffix

if TYPE_CHECKING:
    from .pdf._document import PDFDocument


@dataclass(slots=True)
class PreparedSource:
    """保存输入字节、有效页面映射及底层文档的资源所有权。"""

    data: bytes
    file_suffix: FileSuffix
    source_context: HtmlSourceContext | None = None
    page_index_map: list[int] | None = None
    broken_page_indices: tuple[int, ...] = ()
    document: PDFDocument | None = None
    owns_document: bool = False
    source_properties: DocumentProperties | None = None
    metadata_warnings: tuple[str, ...] = ()
    structure_document: PDFDocument | None = None
    owns_structure_document: bool = False

    def close(self) -> None:
        """按独立所有权释放原件与选页副本，调用方借出的文档继续由其管理。"""
        try:
            if self.owns_document and self.document is not None:
                self.document.close()
        finally:
            if (
                self.owns_structure_document
                and self.structure_document is not None
                and self.structure_document is not self.document
            ):
                self.structure_document.close()


def prepare_source(
    source: str | Path | bytes | PDFDocument,
    *,
    file_suffix: FileSuffix | None = None,
    page_range: str = "",
    source_context: HtmlSourceContext | None = None,
    preserve_pdf_structure: bool = False,
) -> PreparedSource:
    """准备原生解析输入，调用者持有的 PDFDocument 不由引擎关闭。"""
    from .page_range import normalize_page_range_input, parse_page_range

    document = None
    path = Path(source) if isinstance(source, (str, Path)) else None
    if path is not None:
        data = path.read_bytes()
    elif isinstance(source, bytes):
        data = source
    else:
        from .pdf._document import PDFDocument

        if not isinstance(source, PDFDocument):
            raise TypeError("source must be a path, bytes, or PDFDocument")
        document, data, file_suffix = source, source.bytes, "pdf"
    if file_suffix:
        suffix = file_suffix
    else:
        from .detection import guess_suffix_by_bytes

        suffix = guess_suffix_by_bytes(data, str(path) if path else None)
    if suffix not in FILE_SUFFIXES:
        raise InvalidRequestError("file_type_unsupported", f"Unsupported native input format: {suffix}", "file_suffix")
    page_range = normalize_page_range_input(page_range)
    if suffix != "pdf" and page_range:
        raise InvalidRequestError("page_range_invalid", "Page ranges are supported only for PDF input", "page_range")
    if suffix == "html" and source_context is None and path is not None:
        absolute = path.resolve()
        source_context = HtmlSourceContext(source_uri=absolute.as_uri(), local_resource_root=absolute.parent)
    prepared = PreparedSource(data, cast(FileSuffix, suffix), source_context=source_context, document=document)
    if suffix == "pdf":
        from .pdf._document import PDFDocument
        from .pdf.pdfium import safe_rewrite_pdf_bytes_with_pdfium_result

        if document is None:
            document = PDFDocument(data)
            prepared.document, prepared.owns_document = document, True
        try:
            from .pdf.metadata import read_pdf_properties

            prepared.source_properties, warnings = read_pdf_properties(document)
            prepared.metadata_warnings = tuple(warnings)
            indices = parse_page_range(page_range, document.page_count)
            if indices != list(range(document.page_count)):
                keep_structure = preserve_pdf_structure and document._get_structure() is not None
                rewrite = safe_rewrite_pdf_bytes_with_pdfium_result(data, page_indices=indices)
                prepared.data = rewrite.pdf_bytes or data
                prepared.page_index_map = None if rewrite.used_original else rewrite.retained_page_indices
                prepared.broken_page_indices = tuple(rewrite.broken_page_indices)
                if keep_structure:
                    prepared.structure_document = document
                    prepared.owns_structure_document = prepared.owns_document
                elif prepared.owns_document:
                    document.close()
                prepared.document = PDFDocument(prepared.data)
                prepared.owns_document = True
                if keep_structure:
                    prepared.document._structure_source = document
                    prepared.document._structure_page_indices = prepared.page_index_map
        except Exception:
            prepared.close()
            raise
    return prepared


__all__ = ["PreparedSource", "prepare_source", "HtmlSourceContext"]
