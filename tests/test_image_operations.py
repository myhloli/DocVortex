"""用冻结输出和独立像素参考守卫移除图像重依赖后的公开行为。"""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import platform
import sys

import numpy as np
from PIL import Image, __version__ as pillow_version, features
import pytest

from docvortex.assets import calculate_contrast, encode_crop_as_jpeg_data_uri, rotate_image_to_upright
from docvortex.foundation import _image_operations as operations

_FIXTURES = Path(__file__).parent / "fixtures"
_CONTRAST_CASES = json.loads((_FIXTURES / "image_contrast_reference.json").read_text())["cases"]


@pytest.mark.parametrize("case", _CONTRAST_CASES)
def test_contrast_matches_frozen_reference(case: dict) -> None:
    """保持发布前冻结的 RGB/BGR、透明通道、高位深及浮点统计结果。"""
    image = np.array(case["pixels"], dtype=case["dtype"])
    assert calculate_contrast(image, case["mode"]) == case["expected"]


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotation_preserves_pixels_and_ownership(angle: int) -> None:
    """以 Pillow 作为独立方向参考，并确保非连续输入的旋转结果独立且连续。"""
    source = np.arange(7 * 9 * 3, dtype=np.uint8).reshape(7, 9, 3)[:, ::-1]
    transpose = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
    with Image.fromarray(source) as image, image.transpose(transpose[angle]) as expected:
        actual = rotate_image_to_upright(source, angle)
        assert np.array_equal(actual, np.asarray(expected))
    assert actual.flags.c_contiguous
    assert not np.shares_memory(actual, source)
    actual.fill(0)
    assert np.any(source)


@pytest.mark.parametrize("angle", [0, 45, -90])
def test_unhandled_rotation_returns_original(angle: int) -> None:
    """保留方向归一化调用前既有的无操作返回对象约定。"""
    image = np.ones((3, 4, 3), dtype=np.uint8)
    assert rotate_image_to_upright(image, angle) is image


def test_jpeg_matches_frozen_bytes_and_encoding_parameters() -> None:
    """同冻结环境逐字节比较 JPEG，跨环境检查方向、量化表和解码像素。"""
    case = json.loads((_FIXTURES / "image_jpeg_reference.json").read_text())
    image = np.array(case["pixels"], dtype=case["dtype"])
    payload = encode_crop_as_jpeg_data_uri(image, case["bbox"], case["angle"])
    assert payload.startswith("data:image/jpeg;base64,")
    actual_bytes = base64.b64decode(payload.split(",", 1)[1])
    expected_bytes = base64.b64decode(case["jpeg_base64"])
    if (
        sys.platform == case["platform"]
        and platform.machine() == case["machine"]
        and pillow_version == case["pillow"]
        and features.version_feature("libjpeg_turbo") == case["libjpeg_turbo"]
    ):
        assert actual_bytes == expected_bytes
    with Image.open(BytesIO(actual_bytes)) as actual, Image.open(BytesIO(expected_bytes)) as expected:
        assert actual.size == expected.size
        assert actual.mode == expected.mode == "RGB"
        assert actual.quantization == expected.quantization
        assert actual.layer == expected.layer
        assert not actual.info.get("progressive")
        difference = np.abs(np.asarray(actual).astype(int) - np.asarray(expected).astype(int))
        assert difference.max() <= 2


@pytest.mark.parametrize("dtype", [np.uint16, np.float32])
def test_jpeg_depth_conversion_and_alpha_are_preserved(dtype: type) -> None:
    """高位深及半值输入饱和舍入到旧像素值，忽略透明通道而不合成背景。"""
    image = np.empty((19, 23, 4), dtype=dtype)
    image[:] = (1, 128, 511, 0) if dtype == np.uint16 else (1.5, 128.5, 511.5, 0)
    actual = operations._encode_rgb_as_jpeg_bytes(image)
    expected_color = (1, 128, 255) if dtype == np.uint16 else (2, 128, 255)
    with Image.new("RGB", (23, 19), expected_color) as expected, BytesIO() as buffer:
        expected.save(buffer, format="JPEG", quality=95, subsampling=2, optimize=False, progressive=False)
        assert actual == buffer.getvalue()


@pytest.mark.parametrize("bbox", [(0, 0, 0, 1), (5, 0, 6, 1)])
def test_empty_crop_does_not_encode(monkeypatch: pytest.MonkeyPatch, bbox: tuple) -> None:
    """空框不触发编码器，也不产生伪造图像素材。"""

    def reject_encoding(_image: np.ndarray) -> bytes:
        """明确拒绝空区域调用编码器。"""
        raise AssertionError("empty crop reached encoder")

    monkeypatch.setattr(operations, "_encode_rgb_as_jpeg_bytes", reject_encoding)
    assert encode_crop_as_jpeg_data_uri(np.zeros((2, 2, 3), dtype=np.uint8), bbox, 0) == ""


@pytest.mark.parametrize("failure", [False, True])
def test_jpeg_encoder_closes_images(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    """编码成功与底层失败都关闭本函数创建的 Pillow 图像。"""
    image = Image.new("RGB", (4, 3))
    monkeypatch.setattr(Image, "fromarray", lambda _array: image)
    if failure:

        def fail_save(*_args, **_kwargs) -> None:
            """模拟底层 JPEG 写入失败以检查异常分支资源释放。"""
            raise OSError("JPEG encoder failed")

        monkeypatch.setattr(image, "save", fail_save)
        with pytest.raises(OSError):
            operations._encode_rgb_as_jpeg_bytes(np.zeros((3, 4, 3), dtype=np.uint8))
    else:
        assert operations._encode_rgb_as_jpeg_bytes(np.zeros((3, 4, 3), dtype=np.uint8)).startswith(b"\xff\xd8")
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


def test_crop_returns_empty_payload_on_encoder_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """实际编码失败保留公开裁剪入口的空载荷约定。"""

    def fail_encoding(_image: np.ndarray) -> bytes:
        """模拟底层编码失败，避免改变无效输入异常的处理方式。"""
        raise OSError("JPEG encoder failed")

    monkeypatch.setattr(operations, "_encode_rgb_as_jpeg_bytes", fail_encoding)
    assert encode_crop_as_jpeg_data_uri(np.zeros((3, 4, 3), dtype=np.uint8), (0, 0, 4, 3), 0) == ""


@pytest.mark.parametrize(
    "image", [np.zeros((3, 4), dtype=np.uint8), np.zeros((0, 4, 3), dtype=np.uint8), np.zeros((3, 4, 3), dtype=np.float64)]
)
def test_invalid_image_inputs_raise_value_error(image: np.ndarray) -> None:
    """已移除库的专有异常不再泄漏，非法形状、空图与位深统一明确报错。"""
    with pytest.raises(ValueError):
        calculate_contrast(image, "rgb")
    with pytest.raises(ValueError):
        operations._encode_rgb_as_jpeg_bytes(image)


def test_invalid_contrast_mode_is_rejected() -> None:
    """无效通道模式沿用既有 ValueError，而不误解释成 BGR。"""
    with pytest.raises(ValueError, match="Invalid image mode"):
        calculate_contrast(np.zeros((3, 4, 3), dtype=np.uint8), "gray")
