"""Restricted access to OpenDocument ZIP packages and XML part."""

from __future__ import annotations

import posixpath
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile, ZipInfo

from lxml import etree  # type: ignore[reportMissingImports]

from .constants import (
    MAX_ASSET_TOTAL_BYTES,
    MAX_ENTRY_BYTES,
    MAX_ENTRY_COUNT,
    MAX_TOTAL_BYTES,
    MAX_XML_DEPTH,
    MAX_XML_NODES,
    ODF_BODY_BY_SUFFIX,
    ODF_MIME_BY_SUFFIX,
    ODF_SUFFIX_BY_MIME,
    OdfSuffix,
    qname,
)
from .errors import OdfEncryptedError, OdfParseError, OdfResourceLimitError


def _xml_parser() -> etree.XMLParser:
    """Create a new disabled entity, DTD and XML parser for the network for each part resolution."""
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )


class OdfPackage:
    """Read a OpenDocument ZIP package within a fixed resource budget."""

    def __init__(self, file_bytes: bytes) -> None:
        """Open the memory package and complete the central directory security check before reading the text."""
        try:
            self._zip = ZipFile(BytesIO(file_bytes))
        except (BadZipFile, OSError, ValueError) as exc:
            raise OdfParseError(f"Malformed ODF package: {exc}") from exc
        try:
            self._infos = self._validate_members(self._zip.infolist())
        except Exception:
            self._zip.close()
            raise
        self._cache: dict[str, bytes] = {}
        self._asset_bytes = 0
        self._asset_parts: set[str] = set()
        self._manifest_media_types: dict[str, str] | None = None

    @staticmethod
    def _validate_members(infos: list[ZipInfo]) -> dict[str, ZipInfo]:
        """Verify the number of members, decompression volume, paths and duplicate names to avoid package-level ambiguity."""
        if len(infos) > MAX_ENTRY_COUNT:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_entry_count={MAX_ENTRY_COUNT}")
        total_size = 0
        members: dict[str, ZipInfo] = {}
        for info in infos:
            name = info.filename
            if not OdfPackage._is_safe_member_name(name):
                raise OdfParseError(f"Malformed ODF package: unsafe member path {name!r}")
            if name in members:
                raise OdfParseError(f"Malformed ODF package: duplicate member {name!r}")
            if info.file_size > MAX_ENTRY_BYTES:
                raise OdfResourceLimitError(
                    f"ODF resource limit exceeded: member {name!r} exceeds max_entry_bytes={MAX_ENTRY_BYTES}"
                )
            total_size += info.file_size
            if total_size > MAX_TOTAL_BYTES:
                raise OdfResourceLimitError(f"ODF resource limit exceeded: max_total_bytes={MAX_TOTAL_BYTES}")
            members[name] = info
        return members

    @staticmethod
    def _is_safe_member_name(name: str) -> bool:
        """Determine whether the ZIP member is a POSIX path without backslash, absolute path and up-hop segment."""
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            return False
        parts = PurePosixPath(name).parts
        return bool(parts) and all(part not in {"", ".", ".."} for part in parts)

    def has_part(self, part_name: str) -> bool:
        """Return whether the specified specification member exists in the package."""
        return part_name in self._infos

    def read_part(self, part_name: str, *, required: bool = False, asset: bool = False) -> bytes | None:
        """Read a verified member and enforce independent limits on the cumulative image resource."""
        info = self._infos.get(part_name)
        if info is None:
            if required:
                raise OdfParseError(f"Malformed ODF package: missing required part {part_name!r}")
            return None
        if part_name in self._cache:
            data = self._cache[part_name]
            if asset:
                self._charge_asset(part_name, len(data))
            return data
        try:
            data = self._zip.read(info)
        except (BadZipFile, OSError, RuntimeError, ValueError) as exc:
            if required:
                raise OdfParseError(f"Malformed ODF package: cannot read {part_name!r}: {exc}") from exc
            return None
        if len(data) > MAX_ENTRY_BYTES:
            raise OdfResourceLimitError(
                f"ODF resource limit exceeded: member {part_name!r} exceeds max_entry_bytes={MAX_ENTRY_BYTES}"
            )
        if asset:
            self._charge_asset(part_name, len(data))
        self._cache[part_name] = data
        return data

    def _charge_asset(self, part_name: str, byte_count: int) -> None:
        """Bytes are reserved cumulatively based on unique resource members, and repeated references will not be billed again."""
        if part_name in self._asset_parts:
            return
        self._asset_parts.add(part_name)
        self._asset_bytes += byte_count
        if self._asset_bytes > MAX_ASSET_TOTAL_BYTES:
            raise OdfResourceLimitError(f"ODF resource limit exceeded: max_asset_total_bytes={MAX_ASSET_TOTAL_BYTES}")

    def xml_part(self, part_name: str, *, required: bool = False) -> etree._Element | None:
        """Parse XML after disabling entities and networks, and verify the number of nodes and maximum depth."""
        data = self.read_part(part_name, required=required)
        if data is None:
            return None
        try:
            root = etree.fromstring(data, parser=_xml_parser())
        except (etree.XMLSyntaxError, ValueError) as exc:
            if required:
                raise OdfParseError(f"Malformed ODF package: invalid XML part {part_name!r}: {exc}") from exc
            return None
        if root.getroottree().docinfo.doctype:
            raise OdfParseError(f"Malformed ODF package: DTD is not allowed in {part_name!r}")
        self._validate_xml_shape(root, part_name)
        return root

    @staticmethod
    def _validate_xml_shape(root: etree._Element, part_name: str) -> None:
        """Iteratively count XML nodes and depths to avoid deep recursion or continued propagation of extremely large DOM."""
        node_count = 0
        stack: list[tuple[etree._Element, int]] = [(root, 1)]
        while stack:
            element, depth = stack.pop()
            node_count += 1
            if node_count > MAX_XML_NODES:
                raise OdfResourceLimitError(f"ODF resource limit exceeded: {part_name!r} exceeds max_xml_nodes={MAX_XML_NODES}")
            if depth > MAX_XML_DEPTH:
                raise OdfResourceLimitError(f"ODF resource limit exceeded: {part_name!r} exceeds max_xml_depth={MAX_XML_DEPTH}")
            for child in element:
                if isinstance(child.tag, str):
                    stack.append((child, depth + 1))

    def detected_suffix(self) -> OdfSuffix | None:
        """Identify the three package types ODF in sequence according to the root entries of mimetype and manifest."""
        mimetype = self.read_part("mimetype")
        if mimetype is not None:
            try:
                normalized = mimetype.decode("ascii", errors="strict").strip()
            except UnicodeDecodeError:
                normalized = ""
            if suffix := ODF_SUFFIX_BY_MIME.get(normalized):
                return suffix
        return ODF_SUFFIX_BY_MIME.get(self.manifest_media_types().get("/", ""))

    def validate_document(self, expected_suffix: OdfSuffix) -> etree._Element:
        """After verifying the package type, encryption status and required body body, the content root node is returned."""
        detected = self.detected_suffix()
        if detected is not None and detected != expected_suffix:
            raise OdfParseError(
                f"Malformed ODF package: expected {ODF_MIME_BY_SUFFIX[expected_suffix]!r}, got {ODF_MIME_BY_SUFFIX[detected]!r}"
            )
        if self.is_encrypted():
            raise OdfEncryptedError("Encrypted ODF documents are not supported")
        content_root = self.xml_part("content.xml", required=True)
        assert content_root is not None
        body = content_root.find(f".//{qname('office', 'body')}")
        expected_body = ODF_BODY_BY_SUFFIX[expected_suffix]
        if body is None or body.find(qname("office", expected_body)) is None:
            raise OdfParseError(f"Malformed ODF package: content.xml has no office:{expected_body} body")
        return content_root

    def manifest_media_types(self) -> dict[str, str]:
        """Reads the mapping of canonical member paths in manifest to MIME, safely downgrading to null when corrupted."""
        if self._manifest_media_types is not None:
            return self._manifest_media_types
        result: dict[str, str] = {}
        root = self.xml_part("META-INF/manifest.xml")
        if root is not None:
            for entry in root.iter(qname("manifest", "file-entry")):
                path = entry.get(qname("manifest", "full-path"))
                media_type = entry.get(qname("manifest", "media-type"))
                if path and media_type:
                    result[path] = media_type
        self._manifest_media_types = result
        return result

    def is_encrypted(self) -> bool:
        """Use the manifest:encryption-data element to determine whether there is encrypted content in the package."""
        root = self.xml_part("META-INF/manifest.xml")
        return root is not None and next(root.iter(qname("manifest", "encryption-data")), None) is not None

    def content_type_for(self, part_name: str) -> str | None:
        """Return manifest The media type declared for the specified member."""
        return self.manifest_media_types().get(part_name)

    def resolve_reference(self, href: str, *, base_part: str = "content.xml") -> str | None:
        """Resolve relative xlink references as security package members; absolute URI and up-hop paths return null."""
        normalized_href = unquote((href or "").strip())
        if not normalized_href or normalized_href.startswith("#"):
            return None
        try:
            split = urlsplit(normalized_href)
        except ValueError:
            return None
        if split.scheme or split.netloc:
            return None
        candidate = split.path.replace("\\", "/")
        base_dir = posixpath.dirname(base_part)
        resolved = posixpath.normpath(posixpath.join(base_dir, candidate))
        if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
            return None
        return resolved.removeprefix("./")

    def resolve_object_content(self, href: str, *, base_part: str = "content.xml") -> str | None:
        """Resolve the draw:object directory reference to its content.xml member."""
        resolved = self.resolve_reference(href, base_part=base_part)
        if resolved is None:
            return None
        if resolved.endswith(".xml"):
            return resolved
        return f"{resolved.rstrip('/')}/content.xml"

    def body_element(self, content_root: etree._Element, suffix: OdfSuffix) -> etree._Element:
        """Returns the text, spreadsheet, or presentation text node in the verified content tree."""
        body = content_root.find(f".//{qname('office', 'body')}")
        if body is None:
            raise OdfParseError("Malformed ODF package: content.xml has no office:body")
        child = body.find(qname("office", ODF_BODY_BY_SUFFIX[suffix]))
        if child is None:
            raise OdfParseError(f"Malformed ODF package: content.xml has no {suffix} body")
        return child

    def close(self) -> None:
        """Closes the underlying ZipFile without touching the caller-supplied input stream."""
        self._zip.close()


def detect_odf_suffix(file_bytes: bytes) -> OdfSuffix | None:
    """Only use the ODF package to identify the three suffixes, and any damage will be returned to the upper level to continue to take advantage of it."""
    try:
        package = OdfPackage(file_bytes)
    except (OdfParseError, OdfResourceLimitError):
        return None
    try:
        return package.detected_suffix()
    finally:
        package.close()


__all__ = ["OdfPackage", "detect_odf_suffix"]
