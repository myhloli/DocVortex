"""Identifies input suffixes supported by DocVortex based on file content and container structure."""

from __future__ import annotations

from io import BytesIO
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from loguru import logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..foundation.magika import Magika

from .filetypes import CSV_EXTENSIONS, HTML_EXTENSIONS, IMAGE_EXTENSIONS, has_mhtml_header, rtf_header_offset

PDF_SIG_BYTES = b"%PDF"
OLE2_SIG_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OOXML_ROOT_RELS = "_rels/.rels"
OOXML_CONTENT_TYPES = "[Content_Types].xml"
OOXML_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OOXML_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OOXML_OFFICE_DOCUMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
# ISO Strict OOXML uses the purl.oclc.org relationship type, and both main document relationships are recognized.
OOXML_OFFICE_DOCUMENT_RELS = frozenset(
    {
        OOXML_OFFICE_DOCUMENT_REL,
        "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
    }
)
OOXML_MAIN_CONTENT_TYPES = {
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"): "docx",
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"): "pptx",
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"): "xlsx",
}
# Main document root tag (containing Strict/Transitional namespace) → suffix to avoid treating comments or other XML as the main document.
OOXML_MAIN_PART_ROOT_TAG_SUFFIXES = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document": "docx",
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}document": "docx",
    "{http://schemas.openxmlformats.org/presentationml/2006/main}presentation": "pptx",
    "{http://purl.oclc.org/ooxml/presentationml/main}presentation": "pptx",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook": "xlsx",
    "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook": "xlsx",
}
OOXML_CONVENTIONAL_MAIN_PARTS = ("word/document.xml", "ppt/presentation.xml", "xl/workbook.xml")
# Magika will make family-level misjudgments for manual/non-standard Office packages (for example, docx is recognized as xlsx), and the extension name within this family shall prevail.
OFFICE_PACKAGE_FAMILY_SUFFIXES = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})
OOXML_PACKAGE_SUFFIXES = frozenset({"docx", "pptx", "xlsx"})

ODF_MIMETYPE_SUFFIXES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
}
ODF_MANIFEST_PATH = "META-INF/manifest.xml"
ODF_MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
# OLE2 compound file internal stream name → old Office format suffix
# doc: WordDocument stream; xls: Workbook or Book stream; ppt: PowerPoint Document stream
OLE2_STREAM_SUFFIX_MAP: dict[str, str] = {
    "WordDocument": "doc",
    "Workbook": "xls",
    "Book": "xls",
    "PowerPoint Document": "ppt",
}
_STRONG_CONTENT_SUFFIXES = frozenset(
    {
        "pdf",
        "doc",
        "docx",
        "ppt",
        "pptx",
        "xls",
        "xlsx",
        "rtf",
        "epub",
        "mhtml",
        "ofd",
        "odt",
        "ods",
        "odp",
        *IMAGE_EXTENSIONS,
    }
)


@lru_cache(maxsize=1)
def _magika() -> Magika:
    """Lazy creation of file type recognizers to avoid loading models when importing parser."""
    from ..foundation.magika import Magika

    return Magika()


def _strip_package_part_name(part_name: str | None) -> str:
    """Normalize the OPC part path to facilitate matching of PartName in Content_Types."""
    if not part_name:
        return ""
    return part_name.replace("\\", "/").lstrip("/")


def _ooxml_relationship_targets(root: ElementTree.Element) -> list[str]:
    """Extracts the Office main document relationship target from the root relationship file, compatible with Strict and Transitional relationship types."""
    targets = []
    for relationship in root:
        if relationship.tag not in {
            f"{{{OOXML_PACKAGE_REL_NS}}}Relationship",
            "Relationship",
        }:
            continue
        if relationship.get("TargetMode") == "External":
            continue
        if relationship.get("Type") not in OOXML_OFFICE_DOCUMENT_RELS:
            continue
        target = _strip_package_part_name(relationship.get("Target"))
        if target:
            targets.append(target)
    return targets


