"""Read source publication properties and spine count from EPUB OPF."""

from __future__ import annotations
from typing import BinaryIO
from ....schema import DocumentProperties
from ....document.properties import legacy_properties
from .package import EpubPackage


def read_epub_properties(data: bytes) -> tuple[DocumentProperties, list[str]]:
    """Read the complete multi-valued OPF attribute without parsing the spine text or loading the resource."""
    package = EpubPackage(data)
    try:
        return package.document_properties.model_copy(deep=True), list(package.metadata_warnings)
    finally:
        package.close()


def extract_epub_metadata(file_binary: BinaryIO) -> dict[str, object | None]:
    """Retain the existing basic attribute interface and uniformly reuse the publication attribute reader."""
    properties, _ = read_epub_properties(file_binary.read())
    return legacy_properties(properties)


__all__ = ["extract_epub_metadata", "read_epub_properties"]
