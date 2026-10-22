"""公开图像接口使用冻结的独立数值参考，不要求安装 OpenCV。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from docvortex import image

_CASES = json.loads((Path(__file__).parent / "fixtures/image_numeric_reference.json").read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["operation"] + "-" + case["dtype"] + "-" + str(case["args"]))
def test_frozen_numeric_reference(case: dict[str, Any]) -> None:
    """逐字节验证历史图像输入，覆盖轮廓点序、组件顺序及抗锯齿线。"""
    rng = np.random.default_rng(case["seed"])
    shape, operation, args = tuple(case["shape"]), case["operation"], case["args"]
    if operation == "resize":
        source = rng.integers(0, 256, shape, dtype=np.uint8)
        actual = image.resize_image(source, tuple(args["size"]), interpolation=args["interpolation"])
    elif operation == "gray":
        source = (rng.random(shape) * 255).astype(case["dtype"])
        actual = image.gray_image(source, color_order=args["color_order"])
    elif operation == "warp":
        source = rng.integers(0, 256, shape, dtype=np.uint8).astype(case["dtype"])
        matrix = image.perspective_matrix(np.float32(args["source"]), np.float32(args["target"]))
        actual = image.warp_image(
            source, matrix, tuple(args["size"]), interpolation=args["interpolation"], border=args["border"]
        )
    elif operation == "line":
        source = np.zeros(shape, np.uint8)
        actual = image.draw_line(source, tuple(args["start"]), tuple(args["end"]), width=args["width"], antialias=True)
        assert not np.any(source)
    elif operation == "polygon":
        actual = image.rasterize_polygons(shape, [np.int32(args["points"])])
    else:
        source = (rng.random(shape) > 0.7).astype(np.uint8)
        if operation == "morph":
            actual = image.morphology(source, tuple(args["kernel"]), operation=args["operation"])
        elif operation in ("labels", "stats"):
            count, labels, stats = image.label_components(source)
            assert count == len(stats)
            actual = labels if operation == "labels" else stats
        elif operation == "contours":
            actual = np.concatenate(image.trace_contours(source, external_only=args["external_only"]))
        else:
            raise AssertionError(operation)
    assert list(actual.shape) == case["output_shape"]
    assert hashlib.sha256(actual.tobytes()).hexdigest() == case["sha256"]
    assert actual.flags.c_contiguous
    assert actual.flags.writeable


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
@pytest.mark.parametrize("channels", [1, 3, 4])
def test_numeric_input_contract(dtype: type, channels: int) -> None:
    """非连续、多位深和单通道输入保留类型，结果独立且不改动源数组。"""
    source = np.arange(19 * 29 * channels).reshape(19, 29, channels).astype(dtype)[::2, ::2]
    original = source.copy()
    resized = image.resize_image(source, (17, 12))
    assert resized.dtype == source.dtype
    assert resized.shape == ((12, 17) if channels == 1 else (12, 17, channels))
    assert resized.flags.writeable and resized.flags.c_contiguous
    assert not np.shares_memory(resized, source)
    np.testing.assert_array_equal(source, original)


def test_rectangle_degeneracy_and_contour_point_order() -> None:
    """矩形的退化边、负角度以及闭合多边形点序保持裁图约定。"""
    points = np.int32([[0, 0], [10, 0], [10, 20], [0, 20]])
    rectangle = image.minimum_rectangle(points)
    assert rectangle == ((5.0, 10.0), (20.0, 10.0), -90.0)
    np.testing.assert_allclose(image.rectangle_corners(rectangle), points.astype(np.float32)[[2, 3, 0, 1]], atol=1e-12)
    assert image.minimum_rectangle(np.int32([[2, 2], [6, 4]])) == (
        (4.0, 3.0),
        (0.0, float(np.float32(np.sqrt(20)))),
        float(np.float32(-63.43494882292201)),
    )
    assert image.minimum_rectangle(np.empty((0, 2), np.float32)) == ((0.0, 0.0), (0.0, 0.0), -90.0)
    np.testing.assert_array_equal(image.simplify_contour(points, 0.1).reshape(-1, 2), points)
    assert image.contour_area(points) == 200.0
    assert image.contour_length(points) == 60.0


def test_image_facade_does_not_load_optional_runtime() -> None:
    """导入公共接口只定义符号，不加载 NumPy、Pillow、原生扩展或 cv2。"""
    code = """
import sys
import docvortex.image
assert not {"cv2", "numpy", "PIL", "docvortex._native"}.intersection(sys.modules)
assert "resize_image" in docvortex.image.__all__
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("channels", [1, 3, 4])
def test_decode_retains_sixteen_bit_channels(channels: int) -> None:
    """外部编码的十六位 PNG 保留颜色及透明度，结果物化为可写本机数组。"""
    samples = ((np.arange(7 * 9 * channels).reshape(7, 9, channels) * 257 + 33) % 65536).astype(np.uint16)
    content = (Path(__file__).parent / f"fixtures/image_numeric_u16_{channels}.png").read_bytes()
    actual = image.decode_image(content)
    expected = samples[:, :, 0] if channels == 1 else samples[:, :, ::-1] if channels == 3 else samples[:, :, [2, 1, 0, 3]]
    np.testing.assert_array_equal(actual, expected)
    assert actual.dtype == np.uint16
    assert actual.flags.writeable and actual.flags.c_contiguous
    assert image.decode_image(b"not an image") is None


