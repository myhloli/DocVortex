"""Flash lightweight image encoding capability for multiplexing of various formats."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from PIL import Image


ImageFormat: TypeAlias = Literal["jpeg", "png", "webp"]
_IMAGE_FORMATS: dict[ImageFormat, tuple[str, str, str]] = {
    "jpeg": ("JPEG", "image/jpeg", "jpg"),
    "png": ("PNG", "image/png", "png"),
    "webp": ("WEBP", "image/webp", "webp"),
}


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    """Holds independent encoding bytes and dimensions and does not depend on the closed image or source document."""

    data: bytes
    image_format: ImageFormat
    width: int
    height: int

    def __post_init__(self) -> None:
        """Unsupported formats are rejected and the derived MIME is guaranteed to be consistent with the extension."""
        if self.image_format not in _IMAGE_FORMATS:
            raise ValueError(f"Unsupported image format: {self.image_format}")

    @property
    def mime_type(self) -> str:
        """Returns the standard MIME type corresponding to the encoding format."""
        return _IMAGE_FORMATS[self.image_format][1]

    @property
    def extension(self) -> str:
        """Return the extension without the dot, JPEG uniformly uses jpg."""
        return _IMAGE_FORMATS[self.image_format][2]


def encode_image(image: Image.Image, *, image_format: ImageFormat = "jpeg") -> ImageArtifact:
    """Encodes the image held by the caller, turning off only the color-converted copy created by this function."""
    if image_format not in _IMAGE_FORMATS:
        raise ValueError(f"Unsupported image format: {image_format}")
    output_image = image.convert("RGB") if image_format == "jpeg" and image.mode != "RGB" else image
    try:
        data = image_to_bytes(output_image, _IMAGE_FORMATS[image_format][0])
        return ImageArtifact(data=data, image_format=image_format, width=image.width, height=image.height)
    finally:
        if output_image is not image:
            output_image.close()


def transcode_image(data: bytes, *, image_format: ImageFormat = "jpeg") -> ImageArtifact:
    """Decode the memory image and convert it to the target format, releasing the decoded image on success or failure."""
    from PIL import Image

    with BytesIO(data) as buffer:
        image = Image.open(buffer)
        try:
            return encode_image(image, image_format=image_format)
        finally:
            image.close()


def image_to_bytes(
    image: Image.Image,
    image_format: str = "JPEG",
) -> bytes:
    """Encode the Pillow picture into bytes in the specified format."""
    with BytesIO() as image_buffer:
        image.save(image_buffer, format=image_format)
        return image_buffer.getvalue()


def image_to_b64str(
    image: Image.Image,
    image_format: str = "JPEG",
) -> str:
    """Encode Pillow pictures into data and URI according to the specified format."""
    image_bytes = image_to_bytes(image, image_format)
    return f"data:image/{image_format.lower()};base64,{base64.b64encode(image_bytes).decode('utf-8')}"


__all__ = ["ImageArtifact", "ImageFormat", "encode_image", "transcode_image", "image_to_b64str", "image_to_bytes"]
