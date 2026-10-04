"""Explicit byte ownership of document material, image transcoding and reading interfaces."""

from ..foundation._image_payload import parse_image_data_uri_strict, validate_image_sidecar_path
from ..foundation.image_encoding import ImageArtifact, ImageFormat, transcode_image
from .images import (
    ImageArray,
    calculate_contrast,
    crop_pil_image,
    encode_crop_as_jpeg_data_uri,
    image_size,
    rotate_image_to_upright,
)
from .store import AssetStore

__all__ = [
    "AssetStore",
    "ImageArtifact",
    "ImageFormat",
    "ImageArray",
    "parse_image_data_uri_strict",
    "transcode_image",
    "validate_image_sidecar_path",
    "calculate_contrast",
    "crop_pil_image",
    "image_size",
    "rotate_image_to_upright",
    "encode_crop_as_jpeg_data_uri",
]
