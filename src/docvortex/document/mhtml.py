"""Restricted MIME Archiving, root document selection, and scoped resource indexing."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from docvortex.document.contracts import HtmlSourceContext
from docvortex.errors import DocumentError
from docvortex.result import Diagnostic

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_PARTS = 4096
MAX_DEPTH = 32
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class MhtmlParseError(DocumentError):
    """Indicates that the archive structure, root document, or MIME encoding is invalid."""

    def __init__(self, message: str) -> None:
        """Provide stable error codes that can be recognized by CLI and upper-level callers."""
        super().__init__("mhtml_invalid", message)


class MhtmlResourceLimitError(DocumentError):
    """Indicates MIME container or cumulative decoded content exceeds budget."""

    def __init__(self, message: str) -> None:
        """Map archive budget errors to uniform resource limit error codes."""
        super().__init__("resource_limit", message)


def resolve_uri(base: str, reference: str) -> str:
    """Resolve references by base, preserving query parameters, fragments, and percent encoding to distinguish archive subpages."""
    # Archives without an origin address are processed using the virtual HTTP base address; this address is used only for internal indexing.
    return urljoin(base or "https://mhtml.invalid/", reference.strip())


def content_id(value: str) -> str:
    """Specifies surrounding whitespace and angle brackets for MIME Content-ID, preserving identifier case."""
    return value.strip().removeprefix("<").removesuffix(">")


@dataclass(eq=False)
class ResourceScope:
    """Limit the reference to only query the current related container and its outer layer to avoid stringing iframe resources."""

    parent: ResourceScope | None = None
    locations: dict[str, ArchivePart] = field(default_factory=dict)
    ids: dict[str, ArchivePart] = field(default_factory=dict)


@dataclass(eq=False)
class ArchivePart:
    """Save part header information and decode on-demand cache."""

    message: Message
    scope: ResourceScope
    location: str
    payload: bytes | None = None
    error: str | None = None

    @property
    def media_type(self) -> str:
        """Returns the MIME type of the resource without requiring the caller to access the mail object."""
        return self.message.get_content_type()

    @property
    def charset(self) -> str | None:
        """Returns the character set declared by the resource. If missing, it will be inferred by the caller based on usage."""
        return self.message.get_content_charset()

    @property
    def source_uri(self) -> str:
        """Returns the resource address that has been resolved by the container's base address."""
        return self.location


