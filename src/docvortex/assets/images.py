"""Image statistics and material generation portal loaded on demand."""

from __future__ import annotations

from typing import Any, Protocol

from ..schema import BBox


class ImageArray(Protocol):
    """Lightweight shape contract for image arrays, no need to load NumPy when importing material SDK."""

    @property
    def shape(self) -> tuple[int, ...]:
        """Returns the length of each dimension of the array. The first two dimensions represent the image height and width."""
        ...


def calculate_contrast(img: ImageArray, img_mode: str) -> float:
    """Calculate image contrast according to original RGB/BGR channel rules."""
    from ..foundation._image import calculate_contrast as calculate

    return calculate(img, img_mode)


def crop_pil_image(bbox: BBox, image: Any) -> Any:
    """Crop Pillow images by normalized regions, preserving empty box processing semantics."""
    from ..foundation._image import crop_pil_image as crop

    return crop(bbox, image)


def image_size(image: Any) -> tuple[int, int]:
    """Reading the width and height of the Pillow or NumPy image does not trigger the PDF runtime."""
    if hasattr(image, "shape"):
        height, width = image.shape[:2]
        return width, height
    return image.size


def rotate_image_to_upright(image: ImageArray, angle: int) -> ImageArray:
    """Rotate the image according to the visual block direction, retaining the original angle convention."""
    from ..foundation._image_operations import rotate_image_to_upright as rotate

    return rotate(image, angle)


def encode_crop_as_jpeg_data_uri(image: ImageArray, bbox: BBox, angle: int) -> str:
    """Crop, rotate and encode JPEG footage by pixel area."""
    from ..foundation._image_operations import encode_crop_as_jpeg_data_uri as encode

    return encode(image, bbox, angle)


__all__ = ["calculate_contrast", "crop_pil_image", "image_size", "rotate_image_to_upright", "encode_crop_as_jpeg_data_uri"]