def test_rotation_transform_and_batch_lines() -> None:
    """批量线与逐条线保持同一数值结果，仿射点变换保留坐标类型。"""
    mask = np.zeros((30, 50), np.uint8)
    lines = np.int32([[2, 3, 24, 28], [1, 15, 47, 15]])
    expected = mask.copy()
    for x1, y1, x2, y2 in lines:
        expected = image.draw_line(expected, (int(x1), int(y1)), (int(x2), int(y2)), width=2, antialias=True)
    np.testing.assert_array_equal(image.draw_lines(mask, lines, width=2, antialias=True), expected)
    assert not np.any(mask)
    points = np.int32([[[1, 2], [3, 4]]])
    np.testing.assert_array_equal(image.transform_points(points, image.rotation_matrix((0.0, 0.0), 90.0)), [[[2, -1], [4, -3]]])


@pytest.mark.parametrize("scale", [(2.0, 2.0), (4.0, 4.0), (1.371, 2.088)])
def test_independent_scale_uses_original_sampling_grid(scale: tuple[float, float]) -> None:
    """比例入口不从舍入后的尺寸反推坐标，输出按声明比例确定宽高。"""
    source = np.arange(22 * 32 * 3, dtype=np.uint8).reshape(22, 32, 3)
    output = image.resize_image(source, None, scale=scale)
    assert output.shape == (round(22 * scale[1]), round(32 * scale[0]), 3)
    assert output.flags.c_contiguous
    with pytest.raises(ValueError):
        image.resize_image(source, (20, 30), scale=scale)


def test_decode_exif_orientation_is_explicit() -> None:
    """彩色解码显式选择 EXIF 方向，默认字节解码不自行旋转。"""
    from io import BytesIO
    from PIL import Image

    pixels = np.arange(9 * 17 * 3, dtype=np.uint8).reshape(9, 17, 3)
    metadata = Image.Exif()
    metadata[274] = 6
    buffer = BytesIO()
    with Image.fromarray(pixels) as encoded:
        encoded.save(buffer, format="JPEG", exif=metadata)
    normal = image.decode_image(buffer.getvalue(), color=True)
    oriented = image.decode_image(buffer.getvalue(), color=True, apply_orientation=True)
    np.testing.assert_array_equal(oriented, np.rot90(normal, 3))


def test_all_supported_depths_match_frozen_native_outputs() -> None:
    """冻结旧版本真实原生输出，逐字节覆盖高位深和全部缩放插值模式。"""
    fixture = json.loads((Path(__file__).parent / "fixtures/image_numeric_depth_reference.json").read_text())
    expected = {(case["dtype"], case["interpolation"]): case for case in fixture["cases"]}
    rng = np.random.default_rng(fixture["seed"])
    for dtype in (np.uint8, np.uint16, np.float32):
        source = (rng.random((17, 29, 4)) * (65535 if dtype == np.uint16 else 255)).astype(dtype)[::2, ::2]
        for interpolation in ("nearest", "linear", "cubic", "area", "lanczos4"):
            result = image.resize_image(source, (21, 13), interpolation=interpolation)
            case = expected[np.dtype(dtype).name, interpolation]
            assert list(result.shape) == case["shape"]
            assert hashlib.sha256(result.tobytes()).hexdigest() == case["sha256"]


@pytest.mark.parametrize("dtype", [np.int16, np.uint16, np.int64, np.uint64, np.float16, np.float32, np.float64, np.longdouble])
def test_morphology_preserves_non_uint8_values(dtype: type) -> None:
    """非八位像素走原生极值索引，保持大整数及各浮点位深的精确值。"""
    base = 2**63 if dtype == np.uint64 else 0
    source = np.asarray([[base + 1, base + 5, base + 3], [base + 9, base + 2, base + 7]], dtype=dtype)
    dilated = image.morphology(source, (2, 2), operation="dilate")
    eroded = image.morphology(source, (2, 2), operation="erode")
    np.testing.assert_array_equal(
        dilated, np.asarray([[base + 1, base + 5, base + 5], [base + 9, base + 9, base + 7]], dtype=dtype)
    )
    np.testing.assert_array_equal(
        eroded, np.asarray([[base + 1, base + 1, base + 3], [base + 1, base + 1, base + 2]], dtype=dtype)
    )
    assert dilated.dtype == eroded.dtype == source.dtype
    assert not np.shares_memory(dilated, source)


def test_morphology_nan_propagation_and_signed_zero() -> None:
    """矩形覆盖 NaN 时传播首个 NaN，相等零值保留窗口内末次命中的符号。"""
    source = np.asarray([[1.0, np.nan, 3.0], [4.0, 2.0, 5.0]])
    for operation in ("dilate", "erode"):
        actual = image.morphology(source, (2, 2), operation=operation)
        np.testing.assert_array_equal(np.isnan(actual), [[False, True, True], [False, True, True]])
        zeros = image.morphology(np.asarray([[0.0, -0.0, 0.0]]), (2, 1), operation=operation)
        np.testing.assert_array_equal(np.signbit(zeros), [[False, True, False]])