class MhtmlArchive:
    """Holds root HTML and read-only MIME resources without creating temporary files or making network requests."""

    def __init__(self, data: bytes, source_context: HtmlSourceContext | None = None) -> None:
        """Verify containers, select root documents, and build lightweight resource indexes."""
        if len(data) > MAX_ARCHIVE_BYTES:
            raise MhtmlResourceLimitError(f"MHTML archive exceeds {MAX_ARCHIVE_BYTES} bytes")
        try:
            self.message = BytesParser(policy=policy.default).parsebytes(data)
        except (ValueError, RecursionError) as exc:
            raise MhtmlParseError(f"Cannot parse MHTML container: {exc}") from exc
        if self.message.get_content_type() != "multipart/related":
            raise MhtmlParseError("MHTML requires a multipart/related container")
        self.diagnostics: list[Diagnostic] = []
        self._reported: set[tuple[str, str]] = set()
        self._decoded_bytes = 0
        self._parts: dict[int, ArchivePart] = {}
        self._parents: dict[int, Message] = {}
        self._validate_tree()
        root = self._select_root(self.message)
        self._root_message = root
        context = source_context or HtmlSourceContext()
        fallback = str(self.message.get("Snapshot-Content-Location", "")) or context.source_uri or ""
        ancestors = [root]
        while id(ancestors[-1]) in self._parents:
            ancestors.append(self._parents[id(ancestors[-1])])
        source_uri = fallback
        try:
            for ancestor in reversed(ancestors):
                if location := str(ancestor.get("Content-Location", "")):
                    source_uri = urljoin(source_uri, location)
        except ValueError as exc:
            raise MhtmlParseError(f"Invalid MHTML source URI: {exc}") from exc
        self.source_context = HtmlSourceContext(
            source_uri=source_uri or None,
            transport_encoding=root.get_content_charset() or context.transport_encoding,
        )
        self._index(self.message, ResourceScope(), fallback, root=True)
        self.root = self._parts[id(root)]
        self.html = self.decode(self.root)
        self.subject = str(self.message.get("Subject", "")).strip() or None

    def _validate_tree(self) -> None:
        """Iteratively check component quantity, depth and boundary defects without expanding any resource content."""
        stack = [(self.message, 1)]
        count = 0
        while stack:
            message, depth = stack.pop()
            count += 1
            if count > MAX_PARTS or depth > MAX_DEPTH:
                raise MhtmlResourceLimitError("MHTML part count or nesting depth exceeds limit")
            if message.get_content_maintype() == "multipart":
                if not message.is_multipart() or message.defects:
                    raise MhtmlParseError("Malformed MHTML multipart boundary")
                for child in message.get_payload():
                    self._parents[id(child)] = message
                    stack.append((child, depth + 1))
            elif message.is_multipart():
                raise MhtmlParseError("Nested email messages are not MHTML resources")

    def _select_root(self, message: Message) -> Message:
        """Follow the start/first part rules for related and the last available HTML rules for alternative."""
        media = message.get_content_type()
        if media in HTML_MEDIA_TYPES:
            return message
        children = message.get_payload() if message.is_multipart() else []
        if media == "multipart/related" and children:
            start = message.get_param("start")
            target = children[0]
            if start is not None:
                matches = [child for child in children if content_id(str(child.get("Content-ID", ""))) == content_id(start)]
                if len(matches) != 1:
                    raise MhtmlParseError("MHTML start must identify exactly one root part")
                target = matches[0]
            return self._select_root(target)
        if media == "multipart/alternative":
            for child in reversed(children):
                if child.get_content_type() in HTML_MEDIA_TYPES | {"multipart/related", "multipart/alternative"}:
                    return self._select_root(child)
        raise MhtmlParseError("MHTML root does not contain a supported HTML document")

    def _index(self, message: Message, scope: ResourceScope, base: str, *, root: bool = False) -> None:
        """Index addresses isolated by related container and reject ambiguous identities in the same scope."""
        location = str(message.get("Content-Location", ""))
        try:
            uri = urljoin(base, location) if location else base
        except ValueError:
            self.report("mhtml_resource_invalid", location)
            uri = base
            location = ""
        if message is self._root_message:
            uri = self.source_context.source_uri or ""
        if message.is_multipart():
            if message.get_content_type() == "multipart/related":
                scope = ResourceScope(None if root else scope)
            for child in message.get_payload():
                self._index(child, scope, uri)
            return
        # When there is no container base address, the relative component address is relative to the main document; do not splice the relative path of the main document again.
        if location and message is not self._root_message:
            uri = resolve_uri(base or self.source_context.source_uri or "", location)
        elif location:
            uri = resolve_uri("", uri)
        part = ArchivePart(message, scope, uri)
        self._parts[id(message)] = part
        cid = content_id(str(message.get("Content-ID", "")))
        for index, key in ((scope.locations, uri if location else ""), (scope.ids, cid)):
            if key:
                if key in index:
                    raise MhtmlParseError(f"Duplicate MHTML resource identifier: {key}")
                index[key] = part

    def find(self, reference: str, *, base_href: str | None = None) -> ArchivePart | None:
        """Resolve a CID or Content-Location reference within a scope visible to the root document."""
        reference = reference.strip()
        try:
            is_cid = urlsplit(reference).scheme.casefold() == "cid"
            base = resolve_uri(self.source_context.source_uri or "", base_href) if base_href else self.source_context.source_uri
            key = content_id(unquote(reference[4:])) if is_cid else resolve_uri(base or "", reference)
        except ValueError:
            return None
        scope: ResourceScope | None = self.root.scope
        while scope is not None:
            index = scope.ids if is_cid else scope.locations
            part = index.get(key)
            if part is None and is_cid:
                # Blink saves inline CSS with Content-Location: cid:..., standard Content-ID takes precedence.
                part = scope.locations.get(reference)
            if part is None and not is_cid:
                part = index.get(urldefrag(key)[0])
            if part is not None:
                return part
            scope = scope.parent
        return None

    def decode(self, part: ArchivePart) -> bytes:
        """Strictly decode resources on demand and accumulate a byte budget, caching success results and failure reasons."""
        if part.error is not None:
            raise MhtmlParseError(part.error)
        if part.payload is not None:
            return part.payload
        encoding = str(part.message.get("Content-Transfer-Encoding", "7bit")).strip().casefold()
        try:
            if encoding not in {"base64", "quoted-printable", "7bit", "8bit", "binary"}:
                raise ValueError(f"Unsupported transfer encoding: {encoding}")
            if encoding == "base64":
                encoded = part.message.get_payload().encode("ascii")
                payload = base64.b64decode(re.sub(rb"[\t\r\n ]", b"", encoded), validate=True)
            else:
                if encoding == "quoted-printable" and re.search(r"=(?![0-9A-Fa-f]{2}|\r?\n)", part.message.get_payload()):
                    raise ValueError("Invalid quoted-printable escape")
                payload = part.message.get_payload(decode=True)
            if not isinstance(payload, bytes) or part.message.defects:
                raise ValueError("Malformed MIME part")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            part.error = f"Cannot decode MHTML part: {exc}"
            raise MhtmlParseError(part.error) from exc
        self._decoded_bytes += len(payload)
        if self._decoded_bytes > MAX_DECODED_BYTES:
            raise MhtmlResourceLimitError(f"MHTML decoded data exceeds {MAX_DECODED_BYTES} bytes")
        part.payload = payload
        return payload

    def report(self, code: str, reference: str) -> None:
        """The same resource problem is only reported once to avoid repeated image references to fill up the diagnosis."""
        key = (code, reference)
        if key not in self._reported:
            self._reported.add(key)
            self.diagnostics.append(Diagnostic(code, f"MHTML resource: {reference}"))


__all__ = ["ArchivePart", "MhtmlArchive", "MhtmlParseError", "MhtmlResourceLimitError"]
