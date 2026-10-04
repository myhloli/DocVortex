"""Flash Office Image recognition, transcoding and placeholder image generation of documents."""

import base64
import struct
from functools import lru_cache
from io import BytesIO
from pathlib import PurePosixPath
from typing import Final

from loguru import logger
from metafile_render import MetafileError, MetafileResourceLimitError, render_metafile
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from ....foundation._svg_raster import is_svg_image_part, looks_like_svg_payload, serialize_svg_image
from ....foundation.image_encoding import image_to_b64str

VECTOR_IMAGE_FORMATS = frozenset({"WMF", "EMF"})
VECTOR_IMAGE_EXTENSIONS = frozenset({".wmf", ".emf"})
VECTOR_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/x-wmf",
        "image/wmf",
        "image/x-emf",
        "image/emf",
        "application/x-msmetafile",
    }
)
PIL_IMAGE_LOAD_ERRORS = (UnidentifiedImageError, OSError, SyntaxError)
VECTOR_IMAGE_RENDER_DPI: Final = 200
STANDARD_VECTOR_PLACEHOLDER_SIZE: Final = (320, 180)
STANDARD_VECTOR_PLACEHOLDER_LINES: Final = (
    "WMF/EMF placeholder",
    "Metafile preview unavailable",
    "See the original document",
)


def _is_wmf_payload(image_data: bytes) -> bool:
    """Determine whether the original load is WMF based on placeable or standard METAHEADER magic."""

    return image_data.startswith(b"\xd7\xcd\xc6\x9a") or (
        len(image_data) >= 4 and image_data[:2] in {b"\x01\x00", b"\x02\x00"} and image_data[2:4] == b"\x09\x00"
    )


def _is_emf_payload(image_data: bytes) -> bool:
    """Determine whether the load is EMF based on EMR_HEADER type and ENHMETA_SIGNATURE."""
    return len(image_data) >= 44 and struct.unpack_from("<I", image_data, 0)[0] == 1 and image_data[40:44] == b" EMF"


def is_vector_image(pil_image: Image.Image) -> bool:
    """Determine whether the picture opened by Pillow belongs to WMF/EMF vector format."""
    return (getattr(pil_image, "format", None) or "").upper() in VECTOR_IMAGE_FORMATS


def is_vector_image_part(part_name: object | None = None, content_type: str | None = None) -> bool:
    """Determine whether it is a vector image based on the OOXML component extension and content type."""
    suffix = PurePosixPath(str(part_name or "")).suffix.lower()
    if suffix in VECTOR_IMAGE_EXTENSIONS:
        return True
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return normalized_content_type in VECTOR_IMAGE_CONTENT_TYPES


def is_valid_vector_image_payload(
    image_data: bytes,
    *,
    part_name: object | None = None,
    content_type: str | None = None,
) -> bool:
    """Verify WMF/EMF minimal file signature to avoid generating vector bitmaps for arbitrary camouflaged bytes."""
    label = _vector_image_format_label(part_name, content_type)
    if label == "WMF":
        return _is_wmf_payload(image_data)
    if label == "EMF":
        return _is_emf_payload(image_data)
    return _is_wmf_payload(image_data) or _is_emf_payload(image_data)


def _vector_image_format_label(part_name: object | None = None, content_type: str | None = None) -> str:
    """The vector format names used for logs and placemaps are inferred from the OOXML part information."""
    suffix = PurePosixPath(str(part_name or "")).suffix.lower()
    normalized_content_type = (content_type or "").lower()
    if suffix == ".wmf" or "wmf" in normalized_content_type:
        return "WMF"
    if suffix == ".emf" or "emf" in normalized_content_type:
        return "EMF"
    return "WMF/EMF"