def _ooxml_content_type_overrides(root: ElementTree.Element) -> dict[str, str]:
    """Read the ContentType mapping for each explicit part in Content_Types."""
    overrides = {}
    for override in root:
        if override.tag not in {
            f"{{{OOXML_CONTENT_TYPES_NS}}}Override",
            "Override",
        }:
            continue
        part_name = _strip_package_part_name(override.get("PartName"))
        content_type = override.get("ContentType")
        if part_name and content_type:
            overrides[part_name] = content_type
    return overrides


def _ooxml_main_part_suffix(package: ZipFile, part_name: str) -> str | None:
    """Parse the actual root tag type of the main document part and be compatible with the Strict/Transitional namespace."""
    try:
        with package.open(part_name) as stream:
            for _, root in ElementTree.iterparse(stream, events=("start",)):
                return OOXML_MAIN_PART_ROOT_TAG_SUFFIXES.get(root.tag)
    except (KeyError, ElementTree.ParseError, RuntimeError, OSError, ValueError):
        return None
    return None


def _guess_ooxml_suffix_from_zip(package: ZipFile) -> str | None:
    """Determine the Office subtype based on the standard main document relationship and main content type in the OOXML package.

    Content_Types for non-standard packages Override may be missing or point to a non-existent part, and the relationship type may
    It is a variant of ISO Strict; when the Override judgment fails, go to the root element of the main document part and then return to the conventional path.
    """
    try:
        rels_root = ElementTree.fromstring(package.read(OOXML_ROOT_RELS))
    except (KeyError, ElementTree.ParseError):
        rels_root = None
    try:
        content_types_root = ElementTree.fromstring(package.read(OOXML_CONTENT_TYPES))
        overrides = _ooxml_content_type_overrides(content_types_root)
    except (KeyError, ElementTree.ParseError):
        overrides = {}

    targets = _ooxml_relationship_targets(rels_root) if rels_root is not None else []
    for target in targets:
        suffix = OOXML_MAIN_CONTENT_TYPES.get(overrides.get(target, ""))
        if suffix:
            return suffix
    for target in targets:
        suffix = _ooxml_main_part_suffix(package, target)
        if suffix:
            return suffix
    for part_name in OOXML_CONVENTIONAL_MAIN_PARTS:
        try:
            package.getinfo(part_name)
        except KeyError:
            continue
        suffix = _ooxml_main_part_suffix(package, part_name)
        if suffix:
            return suffix
    return None


def _guess_ooxml_suffix_by_bytes(file_bytes: bytes) -> str | None:
    """Priority is given to using the OOXML package structure to identify docx/pptx/xlsx to avoid Magika being misled by embedded objects."""
    try:
        with ZipFile(BytesIO(file_bytes)) as package:
            return _guess_ooxml_suffix_from_zip(package)
    except (
        BadZipFile,
        KeyError,
        ElementTree.ParseError,
        RuntimeError,
        OSError,
        ValueError,
    ):
        return None


def _guess_ooxml_suffix_by_path(file_path: Path) -> str | None:
    """Read the OOXML package structure from the file path; if it fails, leave it to the original logic of Magika."""
    try:
        with ZipFile(file_path) as package:
            return _guess_ooxml_suffix_from_zip(package)
    except (
        BadZipFile,
        KeyError,
        ElementTree.ParseError,
        RuntimeError,
        OSError,
        ValueError,
    ):
        return None


