"""无第三方视觉运行时的图像数值内核，Rust 与 NumPy 使用相同输入输出契约。"""

from __future__ import annotations

import math
from decimal import Context, Decimal
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


def _cast(value: np.ndarray, dtype: np.dtype) -> np.ndarray:
    """按目标位深做饱和偶数舍入，浮点输出保持 float32。"""
    if dtype == np.float32:
        return value.astype(np.float32)
    return np.clip(np.rint(value), 0, np.iinfo(dtype).max).astype(dtype)


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


def _resize_reference(
    array: np.ndarray, size: tuple[int, int], mode: str, scale: tuple[float, float] | None = None
) -> np.ndarray:
    """以分离卷积实现参考缩放，整型线性路径保留两次定点截断。"""
    height, width, channels = array.shape
    target_w, target_h = size
    method = _linear_integer_method(array.shape, size, scale) if mode == "linear" and array.dtype == np.uint8 else 1
    if mode == "nearest":
        ys = np.minimum(
            (np.arange(target_h) * (1.0 / (target_h / height) if scale is None else 1.0 / scale[1])).astype(int), height - 1
        )
        xs = np.minimum(
            (np.arange(target_w) * (1.0 / (target_w / width) if scale is None else 1.0 / scale[0])).astype(int), width - 1
        )
        return array[ys[:, None], xs].copy()
    if array.dtype == np.uint8 and mode == "linear":
        method = _linear_integer_method(array.shape, size, scale)
        if method in (4, 5):
            xi, xw, yi, yw = _linear_round_axes(array.shape, size, method, scale)
            bits = 8 if method == 4 else 7
            rounding = 1 << (bits - 1)
            vertical = (
                array[yi[:, 0]].astype(np.int32) * yw[:, 0, None, None]
                + array[yi[:, 1]].astype(np.int32) * yw[:, 1, None, None]
                + rounding
            ) >> bits
            if method == 4:
                columns = np.arange(target_w * channels) % channels
                flattened = vertical.reshape(target_h, width * channels)
                result = (
                    (
                        flattened[:, xi[:, 0] * channels + columns] * xw[:, 0]
                        + flattened[:, xi[:, 1] * channels + columns] * xw[:, 1]
                        + rounding
                    )
                    >> bits
                ).astype(np.uint8)
                return result.reshape(target_h, target_w, channels)
            return (
                (vertical[:, xi[:, 0]] * xw[:, 0, None] + vertical[:, xi[:, 1]] * xw[:, 1, None] + rounding) >> bits
            ).astype(np.uint8)
    if mode == "linear" and width == target_w * 2 and height == target_h * 2:
        mode = "area"
    if mode == "area" and width >= target_w and height >= target_h:
        if width % target_w == 0 and height % target_h == 0:
            block = array.reshape(target_h, height // target_h, target_w, width // target_w, channels)
            total = block.astype(np.float32).sum(axis=(1, 3)) / np.float32((height // target_h) * (width // target_w))
            if array.dtype == np.uint8 and width == target_w * 2 and height == target_h * 2 and channels in (1, 3, 4):
                total = np.floor(total + np.float32(0.5))
            return _cast(total, array.dtype)
        xs, ys = _area_axis(width, target_w), _area_axis(height, target_h)
        horizontal = np.zeros((height, target_w, channels), np.float32)
        for x, entries in enumerate(xs):
            for index, weight in entries:
                horizontal[:, x] += array[:, index].astype(np.float32) * weight
        result = np.zeros((target_h, target_w, channels), np.float32)
        for y, entries in enumerate(ys):
            for index, weight in entries:
                result[y] += horizontal[index] * weight
        return _cast(result, array.dtype)
    xindex, xweight = _axis(width, target_w, mode, horizontal=True, factor=None if scale is None else scale[0])
    yindex, yweight = _axis(height, target_h, mode, horizontal=False, factor=None if scale is None else scale[1])
    if array.dtype == np.uint8:
        xweight = np.rint(xweight * 2048).astype(np.int32)
        yweight = np.rint(yweight * 2048).astype(np.int32)
        horizontal = np.zeros((height, target_w, channels), np.int32)
        for index in range(xweight.shape[1]):
            horizontal += array[:, xindex[:, index]].astype(np.int32) * xweight[:, index][None, :, None]
        if mode in ("linear", "area") and method != 8:
            result = sum((horizontal[yindex[:, index]] >> 4) * yweight[:, index, None, None] >> 16 for index in range(2))
            return np.clip((result + 2) >> 2, 0, 255).astype(np.uint8)
        result = np.zeros((target_h, target_w, channels), np.int64)
        for index in range(yweight.shape[1]):
            result += horizontal[yindex[:, index]].astype(np.int64) * yweight[:, index, None, None]
        converted = np.clip((result + (1 << 21)) >> 22, 0, 255).astype(np.uint8)
        if mode == "cubic":
            # 向量段使用浮点加乘和偶数舍入，不足八个通道值的尾部保持定点规则。
            vertical = horizontal[yindex[:, 3]].astype(np.float32) * (yweight[:, 3, None, None] / np.float32(1 << 22))
            for index in (2, 1, 0):
                vertical = _fma32(horizontal[yindex[:, index]], yweight[:, index, None, None] / np.float32(1 << 22), vertical)
            prefix = target_w * channels // 8 * 8
            converted.reshape(target_h, -1)[:, :prefix] = _cast(vertical, array.dtype).reshape(target_h, -1)[:, :prefix]
        return converted
    horizontal = np.zeros((height, target_w, channels), np.float32)
    for index in range(xweight.shape[1]):
        horizontal += array[:, xindex[:, index]].astype(np.float32) * xweight[:, index][None, :, None]
    result = np.zeros((target_h, target_w, channels), np.float32)
    for index in range(yweight.shape[1]):
        result += horizontal[yindex[:, index]] * yweight[:, index, None, None]
    return _cast(result, array.dtype)


def resize_image(
    image: np.ndarray, size: tuple[int, int] | None, *, interpolation: str = "linear", scale: tuple[float, float] | None = None
) -> np.ndarray:
    """在原生或参考后端缩放，所有模式都不检测或加载 OpenCV。"""
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
        if native is not None and hasattr(native, "image_resize"):
            mode = interpolation
            integer_linear = (
                _linear_integer_method(array.shape, size, scale) if mode == "linear" and array.dtype == np.uint8 else 1
            )
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
        else:
            result = _resize_reference(array, size, interpolation, scale)
    return np.ascontiguousarray(result[:, :, 0] if gray else result)


def _linear_solve(matrix: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    """小型消元固定主元选择及加乘顺序，参考后端使用局部精确十进制乘加。"""
    native = get_native()
    if native is not None and hasattr(native, "image_solve"):
        result = native.image_solve(matrix.tolist(), rhs.tolist())
        return np.asarray(result, np.float64) if result is not None else np.zeros_like(rhs)
    a, b = matrix.copy(), rhs.copy()
    n = len(b)
    context = Context(prec=110)

    def fma(x: float, y: float, z: float) -> float:
        """局部 Decimal 上下文保留融合乘加的一次 double 舍入，不更改全局设置。"""
        return float(context.fma(Decimal.from_float(float(x)), Decimal.from_float(float(y)), Decimal.from_float(float(z))))

    for i in range(n):
        pivot = i + int(np.argmax(np.abs(a[i:, i])))
        if abs(a[pivot, i]) < np.finfo(np.float64).eps * 100:
            return np.zeros_like(rhs)
        a[[i, pivot], i:] = a[[pivot, i], i:]
        b[[i, pivot]] = b[[pivot, i]]
        reciprocal = -1 / a[i, i]
        for j in range(i + 1, n):
            alpha = a[j, i] * reciprocal
            for k in range(i + 1, n):
                a[j, k] = fma(alpha, a[i, k], a[j, k])
            b[j] = fma(alpha, b[i], b[j])
    for i in range(n - 1, -1, -1):
        value = float(b[i])
        for k in range(i + 1, n):
            value = fma(-a[i, k], b[k], value)
        b[i] = value / a[i, i]
    return b


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


def _fma16(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """在半精度乘加后只做一次舍入，用于八位三次插值的固定数值契约。"""
    return (
        np.asarray(a, np.float16).astype(np.float64) * np.asarray(b, np.float16).astype(np.float64)
        + np.asarray(c, np.float16).astype(np.float64)
    ).astype(np.float16)


def _warp_weights(fraction: np.ndarray, dtype: np.dtype) -> np.ndarray:
    """连续三次采样核的系数按目标计算精度显式量化。"""
    x = fraction.astype(dtype)
    one, a = dtype(1), dtype(-0.75)
    x2 = x * x
    b = one - x
    w0 = a * (x * (b * b))
    w3 = a * (x2 * b)
    fma = _fma16 if dtype == np.float16 else _fma32
    w1 = fma(x2, fma(dtype(1.25), x, dtype(-2.25)), one)
    w2 = one - w0 - w1 - w3
    return np.stack((w0, w1, w2, w3), axis=-1)


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
    if native is not None and hasattr(native, "image_warp"):
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
    output = np.empty((size[1], size[0], array.shape[2]), dtype=array.dtype)
    xd = np.arange(size[0], dtype=np.float64)
    xf = xd.astype(np.float32)
    precision = np.float16 if interpolation == "cubic" and array.dtype == np.uint8 and array.shape[2] != 2 else np.float32
    fma = _fma16 if precision == np.float16 else _fma32
    for y in range(size[1]):
        if interpolation == "cubic":
            inv = inverse.astype(np.float32)
            row = _fma32(np.float32(y), inv[:, 1], inv[:, 2])
            if affine:
                sx = _fma32(xf, inv[0, 0], row[0])
                sy = _fma32(xf, inv[1, 0], row[1])
            else:
                denominator = row[2].astype(np.float64) + xd * float(inv[2, 0])
                sx = np.divide(
                    float(row[0]) + xd * float(inv[0, 0]), denominator, out=np.zeros_like(xd), where=denominator != 0
                ).astype(np.float32)
                sy = np.divide(
                    float(row[1]) + xd * float(inv[1, 0]), denominator, out=np.zeros_like(xd), where=denominator != 0
                ).astype(np.float32)
        else:
            row = (
                _fma32(np.float32(y), inverse[:, 1].astype(np.float32), inverse[:, 2].astype(np.float32))
                if array.dtype == np.float32
                else (y * inverse[:, 1] + inverse[:, 2]).astype(np.float32)
            )
            denominator = _fma32(xf, inverse[2, 0], row[2])
            sx = np.divide(_fma32(xf, inverse[0, 0], row[0]), denominator, out=np.zeros_like(xf), where=denominator != 0)
            sy = np.divide(_fma32(xf, inverse[1, 0], row[1]), denominator, out=np.zeros_like(xf), where=denominator != 0)
            # 不足八个像素的尾部按双精度系数求值，再量化连续坐标。
            tail = size[0] // 8 * 8
            if tail < size[0]:
                denominator = (xd[tail:] * inverse[2, 0] + y * inverse[2, 1] + inverse[2, 2]).astype(np.float32)
                sx[tail:] = np.divide(
                    xd[tail:] * inverse[0, 0] + y * inverse[0, 1] + inverse[0, 2],
                    denominator,
                    out=np.zeros_like(xd[tail:]),
                    where=denominator != 0,
                )
                sy[tail:] = np.divide(
                    xd[tail:] * inverse[1, 0] + y * inverse[1, 1] + inverse[1, 2],
                    denominator,
                    out=np.zeros_like(xd[tail:]),
                    where=denominator != 0,
                )
        if interpolation == "nearest":
            ix, iy = np.rint(sx).astype(np.int64), np.rint(sy).astype(np.int64)
            pixels = array[np.clip(iy, 0, array.shape[0] - 1), np.clip(ix, 0, array.shape[1] - 1)].copy()
            if border == "constant":
                pixels[(ix < 0) | (iy < 0) | (ix >= array.shape[1]) | (iy >= array.shape[0])] = value
            output[y] = pixels
            continue
        bx, by = np.floor(sx).astype(np.int64), np.floor(sy).astype(np.int64)
        alpha, beta = sx - bx.astype(np.float32), sy - by.astype(np.float32)
        pixels = []
        taps = 4 if interpolation == "cubic" else 2
        half = 1 if taps == 4 else 0
        for j in range(taps):
            iy = by + j - half
            row_pixels = []
            for i in range(taps):
                ix = bx + i - half
                sampled = array[np.clip(iy, 0, array.shape[0] - 1), np.clip(ix, 0, array.shape[1] - 1)].astype(precision)
                if border == "constant":
                    sampled[(ix < 0) | (iy < 0) | (ix >= array.shape[1]) | (iy >= array.shape[0])] = value
                row_pixels.append(sampled)
            pixels.append(row_pixels)
        if interpolation == "linear":
            top = _fma32(alpha[:, None], pixels[0][1] - pixels[0][0], pixels[0][0])
            bottom = _fma32(alpha[:, None], pixels[1][1] - pixels[1][0], pixels[1][0])
            result = _fma32(beta[:, None], bottom - top, top)
        else:
            wx, wy = _warp_weights(alpha, precision), _warp_weights(beta, precision)
            result = np.zeros((size[0], array.shape[2]), precision)
            for j in range(4):
                horizontal = pixels[j][0] * wx[:, 0, None]
                for i in range(1, 4):
                    horizontal = fma(pixels[j][i], wx[:, i, None], horizontal)
                result = fma(horizontal, wy[:, j, None], result)
        output[y] = _cast(result, array.dtype)
    return output[:, :, 0] if gray else output


def morphology(image: np.ndarray, kernel: tuple[int, int], *, operation: str = "dilate") -> np.ndarray:
    """矩形二值结构元素通过积分图计算，非二值输入使用明确的极值规则。"""
    if image.ndim != 2 or min(kernel) <= 0 or operation not in ("dilate", "erode", "close"):
        raise ValueError("Expected 2D image and positive rectangular kernel")
    if operation == "close":
        return morphology(morphology(image, kernel, operation="dilate"), kernel, operation="erode")
    width, height = kernel
    native = get_native()
    if image.dtype == np.uint8 and native is not None and hasattr(native, "image_morphology"):
        output = native.image_morphology(
            np.ascontiguousarray(image).tobytes(), image.shape[1], image.shape[0], width, height, operation == "dilate"
        )
        return np.frombuffer(output, np.uint8).reshape(image.shape).copy()
    ax, ay = width // 2, height // 2
    high = operation == "dilate"
    if image.dtype == np.uint8 and np.all((image == 0) | (image == 255)):
        binary = np.pad(image != 0, ((ay, height - ay - 1), (ax, width - ax - 1)), constant_values=not high)
        integral = np.pad(binary.astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        sums = integral[height:, width:] - integral[:-height, width:] - integral[height:, :-width] + integral[:-height, :-width]
        return ((sums > 0 if high else sums == width * height) * 255).astype(np.uint8)
    border = (
        (-np.inf if high else np.inf)
        if np.issubdtype(image.dtype, np.floating)
        else (np.iinfo(image.dtype).min if high else np.iinfo(image.dtype).max)
    )
    padded = np.pad(image, ((ay, height - ay - 1), (ax, width - ax - 1)), constant_values=border)
    result = np.full_like(image, border)
    for dy in range(height):
        for dx in range(width):
            (np.maximum if high else np.minimum)(result, padded[dy : dy + image.shape[0], dx : dx + image.shape[1]], out=result)
    return result


def label_components(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """采用八邻域并查集标记，统计 xywh 与实际像素面积。"""
    array = np.ascontiguousarray(mask != 0, dtype=np.uint8)
    if array.ndim != 2:
        raise ValueError("Expected 2D mask")
    native = get_native()
    if native is not None and hasattr(native, "image_components"):
        data, stats = native.image_components(array.tobytes(), array.shape[1], array.shape[0])
        labels = np.frombuffer(data, np.int32).reshape(array.shape).copy()
        return len(stats), labels, np.asarray(stats, dtype=np.int32)
    labels = np.zeros(array.shape, np.int32)
    parents = [0]

    def root(index: int) -> int:
        """压缩已发现标签的并查集路径。"""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for y in range(array.shape[0]):
        for x in range(array.shape[1]):
            if not array[y, x]:
                continue
            neighbors = [
                int(labels[ny, nx])
                for ny, nx in ((y, x - 1), (y - 1, x - 1), (y - 1, x), (y - 1, x + 1))
                if 0 <= ny < array.shape[0] and 0 <= nx < array.shape[1] and labels[ny, nx]
            ]
            if not neighbors:
                label = len(parents)
                parents.append(label)
            else:
                label = min(root(index) for index in neighbors)
                for index in neighbors:
                    parents[root(index)] = label
            labels[y, x] = label
    roots = np.asarray([root(index) for index in range(len(parents))], np.int32)
    mapping = np.zeros(len(parents), np.int32)
    count = 1
    for y in range(0, array.shape[0], 2):
        for x in range(0, array.shape[1], 2):
            for index in roots[labels[y : y + 2, x : x + 2]].reshape(-1):
                if index != 0 and mapping[index] == 0:
                    mapping[index] = count
                    count += 1
    labels = mapping[roots[labels]]
    stats = []
    for label in range(int(labels.max(initial=0)) + 1):
        ys, xs = np.nonzero(labels == label)
        stats.append(
            [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1), int(xs.size)]
            if xs.size
            else [-1, -1, 0, 0, 0]
        )
    return len(stats), labels, np.asarray(stats, np.int32)


def _contours_reference(mask: np.ndarray, external_only: bool) -> list[np.ndarray]:
    """沿八邻域边界追踪，使用边界标记保留孔洞并去除直线中间点。"""
    data = np.pad(mask != 0, 1).astype(np.int16)
    directions = [(1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1)]
    contours = []
    for y in range(1, data.shape[0] - 1):
        previous = 0
        last_boundary = (0, y)
        for x in range(1, data.shape[1] - 1):
            current = int(data[y, x])
            if current == previous:
                continue
            outer = previous == 0 and current == 1
            hole = current == 0 and previous >= 1
            if not outer and not hole:
                previous = current
                if previous & -2:
                    last_boundary = (x, y)
                continue
            if hole and previous & -2:
                last_boundary = (x - 1, y)
            if external_only and (hole or data[last_boundary[1], last_boundary[0]] > 0):
                previous = current
                continue
            start = (x - int(hole), y)
            s_end = s = 0 if hole else 4
            while True:
                s = (s - 1) & 7
                dx, dy = directions[s]
                first = (start[0] + dx, start[1] + dy)
                if data[first[1], first[0]] or s == s_end:
                    break
            points = []
            if s == s_end:
                data[start[1], start[0]] = -126
                points.append((start[0] - 1, start[1] - 1))
            else:
                position = start
                previous_direction = s ^ 4
                for _ in range(data.size * 8):
                    s_end = s
                    next_direction = s + 1
                    while next_direction < 16:
                        dx, dy = directions[next_direction & 7]
                        following = (position[0] + dx, position[1] + dy)
                        if data[following[1], following[0]]:
                            break
                        next_direction += 1
                    s = next_direction & 7
                    if (s - 1) & 0xFFFFFFFF < s_end:
                        data[position[1], position[0]] = -126
                    elif data[position[1], position[0]] == 1:
                        data[position[1], position[0]] = 2
                    if s != previous_direction:
                        points.append((position[0] - 1, position[1] - 1))
                        previous_direction = s
                    if following == start and position == first:
                        break
                    position = following
                    s = (s + 4) & 7
                else:
                    raise RuntimeError("Contour traversal did not terminate")
            contours.append(np.asarray(points, np.int32).reshape(-1, 1, 2))
            last_boundary = start
            previous = int(data[y, x])
    return list(reversed(contours))


def trace_contours(mask: np.ndarray, *, external_only: bool = False) -> list[np.ndarray]:
    """从二值数组返回独立轮廓，不暴露或引用图像运行时对象。"""
    array = np.ascontiguousarray(mask != 0, dtype=np.uint8)
    if array.ndim != 2:
        raise ValueError("Expected 2D mask")
    native = get_native()
    if native is not None and hasattr(native, "image_contours"):
        return [
            np.asarray(points, dtype=np.int32).reshape(-1, 1, 2)
            for points in native.image_contours(array.tobytes(), array.shape[1], array.shape[0], external_only)
        ]
    return _contours_reference(array, external_only)


def _hull(points: np.ndarray) -> np.ndarray:
    """单调链凸包保留确定的逆时针顺序与 float32 坐标。"""
    values = sorted(set(map(tuple, np.asarray(points, np.float32).reshape(-1, 2))))
    if len(values) < 3:
        return np.asarray(values, np.float32).reshape(-1, 2)

    def cross(a: tuple, b: tuple, c: tuple) -> float:
        """以双精度判断三点的方向，坐标存储仍保持 float32。"""
        return float(b[0] - a[0]) * float(c[1] - a[1]) - float(b[1] - a[1]) * float(c[0] - a[0])

    lower, upper = [], []
    for point in values:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(values):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return np.asarray(lower[:-1] + upper[:-1], np.float32)


def minimum_rectangle(points: np.ndarray) -> tuple[tuple[float, float], tuple[float, float], float]:
    """以旋转卡尺寻找最小矩形，角度固定在 [-90, 0) 并保留等面积裁决。"""
    original = np.asarray(points, np.float32).reshape(-1, 2)
    if not np.all(np.isfinite(original)):
        raise ValueError("Non-finite rectangle points")
    native = get_native()
    if native is not None and hasattr(native, "image_minimum_rectangle"):
        cx, cy, width, height, angle = native.image_minimum_rectangle(original.tobytes())
        return (cx, cy), (width, height), angle
    hull = _hull(original)
    n = len(hull)
    if n == 0:
        return (0.0, 0.0), (0.0, 0.0), -90.0
    if n == 1:
        return tuple(map(float, hull[0])), (0.0, 0.0), -90.0
    # 凸包以最右点开始；有单调输入索引时回到该索引序列的起点。
    start = max(range(n), key=lambda i: (hull[i, 0], hull[i, 1]))
    hull = np.roll(hull, -start, axis=0)
    indices = [int(np.flatnonzero(np.all(original == point, axis=1))[0]) for point in hull]
    for increasing in (True, False) if n >= 3 else ():
        start = int(np.argmin(indices) if increasing else np.argmax(indices))
        rotated = indices[start:] + indices[:start]
        if all((a < b) == increasing for a, b in zip(rotated, rotated[1:])):
            hull = np.roll(hull, -start, axis=0)
            break
    if n == 2:
        dx, dy = map(float, hull[0] - hull[1])
        width, height, angle = 0.0, float(np.float32(math.hypot(dx, dy))), -90.0
        if dx == 0:
            width, height = height, width
        elif dy < 0:
            width, height = height, width
            angle = math.degrees(math.atan2(dy, dx))
        elif dy > 0:
            angle = -math.degrees(math.atan2(dx, dy))
        return tuple(map(float, (hull[0] + hull[1]) * np.float32(0.5))), (width, height), float(np.float32(angle))
    vectors = np.roll(hull, -1, axis=0) - hull
    lengths = (1 / np.sqrt(np.sum(vectors.astype(np.float64) ** 2, axis=1))).astype(np.float32)
    seq = [int(np.argmin(hull[:, 1])), int(np.argmax(hull[:, 0])), int(np.argmax(hull[:, 1])), int(np.argmin(hull[:, 0]))]
    best_area = np.float32(np.inf)
    saved = None
    for _ in range(n):
        e0, e1, e2, e3 = vectors[seq]
        rotated = [e0, np.array([e1[1], -e1[0]]), -e2, np.array([-e3[1], e3[0]])]
        main = 0
        for index in range(1, 4):
            if _fma32(rotated[index][1], rotated[main][0], -rotated[index][0] * rotated[main][1]) < 0:
                main = index
        lead_x, lead_y = vectors[seq[main]] * lengths[seq[main]]
        a, b = [(lead_x, lead_y), (lead_y, -lead_x), (-lead_x, -lead_y), (-lead_y, lead_x)][main]
        seq[main] = (seq[main] + 1) % n
        dx, dy = hull[seq[1]] - hull[seq[3]]
        width = _fma32(dx, a, dy * b)
        dx, dy = hull[seq[2]] - hull[seq[0]]
        height = _fma32(-dx, b, dy * a)
        area = np.float32(width * height)
        if area <= best_area:
            best_area = area
            saved = (seq[3], a, b, width, height, seq[0])
    left, a, b, width, height, bottom = saved
    c1 = _fma32(a, hull[left, 0], hull[left, 1] * b)
    c2 = _fma32(-b, hull[bottom, 0], hull[bottom, 1] * a)
    determinant = _fma32(a, a, b * b)
    inverse = np.float32(1) / determinant
    corner = np.array([_fma32(c1, a, -c2 * b) * inverse, _fma32(a, c2, b * c1) * inverse], np.float32)
    edge1 = np.array([a * width, b * width], np.float32)
    edge2 = np.array([-b * height, a * height], np.float32)
    center = corner + (edge1 + edge2) * np.float32(0.5)
    w, h = np.float32(math.hypot(*map(float, edge2))), np.float32(math.hypot(*map(float, edge1)))
    angle = -90.0
    if edge1[0] == 0 and edge1[1] > 0:
        w, h = h, w
    else:
        angle = -math.degrees(math.atan2(float(edge1[0]), float(edge1[1])))
    return tuple(map(float, center)), (float(w), float(h)), float(np.float32(angle))


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


def _divide_int(numerator: int, denominator: int) -> int:
    """有符号整数除法向零截断，不经过浮点坐标转换。"""
    return (abs(numerator) // abs(denominator)) * (-1 if (numerator < 0) != (denominator < 0) else 1)


def _clip_line(
    shape: tuple[int, int], start: tuple[int, int], end: tuple[int, int]
) -> tuple[bool, tuple[int, int], tuple[int, int]]:
    """按先垂直后水平的顺序裁剪整数线段，端点向零截断。"""
    height, width = shape
    x1, y1 = start
    x2, y2 = end
    right, bottom = width - 1, height - 1
    c1 = int(x1 < 0) + int(x1 > right) * 2 + int(y1 < 0) * 4 + int(y1 > bottom) * 8
    c2 = int(x2 < 0) + int(x2 > right) * 2 + int(y2 < 0) * 4 + int(y2 > bottom) * 8
    if not (c1 & c2) and (c1 | c2):
        if c1 & 12:
            a = 0 if c1 < 8 else bottom
            x1 += int(float(a - y1) * (x2 - x1) / (y2 - y1))
            y1 = a
            c1 = int(x1 < 0) + int(x1 > right) * 2
        if c2 & 12:
            a = 0 if c2 < 8 else bottom
            x2 += int(float(a - y2) * (x2 - x1) / (y2 - y1))
            y2 = a
            c2 = int(x2 < 0) + int(x2 > right) * 2
        if not (c1 & c2) and (c1 | c2):
            if c1:
                a = 0 if c1 == 1 else right
                y1 += int(float(a - x1) * (y2 - y1) / (x2 - x1))
                x1 = a
                c1 = 0
            if c2:
                a = 0 if c2 == 1 else right
                y2 += int(float(a - x2) * (y2 - y1) / (x2 - x1))
                x2 = a
                c2 = 0
    return not (c1 | c2), (x1, y1), (x2, y2)


def _line8(image: np.ndarray, start: tuple[int, int], end: tuple[int, int], value: int) -> None:
    """八连通线段固定从左往右绘制，斜率中点不提前推进次轴。"""
    inside, start, end = _clip_line(image.shape[:2], start, end)
    if not inside:
        return
    if start[0] > end[0]:
        start, end = end, start
    x, y = start
    dx, dy = end[0] - x, abs(end[1] - y)
    sign = 1 if end[1] >= y else -1
    vertical = dy > dx
    major, minor = (dy, dx) if vertical else (dx, dy)
    error = major - 2 * minor
    for _ in range(major + 1):
        image[y, x] = value
        move = error < 0
        error += -2 * minor + (2 * major if move else 0)
        x += int(move) if vertical else 1
        y += sign if vertical else sign * int(move)


_SLOPE_CORRECTION = (
    181,
    181,
    181,
    182,
    182,
    183,
    184,
    185,
    187,
    188,
    190,
    192,
    194,
    196,
    198,
    201,
    203,
    206,
    209,
    211,
    214,
    218,
    221,
    224,
    227,
    231,
    235,
    238,
    242,
    246,
    250,
    254,
)
_AA_FILTER = (
    168,
    177,
    185,
    194,
    202,
    210,
    218,
    224,
    231,
    236,
    241,
    246,
    249,
    252,
    254,
    254,
    254,
    254,
    252,
    249,
    246,
    241,
    236,
    231,
    224,
    218,
    210,
    202,
    194,
    185,
    177,
    168,
    158,
    149,
    140,
    131,
    122,
    114,
    105,
    97,
    89,
    82,
    75,
    68,
    62,
    56,
    50,
    45,
    40,
    36,
    32,
    28,
    25,
    22,
    19,
    16,
    14,
    12,
    11,
    9,
    8,
    7,
    5,
    5,
)


def _line_aa(image: np.ndarray, start: tuple[int, int], end: tuple[int, int], value: int) -> None:
    """十六位定点抗锯齿线使用高斯查表和两次端点覆盖混合。"""
    inside, start, end = _clip_line((image.shape[0] << 16, image.shape[1] << 16), start, end)
    if not inside:
        return
    dx, dy = end[0] - start[0], end[1] - start[1]
    horizontal = abs(dx) > abs(dy)
    major = 0 if horizontal else 1
    minor = 1 - major
    if start[major] > end[major]:
        start, end = end, start
    distance = end[major] - start[major]
    step = _divide_int((end[minor] - start[minor]) << 16, distance | 1)
    last = end[major] + 65536
    count = (last >> 16) - (start[major] >> 16)
    coordinate = start[minor] + ((step * -(start[major] & 65535)) >> 16) + 32768
    slope = (step >> 11) & 63
    if step < 0:
        slope ^= 63
    slope = 256 if slope & 32 else _SLOPE_CORRECTION[slope]
    i, j = (start[major] >> 9) & 120, (last >> 9) & 120
    t0, t1, t2 = slope << 7, ((120 - i) | 4) * slope, (j | 4) * slope
    ep = [
        0,
        ((((j - i) & 120) | 4) * slope >> 8) & 511,
        (t1 >> 8) & 511,
        ((((j - i) & 120) | 4) * slope >> 8) & 511,
        ((((j - i) + 128) | 4) * slope >> 8) & 511,
        ((t1 + t0) >> 8) & 511,
        (t2 >> 8) & 511,
        ((t2 + t0) >> 8) & 511,
        slope,
    ]
    for scount in range(count + 1):
        axis = (start[major] >> 16) + scount
        minor_axis = (coordinate >> 16) - 1
        remaining = count - scount
        correction = ep[(((scount >= 2) + 1) & (scount | 2)) * 3 + (((remaining >= 2) + 1) & (remaining | 2))]
        dist = (coordinate >> 11) & 31
        for offset, weight in enumerate((_AA_FILTER[dist + 32], _AA_FILTER[dist], _AA_FILTER[63 - dist])):
            x, y = (axis, minor_axis + offset) if horizontal else (minor_axis + offset, axis)
            if 0 <= x < image.shape[1] and 0 <= y < image.shape[0]:
                alpha = (correction * weight >> 8) & 255
                current = image[y, x].astype(np.int32)
                current += ((value - current) * alpha + 127) >> 8
                current += ((value - current) * alpha + 127) >> 8
                image[y, x] = current
        coordinate += step


def _convex_aa(image: np.ndarray, points: list[tuple[int, int]], value: int) -> None:
    """抗锯齿凸多边形的左右边逐行推进，保留固定点端点舍入。"""
    count = len(points)
    for index in range(count):
        _line_aa(image, points[index - 1], points[index], value)
    low = min(range(count), key=lambda i: points[i][1])
    ymin, ymax = (points[low][1] + 32768) >> 16, (max(p[1] for p in points) + 32768) >> 16
    xmin = (min(p[0] for p in points) + 32768) >> 16
    xmax = (max(p[0] for p in points) + 32768) >> 16
    if xmax < 0 or ymax < 0 or xmin >= image.shape[1] or ymin >= image.shape[0]:
        return
    ymax = min(ymax, image.shape[0] - 1)
    edges = [[low, 1, -65536, 0, ymin], [low, count - 1, -65536, 0, ymin]]
    remaining = count
    for y in range(ymin, ymax + 1):
        if y < ymax or y == ymin:
            for edge in edges:
                if y >= edge[4]:
                    index0, direction = edge[0], edge[1]
                    index = (index0 + direction) % count
                    while remaining > 0:
                        remaining -= 1
                        ty = (points[index][1] + 32768) >> 16
                        if ty > y:
                            xs, xe = points[index0][0], points[index][0]
                            edge[4] = ty
                            edge[3] = _divide_int((xe - xs) * 2 + ty - y, 2 * (ty - y))
                            edge[2] = xs
                            edge[0] = index
                            break
                        index0, index = index, (index + direction) % count
                    else:
                        remaining = -1
        if remaining < 0:
            break
        if 0 <= y < image.shape[0]:
            left, right = sorted((edges[0][2], edges[1][2]))
            x1, x2 = max(0, (left + 65535) >> 16), min(image.shape[1] - 1, right >> 16)
            if x1 <= x2:
                image[y, x1 : x2 + 1] = value
        for edge in edges:
            edge[2] += edge[3]


def rasterize_polygons(shape: tuple[int, int], polygons: list[np.ndarray], *, value: int = 1) -> np.ndarray:
    """扫描线按偶奇规则填充，边界整数线段另外栅格化。"""
    height, width = shape
    if min(shape) <= 0 or not 0 <= value <= 255:
        raise ValueError("Invalid polygon image shape or value")
    native = get_native()
    if native is not None and hasattr(native, "image_polygons"):
        output = native.image_polygons(
            width, height, [np.asarray(p, np.int32).reshape(-1, 2).tolist() for p in polygons], value
        )
        return np.frombuffer(output, np.uint8).reshape(shape).copy()
    result = np.zeros(shape, np.uint8)
    edges = []
    for polygon in polygons:
        points = np.asarray(polygon, np.int32).reshape(-1, 2)
        for index in range(len(points)):
            x0, y0 = map(int, points[index - 1])
            x1, y1 = map(int, points[index])
            _line8(result, (x0, y0), (x1, y1), value)
            if y0 != y1:
                inside, clipped0, clipped1 = _clip_line(shape, (x0, y0), (x1, y1))
                cy0, cy1 = (clipped0[1], clipped1[1]) if clipped0[1] != clipped1[1] else (y0, y1)
                delta = _divide_int((clipped1[0] - clipped0[0]) << 16, cy1 - cy0)
                base = (clipped0[0] << 16) + (y0 - cy0) * delta
                if y0 > y1:
                    base += (y1 - y0) * delta
                    y0, y1 = y1, y0
                edges.append((y0, y1, base, delta))
    for y in range(height):
        intersections = sorted(x + (y - y0) * delta for y0, y1, x, delta in edges if y0 <= y < y1)
        for index in range(0, len(intersections) - 1, 2):
            left = (intersections[index] + 65535) >> 16
            right = intersections[index + 1] >> 16
            left, right = max(0, left), min(width - 1, right)
            if left <= right:
                result[y, left : right + 1] = value
    return result


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
    if native is not None and hasattr(native, "image_lines"):
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
    if width > 1 and any(x < 0 or y < 0 or x >= array.shape[1] or y >= array.shape[0] for x, y in (start, end)):
        # 粗线先按线宽扩展区域裁剪，避免远端坐标改变边界像素的斜率舍入。
        _, clipped0, clipped1 = _clip_line(
            (array.shape[0] + width * 2, array.shape[1] + width * 2),
            (start[0] + width, start[1] + width),
            (end[0] + width, end[1] + width),
        )
        start = clipped0[0] - width, clipped0[1] - width
        end = clipped1[0] - width, clipped1[1] - width
    p0, p1 = tuple(v << 16 for v in start), tuple(v << 16 for v in end)
    if width == 1:
        _line_aa(array, p0, p1, value)
        return array
    dx, dy = start[0] - end[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    thickness = width << 15
    if length > 0:
        factor = (thickness + (width & 1) * 32768) / length
        px, py = round(dy * factor), round(dx * factor)
        points = [(p0[0] + px, p0[1] + py), (p0[0] - px, p0[1] - py), (p1[0] - px, p1[1] - py), (p1[0] + px, p1[1] + py)]
        _convex_aa(array, points, value)
    radius = (thickness + 32768) >> 16
    delta = 90 if radius < 3 else 30 if radius < 10 else 18 if radius < 15 else 5
    for cx, cy in (p0, p1):
        cap = []
        for angle in range(0, 361, delta):
            # 三角表系数先量化为 float32，再形成十六位定点端帽。
            x = round(cx + thickness * float(np.float32(math.cos(math.radians(angle)))))
            y = round(cy + thickness * float(np.float32(math.sin(math.radians(angle)))))
            if not cap or cap[-1] != (x, y):
                cap.append((x, y))
        _convex_aa(array, cap, value)
    return array


def draw_lines(
    image: np.ndarray, lines: np.ndarray, *, value: int = 255, width: int = 1, antialias: bool = False
) -> np.ndarray:
    """批量线段只在入口复制一次图像，原生后端一次完成所有抗锯齿绘制。"""
    array = np.ascontiguousarray(image)
    coordinates = np.asarray(lines, np.int32).reshape(-1, 4)
    if array.dtype != np.uint8 or width < 1 or not 0 <= value <= 255:
        raise ValueError("Lines require uint8 input and positive width")
    native = get_native()
    if antialias and native is not None and hasattr(native, "image_lines"):
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
