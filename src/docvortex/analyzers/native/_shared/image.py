"""The original import entry is retained; the shared implementation is uniquely maintained by the lower module."""

from ....foundation.image_encoding import image_to_b64str, image_to_bytes

__all__ = ["image_to_b64str", "image_to_bytes"]
