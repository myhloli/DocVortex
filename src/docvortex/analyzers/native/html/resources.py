"""Safe parsing of Standalone HTML links, pictures and local stylesheet."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, unquote, urljoin, urlsplit, urlunsplit

from lxml import etree  # type: ignore[reportMissingImports]

from docvortex.content.markup import ResolvedMarkupImage
from docvortex.document.contracts import HtmlSourceContext

from ....foundation._image_payload import parse_image_data_uri_strict, validate_remote_image_url
from ....foundation._svg_raster import looks_like_svg_payload, serialize_svg_image
from .._shared.hyperlink import sanitize_hyperlink_target
from .constants import (
    MAX_HTML_IMAGE_BYTES,
    MAX_HTML_IMAGE_TOTAL_BYTES,
    MAX_HTML_STYLESHEET_BYTES,
    MAX_HTML_STYLESHEET_TOTAL_BYTES,
)
from .errors import HtmlResourceLimitError

_IMAGE_MIME_SIGNATURES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("image/jpeg", (b"\xff\xd8\xff",)),
    ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ("image/gif", (b"GIF87a", b"GIF89a")),
    ("image/webp", (b"RIFF",)),
    ("image/bmp", (b"BM",)),
    ("image/tiff", (b"II*\x00", b"MM\x00*")),
)
_SAME_DOCUMENT_SCHEMES = frozenset({"file", "http", "https"})


class HtmlAnchorResolver(Protocol):
    """Define the in-document anchor query required for resource resolution and sharing projector."""

    def resolve_fragment(self, fragment: str) -> str | None:
        """Convert source fragment to unified internal link."""

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Return the unified anchor of the title node."""

    def heading_label(self, anchor: str) -> str | None:
        """Return the title text corresponding to unified anchor."""

    def note_anchor(self, note: etree._Element) -> str | None:
        """Return the unified anchor of the footnote node."""


