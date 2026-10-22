"""Rust 图像数值内核的数组适配，保留输入输出及像素舍入契约。"""

from __future__ import annotations

import math
from io import BytesIO
from typing import Literal

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .._compute_backend import get_native
from ._png16 import decode_png16

_INTERPOLATIONS = {"nearest": 0, "linear": 1, "cubic": 2, "area": 3, "lanczos4": 4}


def _image(image: np.ndarray) -> tuple[np.ndarray, bool]:
    """验证位深和通道，转换为本机连续 HWC 输入并记录灰度维度。"""
    if not isinstance(image, np.ndarray) or image.size == 0 or image.ndim not in (2, 3):
        raise ValueError("Expected a non-empty HW/HWC image")
    if image.dtype.type not in (np.uint8, np.uint16, np.float32):
        raise ValueError("Expected uint8, uint16 or float32 image")
    if image.ndim == 3 and not 1 <= image.shape[2] <= 4:
        raise ValueError("Expected one to four image channels")
    return np.ascontiguousarray(image[:, :, None] if image.ndim == 2 else image, dtype=image.dtype.type), image.ndim == 2 or (
        image.ndim == 3 and image.shape[2] == 1
    )


def decode_image(data: bytes, *, color: bool = False, apply_orientation: bool = False) -> np.ndarray | None:
    """以 Pillow 解码常用图片并物化数组，失败保持旧解码返回 None 的约定。"""
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            if not color:
                raw = decode_png16(data)
                if raw is not None:
                    if apply_orientation:
                        orientation = opened.getexif().get(274, 1)
                        if orientation == 2:
                            raw = raw[:, ::-1]
                        elif orientation == 3:
                            raw = np.rot90(raw, 2)
                        elif orientation == 4:
                            raw = raw[::-1]
                        elif orientation == 5:
                            raw = raw.swapaxes(0, 1)
                        elif orientation == 6:
                            raw = np.rot90(raw, 3)
                        elif orientation == 7:
                            raw = raw.swapaxes(0, 1)[::-1, ::-1]
                        elif orientation == 8:
                            raw = np.rot90(raw)
                    if raw.shape[2] == 2:
                        raw = np.stack((raw[:, :, 0],) * 3 + (raw[:, :, 1],), axis=2)
                    return np.ascontiguousarray(raw[:, :, [2, 1, 0, 3] if raw.shape[2] == 4 else [2, 1, 0]])
            image = ImageOps.exif_transpose(opened) if apply_orientation else opened.copy()
            try:
                if color:
                    converted = image.convert("RGB")
                elif image.mode in ("P", "PA"):
                    converted = image.convert("RGBA" if "transparency" in image.info else "RGB")
                elif image.mode in ("1", "L", "I", "I;16", "I;16B", "F", "LA"):
                    converted = image.copy()
                else:
                    converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                try:
                    result = np.array(converted)
                finally:
                    converted.close()
            finally:
                image.close()
        if result.dtype == np.int32 and result.min() >= 0 and result.max() <= 65535:
            result = result.astype(np.uint16)
        if result.ndim == 3 and result.shape[2] in (3, 4):
            result = result[:, :, [2, 1, 0, 3] if result.shape[2] == 4 else [2, 1, 0]].copy()
        elif result.ndim == 3 and result.shape[2] == 2:
            result = np.stack((result[:, :, 0],) * 3 + (result[:, :, 1],), axis=2)
        return np.ascontiguousarray(result, dtype=result.dtype.type)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def _fma32(a: np.ndarray | float, b: np.ndarray | float, c: np.ndarray | float) -> np.ndarray:
    """模拟 float32 单次舍入加乘，双精度恰逢中点时用双和残差裁决。"""
    a, b, c = (np.asarray(v, np.float32).astype(np.float64) for v in (a, b, c))
    product = a * b
    total = product + c
    virtual_c = total - product
    remainder = (product - (total - virtual_c)) + (c - virtual_c)
    rounded = total.astype(np.float32)
    direction = np.where(total >= rounded, np.float32(np.inf), np.float32(-np.inf))
    neighbor = np.nextafter(rounded, direction).astype(np.float64)
    midpoint = (np.abs(total - rounded) * 2 == np.abs(neighbor - rounded)) & (remainder != 0)
    if np.any(midpoint):
        total = np.where(midpoint, np.nextafter(total, np.where(remainder > 0, np.inf, -np.inf)), total)
    return total.astype(np.float32)


