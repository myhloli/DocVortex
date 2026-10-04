"""Project the MHTML master document and archive resources into existing HTML semantic blocks."""

from __future__ import annotations

from typing import BinaryIO

from docvortex.document.contracts import HtmlSourceContext
from docvortex.document.mhtml import MAX_ARCHIVE_BYTES, MhtmlArchive
from docvortex.result import Diagnostic
from docvortex.schema import DocumentProperties

from ..html.converter import HtmlConverter
from ..html.document import HtmlDocument, parse_html_document
from ..html.metadata import read_html_properties
from .resources import MhtmlResourceContext


def read_archive_properties(archive: MhtmlArchive) -> tuple[DocumentProperties, list[str]]:
    """Reuse HTML declaration metadata and only use MIME Subject to supplement missing titles."""
    properties, warnings = read_html_properties(archive.html, archive.source_context)
    properties.title = properties.title or archive.subject
    return properties, warnings


class MhtmlConverter(HtmlConverter):
    """Generate text, metadata and optional resource diagnostics through one unpacking."""

    def __init__(self) -> None:
        """Initialize metadata and diagnostics bound to a single conversion."""
        super().__init__()
        self.properties = DocumentProperties()
        self.diagnostics: tuple[Diagnostic, ...] = ()

    def convert(self, file_binary: BinaryIO, *, source_context: HtmlSourceContext | None = None) -> None:
        """Shared projection is called after loading the main HTML, without merging iframe or other HTML attachments."""
        archive = MhtmlArchive(file_binary.read(MAX_ARCHIVE_BYTES + 1), source_context)
        self.properties, warnings = read_archive_properties(archive)
        document = parse_html_document(archive.html, archive.source_context)
        if document.title is None and archive.subject:
            from dataclasses import replace

            document = replace(document, title=archive.subject)
        resources = MhtmlResourceContext(archive, base_href=document.base_href)
        _restore_picture_sources(document, archive)
        self._convert_document(document, resources)
        self.diagnostics = tuple(Diagnostic("read_metadata_failed", warning) for warning in warnings) + tuple(
            archive.diagnostics
        )


def _restore_picture_sources(document: HtmlDocument, archive: MhtmlArchive) -> None:
    """When the rollback picture is not archived, the saved picture/srcset candidate is used and no external request is initiated."""
    for image in document.body.iter("img"):
        if (image.get("src") or "").strip().casefold().startswith("data:"):
            continue
        if archive.find(image.get("src") or "", base_href=document.base_href) is not None:
            continue
        picture = next((parent for parent in image.iterancestors() if parent.tag == "picture"), None)
        sources = list(picture.iter("source")) if picture is not None else []
        for source in [*sources, image]:
            # The network candidates in the browser archive are separated by commas; data URI No need to do archive matching.
            candidates = (source.get("srcset") or "").split(",")
            selected = next(
                (
                    fields[0]
                    for candidate in candidates
                    if (fields := candidate.split()) and archive.find(fields[0], base_href=document.base_href) is not None
                ),
                None,
            )
            if selected:
                image.set("src", selected)
                break
