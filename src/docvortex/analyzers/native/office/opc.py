"""OOXML Open Packaging Conventions basic capabilities for format multiplexing."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
import posixpath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from lxml import etree

CONTENT_TYPES_MEMBER = "[Content_Types].xml"
ROOT_RELS_MEMBER = "_rels/.rels"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
RELATIONSHIP_TAG = f"{{{PACKAGE_RELATIONSHIPS_NS}}}Relationship"
OFFICE_DOCUMENT_REL_TAIL = "officeDocument"


WORDPROCESSINGML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_WORDPROCESSINGML_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"

# Strict OOXML URI mapping common to Transitional; format-specific namespaces (such as presentationml) are defined by each
# normalizer appended. Prefix class mappings must precede shorter same-prefix mappings.
STRICT_OOXML_COMMON_REPLACEMENTS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships/metadata/thumbnail",
        b"http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships/customProperties",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships/extendedProperties",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/relationships",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    ),
    (
        b"http://purl.oclc.org/ooxml/drawingml/main",
        b"http://schemas.openxmlformats.org/drawingml/2006/main",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/math",
        b"http://schemas.openxmlformats.org/officeDocument/2006/math",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/customProperties",
        b"http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/extendedProperties",
        b"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/docPropsVTypes",
        b"http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    ),
    (
        b"http://purl.oclc.org/ooxml/officeDocument/oleObject",
        b"http://schemas.openxmlformats.org/officeDocument/2006/oleObject",
    ),
)


def translate_strict_ooxml_uris(
    xml_bytes: bytes,
    replacements: Sequence[tuple[bytes, bytes]],
) -> bytes:
    """Replace Strict OOXML URI bytes with Transitional URI according to the given mapping."""
    normalized = xml_bytes
    for strict_uri, transitional_uri in replacements:
        normalized = normalized.replace(strict_uri, transitional_uri)
    return normalized


def repair_content_type_overrides(
    member_data: dict[str, bytes],
    rel_tail_content_types: dict[str, str],
) -> bytes | None:
    """Complement [Content_Types].xml Override for missing or incorrect Office part by diagram.

    The main document and sub-part of a non-standard package often do not have Override (the effective content type degenerates to application/xml),
    python-docx/python-pptx not recognized. The relationship type is more trustworthy than Content_Types, here we start from the root
    officeDocument The relationship starts along the relationship diagram completion; rel_tail_content_types starts with the relationship type tail segment (such as
    "officeDocument", "slide", "styles") are mapped to the expected content type. return new
    [Content_Types].xml bytes, returns None if no modification is required.
    """
    content_types_xml = member_data.get(CONTENT_TYPES_MEMBER)
    root_rels_xml = member_data.get(ROOT_RELS_MEMBER)
    if content_types_xml is None or root_rels_xml is None:
        return None

    try:
        parser = etree.XMLParser(resolve_entities=False, remove_blank_text=False)
        content_types_root = etree.fromstring(content_types_xml, parser)
        root_rels_root = etree.fromstring(root_rels_xml, parser)
    except etree.XMLSyntaxError:
        return None

    overrides = _content_type_override_elements(content_types_root)
    try:
        pending = _collect_parts_needing_overrides(member_data, overrides, root_rels_root, rel_tail_content_types)
    except (etree.XMLSyntaxError, KeyError, RuntimeError):
        return None
    if not pending:
        return None

    for part_name, expected_ct in pending:
        override = overrides.get(part_name)
        if override is not None:
            override.set("ContentType", expected_ct)
        else:
            override = etree.SubElement(content_types_root, f"{{{CONTENT_TYPES_NS}}}Override")
            override.set("PartName", "/" + part_name)
            override.set("ContentType", expected_ct)
            overrides[part_name] = override

    return etree.tostring(
        content_types_root,
        xml_declaration=content_types_xml.lstrip().startswith(b"<?xml"),
        encoding="UTF-8",
        standalone=True,
    )


def _content_type_override_elements(content_types_root: etree._Element) -> dict[str, etree._Element]:
    """Index an existing Override by PartName with the leading slash removed."""
    overrides: dict[str, etree._Element] = {}
    for override in content_types_root:
        if override.tag != f"{{{CONTENT_TYPES_NS}}}Override":
            continue
        part_name = (override.get("PartName") or "").replace("\\", "/").lstrip("/")
        if part_name:
            overrides[part_name] = override
    return overrides


def _collect_parts_needing_overrides(
    member_data: dict[str, bytes],
    overrides: dict[str, etree._Element],
    root_rels_root: etree._Element,
    rel_tail_content_types: dict[str, str],
) -> list[tuple[str, str]]:
    """Starting from the root officeDocument relationship, collect the Office part that need to be supplemented by Override along the relationship graph."""
    parser = etree.XMLParser(resolve_entities=False, remove_blank_text=False)
    queue: list[tuple[str, str]] = []
    for relationship in root_rels_root:
        if not _is_relationship_element(relationship):
            continue
        rel_tail = relationship.get("Type", "").rsplit("/", 1)[-1]
        expected_ct = rel_tail_content_types.get(OFFICE_DOCUMENT_REL_TAIL)
        if rel_tail != OFFICE_DOCUMENT_REL_TAIL or expected_ct is None:
            continue
        target = _resolve_relationship_target(ROOT_RELS_MEMBER, relationship.get("Target"))
        if target and target in member_data:
            queue.append((target, expected_ct))

    pending: list[tuple[str, str]] = []
    visited: set[str] = set()
    while queue:
        part_name, expected_ct = queue.pop()
        if part_name in visited:
            continue
        visited.add(part_name)

        override = overrides.get(part_name)
        if override is None or override.get("ContentType") != expected_ct:
            pending.append((part_name, expected_ct))

        rels_name = _part_relationships_member(part_name)
        rels_xml = member_data.get(rels_name) if rels_name else None
        if rels_xml is None:
            continue
        rels_root = etree.fromstring(rels_xml, parser)
        for relationship in rels_root:
            if not _is_relationship_element(relationship):
                continue
            rel_tail = relationship.get("Type", "").rsplit("/", 1)[-1]
            expected_part_ct = rel_tail_content_types.get(rel_tail)
            if expected_part_ct is None:
                continue
            target = _resolve_relationship_target(rels_name, relationship.get("Target"))
            if target and target in member_data:
                queue.append((target, expected_part_ct))
    return pending


def _part_relationships_member(part_name: str) -> str | None:
    """Derive its relationship member path based on the part path (such as word/document.xml → word/_rels/document.xml.rels)."""
    normalized = part_name.replace("\\", "/")
    if normalized in {"", "."}:
        return None
    part_dir, part_basename = posixpath.split(normalized)
    if not part_basename:
        return None
    if part_dir:
        return f"{part_dir}/_rels/{part_basename}.rels"
    return f"_rels/{part_basename}.rels"


def _is_relationship_element(element: etree._Element) -> bool:
    """Determine whether the element is Relationship, which is compatible with the default namespace writing method."""
    if element.tag == RELATIONSHIP_TAG:
        return True
    try:
        return etree.QName(element).localname == "Relationship"
    except ValueError:
        return False


def _resolve_relationship_target(rels_filename: str, target: str | None) -> str | None:
    """Parse Target in the relationship file into the canonical member path in the ZIP package."""
    if not target:
        return None

    target = target.replace("\\", "/")
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        base_dir = relationship_source_base_dir(rels_filename.replace("\\", "/"))
        if base_dir is None:
            return None
        resolved = posixpath.normpath(posixpath.join(base_dir, target))

    if resolved in {"", "."} or resolved.startswith("../"):
        return None
    return resolved


def write_zip_package(members: Sequence[tuple[ZipInfo, bytes]]) -> bytes:
    """Rewrite the normalized members into the ZIP package, and recalculate CRC by the standard library."""
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info, member_data in members:
            target.writestr(info, member_data)
    return output.getvalue()


def relationship_source_base_dir(rels_filename: str) -> str | None:
    """The directory where the source part is located is deduced from the canonical OPC relationship member path."""
    if rels_filename == "_rels/.rels":
        return ""

    marker = "/_rels/"
    if marker not in rels_filename:
        return None

    prefix, rels_basename = rels_filename.rsplit(marker, 1)
    if not rels_basename.endswith(".rels"):
        return None

    source_part_name = rels_basename[: -len(".rels")]
    source_part_path = posixpath.normpath(posixpath.join(prefix, source_part_name))
    return posixpath.dirname(source_part_path)


__all__ = [
    "CONTENT_TYPES_MEMBER",
    "OFFICE_DOCUMENT_REL_TAIL",
    "ROOT_RELS_MEMBER",
    "STRICT_OOXML_COMMON_REPLACEMENTS",
    "STRICT_WORDPROCESSINGML_NS",
    "WORDPROCESSINGML_NS",
    "relationship_source_base_dir",
    "repair_content_type_overrides",
    "translate_strict_ooxml_uris",
    "write_zip_package",
]
