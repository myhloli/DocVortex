"""OOXML Content sniffing determination test for non-standard packets (ghost Override, Strict relationships, non-conventional paths)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docvortex.document.detection import (
    _guess_ooxml_suffix_by_bytes,
    _prefer_extension_over_office_guess,
    _resolve_signatureless_package_suffix,
    guess_suffix_by_bytes,
    guess_suffix_by_path,
)

WORD_MAIN_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PPT_MAIN_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
TRANSITIONAL_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
STRICT_OFFICE_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"

GHOST_OVERRIDES = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{WORD_MAIN_CT}"/>'
    '<Override PartName="/word/styles.xml" ContentType='
    '"application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)


def _build_ooxml_package(
    *,
    content_types: str = GHOST_OVERRIDES,
    office_rel: str = TRANSITIONAL_OFFICE_REL,
    main_part: str = "content/main.xml",
    main_xml: bytes = b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    include_root_rels: bool = True,
) -> bytes:
    """Constructs a parameterizable minimal OOXML zip package."""
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        if include_root_rels:
            package.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{office_rel}" Target="{main_part}"/>'
                "</Relationships>",
            )
        package.writestr(main_part, main_xml)
    return output.getvalue()


def test_ghost_override_falls_back_to_main_part_root_element() -> None:
    """When Override points to part (ghost Override), the type is determined based on the main part root element."""
    package = _build_ooxml_package()  # rels points to content/main.xml, no corresponding Override
    assert guess_suffix_by_bytes(package, "handmade-altpath.docx") == "docx"


def test_main_part_comment_cannot_spoof_root_element() -> None:
    """The XML comment before the main document root element must not misinterpret DOCX as PPTX."""
    package = _build_ooxml_package(
        main_xml=(
            b'<?xml version="1.0"?><!-- <presentation> -->'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        ),
    )
    assert guess_suffix_by_bytes(package) == "docx"


def test_main_part_requires_ooxml_namespace() -> None:
    """The root element with the same name as the irrelevant XML cannot be treated as the main document of Office."""
    package = _build_ooxml_package(main_xml=b'<document xmlns="urn:unrelated"/>')
    assert _guess_ooxml_suffix_by_bytes(package) is None


def test_ghost_override_presentation_root_detects_pptx() -> None:
    """When the main part root element is p:presentation, it should be judged as pptx and no longer fall into the trap of Magika."""
    package = _build_ooxml_package(
        main_xml=(
            b'<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
        ),
        main_part="deck/pres.xml",
    )
    assert guess_suffix_by_bytes(package, "handmade-altpath.pptx") == "pptx"


def test_strict_office_relationship_is_accepted() -> None:
    """ISO Strict The relationship type should also be recognized and typed as the main document relationship."""
    package = _build_ooxml_package(
        office_rel=STRICT_OFFICE_REL,
        main_xml=(b'<?xml version="1.0"?><w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"/>'),
    )
    assert guess_suffix_by_bytes(package) == "docx"


def test_conventional_main_part_path_detects_docx() -> None:
    """When the root relationship is missing, the root element of the conventional path word/document.xml will be used."""
    package = _build_ooxml_package(include_root_rels=False, main_part="word/document.xml")
    assert guess_suffix_by_bytes(package) == "docx"


def test_proper_override_still_wins_over_root_element() -> None:
    """The determination of the main document Override of the normal package remains in priority and is not affected by the cover-up logic."""
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/ppt/presentation.xml" ContentType="{PPT_MAIN_CT}"/>'
        "</Types>"
    )
    package = _build_ooxml_package(
        content_types=content_types,
        main_part="ppt/presentation.xml",
        main_xml=(
            b'<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
        ),
    )
    assert guess_suffix_by_bytes(package) == "pptx"


def test_extension_wins_over_conflicting_office_guess() -> None:
    """Magika When a family-level misjudgment conflicts with an extension, the extension shall prevail."""
    assert _prefer_extension_over_office_guess("xlsx", "handmade-altpath.docx") == "docx"
    assert _prefer_extension_over_office_guess("docx", "deck.pptx") == "pptx"
    assert _prefer_extension_over_office_guess("docx", "plain.docx") == "docx"
    assert _prefer_extension_over_office_guess("docx", None) == "docx"
    assert _prefer_extension_over_office_guess("pdf", "note.docx") == "pdf"


def test_unknown_falls_back_to_office_extension() -> None:
    """Only real Office containers can use extension fallback after failed content sniffing."""
    package = _build_ooxml_package()
    unknown = b"\xff" * 256
    ordinary_zip = BytesIO()
    with ZipFile(ordinary_zip, "w", ZIP_DEFLATED) as archive:
        archive.writestr("readme.txt", "not an Office package")
    ole_header_only = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    assert _resolve_signatureless_package_suffix("unknown", "handmade-altpath.pptx", package) == "pptx"
    assert _resolve_signatureless_package_suffix("unknown", "archive.zip", package) == "unknown"
    assert _resolve_signatureless_package_suffix("txt", "note.docx", package) == "txt"
    assert _resolve_signatureless_package_suffix("unknown", None, package) == "unknown"
    assert _resolve_signatureless_package_suffix("unknown", "fake.docx", unknown) == "unknown"
    assert _resolve_signatureless_package_suffix("unknown", "fake.doc", unknown) == "unknown"
    assert _resolve_signatureless_package_suffix("unknown", "fake.docx", ordinary_zip.getvalue()) == "unknown"
    assert _resolve_signatureless_package_suffix("unknown", "fake.doc", ole_header_only) == "unknown"
    assert guess_suffix_by_bytes(unknown, "fake.docx") == "unknown"


def test_unknown_path_does_not_fall_back_to_office_extension(tmp_path: Path) -> None:
    """Path entries must also not identify unknown binaries as documents based solely on the Office extension."""
    path = tmp_path / "fake.pptx"
    path.write_bytes(b"\xff" * 256)
    assert guess_suffix_by_path(path) == "unknown"