def gray_image(image: np.ndarray, *, color_order: Literal["rgb", "bgr"] = "rgb") -> np.ndarray:
    """使用整数定点或 float32 权重，忽略 alpha，不经 Pillow 灰度量化。"""
    array, _ = _image(image)
    if array.shape[2] not in (3, 4) or color_order not in ("rgb", "bgr"):
        raise ValueError("Gray conversion expects RGB/BGR with optional alpha")
    rgb = array[:, :, :3] if color_order == "rgb" else array[:, :, 2::-1]
    if array.dtype in (np.uint8, np.uint16):
        values = rgb.astype(np.int32)
        return ((values[:, :, 0] * 9798 + values[:, :, 1] * 19235 + values[:, :, 2] * 3735 + 16384) >> 15).astype(array.dtype)
    coefficients = np.array([0.299, 0.587, 0.114] if color_order == "rgb" else [0.114, 0.587, 0.299], np.float32)
    first, second = (0, 1) if array.shape[1] >= 4 else (1, 0)
    result = array[:, :, first] * coefficients[first]
    result = _fma32(array[:, :, second], coefficients[second], result)
    result = _fma32(array[:, :, 2], coefficients[2], result)
    tail = array.shape[1] // 4 * 4
    if tail < array.shape[1]:
        scalar = _fma32(array[:, tail:, 0], coefficients[0], array[:, tail:, 1] * coefficients[1])
        result[:, tail:] = _fma32(array[:, tail:, 2], coefficients[2], scalar)
    return result


def _weights(fraction: np.ndarray, mode: str) -> np.ndarray:
    """生成 float32 插值系数，三次核参数固定为 -0.75。"""
    x = fraction.astype(np.float32)
    if mode in ("linear", "area"):
        return np.stack((1 - x, x), axis=-1)
    if mode == "cubic":
        a = np.float32(-0.75)
        p = x + np.float32(1)
        w0 = _fma32(_fma32(_fma32(a, p, -5 * a), p, 8 * a), p, -4 * a)
        w1 = _fma32(_fma32(a + 2, x, -(a + 3)) * x, x, 1)
        q = 1 - x
        w2 = _fma32(_fma32(a + 2, q, -(a + 3)) * q, q, 1)
        w3 = 1 - w0 - w1 - w2
        return np.stack((w0, w1, w2, w3), axis=-1)
    offsets = x[:, None].astype(np.float64) + 3 - np.arange(8)
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = np.sinc(offsets) * np.sinc(offsets / 4)
    weights = weights.astype(np.float32)
    return weights / np.sum(weights, axis=-1, keepdims=True, dtype=np.float32)


