"""保留原有导入入口；共享实现由下层模块唯一维护。"""

from docvortex.foundation.image_encoding import (
    image_to_b64str as image_to_b64str,
    image_to_bytes as image_to_bytes,
)

__all__ = ["image_to_b64str", "image_to_bytes"]
