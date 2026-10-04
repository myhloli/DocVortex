"""Pure path of LaTeX renderer sidecar image parsing."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ....foundation._image_payload import validate_image_sidecar_path
from ....schema import ImagePayloadBlock

_SUPPORTED_IMAGE_EXTENSIONS = {".jpeg", ".jpg", ".pdf", ".png"}
_FORBIDDEN_TEX_PATH_CHARACTERS = {"%", "{", "}"}


def normalize_asset_base_path(asset_base_path: str) -> str:
    """Normalizes the caller path prefix to a cross-platform readable forward slash form of TeX."""
    if not isinstance(asset_base_path, str):
        raise TypeError("asset_base_path must be a string")
    if any(character == "\x00" or character in "\r\n" for character in asset_base_path):
        raise ValueError("asset_base_path must not contain control characters")
    normalized = asset_base_path.replace("\\", "/")
    if normalized == "/" or (len(normalized) == 3 and normalized[1:] == ":/"):
        return normalized
    return normalized.rstrip("/")


def resolve_block_image_path(block: ImagePayloadBlock, asset_base_path: str) -> str | None:
    """Only XeLaTeX directly readable images are resolved from the safe sidecar path of block."""
    if block.image_path is None:
        return None
    return resolve_relative_image_path(block.image_path, asset_base_path)


def resolve_html_image_path(source: str, asset_base_path: str) -> str | None:
    """Resolving safe relative paths for HTML img, rejecting data URI with remote addresses."""
    if not isinstance(source, str):
        return None
    normalized = source.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme or parsed.netloc or normalized.startswith(("/", "#")):
        return None
    try:
        return resolve_relative_image_path(normalized, asset_base_path)
    except ValueError:
        return None


def resolve_relative_image_path(relative_path: str, asset_base_path: str) -> str | None:
    """Splice safe relative paths and restrict to image extensions natively supported by XeLaTeX."""
    safe_path = validate_image_sidecar_path(relative_path)
    if PurePosixPath(safe_path).suffix.casefold() not in _SUPPORTED_IMAGE_EXTENSIONS:
        return None
    if not asset_base_path:
        resolved = safe_path
    elif asset_base_path.endswith("/"):
        resolved = f"{asset_base_path}{safe_path}"
    else:
        resolved = f"{asset_base_path}/{safe_path}"
    if any(character in _FORBIDDEN_TEX_PATH_CHARACTERS for character in resolved):
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in resolved):
        return None
    return resolved


def remote_block_image_url(block: ImagePayloadBlock) -> str | None:
    """Returns block Remote image URL that has been verified by the strict model."""
    return block.image_url


def tex_image_path(path: str) -> str:
    """Use detokenize to wrap the verified path, retaining spaces and TeX reserved characters."""
    return rf"\detokenize{{{path}}}"


__all__ = [
    "normalize_asset_base_path",
    "remote_block_image_url",
    "resolve_block_image_path",
    "resolve_html_image_path",
    "resolve_relative_image_path",
    "tex_image_path",
]