class HtmlResourceContext:
    """To realize the adaptation of HTML sources and resources required for sharing projector to anchor."""

    def __init__(
        self,
        source_context: HtmlSourceContext,
        *,
        base_href: str | None = None,
    ) -> None:
        """Bind source context and compute base that does not escape the local root directory."""
        self.source_context = source_context
        self.base_href = base_href
        self.anchors: HtmlAnchorResolver | None = None
        self._image_bytes = 0
        self._stylesheet_bytes = 0
        self._image_cache: dict[Path, str] = {}
        self._data_image_cache: dict[str, ResolvedMarkupImage] = {}
        self._stylesheet_cache: dict[Path, str] = {}
        self._local_root = source_context.local_resource_root.resolve() if source_context.local_resource_root else None
        self._local_base = self._resolve_local_base()
        self._remote_base = self._resolve_remote_base()

    def bind_anchors(self, anchors: HtmlAnchorResolver) -> None:
        """Bind the anchor table containing only the actual output targets after text selection is completed."""
        self.anchors = anchors

    def same_document_fragment(self, href: str) -> str | None:
        """Resolve pure fragment or relative, absolute URL fragment with the same identity as the source document."""
        normalized = sanitize_hyperlink_target(
            href,
            allowed_schemes=_SAME_DOCUMENT_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
            allow_root_relative=True,
        )
        if normalized is None:
            return None
        base_href = (self.base_href or "").strip()
        if normalized.startswith("#") and (not base_href or base_href.startswith("#")):
            fragment = unquote(normalized[1:]).strip()
            return fragment or None
        source_uri = (self.source_context.source_uri or "").strip()
        if not source_uri:
            return None
        try:
            source_parts = urlsplit(source_uri)
            base_uri = urljoin(source_uri, base_href)
            target_parts = urlsplit(urljoin(base_uri, normalized))
        except ValueError:
            return None
        fragment = unquote(target_parts.fragment).strip()
        if not fragment or _document_url_identity(target_parts) != _document_url_identity(source_parts):
            return None
        return fragment

    def resolve_link(self, href: str) -> str | None:
        """Parse fragment within safe external links, relative links or actual existing documents."""
        candidate = (href or "").strip()
        if self._remote_base and candidate.startswith("//"):
            try:
                candidate = urljoin(self._remote_base, candidate)
            except ValueError:
                return None
        if self.anchors is not None and (fragment := self.same_document_fragment(candidate)) is not None:
            if internal := self.anchors.resolve_fragment(fragment):
                return internal
        normalized = sanitize_hyperlink_target(
            candidate,
            allow_relative=True,
            allow_fragment=True,
            allow_root_relative=True,
        )
        if normalized is None:
            return None
        if normalized.startswith("#"):
            base_href = (self.base_href or "").strip()
            if base_href:
                source_uri = (self.source_context.source_uri or "").strip()
                try:
                    target = urljoin(urljoin(source_uri, base_href), normalized)
                except ValueError:
                    return None
                return sanitize_hyperlink_target(
                    target,
                    allow_relative=True,
                    allow_fragment=True,
                    allow_root_relative=True,
                )
            return self.anchors.resolve_fragment(normalized) if self.anchors else None
        parsed = urlsplit(normalized)
        if parsed.scheme:
            return normalized
        if self._remote_base:
            resolved = urljoin(self._remote_base, normalized)
            return sanitize_hyperlink_target(resolved)
        if self._local_root is not None and self._local_base is not None and parsed.path and not parsed.path.startswith("/"):
            local_path = self._resolve_local_path(normalized)
            if local_path is None:
                return None
            relative_path = local_path.relative_to(self._local_root).as_posix()
            return sanitize_hyperlink_target(
                urlunsplit(("", "", relative_path, parsed.query, parsed.fragment)),
                allow_relative=True,
                allow_fragment=True,
            )
        return sanitize_hyperlink_target(normalized, allow_relative=True, allow_fragment=True)

    def resolve_image(self, source: str, *, alt: str = "") -> ResolvedMarkupImage | None:
        """The pictures are parsed in the order of data, URI, remote URL, and local security files, and the remote pictures only retain restricted external links."""
        normalized = (source or "").strip()
        if not normalized:
            return ResolvedMarkupImage(alt=alt) if alt else None
        if normalized.casefold().startswith("data:"):
            return self._resolve_data_image(normalized, alt=alt)
        if remote_url := self._resolve_remote_image_url(normalized):
            return ResolvedMarkupImage(image_url=remote_url, alt=alt)
        local_path = self._resolve_local_path(normalized)
        if local_path is None or not local_path.is_file():
            return ResolvedMarkupImage(alt=alt) if alt else None
        cached = self._image_cache.get(local_path)
        if cached is not None:
            return ResolvedMarkupImage(image_base64=cached, alt=alt)
        with local_path.open("rb") as source_file:
            payload = source_file.read(MAX_HTML_IMAGE_BYTES + 1)
        if len(payload) > MAX_HTML_IMAGE_BYTES:
            raise HtmlResourceLimitError(f"HTML image exceeds max_html_image_bytes={MAX_HTML_IMAGE_BYTES}")
        if local_path.suffix.casefold() == ".svg" or looks_like_svg_payload(payload):
            data_uri = serialize_svg_image(payload)
        else:
            data_uri = _image_data_uri(payload)
        if data_uri is None:
            return ResolvedMarkupImage(alt=alt) if alt else None
        self._charge_image_bytes(len(payload))
        self._image_cache[local_path] = data_uri
        return ResolvedMarkupImage(image_base64=data_uri, alt=alt)

    def load_stylesheet(self, href: str) -> str | None:
        """Only stylesheet within the secure local root directory is read, remote CSS is always ignored."""
        path = self._resolve_local_path(href)
        if path is None or not path.is_file():
            return None
        cached = self._stylesheet_cache.get(path)
        if cached is not None:
            return cached
        with path.open("rb") as source_file:
            payload = source_file.read(MAX_HTML_STYLESHEET_BYTES + 1)
        self._charge_stylesheet_bytes(len(payload))
        stylesheet = payload.decode("utf-8-sig", errors="replace")
        self._stylesheet_cache[path] = stylesheet
        return stylesheet

    def charge_inline_stylesheet(self, stylesheet: str) -> None:
        """Count the UTF-8 bytes of an inline CSS into the unified stylesheet budget."""
        self._charge_stylesheet_bytes(len(stylesheet.encode("utf-8")))

    def _charge_stylesheet_bytes(self, byte_count: int) -> None:
        """Implement stylesheet byte limit for single copy and entire document, and only accumulate after passing the verification."""
        if byte_count > MAX_HTML_STYLESHEET_BYTES:
            raise HtmlResourceLimitError(f"HTML stylesheet exceeds max_html_stylesheet_bytes={MAX_HTML_STYLESHEET_BYTES}")
        total_bytes = self._stylesheet_bytes + byte_count
        if total_bytes > MAX_HTML_STYLESHEET_TOTAL_BYTES:
            raise HtmlResourceLimitError(
                f"HTML stylesheets exceed max_html_stylesheet_total_bytes={MAX_HTML_STYLESHEET_TOTAL_BYTES}"
            )
        self._stylesheet_bytes = total_bytes

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Delegate query for title anchor to bound registry."""
        return self.anchors.heading_anchor(heading) if self.anchors else None

    def heading_label(self, anchor: str) -> str | None:
        """Delegate title tag queries to bound registries."""
        return self.anchors.heading_label(anchor) if self.anchors else None

    def note_anchor(self, note: etree._Element) -> str | None:
        """Delegate the footnote anchor query to the bound registry."""
        return self.anchors.note_anchor(note) if self.anchors else None

    def _resolve_data_image(self, data_uri: str, *, alt: str) -> ResolvedMarkupImage | None:
        """Strictly parse data URI and perform image budgeting, SVG is rasterized into PNG and then embedded."""
        if cached := self._data_image_cache.get(data_uri):
            return ResolvedMarkupImage(image_base64=cached.image_base64, alt=alt)
        if len(data_uri) > MAX_HTML_IMAGE_BYTES * 2:
            raise HtmlResourceLimitError(f"HTML image exceeds max_html_image_bytes={MAX_HTML_IMAGE_BYTES}")
        try:
            payload, extension = parse_image_data_uri_strict(data_uri)
        except ValueError:
            return ResolvedMarkupImage(alt=alt) if alt else None
        if extension == "svg":
            if len(payload) > MAX_HTML_IMAGE_BYTES:
                raise HtmlResourceLimitError(f"HTML image exceeds max_html_image_bytes={MAX_HTML_IMAGE_BYTES}")
            raster_uri = serialize_svg_image(payload)
            if raster_uri is None:
                return ResolvedMarkupImage(alt=alt) if alt else None
            self._charge_image_bytes(len(payload))
            resolved = ResolvedMarkupImage(image_base64=raster_uri, alt=alt)
            self._data_image_cache[data_uri] = resolved
            return resolved
        if len(payload) > MAX_HTML_IMAGE_BYTES:
            raise HtmlResourceLimitError(f"HTML image exceeds max_html_image_bytes={MAX_HTML_IMAGE_BYTES}")
        self._charge_image_bytes(len(payload))
        resolved = ResolvedMarkupImage(image_base64=data_uri, alt=alt)
        self._data_image_cache[data_uri] = resolved
        return resolved

    def _charge_image_bytes(self, byte_count: int) -> None:
        """The actual reserved picture bytes are accumulated, and the entire document is terminated when the limit is exceeded."""
        self._image_bytes += byte_count
        if self._image_bytes > MAX_HTML_IMAGE_TOTAL_BYTES:
            raise HtmlResourceLimitError(f"HTML images exceed max_html_image_total_bytes={MAX_HTML_IMAGE_TOTAL_BYTES}")

    def _resolve_remote_image_url(self, source: str) -> str | None:
        """Parse remote or remote source relative pictures to restricted HTTP (S) absolute URL."""
        try:
            parsed = urlsplit(source)
        except ValueError:
            return None
        if parsed.scheme:
            try:
                return validate_remote_image_url(source)
            except ValueError:
                return None
        if not self._remote_base:
            return None
        try:
            return validate_remote_image_url(urljoin(self._remote_base, source))
        except ValueError:
            return None

    def _resolve_remote_base(self) -> str | None:
        """Return the resource base address of HTTP (S) synthesized from URI and base href."""
        source_uri = (self.source_context.source_uri or "").strip()
        base_href = (self.base_href or "").strip()
        try:
            base_scheme = urlsplit(base_href).scheme.casefold() if base_href else ""
            source_scheme = urlsplit(source_uri).scheme.casefold() if source_uri else ""
        except ValueError:
            return None
        if base_href and base_scheme in {"http", "https"}:
            base = base_href
        elif source_uri and source_scheme in {"http", "https"}:
            base = urljoin(source_uri, base_href) if base_href else source_uri
        else:
            return None
        try:
            parsed = urlsplit(base)
        except ValueError:
            return None
        return base if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname else None

    def _resolve_local_base(self) -> Path | None:
        """Calculate resource starting directory by local security root and relative base href."""
        root = self._local_root
        if root is None:
            return None
        raw_base = (self.base_href or "").strip()
        if not raw_base:
            return root
        try:
            parsed = urlsplit(raw_base)
        except ValueError:
            return root
        if parsed.scheme or parsed.netloc or parsed.path.startswith(("/", "\\")) or "\\" in parsed.path:
            return root
        decoded = unquote(parsed.path)
        if not decoded:
            return root
        candidate = (root / decoded).resolve()
        base = candidate if raw_base.endswith("/") else candidate.parent
        return base if _is_within(base, root) else root

    def _resolve_local_path(self, source: str) -> Path | None:
        """Parse local relative resources and verify the root directory boundary again after resolve."""
        root = self._local_root
        base = self._local_base
        if root is None or base is None:
            return None
        try:
            parsed = urlsplit(source)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc or parsed.path.startswith(("/", "\\")) or "\\" in parsed.path:
            return None
        decoded = unquote(parsed.path)
        if not decoded or "\x00" in decoded or ".." in Path(decoded).parts:
            return None
        candidate = (base / decoded).resolve()
        return candidate if _is_within(candidate, root) else None


def _image_data_uri(payload: bytes) -> str | None:
    """Construct and review data URI for supported raster images by file signature."""
    mime = next(
        (
            media_type
            for media_type, signatures in _IMAGE_MIME_SIGNATURES
            if any(payload.startswith(signature) for signature in signatures)
        ),
        None,
    )
    if mime == "image/webp" and (len(payload) < 12 or payload[8:12] != b"WEBP"):
        mime = None
    if mime is None:
        return None
    data_uri = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    try:
        parse_image_data_uri_strict(data_uri)
    except ValueError:
        return None
    return data_uri


def _document_url_identity(parts: SplitResult) -> tuple[str, str, str, str]:
    """Return a confirmed document identity ignoring fragment, and an empty path to the normalized hierarchy URL."""
    scheme = parts.scheme.casefold()
    path = parts.path or ("/" if scheme in {"http", "https"} else "")
    return scheme, parts.netloc.casefold(), unquote(path), parts.query


def _is_within(path: Path, root: Path) -> bool:
    """Determine whether the resolved path is equal to or within the security root."""
    return path == root or root in path.parents


__all__ = ["HtmlAnchorResolver", "HtmlResourceContext"]