def _guess_odf_suffix_from_zip(package: ZipFile) -> str | None:
    """Identify odt/ods/odp by ODF mimetype, manifest root entries in sequence."""
    try:
        mimetype_info = package.getinfo("mimetype")
        if mimetype_info.file_size <= 256:
            mimetype = package.read(mimetype_info).decode("ascii", errors="strict").strip()
            if suffix := ODF_MIMETYPE_SUFFIXES.get(mimetype):
                return suffix
    except (KeyError, UnicodeDecodeError, RuntimeError, OSError, ValueError):
        pass
    try:
        manifest_info = package.getinfo(ODF_MANIFEST_PATH)
        if manifest_info.file_size > 1024 * 1024:
            return None
        root = ElementTree.fromstring(package.read(manifest_info))
    except (KeyError, ElementTree.ParseError, RuntimeError, OSError, ValueError):
        return None
    for entry in root.iter(f"{{{ODF_MANIFEST_NS}}}file-entry"):
        if entry.get(f"{{{ODF_MANIFEST_NS}}}full-path") != "/":
            continue
        media_type = entry.get(f"{{{ODF_MANIFEST_NS}}}media-type", "").strip()
        return ODF_MIMETYPE_SUFFIXES.get(media_type)
    return None


def _guess_odf_suffix_by_bytes(file_bytes: bytes) -> str | None:
    """Identify ODF from memory ZIP packet, failure will not affect subsequent OLE/Magika/CSV routing."""
    try:
        with ZipFile(BytesIO(file_bytes)) as package:
            return _guess_odf_suffix_from_zip(package)
    except (BadZipFile, RuntimeError, OSError, ValueError):
        return None


def _guess_odf_suffix_by_path(file_path: Path) -> str | None:
    """Identify ODF from path ZIP packet, maintaining existing OOXML detection priority."""
    try:
        with ZipFile(file_path) as package:
            return _guess_odf_suffix_from_zip(package)
    except (BadZipFile, RuntimeError, OSError, ValueError):
        return None


def _guess_epub_suffix_by_bytes(file_bytes: bytes) -> str | None:
    """Verify EPUB strong content identity from memory ZIP package."""
    from ..analyzers.native.epub import detect_epub

    return "epub" if detect_epub(file_bytes) else None


def _guess_epub_suffix_by_path(file_path: Path) -> str | None:
    """Verify EPUB strong content identity from path ZIP package."""
    from ..analyzers.native.epub import detect_epub_path

    return "epub" if detect_epub_path(file_path) else None


def _guess_ofd_suffix_by_bytes(file_bytes: bytes) -> str | None:
    """Verify OFD strong content identity from memory ZIP package."""
    from ..analyzers.native.ofd import detect_ofd

    return "ofd" if detect_ofd(file_bytes) else None


def _guess_ofd_suffix_by_path(file_path: Path) -> str | None:
    """Verify OFD strong content identity from path ZIP package."""
    from ..analyzers.native.ofd import detect_ofd_path

    return "ofd" if detect_ofd_path(file_path) else None


def _guess_ole2_suffix_by_bytes(file_bytes: bytes) -> str | None:
    """Differentiate doc/xls/ppt with OLE2 magic + olefile internal stream.

    olefile is a pure Python library and already a core dependency (used by mineru.model.flash.office.legacy).
    Insert this layer after OOXML fails to be recognized and before Magika, to prevent Magika from returning unknown to OLE2.
    """
    if len(file_bytes) < 8 or file_bytes[:8] != OLE2_SIG_BYTES:
        return None
    try:
        import olefile  # type: ignore[import-untyped]

        with olefile.OleFileIO(BytesIO(file_bytes)) as ole:
            for stream_name in ole.listdir(streams=True):
                name = "/".join(stream_name)
                suffix = OLE2_STREAM_SUFFIX_MAP.get(name)
                if suffix:
                    return suffix
    except Exception:
        return None
    return None


def _guess_ole2_suffix_by_path(file_path: Path) -> str | None:
    """Reads the OLE2 container from the file path and recognizes the old Office format."""
    try:
        with open(file_path, "rb") as f:
            return _guess_ole2_suffix_by_bytes(f.read())
    except OSError:
        return None


def _has_pdf_signature_by_path(file_path: Path) -> bool:
    """Read the file header to determine whether the content pointed to by the path has a strong PDF signature."""
    try:
        with open(file_path, "rb") as file:
            return file.read(len(PDF_SIG_BYTES)) == PDF_SIG_BYTES
    except OSError:
        return False