def _load_placeholder_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load placeholder fonts according to priority, and fall back to the default font Pillow when none is available."""
    for font_name in ("DejaVuSans.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(font_name, font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_text_placeholder(size: tuple[int, int], lines: list[str]) -> Image.Image:
    """Draws the RGB placeholder image with borders and centered copy at the target size."""
    width = max(int(size[0]), 1)
    height = max(int(size[1]), 1)
    placeholder = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(placeholder)

    border_width = max(1, min(width, height) // 80)
    draw.rectangle(
        (0, 0, width - 1, height - 1),
        outline=(190, 190, 190),
        width=border_width,
    )

    max_text_width = max(width - 16, 1)
    max_text_height = max(height - 16, 1)
    fallback_text = "WMF/EMF"
    text = "\n".join(line for line in lines if line)
    if not text:
        text = fallback_text

    font = None
    spacing = 4
    bbox = None
    for font_size in range(max(min(width, height) // 7, 10), 7, -1):
        font = _load_placeholder_font(font_size)
        spacing = max(2, font_size // 4)
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_text_width and text_height <= max_text_height:
            break
    else:
        text = fallback_text
        font = _load_placeholder_font(max(min(width, height) // 5, 10))
        spacing = 2
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")

    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    origin = ((width - text_width) / 2, (height - text_height) / 2)
    draw.multiline_text(
        origin,
        text,
        fill=(90, 90, 90),
        font=font,
        spacing=spacing,
        align="center",
    )
    return placeholder


@lru_cache(maxsize=1)
def _standard_vector_placeholder_data_uri() -> str:
    """Generate and cache standard WMF/EMF placeholder images to avoid repeated drawing of each vector image."""
    placeholder = create_text_placeholder(
        STANDARD_VECTOR_PLACEHOLDER_SIZE,
        list(STANDARD_VECTOR_PLACEHOLDER_LINES),
    )
    return image_to_b64str(placeholder, image_format="JPEG")


def get_standard_vector_placeholder_data_uri() -> str:
    """Returns the standard WMF/EMF placeholder image data URI for reuse in each format of Office."""
    return _standard_vector_placeholder_data_uri()


def serialize_vector_part_with_placeholder(
    part_name: object | None = None,
    content_type: str | None = None,
    size: tuple[int, int] = (320, 180),
) -> str:
    """Skips unloaded vector parts and returns a standard placeholder image that can be embedded in the document."""
    image_format = _vector_image_format_label(part_name, content_type)
    logger.debug(
        f"Skipping {image_format} image part before Pillow load, "
        f"part_name={part_name}, content_type={content_type}, requested_size={size}"
    )
    return get_standard_vector_placeholder_data_uri()


def _render_size_from_emu(render_size_emu: tuple[int, int] | None, *, dpi: int) -> tuple[int, int] | None:
    """Convert EMU dimensions of OfficeArt ptSize to pixel hints as specified DPI."""
    if render_size_emu is None or render_size_emu[0] <= 0 or render_size_emu[1] <= 0:
        return None
    emu_per_inch = 914_400
    return (
        max(1, round(render_size_emu[0] * dpi / emu_per_inch)),
        max(1, round(render_size_emu[1] * dpi / emu_per_inch)),
    )


def _serialize_metafile(
    image_data: bytes,
    image_format: str,
    *,
    size_hint: tuple[int, int] | None = None,
) -> str | None:
    """Calls the metafile-render WMF/EMF engine and generates SVG with PNG fallback."""
    try:
        rendered = render_metafile(
            image_data,
            output_format="svg",
            dpi=VECTOR_IMAGE_RENDER_DPI,
            size_hint=size_hint,
            backend="auto",
        )
    except MetafileResourceLimitError as exc:
        logger.warning(f"Generated {image_format} SVG cannot be consumed safely: {exc}. Falling back to PNG.")
        try:
            rendered = render_metafile(
                image_data,
                output_format="png",
                dpi=VECTOR_IMAGE_RENDER_DPI,
                size_hint=size_hint,
                backend="auto",
            )
        except MetafileError as png_exc:
            logger.warning(
                f"Failed to render {image_format} PNG fallback with metafile-render: {png_exc}. Using placeholder instead."
            )
            return None
    except MetafileError as exc:
        logger.warning(f"Failed to render {image_format} image with metafile-render: {exc}. Using placeholder instead.")
        return None
    if rendered.partial:
        diagnostic_codes = sorted({diagnostic.code for diagnostic in rendered.diagnostics})
        logger.warning(
            f"Rendered partial {image_format} image with metafile-render, diagnostics={diagnostic_codes[:16]}, "
            f"size={rendered.width}x{rendered.height}"
        )
    encoded = base64.b64encode(rendered.data).decode("ascii")
    return f"data:{rendered.media_type};base64,{encoded}"


def serialize_office_image(
    image_data: bytes,
    *,
    part_name: object | None = None,
    content_type: str | None = None,
    render_size_emu: tuple[int, int] | None = None,
) -> str | None:
    """Recognize and serialize Office images, retain PNG for transparent images, and use JPEG for ordinary bitmaps."""
    is_wmf_payload = _is_wmf_payload(image_data)
    is_emf_payload = _is_emf_payload(image_data)
    if is_wmf_payload:
        part_name = "image.wmf"
        content_type = "image/wmf"
    elif is_emf_payload:
        part_name = "image.emf"
        content_type = "image/emf"
    if is_wmf_payload or is_emf_payload or is_vector_image_part(part_name, content_type):
        image_format = (
            "WMF" if is_wmf_payload else "EMF" if is_emf_payload else _vector_image_format_label(part_name, content_type)
        )
        if not (is_wmf_payload or is_emf_payload):
            logger.warning(
                f"Vector image payload does not match WMF/EMF signature, part_name={part_name}, content_type={content_type}. "
                "Using placeholder instead."
            )
            return serialize_vector_part_with_placeholder(part_name, content_type)
        rendered = _serialize_metafile(
            image_data,
            image_format,
            size_hint=_render_size_from_emu(render_size_emu, dpi=VECTOR_IMAGE_RENDER_DPI),
        )
        if rendered is not None:
            return rendered
        return serialize_vector_part_with_placeholder(part_name, content_type)

    if is_svg_image_part(part_name, content_type) or looks_like_svg_payload(image_data):
        return serialize_svg_image(image_data)

    try:
        pil_image = Image.open(BytesIO(image_data))
        if is_vector_image(pil_image):
            rendered = _serialize_metafile(
                image_data,
                str(pil_image.format),
                size_hint=_render_size_from_emu(render_size_emu, dpi=VECTOR_IMAGE_RENDER_DPI),
            )
            return rendered if rendered is not None else get_standard_vector_placeholder_data_uri()
        pil_image.load()
    except PIL_IMAGE_LOAD_ERRORS as e:
        logger.warning(f"Warning: image cannot be loaded by Pillow: {e}, part_name={part_name}, content_type={content_type}")
        return None

    if pil_image.mode == "RGB":
        return image_to_b64str(pil_image, image_format="JPEG")

    if pil_image.mode in {"RGBA", "LA"} or (pil_image.mode == "P" and "transparency" in pil_image.info):
        return image_to_b64str(pil_image.convert("RGBA"), image_format="PNG")

    return image_to_b64str(pil_image.convert("RGB"), image_format="JPEG")


def ensure_bmp_header(image_data: bytes) -> bytes:
    """Complete the BMP file header for bare DIB, retaining the original payload when it cannot be reliably inferred."""
    if image_data.startswith(b"BM") or len(image_data) < 4:
        return image_data
    header_size = int(struct.unpack_from("<I", image_data, 0)[0])
    if header_size < 12 or header_size > len(image_data):
        return image_data
    palette_bytes = 0
    if header_size >= 40 and len(image_data) >= 36:
        bit_count = int(struct.unpack_from("<H", image_data, 14)[0])
        colors_used = int(struct.unpack_from("<I", image_data, 32)[0])
        colors = colors_used or ((1 << bit_count) if bit_count <= 8 else 0)
        palette_bytes = colors * 4
    pixel_offset = min(14 + header_size + palette_bytes, 14 + len(image_data))
    return b"BM" + struct.pack("<IHHI", 14 + len(image_data), 0, 0, pixel_offset) + image_data
