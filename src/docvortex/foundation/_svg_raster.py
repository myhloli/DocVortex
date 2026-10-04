"""Sharing capability for controlled rasterization of external SVG image resources into PNG data URI."""

from __future__ import annotations

import base64
import math
import re
import xml.etree.ElementTree as ElementTree
from io import BytesIO
from pathlib import PurePosixPath
from typing import Final

from loguru import logger

from ._image_payload import (
    MAX_DECODED_RASTER_DIMENSION,
    MAX_DECODED_RASTER_PIXELS,
    _parse_svg_root_strict,
)

SVG_MEDIA_TYPE: Final = "image/svg+xml"
SVG_IMAGE_EXTENSION: Final = ".svg"
# The small size logo will be obviously blurry when viewed straight out according to the inherent pixels, and it will be uniformly enlarged to the minimum visible long side.
_MIN_RENDER_LONG_EDGE: Final = 512
_SVG_LENGTH_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*(px|pt|pc|mm|cm|in|q|%)?$", re.IGNORECASE)
_LENGTH_TO_PX: Final = {
    "": 1.0,
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
    "q": 96.0 / 101.6,
}
_SVG_PAYLOAD_SNIFF_WINDOW: Final = 4096


def is_svg_image_part(part_name: object | None = None, content_type: str | None = None) -> bool:
    """Determine whether it is a SVG picture component based on the component extension and declared media type."""
    normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if normalized_type == SVG_MEDIA_TYPE:
        return True
    suffix = PurePosixPath(str(part_name or "")).suffix.casefold()
    return suffix == SVG_IMAGE_EXTENSION


def looks_like_svg_payload(image_data: bytes) -> bool:
    """Sniff the SVG root node according to the payload prefix to identify incorrectly labeled image components."""
    head = image_data[:_SVG_PAYLOAD_SNIFF_WINDOW].lstrip()
    if head.startswith(b"<svg") or head.startswith(b"<svg:"):
        return True
    return head.startswith(b"<?xml") and b"<svg" in head


def _length_to_px(value: str | None) -> float | None:
    """Parse the SVG width/height length into pixel values, percentages and other context-sensitive units and return None."""
    match = _SVG_LENGTH_RE.match((value or "").strip())
    if match is None:
        return None
    unit = match.group(2) or ""
    if unit == "%":
        return None
    return float(match.group(1)) * _LENGTH_TO_PX[unit.casefold()]


def _intrinsic_size(root: ElementTree.Element) -> tuple[int, int] | None:
    """Resolve intrinsic pixel dimensions from the svg root element, falling back to viewBox when width/height is not available."""
    width = _length_to_px(root.get("width"))
    height = _length_to_px(root.get("height"))
    if width is not None and height is not None and width > 0 and height > 0:
        return max(1, round(width)), max(1, round(height))
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(view_box) == 4:
        try:
            box_width, box_height = float(view_box[2]), float(view_box[3])
        except ValueError:
            return None
        if box_width > 0 and box_height > 0:
            return max(1, round(box_width)), max(1, round(box_height))
    return None


def _clamped_size(width: int, height: int) -> tuple[int, int]:
    """Shrink the target size proportionally to the total pixel budget per side of the raster."""
    scale = min(
        1.0,
        MAX_DECODED_RASTER_DIMENSION / width,
        MAX_DECODED_RASTER_DIMENSION / height,
        math.sqrt(MAX_DECODED_RASTER_PIXELS / (width * height)),
    )
    if scale >= 1.0:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def _render_size(
    intrinsic: tuple[int, int] | None,
    size_hint: tuple[int, int] | None,
) -> tuple[int, int] | None:
    """Resolve render target dimensions: explicit hints take precedence, intrinsic dimensions second and scaled to the smallest long side."""
    if size_hint is not None and size_hint[0] > 0 and size_hint[1] > 0:
        return _clamped_size(max(1, round(size_hint[0])), max(1, round(size_hint[1])))
    if intrinsic is None:
        return None
    width, height = intrinsic
    long_edge = max(width, height)
    if long_edge < _MIN_RENDER_LONG_EDGE:
        width = max(1, round(width * _MIN_RENDER_LONG_EDGE / long_edge))
        height = max(1, round(height * _MIN_RENDER_LONG_EDGE / long_edge))
    return _clamped_size(width, height)


def _is_fully_transparent(png: bytes) -> bool:
    """Detects whether the rasterization result is fully transparent, used to degrade SVG callers without visual content."""
    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        if image.mode not in {"RGBA", "LA"}:
            return False
        return image.getextrema()[-1][1] == 0


def serialize_svg_image(image_data: bytes, *, size_hint: tuple[int, int] | None = None) -> str | None:
    """Rasterize external SVG bytes to transparent PNG data URI, returning None when rendering cannot be done safely.

    resvg neither executes scripts nor loads external resources, providing a sanitization boundary for external SVG output;
    ``skip_system_fonts=True`` makes output deterministic across platforms, but ``<text>`` not converted to paths
    will not render text. Reject DTDs, entity declarations, and payloads exceeding resource limits during parsing.
    """
    try:
        root = _parse_svg_root_strict(image_data)
    except ValueError as exc:
        logger.warning(f"SVG image payload cannot be parsed safely: {exc}")
        return None

    import resvg_py

    target_size = _render_size(_intrinsic_size(root), size_hint)
    # The strictly parsed tree is reserialized to UTF-8 to avoid the originally declared non-UTF-8 encoding interfering with rendering input.
    markup = ElementTree.tostring(root, encoding="unicode")
    render_options: dict[str, int] = {}
    if target_size is not None:
        render_options["width"] = target_size[0]
        render_options["height"] = target_size[1]
    try:
        png = resvg_py.svg_to_bytes(svg_string=markup, skip_system_fonts=True, **render_options)
        if not png:
            return None
        if _is_fully_transparent(png):
            logger.debug("Rasterized SVG image is fully transparent; degrading to caller fallback")
            return None
    except Exception as exc:  # noqa: BLE001 - resvg The exception surface has not been finalized, and the unified downgrade will be covered by the caller.
        logger.warning(f"SVG image cannot be rasterized: {exc}")
        return None

    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"
