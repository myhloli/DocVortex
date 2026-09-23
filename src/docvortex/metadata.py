"""独立元数据调度层，显式调用各格式的只读属性提取器。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from .errors import DocumentError, InvalidRequestError
from .result import Diagnostic, MetadataResult
from .schema import FILE_SUFFIXES, DocumentMetadata, DocumentProperties, FileSuffix, Producer
from .version import __version__

_OOXML_SUFFIXES = frozenset({"docx", "pptx", "xlsx"})
_METADATA_INPUT_LIMIT = 512 * 1024 * 1024

if TYPE_CHECKING:
    from .document.contracts import HtmlSourceContext
    from .document.pdf._document import PDFDocument


def extract_metadata(
    source: str | Path | bytes | PDFDocument,
    *,
    file_suffix: FileSuffix | None = None,
    source_context: HtmlSourceContext | None = None,
) -> MetadataResult:
    """仅读取文档属性；未知格式和打不开的输入使用稳定错误码。"""
    document = None
    path = Path(source) if isinstance(source, (str, Path)) else None
    data: bytes | None = None
    try:
        if path is not None:
            suffix_hint = file_suffix or path.suffix.lower().lstrip(".")
            if suffix_hint in _OOXML_SUFFIXES:
                suffix = suffix_hint
            else:
                with path.open("rb") as stream:
                    data = stream.read(_METADATA_INPUT_LIMIT + 1)
        elif isinstance(source, bytes):
            data = source
        else:
            from .document.pdf._document import PDFDocument

            if not isinstance(source, PDFDocument):
                raise TypeError("source must be a path, bytes, or PDFDocument")
            document = source
            data = document.bytes
        if document is not None:
            suffix = "pdf"
        elif path is None or data is not None:
            if data is None:
                raise AssertionError("metadata source bytes are missing")
            if file_suffix is not None:
                suffix = file_suffix
            else:
                from .document.detection import guess_suffix_by_bytes

                suffix = guess_suffix_by_bytes(data, str(path) if path else None)
        if suffix not in FILE_SUFFIXES:
            raise InvalidRequestError("file_type_unsupported", f"Unsupported native input format: {suffix}", "file_suffix")
        if data is not None and len(data) > _METADATA_INPUT_LIMIT and suffix not in _OOXML_SUFFIXES:
            raise DocumentError("resource_limit", "Metadata input exceeds 512 MiB")
        if path is not None and suffix in _OOXML_SUFFIXES and data is None:
            properties, messages = _read_properties(path, cast(FileSuffix, suffix), document, source_context)
        else:
            if data is None:
                raise AssertionError("metadata source bytes are missing")
            properties, messages = _read_properties(data, cast(FileSuffix, suffix), document, source_context)
    except (DocumentError, TypeError):
        raise
    except Exception as exc:
        raise DocumentError("open_failed", f"Cannot read document metadata: {exc}") from exc
    return MetadataResult(
        DocumentMetadata(
            file_suffix=cast(FileSuffix, suffix),
            producer=Producer(name="docvortex", version=__version__),
            document=properties,
        ),
        tuple(Diagnostic("read_metadata_failed", message) for message in messages),
    )


def _read_properties(
    data: bytes | Path,
    suffix: FileSuffix,
    document: PDFDocument | None,
    source_context: HtmlSourceContext | None,
) -> tuple[DocumentProperties, list[str]]:
    """按格式惰性加载读取器，不加载其他格式正文解析或推理模型。"""
    if suffix == "pdf":
        from .document.pdf._document import PDFDocument
        from .document.pdf.metadata import read_pdf_properties

        if document is not None:
            return read_pdf_properties(document)
        with PDFDocument(data) as owned:
            return read_pdf_properties(owned)
    if suffix in {"docx", "pptx", "xlsx"}:
        from .analyzers.native.office.metadata import read_ooxml_properties

        return read_ooxml_properties(data, suffix)
    if suffix in {"doc", "ppt", "xls"}:
        from .analyzers.native.office.metadata import read_ole_properties

        return read_ole_properties(data, suffix)
    if suffix in {"odt", "ods", "odp"}:
        from .analyzers.native.office.odf.constants import OdfSuffix
        from .analyzers.native.office.odf.metadata import read_odf_properties

        return read_odf_properties(data, cast(OdfSuffix, suffix))
    if suffix == "epub":
        from .analyzers.native.epub.metadata import read_epub_properties

        return read_epub_properties(data)
    if suffix == "ofd":
        from .analyzers.native.ofd.metadata import read_ofd_properties

        return read_ofd_properties(data)
    if suffix == "rtf":
        from .analyzers.native.office.rtf.metadata import read_rtf_properties

        return read_rtf_properties(data)
    if suffix == "html":
        from .analyzers.native.html.metadata import read_html_properties

        return read_html_properties(data, source_context)
    if suffix == "mhtml":
        from .analyzers.native.mhtml.archive import MhtmlArchive
        from .analyzers.native.mhtml.converter import read_archive_properties

        return read_archive_properties(MhtmlArchive(data, source_context))
    return DocumentProperties(page_count=1, page_count_kind="logical"), []


__all__ = ["extract_metadata"]
