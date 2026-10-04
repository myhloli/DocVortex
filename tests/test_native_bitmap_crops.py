"""Bitmap cropping differential covers channel arrangement, row filling, rotation, boundaries and computational error propagation."""

from copy import deepcopy

import cv2
import numpy as np
import pytest
from PIL import Image

from docvortex._compute_backend import get_native
from docvortex.document.pdf.visuals import _attach_owned_bitmap_crops, _attach_prepared_visual_block_images


@pytest.fixture
def native():
    """Skip native operator checking only under explicit Python reference backend."""
    module = get_native()
    if module is None:
        pytest.skip("Python reference backend")
    return module


@pytest.mark.parametrize("mode", ["BGR", "BGRX", "BGRA", "RGB", "RGBX", "RGBA", "L"])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_padded_bitmap_crop_matches_pillow_and_opencv(native, mode, angle):
    """Random pixels and non-zero line padding byte-by-byte comparison PIL decoding, OpenCV rotation and final JPEG."""
    rng = np.random.default_rng(123)
    width, height = 37, 29
    channels = 1 if mode == "L" else len(mode)
    stride = width * channels + 5
    data = rng.integers(0, 256, size=height * stride, dtype=np.uint8).tobytes()
    dest = "L" if mode == "L" else "RGBA" if mode.endswith("A") else "RGB"
    image = Image.frombytes(dest, (width, height), data, "raw", mode, stride, 1)
    rgb = np.asarray(image.convert("RGB"))
    bbox = (3, 2, 34, 28)
    expected = rgb[2:28, 3:34]
    rotations = {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_CLOCKWISE}
    if angle:
        expected = cv2.rotate(expected, rotations[angle])
    expected = cv2.cvtColor(expected, cv2.COLOR_RGB2BGR)
    pixels, w, h = native.crop_bitmap_bgr(data, width, height, stride, mode, bbox, angle)
    assert bytes(pixels) == expected.tobytes()
    assert (h, w) == expected.shape[:2]
    specs = [[(0, {"bbox": bbox, "angle": angle})]]
    reference = deepcopy(specs)
    _attach_prepared_visual_block_images(reference, [{"img_pil": image}])
    _attach_owned_bitmap_crops(specs[0], (data, width, height, stride, mode), native, 0)
    assert specs == reference
    image.close()


@pytest.mark.parametrize("bbox", [(0, 0, 0, 2), (0, 0, 5, 2), (2, 2, 1, 1)])
def test_native_crop_rejects_invalid_bounds(native, bbox):
    """Illegal clipping cannot access bytes outside the buffer or implicitly modify the native calling contract."""
    with pytest.raises(ValueError, match="bounds"):
        native.crop_bitmap_bgr(bytes(48), 4, 4, 12, "BGR", bbox, 0)


@pytest.mark.parametrize("data,stride,mode,angle", [(bytes(47), 12, "BGR", 0), (bytes(48), 11, "BGR", 0), (bytes(48), 12, "XYZ", 0), (bytes(48), 12, "BGR", 45)])
def test_native_crop_rejects_invalid_metadata(native, data, stride, mode, angle):
    """Truncated buffers, illegal step sizes, unknown formats and non-rectangular directions must be explicitly reported as errors."""
    with pytest.raises(ValueError):
        native.crop_bitmap_bgr(data, 4, 4, stride, mode, (0, 0, 4, 4), angle)


def test_crop_compute_failure_is_not_silently_skipped(native):
    """Native calculation exception propagation, only existing invalid visual boxes are allowed to be skipped."""
    with pytest.raises(ValueError, match="buffer"):
        _attach_owned_bitmap_crops([(0, {"bbox": [0, 0, 1, 1]})], (b"", 4, 4, 12, "BGR"), native, 0)


@pytest.mark.parametrize("bbox", [None, [], [float("nan"), 0, 1, 1], [-3, -3, -1, -1], [0.05, 0.1, 0.9, 0.85], [-1, 1, 8, 8]])
def test_crop_normalization_matches_reference(native, bbox):
    """Edges, normalized boxes, non-finite values, and completely out-of-bounds boxes follow the cropped or skipped results of the old path."""
    image = Image.new("RGB", (4, 5), (20, 30, 40))
    specs = [[(0, {"bbox": bbox, "angle": -90})]]
    expected = deepcopy(specs)
    _attach_prepared_visual_block_images(expected, [{"img_pil": image}])
    _attach_owned_bitmap_crops(specs[0], (image.tobytes(), 4, 5, 12, "RGB"), native, 0)
    assert specs[0][0][1].get("image_base64") == expected[0][0][1].get("image_base64")
    image.close()