def _axis(
    source: int, target: int, mode: str, *, horizontal: bool, factor: float | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """显式计算半像素坐标和边缘系数，不让其他图像库选择采样规则。"""
    scale = 1.0 / (target / source) if factor is None else 1.0 / factor
    if mode == "area":
        base = np.floor(np.arange(target) * scale).astype(np.int64)
        fraction = ((np.arange(target) + 1) - (base + 1) / scale).astype(np.float32)
        fraction = np.where(fraction <= 0, 0, fraction - np.floor(fraction))
    else:
        coordinate = ((np.arange(target) + 0.5) * scale - 0.5).astype(np.float32)
        base = np.floor(coordinate).astype(np.int64)
        fraction = coordinate - base.astype(np.float32)
    if horizontal and mode in ("linear", "area"):
        fraction[(base < 0) | (base >= source - 1)] = 0
        base = np.clip(base, 0, source - 1)
    weights = _weights(fraction, mode)
    offsets = np.arange(weights.shape[1]) - (weights.shape[1] // 2 - 1)
    indices = np.clip(base[:, None] + offsets, 0, source - 1)
    return indices, weights


def _area_axis(source: int, target: int) -> list[list[tuple[int, np.float32]]]:
    """按源像素覆盖长度生成降采样系数，边缘仍用实际覆盖宽度。"""
    scale = source / target
    result = []
    for position in range(target):
        left, right = position * scale, min((position + 1) * scale, source)
        result.append(
            [
                (index, np.float32((min(right, index + 1) - max(left, index)) / (right - left)))
                for index in range(math.floor(left), math.ceil(right))
                if min(right, index + 1) > max(left, index)
            ]
        )
    return result


def _linear_integer_method(shape: tuple[int, int, int], size: tuple[int, int], scale: tuple[float, float] | None = None) -> int:
    """明确选择二次舍入或定点卷积，避免平台库自行更改数值路径。"""
    h, w, c = shape
    tw, th = size
    if c == 1 and any(tw == w * v and th == h * v and (scale is None or scale == (v, v)) for v in (2, 4)):
        return 8
    if scale is not None:
        if c == 1 and w >= 8 and tw >= 8 and th >= 8 and scale[0] >= 1 and scale[1] >= 1:
            return 5
        return 1
    if c <= 3 and tw < w and th < h and tw * 3 >= w and tw * c >= 8 and w * c >= (16 if c != 3 and tw * 2 >= w else 32):
        return 4
    if c == 1 and w >= 8 and tw >= 8 and th >= 8 and ((tw >= w and th >= h) or w / tw > 2 or h / th > 2):
        return 5
    if c == 4 and w >= 2 and tw >= 2 and th >= 8 and (w != tw * 2 or h != th * 2):
        return 5
    return 1


def _linear_block_axis(source: int, target: int, channels: int) -> tuple[np.ndarray, np.ndarray]:
    """按固定通道块生成八位系数，保留边缘回拉和定点步长的历史舍入。"""
    count = target * channels
    ratio = 2 if target * 2 >= source else 3
    full_count = count // 32 * 2 if source * channels >= 16 * ratio else 0
    coordinates = np.empty(count, np.int64)

    def rounded(numerator: int | np.ndarray) -> int | np.ndarray:
        """有理数固定点向最近整数舍入，中点向正数方向。"""
        return (numerator + target // 2) // target

    def aligned(pixel: int) -> int:
        """源中心坐标加半个八位权重量级，为后续截断提供偏置。"""
        return int(rounded(((pixel << 16) + 32768) * source)) - 32768 + 128

    def store(position: int, width: int, base: int, *, incremental: bool = False) -> None:
        """通道块内相同源像素共享系数，跨块边缘按固定点路径计算。"""
        phase = position % channels
        offsets = (np.arange(width) + phase) // channels
        delta = offsets * int(rounded(source << 16)) if incremental else rounded(offsets * source * 65536)
        coordinates[position : position + width] = base + delta

    handled = 0
    if channels == 3:
        without_pullback = full_count
        while without_pullback:
            position = (without_pullback - 1) * 16
            base_index = (aligned(position // 3) >> 16) * 3 + position % 3
            if base_index + 16 * ratio <= source * 3:
                break
            without_pullback -= 1
        triplets = without_pullback // 3 if full_count > 3 else 0
        base, recalibration = aligned(0), 0
        five, six = int(rounded(source * 5 * 65536)), int(rounded(source * 6 * 65536))
        for _ in range(triplets):
            if recalibration == 170:
                base, recalibration = aligned(handled * 16 // 3), 0
            else:
                recalibration += 1
            for increment in (five, five, six):
                store(handled * 16, 16, base)
                base += increment
                handled += 1
        while handled < full_count:
            position = handled * 16
            store(position, 16, aligned(position // channels), incremental=True)
            handled += 1
    else:
        base = aligned(0)
        step = int(rounded((source * 16 // channels) << 16))
        for block in range(full_count):
            if block % 512 == 0:
                base = aligned(block * 16 // channels)
            store(block * 16, 16, base)
            base += step
        handled = full_count
    position = handled * 16
    while position < count:
        position = min(position, count - 8)
        store(position, 8, aligned(position // channels), incremental=channels == 3)
        if position + 8 >= count:
            break
        position += 8
    base = coordinates >> 16
    fraction = (coordinates & 65535) >> 8
    return np.clip(base[:, None] + np.arange(2), 0, source - 1), np.column_stack((256 - fraction, fraction)).astype(np.int32)


def _linear_round_axes(
    shape: tuple[int, int, int], size: tuple[int, int], method: int, scale: tuple[float, float] | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """二次舍入路径使用八位或七位权重，坐标仍遵守半像素中心。"""
    axes = []
    for axis, (source, target) in enumerate(zip((shape[1], shape[0]), size, strict=True)):
        positions = np.arange(target, dtype=np.int64)
        if method == 4:
            fixed = ((((positions << 16) + 32768) * source + target // 2) // target) - 32768 + 128
            base = fixed >> 16
            fraction = (fixed & 65535) >> 8
            weights = np.column_stack((256 - fraction, fraction)).astype(np.int32)
        else:
            ratio = np.float32(source / target if scale is None else 1 / scale[axis])
            offset = np.float32(np.float32(0.5) * ratio - np.float32(0.5))
            coord = _fma32(positions.astype(np.float32), ratio, offset)
            base = np.floor(coord).astype(np.int64)
            left = ((base.astype(np.float32) + np.float32(1) - coord) * np.float32(128)).astype(np.int32)
            weights = np.column_stack((left, 128 - left))
        axes.append((np.clip(base[:, None] + np.arange(2), 0, source - 1), weights))
    if method == 4:
        axes[0] = _linear_block_axis(shape[1], size[0], shape[2])
    return axes[0][0], axes[0][1], axes[1][0], axes[1][1]


def resize_image(
    image: np.ndarray, size: tuple[int, int] | None, *, interpolation: str = "linear", scale: tuple[float, float] | None = None
) -> np.ndarray:
    """在原生内核缩放，所有模式都不检测或加载 OpenCV。"""
    array, gray = _image(image)
    if size is None:
        if scale is None or len(scale) != 2 or not all(math.isfinite(v) and v > 0 for v in scale):
            raise ValueError("Positive scale is required when size is omitted")
        size = round(array.shape[1] * scale[0]), round(array.shape[0] * scale[1])
    elif scale is not None:
        raise ValueError("Provide size or scale, not both")
    if interpolation not in _INTERPOLATIONS or len(size) != 2 or min(size) <= 0:
        raise ValueError("Invalid image size or interpolation")
    if size == (array.shape[1], array.shape[0]):
        result = array.copy()
    else:
        native = get_native()
        mode = interpolation
        integer_linear = _linear_integer_method(array.shape, size, scale) if mode == "linear" and array.dtype == np.uint8 else 1
        if mode == "linear" and integer_linear in (4, 5):
            xi, xw, yi, yw = _linear_round_axes(array.shape, size, integer_linear, scale)
            xi, xw, yi, yw = xi.tolist(), xw.tolist(), yi.tolist(), yw.tolist()
            method = integer_linear
        elif mode == "nearest":
            xi = (
                (np.arange(size[0]) * (1.0 / (size[0] / array.shape[1]) if scale is None else 1.0 / scale[0])).astype(int)[
                    :, None
                ]
            ).tolist()
            yi = (
                (np.arange(size[1]) * (1.0 / (size[1] / array.shape[0]) if scale is None else 1.0 / scale[1])).astype(int)[
                    :, None
                ]
            ).tolist()
            xw, yw = [[1.0]] * size[0], [[1.0]] * size[1]
            method = 0
        elif (
            (mode == "area" or (mode == "linear" and array.shape[1] == size[0] * 2 and array.shape[0] == size[1] * 2))
            and array.shape[1] >= size[0]
            and array.shape[0] >= size[1]
        ):
            xt, yt = _area_axis(array.shape[1], size[0]), _area_axis(array.shape[0], size[1])
            xi, xw = [[i for i, w in entries] for entries in xt], [[float(w) for i, w in entries] for entries in xt]
            yi, yw = [[i for i, w in entries] for entries in yt], [[float(w) for i, w in entries] for entries in yt]
            method = (
                7
                if array.dtype == np.uint8
                and array.shape[1] == size[0] * 2
                and array.shape[0] == size[1] * 2
                and array.shape[2] in (1, 3, 4)
                else 3
            )
        else:
            xi, xw = _axis(array.shape[1], size[0], mode, horizontal=True, factor=None if scale is None else scale[0])
            yi, yw = _axis(array.shape[0], size[1], mode, horizontal=False, factor=None if scale is None else scale[1])
            method = 0
            if array.dtype == np.uint8:
                xw, yw = np.rint(xw * 2048), np.rint(yw * 2048)
                method = 2 if integer_linear == 8 else 1 if mode in ("linear", "area") else 6 if mode == "cubic" else 2
            xi, yi, xw, yw = xi.tolist(), yi.tolist(), xw.tolist(), yw.tolist()
        output = native.image_resize(
            array.tobytes(),
            array.shape[1],
            array.shape[0],
            array.shape[2],
            array.dtype.itemsize,
            size[0],
            size[1],
            method,
            xi,
            xw,
            yi,
            yw,
        )
        result = np.frombuffer(output, dtype=array.dtype).reshape(size[1], size[0], array.shape[2]).copy()
    return np.ascontiguousarray(result[:, :, 0] if gray else result)


def _linear_solve(matrix: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    """原生小型消元固定主元选择及融合乘加顺序，奇异输入返回零系数。"""
    native = get_native()
    result = native.image_solve(matrix.tolist(), rhs.tolist())
    return np.asarray(result, np.float64) if result is not None else np.zeros_like(rhs)


def perspective_matrix(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """使用四组对应点求解八个自由参数，奇异输入保留齐次分量。"""
    source = np.asarray(source, dtype=np.float32).reshape(4, 2)
    target = np.asarray(target, dtype=np.float32).reshape(4, 2)
    if not np.all(np.isfinite(source)) or not np.all(np.isfinite(target)):
        raise ValueError("Non-finite perspective points")
    matrix = np.zeros((8, 8), np.float64)
    rhs = target.T.reshape(8).astype(np.float64)
    for i, ((x, y), (u, v)) in enumerate(zip(source, target, strict=True)):
        matrix[i] = [x, y, 1, 0, 0, 0, -x * u, -y * u]
        matrix[i + 4] = [0, 0, 0, x, y, 1, -x * v, -y * v]
    return np.append(_linear_solve(matrix, rhs), 1).reshape(3, 3)


def affine_matrix(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """使用三组 float32 对应点求解双精度仿射系数。"""
    source = np.asarray(source, dtype=np.float32).reshape(3, 2)
    target = np.asarray(target, dtype=np.float32).reshape(3, 2)
    if not np.all(np.isfinite(source)) or not np.all(np.isfinite(target)):
        raise ValueError("Non-finite affine points")
    rows = np.column_stack((source, np.ones(3))).astype(np.float64)
    matrix = np.zeros((6, 6), np.float64)
    matrix[:3, :3] = matrix[3:, 3:] = rows
    return _linear_solve(matrix, target.T.reshape(-1).astype(np.float64)).reshape(2, 3)


def warp_image(
    image: np.ndarray,
    matrix: np.ndarray,
    size: tuple[int, int],
    *,
    interpolation: str = "linear",
    border: str = "constant",
    value: float = 0,
) -> np.ndarray:
    """以连续坐标逆向采样，明确边界、舍入及三次核计算精度。"""
    array, gray = _image(image)
    if interpolation not in ("nearest", "linear", "cubic") or border not in ("constant", "replicate") or min(size) <= 0:
        raise ValueError("Unsupported warp settings")
    matrix = np.asarray(matrix, dtype=np.float64)
    affine = matrix.shape == (2, 3)
    if affine:
        matrix = np.vstack((matrix, [0, 0, 1]))
    if matrix.shape != (3, 3):
        raise ValueError("Expected affine or perspective matrix")
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        inverse = np.zeros((3, 3))
    native = get_native()
    output = native.image_warp(
        array.tobytes(),
        array.shape[1],
        array.shape[0],
        array.shape[2],
        array.dtype.itemsize,
        size[0],
        size[1],
        _INTERPOLATIONS[interpolation],
        border == "replicate",
        value,
        inverse.reshape(-1).tolist(),
        affine,
    )
    result = np.frombuffer(output, array.dtype).reshape(size[1], size[0], array.shape[2]).copy()
    return result[:, :, 0] if gray else result


def morphology(image: np.ndarray, kernel: tuple[int, int], *, operation: str = "dilate") -> np.ndarray:
    """原生矩形极值保留位深；通用数值以有序秩传输并按原索引物化像素。"""
    if image.ndim != 2 or min(kernel) <= 0 or operation not in ("dilate", "erode", "close"):
        raise ValueError("Expected 2D image and positive rectangular kernel")
    if operation == "close":
        return morphology(morphology(image, kernel, operation="dilate"), kernel, operation="erode")
    width, height = kernel
    native = get_native()
    if image.dtype == np.uint8:
        output = native.image_morphology(
            np.ascontiguousarray(image).tobytes(), image.shape[1], image.shape[0], width, height, operation == "dilate"
        )
        return np.frombuffer(output, np.uint8).reshape(image.shape).copy()
    if image.dtype.kind not in "iuf":
        raise ValueError("Morphology requires a real numeric image")
    # 传输排序秩避免 uint64/longdouble 经 double 转换丢精度；最终索引仍指向原始像素。
    values, inverse = np.unique(image, return_inverse=True)
    ranks = np.ascontiguousarray(inverse, dtype=np.uint64)
    nan_rank = int(np.flatnonzero(np.isnan(values))[0]) if image.dtype.kind == "f" and np.any(np.isnan(values)) else None
    indices = native.image_morphology_indices(
        ranks.tobytes(), image.shape[1], image.shape[0], width, height, operation == "dilate", nan_rank
    )
    return image.reshape(-1)[np.frombuffer(indices, np.uint64)].reshape(image.shape).copy()


def label_components(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """采用八邻域并查集标记，统计 xywh 与实际像素面积。"""
    array = np.ascontiguousarray(mask != 0, dtype=np.uint8)
    if array.ndim != 2:
        raise ValueError("Expected 2D mask")
    native = get_native()
    data, stats = native.image_components(array.tobytes(), array.shape[1], array.shape[0])
    labels = np.frombuffer(data, np.int32).reshape(array.shape).copy()
    return len(stats), labels, np.asarray(stats, dtype=np.int32)


def trace_contours(mask: np.ndarray, *, external_only: bool = False) -> list[np.ndarray]:
    """从二值数组返回独立轮廓，不暴露或引用图像运行时对象。"""
    array = np.ascontiguousarray(mask != 0, dtype=np.uint8)
    if array.ndim != 2:
        raise ValueError("Expected 2D mask")
    native = get_native()
    return [
        np.asarray(points, dtype=np.int32).reshape(-1, 1, 2)
        for points in native.image_contours(array.tobytes(), array.shape[1], array.shape[0], external_only)
    ]


def minimum_rectangle(points: np.ndarray) -> tuple[tuple[float, float], tuple[float, float], float]:
    """以旋转卡尺寻找最小矩形，角度固定在 [-90, 0) 并保留等面积裁决。"""
    original = np.asarray(points, np.float32).reshape(-1, 2)
    if not np.all(np.isfinite(original)):
        raise ValueError("Non-finite rectangle points")
    native = get_native()
    cx, cy, width, height, angle = native.image_minimum_rectangle(original.tobytes())
    return (cx, cy), (width, height), angle


def rectangle_corners(rectangle: tuple[tuple[float, float], tuple[float, float], float]) -> np.ndarray:
    """按矩形角度构造旧裁图所用的四点顺序。"""
    center, size, angle = rectangle
    radians = angle * math.pi / 180
    b = np.float32(math.cos(radians) * 0.5)
    a = np.float32(math.sin(radians) * 0.5)
    cx, cy = np.float32(center[0]), np.float32(center[1])
    width, height = np.float32(size[0]), np.float32(size[1])
    points = np.empty((4, 2), np.float32)
    ah, aw, bh, bw = a * height, a * width, b * height, b * width
    points[0] = (cx - ah - bw, cy + bh - aw)
    points[1] = (cx + ah - bw, cy - bh - aw)
    points[2] = (cx + ah + bw, cy - bh + aw)
    points[3] = (cx - ah + bw, cy + bh + aw)
    return points


def simplify_contour(points: np.ndarray, epsilon: float, *, closed: bool = True) -> np.ndarray:
    """迭代 Douglas–Peucker 后清理近直线点，保留闭合轮廓的起点裁决。"""
    original = np.asarray(points)
    values = original.reshape(-1, 2)
    n = len(values)
    if not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("Contour epsilon must be finite and non-negative")
    if n == 0:
        return original.copy()
    stack, output = [], []
    eps = epsilon * epsilon
    is_closed = closed
    iterations, pos, distant = 3, 0, 0
    if not is_closed:
        if not np.array_equal(values[0], values[-1]):
            stack.append((0, n - 1))
        else:
            is_closed, iterations = True, 1
    if is_closed:
        for _ in range(iterations):
            pos = (pos + distant) % n
            start = values[pos]
            pos = (pos + 1) % n
            max_distance = 0.0
            for j in range(1, n):
                delta = values[pos].astype(np.float64) - start
                pos = (pos + 1) % n
                distance = float(np.dot(delta, delta))
                if distance > max_distance:
                    max_distance, distant = distance, j
        if max_distance <= eps:
            output.append(start.copy())
        else:
            right = (distant + pos) % n
            stack.extend(((right, pos), (pos, right)))
    while stack:
        left, right = stack.pop()
        start, end = values[left], values[right]
        pos = (left + 1) % n
        farthest, maximum = left, 0.0
        delta = end.astype(np.float64) - start
        while pos != right:
            offset = values[pos].astype(np.float64) - start
            length = float(np.dot(delta, delta))
            projection = float(np.dot(offset, delta))
            if projection < 0:
                distance = float(np.dot(offset, offset)) * length
            elif projection > length:
                endpoint = values[pos].astype(np.float64) - end
                distance = float(np.dot(endpoint, endpoint)) * length
            else:
                distance = float(offset[1] * delta[0] - offset[0] * delta[1]) ** 2
            if distance > maximum:
                maximum, farthest = distance, pos
            pos = (pos + 1) % n
        if maximum <= eps * float(np.dot(delta, delta)):
            output.append(start.copy())
        else:
            stack.extend(((farthest, right), (left, farthest)))
    if not is_closed:
        output.append(values[-1].copy())
    result = np.asarray(output, dtype=original.dtype)
    count, retained = len(result), len(result)
    pos = count - 1 if closed else 0
    start = result[pos].copy()
    pos = (pos + 1) % count
    write = pos
    point = result[pos].copy()
    pos = (pos + 1) % count
    index = 0 if closed else 1
    while index < count - (not closed) and retained > 2:
        end = result[pos].copy()
        pos = (pos + 1) % count
        delta = end.astype(np.float64) - start
        offset = point.astype(np.float64) - start
        distance = abs(offset[0] * delta[1] - offset[1] * delta[0])
        inner = float(np.dot(offset, end.astype(np.float64) - point))
        if distance * distance <= 0.5 * eps * float(np.dot(delta, delta)) and delta[0] != 0 and delta[1] != 0 and inner >= 0:
            retained -= 1
            result[write] = start = end
            write = (write + 1) % count
            point = result[pos].copy()
            pos = (pos + 1) % count
            index += 2
            continue
        result[write] = start = point
        write = (write + 1) % count
        point = end
        index += 1
    if not closed:
        result[write] = point
    return result[:retained].reshape(-1, 1, 2)


def rasterize_polygons(shape: tuple[int, int], polygons: list[np.ndarray], *, value: int = 1) -> np.ndarray:
    """扫描线按偶奇规则填充，边界整数线段另外栅格化。"""
    height, width = shape
    if min(shape) <= 0 or not 0 <= value <= 255:
        raise ValueError("Invalid polygon image shape or value")
    native = get_native()
    output = native.image_polygons(width, height, [np.asarray(p, np.int32).reshape(-1, 2).tolist() for p in polygons], value)
    return np.frombuffer(output, np.uint8).reshape(shape).copy()


def draw_line(
    image: np.ndarray,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    value: int = 255,
    width: int = 1,
    antialias: bool = False,
) -> np.ndarray:
    """推理线使用固定点抗锯齿规则，普通诊断线使用 Pillow 并返回独立数组。"""
    array = np.ascontiguousarray(image).copy()
    if width < 1 or array.dtype != np.uint8:
        raise ValueError("Lines require uint8 image and positive width")
    if not antialias:
        from PIL import ImageDraw

        with Image.fromarray(array) as canvas:
            ImageDraw.Draw(canvas).line((start, end), fill=value, width=width)
            return np.array(canvas)
    native = get_native()
    output = native.image_lines(
        array.tobytes(),
        array.shape[1],
        array.shape[0],
        1 if array.ndim == 2 else array.shape[2],
        [[*start, *end]],
        value,
        width,
    )
    return np.frombuffer(output, array.dtype).reshape(array.shape).copy()


def draw_lines(
    image: np.ndarray, lines: np.ndarray, *, value: int = 255, width: int = 1, antialias: bool = False
) -> np.ndarray:
    """批量线段只在入口复制一次图像，原生后端一次完成所有抗锯齿绘制。"""
    array = np.ascontiguousarray(image)
    coordinates = np.asarray(lines, np.int32).reshape(-1, 4)
    if array.dtype != np.uint8 or width < 1 or not 0 <= value <= 255:
        raise ValueError("Lines require uint8 input and positive width")
    native = get_native()
    if antialias:
        output = native.image_lines(
            array.tobytes(),
            array.shape[1],
            array.shape[0],
            1 if array.ndim == 2 else array.shape[2],
            coordinates.tolist(),
            value,
            width,
        )
        return np.frombuffer(output, np.uint8).reshape(array.shape).copy()
    output = array.copy()
    for x1, y1, x2, y2 in coordinates:
        output = draw_line(output, (int(x1), int(y1)), (int(x2), int(y2)), value=value, width=width, antialias=antialias)
    return output


def contour_area(points: np.ndarray) -> float:
    """有向鞋带和在 double 精度累计，面积返回非负值。"""
    values = np.asarray(points).reshape(-1, 2).astype(np.float64)
    if len(values) < 3:
        return 0.0
    previous = np.roll(values, 1, axis=0)
    return abs(float(np.sum(previous[:, 0] * values[:, 1] - previous[:, 1] * values[:, 0], dtype=np.float64))) * 0.5


def contour_length(points: np.ndarray, *, closed: bool = True) -> float:
    """线段长度先量化到 float32，再用 double 累计闭合或开放周长。"""
    values = np.asarray(points, np.float32).reshape(-1, 2)
    if len(values) < 2:
        return 0.0
    segments = values - np.roll(values, 1, axis=0) if closed else np.diff(values, axis=0)
    return float(np.sqrt(np.sum(segments * segments, axis=1, dtype=np.float32)).sum(dtype=np.float64))


def rotation_matrix(center: tuple[float, float], angle: float, scale: float = 1.0) -> np.ndarray:
    """以图像坐标约定构造绕中心的 double 精度旋转仿射矩阵。"""
    cx, cy = map(float, np.asarray(center, np.float32))
    radians = angle * math.pi / 180
    alpha, beta = math.cos(radians) * scale, math.sin(radians) * scale
    return np.array(
        [[alpha, beta, (1 - alpha) * cx - beta * cy], [-beta, alpha, beta * cx + (1 - alpha) * cy]], dtype=np.float64
    )


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """按 float32 仿射系数变换点阵，整数输入保留饱和偶数舍入。"""
    original = np.asarray(points)
    values = original.reshape(-1, 2).astype(np.float32)
    coefficients = np.asarray(matrix, np.float32).reshape(2, 3)
    transformed = np.column_stack([_fma32(values[:, 0], row[0], values[:, 1] * row[1]) + row[2] for row in coefficients])
    if np.issubdtype(original.dtype, np.integer):
        bounds = np.iinfo(original.dtype)
        transformed = np.clip(np.rint(transformed), bounds.min, bounds.max)
    return transformed.astype(original.dtype).reshape(original.shape)