def _has_rtf_signature_by_path(file_path: Path) -> bool:
    """Read the limited file header and identify the RTF root group by sharing rules."""
    try:
        with open(file_path, "rb") as file:
            return rtf_header_offset(file.read(128)) is not None
    except OSError:
        return False


def _resolve_signatureless_csv_suffix(detected_suffix: str, file_path: str | Path | None) -> str:
    """Unsigned delimited text with .csv/.tsv extension: Returns an independent suffix by extension and preserves the precedence of strong content types."""
    extension = Path(file_path).suffix.lower().lstrip(".") if file_path else ""
    if extension in CSV_EXTENSIONS:
        if detected_suffix in _STRONG_CONTENT_SUFFIXES:
            return detected_suffix
        return extension
    if detected_suffix in ("csv", "tsv"):
        if extension in ODF_MIMETYPE_SUFFIXES.values():
            return "txt"
        return extension or "txt"
    return detected_suffix


def _resolve_signatureless_html_suffix(detected_suffix: str, file_path: str | Path | None) -> str:
    """Use HTML_EXTENSIONS to capture the short text, and standardize the HTML results of Magika to html."""
    extension = Path(file_path).suffix.lower().lstrip(".") if file_path else ""
    if extension in HTML_EXTENSIONS and detected_suffix not in _STRONG_CONTENT_SUFFIXES:
        return "html"
    return "html" if detected_suffix == "html" else detected_suffix


def _reject_unverified_package_suffix(detected_suffix: str) -> str:
    """Reject ODF/EPUB/OFD type that does not pass package authentication and is only guessed by heuristic tools."""
    package_suffixes = {*ODF_MIMETYPE_SUFFIXES.values(), "epub", "ofd"}
    return "unknown" if detected_suffix in package_suffixes else detected_suffix


def _prefer_extension_over_office_guess(suffix: str, file_path: str | Path | None) -> str:
    """Magika has a family-level misjudgment for non-standard Office packages. If there is a conflict in the same family, the extension name shall prevail."""
    if not file_path:
        return suffix
    extension = Path(file_path).suffix.lower().lstrip(".")
    if suffix in OFFICE_PACKAGE_FAMILY_SUFFIXES and extension in OFFICE_PACKAGE_FAMILY_SUFFIXES and suffix != extension:
        return extension
    return suffix


def _has_office_container(source: bytes | Path, extension: str) -> bool:
    """Verify the ZIP/OPC or OLE2 container corresponding to the extension and reject the Office fallback for ordinary unknown binaries."""
    if extension in OOXML_PACKAGE_SUFFIXES:
        try:
            with ZipFile(BytesIO(source) if isinstance(source, bytes) else source) as package:
                content_types = ElementTree.fromstring(package.read(OOXML_CONTENT_TYPES))
                relationships = ElementTree.fromstring(package.read(OOXML_ROOT_RELS))
                return content_types.tag in {
                    f"{{{OOXML_CONTENT_TYPES_NS}}}Types",
                    "Types",
                } and relationships.tag in {
                    f"{{{OOXML_PACKAGE_REL_NS}}}Relationships",
                    "Relationships",
                }
        except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, OSError, ValueError):
            return False
    try:
        import olefile  # type: ignore[import-untyped]

        with olefile.OleFileIO(BytesIO(source) if isinstance(source, bytes) else str(source)) as package:
            return bool(package.listdir(streams=True))
    except (OSError, ValueError, RuntimeError, IndexError):
        return False


def _resolve_signatureless_package_suffix(
    suffix: str,
    file_path: str | Path | None,
    source: bytes | Path,
) -> str:
    """When content sniffing is unknown, use the Office family extension fallback only for authenticated containers."""
    if suffix != "unknown" or not file_path:
        return suffix
    extension = Path(file_path).suffix.lower().lstrip(".")
    if extension in OFFICE_PACKAGE_FAMILY_SUFFIXES and _has_office_container(source, extension):
        return extension
    return suffix


