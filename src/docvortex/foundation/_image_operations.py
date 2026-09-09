"""无 PDF 句柄依赖的图像裁剪、旋转及编码。"""

from __future__ import annotations

import base64

import cv2
import numpy as np

from ..schema import BBox
from ._geometry import normalize_to_int_bbox


def rotate_image_to_upright(image: np.ndarray, angle: int) -> np.ndarray:
    """按 layout 视觉块角度把裁图旋转至正向，角度语义与方向分类模型保持一致。"""
    if angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    return image


def encode_crop_as_jpeg_data_uri(
    np_image: np.ndarray,
    page_bbox: BBox,
    angle: int,
) -> str:
    """从页面原图按像素框裁剪，按视觉块方向回正后编码为 JPEG data URI。"""
    image_h, image_w = np_image.shape[:2]
    image_bbox = normalize_to_int_bbox(page_bbox, image_size=(image_h, image_w))
    if image_bbox is None:
        return ""
    x0, y0, x1, y1 = image_bbox
    crop_rgb = np_image[y0:y1, x0:x1].copy()
    if crop_rgb.size == 0:
        return ""

    crop_rgb = rotate_image_to_upright(crop_rgb, angle)
    crop_bgr = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR)
    success, encoded = cv2.imencode(".jpg", crop_bgr)
    if not success:
        return ""
    return f"data:image/jpeg;base64,{base64.b64encode(encoded.tobytes()).decode('ascii')}"
