"""Flash PDF, EPUB, HTML, OFD, CSV and Office/RTF document models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, BinaryIO

from docvortex.document.contracts import HtmlSourceContext

if TYPE_CHECKING:
    from ...document.pdf._document import PDFDocument


class PdfModel:
    """The Flash native PDF pipeline is packaged as a stateless model."""

    def predict(self, pdf_doc: PDFDocument) -> list[list[dict[str, Any]]]:
        """Analyze the PDFDocument held by the caller, and uniformly output visible English numbers after all text matching is completed."""
        from ...content import normalize_pdf_model_text
        from .pdf import pipeline

        pages = pipeline._analyze_native_document(pdf_doc)
        normalize_pdf_model_text(pages)
        return pages


class CsvModel:
    """Wrap CSV delimiter text into a stateless Flash model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the CSV binary stream held by the caller and return the single logical page model_list."""
        from .csv import convert_csv

        return convert_csv(file_binary)


class EpubModel:
    """Wrap the EPUB OCF/OPF document into a stateless Flash model."""

    def predict(
        self,
        file_binary: BinaryIO,
    ) -> list[list[dict[str, Any]]]:
        """Convert the entire EPUB stream held by the caller, and return the directory page and all text logical pages."""
        from .epub.converter import EpubConverter

        converter = EpubConverter()
        converter.convert(file_binary)
        return converter.pages


class HtmlModel:
    """Wrap the standalone HTML document into a stateless Flash model."""

    def predict(
        self,
        file_binary: BinaryIO,
        *,
        source_context: HtmlSourceContext | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Convert static HTML stream and return single logical page model_list."""
        from .html.converter import HtmlConverter

        converter = HtmlConverter()
        converter.convert(file_binary, source_context=source_context)
        return converter.pages


class OfdModel:
    """Wrap OFD fixed layout parsing, retaining the most recently called non-fatal diagnosis."""

    def __init__(self) -> None:
        """Initialize instance-level diagnosis to avoid cross-using results between different documents."""
        self.diagnostics: list[dict[str, Any]] = []

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the entire OFD stream held by the caller and return model-list on a per-physical page basis."""
        from .ofd.converter import OfdConverter

        self.diagnostics = []
        converter = OfdConverter()
        converter.convert(file_binary)
        self.diagnostics = converter.diagnostics
        return converter.pages


class RtfModel:
    """Wrap the Rich Text Format document into a stateless Flash model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the RTF binary stream held by the caller and return the single logical page model_list."""
        from .office.rtf.converter import RtfConverter

        converter = RtfConverter()
        converter.convert(file_binary)
        return converter.pages


class DocxModel:
    """Pack DOCX Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the DOCX binary stream held by the caller and return the paged model_list."""

        # Delay loading of Converter to avoid early loading of Office dependencies in pure PDF paths.
        from .office.docx.docx_converter import DocxConverter

        converter = DocxConverter()
        converter.convert(file_binary)
        return converter.pages


class DocModel:
    """Wrap Word 97–2003 Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the DOC binary stream held by the caller and return section to model-list."""

        # Delay loading of the old version of DOC parser to prevent other formats from loading olefile in advance.
        from .office.doc.doc_converter import DocConverter

        converter = DocConverter()
        converter.convert(file_binary)
        return converter.pages


class PptxModel:
    """Package PPTX Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the PPTX binary stream held by the caller and return the paged model_list."""

        # Delay loading of Converter to avoid early loading of Office dependencies in pure PDF paths.
        from .office.pptx.pptx_converter import PptxConverter

        converter = PptxConverter()
        converter.convert(file_binary)
        return converter.pages


class PptModel:
    """Wrap PowerPoint 97–2003 Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Convert the PPT binary stream held by the caller and return slide-by-slide model-list."""

        # Delay loading of the old version of PPT parser to prevent other formats from loading olefile in advance.
        from .office.ppt.ppt_converter import PptConverter

        converter = PptConverter()
        converter.convert(file_binary)
        return converter.pages


class XlsModel:
    """Wrap Excel 97–2003 Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Converts the XLS binary stream held by the caller and returns model-list per worksheet."""

        # Delay loading of legacy XLS parsers to avoid early loading of olefile/openpyxl for other formats.
        from .office.xls.xls_converter import XlsConverter

        converter = XlsConverter()
        converter.convert(file_binary)
        return converter.pages


class XlsxModel:
    """Wrap XLSX Converter as a stateless model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Converts the XLSX binary stream held by the caller and returns paged model_list."""

        # Delay loading of Converter to avoid early loading of Office dependencies in pure PDF paths.
        from .office.xlsx.xlsx_converter import XlsxConverter

        converter = XlsxConverter()
        converter.convert(file_binary)
        return converter.pages


class OdtModel:
    """Wrap OpenDocument Text into a stateless Flash model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Converts the ODT binary stream held by the caller and returns paged model_list."""
        from .office.odf.converters import OdtConverter

        converter = OdtConverter()
        converter.convert(file_binary)
        return converter.pages


class OdsModel:
    """Wrap OpenDocument Spreadsheet into a stateless Flash model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Converts the ODS binary stream held by the caller and returns model_list per worksheet."""
        from .office.odf.converters import OdsConverter

        converter = OdsConverter()
        converter.convert(file_binary)
        return converter.pages


class OdpModel:
    """Wrap OpenDocument Presentation into a stateless Flash model."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Converts the ODP binary stream held by the caller and returns slide-by-slide model_list."""
        from .office.odf.converters import OdpConverter

        converter = OdpConverter()
        converter.convert(file_binary)
        return converter.pages
