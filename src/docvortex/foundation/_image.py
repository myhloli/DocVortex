"""Lightweight image statistics and cropping primitives shared across models."""

import cv2
import numpy as np
from PIL import Image

from ..schema import BBox
from ._geometry import normalize_to_int_bbox


def calculate_contrast(img: np.ndarray, img_mode: str) -> float:
    """
    Calculate the contrast of a given image.
    :param img: Image, type numpy.ndarray
    :Param img_mode = color channel of the image, 'rgb' or 'bgr'
    :return: Contrast value of the image
    """
    if img_mode == "rgb":
        # Convert RGB image to grayscale
        gray_img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    elif img_mode == "bgr":
        # Convert BGR image to grayscale
        gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError("Invalid image mode. Please provide 'rgb' or 'bgr'.")

    # Calculate mean and standard deviation
    mean_value = np.mean(gray_img)
    std_dev = np.std(gray_img)
    # Contrast is defined as the standard deviation divided by the mean (plus a small constant to avoid divide-by-zero errors)
    contrast = std_dev / (mean_value + 1e-6)
    # logger.debug(f"contrast: {contrast}")
    return round(float(contrast), 2)


def crop_pil_image(bbox: BBox, image: Image.Image) -> Image.Image:
    """Normalize bbox by 0-1 Crop Pillow image."""
    width, height = image.size
    scaled_bbox = normalize_to_int_bbox(
        [bbox[0] * width, bbox[1] * height, bbox[2] * width, bbox[3] * height],
        image_size=(height, width),
    )
    if scaled_bbox is None:
        return image.crop((0, 0, 0, 0))
    return image.crop(scaled_bbox)


__all__ = ["calculate_contrast", "crop_pil_image"]