def guess_suffix_by_bytes(file_bytes: bytes, file_path: str | None = None) -> str:
    """Prioritize format recognition based on strong signatures and container headers, and then use universal recognizers."""
    if file_bytes[: len(PDF_SIG_BYTES)] == PDF_SIG_BYTES:
        return "pdf"
    if rtf_header_offset(file_bytes[:128]) is not None:
        return "rtf"
    if has_mhtml_header(file_bytes):
        return "mhtml"

    ofd_suffix = _guess_ofd_suffix_by_bytes(file_bytes)
    if ofd_suffix:
        return ofd_suffix

    epub_suffix = _guess_epub_suffix_by_bytes(file_bytes)
    if epub_suffix:
        return epub_suffix

    ooxml_suffix = _guess_ooxml_suffix_by_bytes(file_bytes)
    if ooxml_suffix:
        return ooxml_suffix

    odf_suffix = _guess_odf_suffix_by_bytes(file_bytes)
    if odf_suffix:
        return odf_suffix

    ole2_suffix = _guess_ole2_suffix_by_bytes(file_bytes)
    if ole2_suffix:
        return ole2_suffix

    suffix = _magika().identify_bytes(file_bytes).prediction.output.label
    if (
        file_path
        and suffix in ["ai", "html"]
        and Path(file_path).suffix.lower() in [".pdf"]
        and file_bytes[:4] == PDF_SIG_BYTES
    ):
        suffix = "pdf"
    suffix = _prefer_extension_over_office_guess(suffix, file_path)
    suffix = _resolve_signatureless_csv_suffix(_reject_unverified_package_suffix(suffix), file_path)
    suffix = _resolve_signatureless_html_suffix(suffix, file_path)
    return _resolve_signatureless_package_suffix(suffix, file_path, file_bytes)


def guess_suffix_by_path(file_path: str | Path) -> str:
    """Read the file signature and bounded MIME header, keeping the path consistent with the byte entry."""
    if not isinstance(file_path, Path):
        file_path = Path(file_path)

    if _has_rtf_signature_by_path(file_path):
        return "rtf"

    ofd_suffix = _guess_ofd_suffix_by_path(file_path)
    if ofd_suffix:
        return ofd_suffix

    epub_suffix = _guess_epub_suffix_by_path(file_path)
    if epub_suffix:
        return epub_suffix

    ooxml_suffix = _guess_ooxml_suffix_by_path(file_path)
    if ooxml_suffix:
        return ooxml_suffix

    odf_suffix = _guess_odf_suffix_by_path(file_path)
    if odf_suffix:
        return odf_suffix

    ole2_suffix = _guess_ole2_suffix_by_path(file_path)
    if ole2_suffix:
        return ole2_suffix

    if _has_pdf_signature_by_path(file_path):
        return "pdf"

    with file_path.open("rb") as source:
        if has_mhtml_header(source.read(65536)):
            return "mhtml"

    suffix = _magika().identify_path(file_path).prediction.output.label
    if suffix in ["ai", "html"] and file_path.suffix.lower() in [".pdf"]:
        try:
            with open(file_path, "rb") as f:
                if f.read(4) == PDF_SIG_BYTES:
                    suffix = "pdf"
        except Exception as e:
            logger.warning(f"Failed to read file {file_path} for PDF signature check: {e}")
    suffix = _prefer_extension_over_office_guess(suffix, file_path)
    suffix = _resolve_signatureless_csv_suffix(_reject_unverified_package_suffix(suffix), file_path)
    suffix = _resolve_signatureless_html_suffix(suffix, file_path)
    return _resolve_signatureless_package_suffix(suffix, file_path, file_path)


__all__ = ["guess_suffix_by_bytes", "guess_suffix_by_path"]
