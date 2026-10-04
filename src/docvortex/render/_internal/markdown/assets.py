"""Analysis of image resources shared by Markdown and Content List."""

from __future__ import annotations

import re
from urllib.parse import quote

from ....schema import ImagePayloadBlock

_HTML_IMAGE_SRC_RE = re.compile(
    r"(?P<prefix>\bsrc\s*=\s*)(?P<quote>[\"'])(?P<src>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def resolve_image_source(block: ImagePayloadBlock, asset_base_url: str = "") -> str | None:
    """Resolve image sources by stable priority of sidecar, data, URI, remote URL."""
    if block.image_path:
        return join_asset_base_url(asset_base_url, block.image_path)
    if block.image_base64:
        return block.image_base64
    if block.image_url:
        return block.image_url
    return None


def join_asset_base_url(asset_base_url: str, relative_path: str) -> str:
    """Use POSIX semantics to splice resource root addresses and safe relative paths."""
    if not asset_base_url:
        return relative_path
    return f"{asset_base_url.rstrip('/')}/{relative_path.lstrip('/')}"


def build_markdown_image(source: str, alt: str = "") -> str:
    """Constructs a Markdown picture syntax that is not truncated by spaces or parentheses."""
    if not source:
        return ""
    safe_alt = alt.replace("[", r"\[").replace("]", r"\]")
    destination = normalize_image_source(source)
    return f"![{safe_alt}]({destination})"


def normalize_image_source(source: str) -> str:
    """Normalize the image source to the secure address shared by Markdown and structured_content."""
    if source.startswith("data:"):
        return source
    return quote(source, safe="/:#?&=%@+~,;!$'*-._")


def prefix_html_image_sources(markup: str, asset_base_url: str = "") -> str:
    """Add the resource root address to the relative image address in HTML."""
    if not markup or not asset_base_url:
        return markup

    def _replace(match: re.Match[str]) -> str:
        """Only relative src is rewritten, data URI, absolute URL and the root path are retained."""
        source = match.group("src")
        if _is_absolute_image_source(source):
            return match.group(0)
        resolved = join_asset_base_url(asset_base_url, source)
        return f"{match.group('prefix')}{match.group('quote')}{resolved}{match.group('quote')}"

    return _HTML_IMAGE_SRC_RE.sub(_replace, markup)


def _is_absolute_image_source(source: str) -> bool:
    """Determine whether the HTML image address is already an absolute or non-prefixable source."""
    normalized = source.strip().lower()
    return normalized.startswith(("data:", "http://", "https://", "//", "/", "#"))


__all__ = [
    "build_markdown_image",
    "join_asset_base_url",
    "normalize_image_source",
    "prefix_html_image_sources",
    "resolve_image_source",
]
