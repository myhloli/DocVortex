"""不依赖 OpenCV 的公开图像数值接口，导入时不加载 NumPy、Pillow 或原生扩展。"""

from __future__ import annotations
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import numpy as np
Interpolation = Literal["nearest", "linear", "cubic", "area", "lanczos4"]
Border = Literal["constant", "replicate"]
WarpInterpolation = Literal["nearest", "linear", "cubic"]


def decode_image(data: bytes, *, color: bool = False, apply_orientation: bool = False) -> np.ndarray | None:
    """解码为 BGR/BGRA 或灰度数组，明确控制彩色转换与 EXIF 方向。"""
    from .foundation._image_numeric import decode_image as implementation

    return implementation(data, color=color, apply_orientation=apply_orientation)


def gray_image(image: np.ndarray, *, color_order: Literal["rgb", "bgr"] = "rgb") -> np.ndarray:
    """转换三或四通道图像为同位深灰度，忽略透明通道。"""
    from .foundation._image_numeric import gray_image as implementation

    return implementation(image, color_order=color_order)


def resize_image(
    image: np.ndarray,
    size: tuple[int, int] | None,
    *,
    interpolation: Interpolation = "linear",
    scale: tuple[float, float] | None = None,
) -> np.ndarray:
    """按宽高和显式插值缩放，输出独立连续数组。"""
    from .foundation._image_numeric import resize_image as implementation

    return implementation(image, size, interpolation=interpolation, scale=scale)


def warp_image(
    image: np.ndarray,
    matrix: np.ndarray,
    size: tuple[int, int],
    *,
    interpolation: WarpInterpolation = "linear",
    border: Border = "constant",
    value: float = 0,
) -> np.ndarray:
    """按仿射或投影矩阵逆向采样，矩阵表示源坐标到目标坐标。"""
    from .foundation._image_numeric import warp_image as implementation

    return implementation(image, matrix, size, interpolation=interpolation, border=border, value=value)


def perspective_matrix(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """从四组对应点构造确定性投影矩阵。"""
    from .foundation._image_numeric import perspective_matrix as implementation

    return implementation(source, target)


def affine_matrix(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """从三组对应点求解仿射矩阵。"""
    from .foundation._image_numeric import affine_matrix as implementation

    return implementation(source, target)


def trace_contours(mask: np.ndarray, *, external_only: bool = False) -> list[np.ndarray]:
    """按扫描顺序提取并简化八邻域轮廓，保留孔洞和点序。"""
    from .foundation._image_numeric import trace_contours as implementation

    return implementation(mask, external_only=external_only)


def minimum_rectangle(points: np.ndarray) -> tuple[tuple[float, float], tuple[float, float], float]:
    """返回最小外接矩形的中心、尺寸及角度。"""
    from .foundation._image_numeric import minimum_rectangle as implementation

    return implementation(points)


def rectangle_corners(rectangle: tuple[tuple[float, float], tuple[float, float], float]) -> np.ndarray:
    """将中心、尺寸和角度转换为四个有序 float32 角点。"""
    from .foundation._image_numeric import rectangle_corners as implementation

    return implementation(rectangle)


def simplify_contour(points: np.ndarray, epsilon: float, *, closed: bool = True) -> np.ndarray:
    """按距离阈值简化轮廓，保留输入点类型与封闭语义。"""
    from .foundation._image_numeric import simplify_contour as implementation

    return implementation(points, epsilon, closed=closed)


def rasterize_polygons(shape: tuple[int, int], polygons: list[np.ndarray], *, value: int = 1) -> np.ndarray:
    """按整数扫描线填充多边形及其边界，返回 uint8 掩码。"""
    from .foundation._image_numeric import rasterize_polygons as implementation

    return implementation(shape, polygons, value=value)


def label_components(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """标记八连通域并返回标签和 xywh/像素面积统计，含背景。"""
    from .foundation._image_numeric import label_components as implementation

    return implementation(mask)


def morphology(
    image: np.ndarray, kernel: tuple[int, int], *, operation: Literal["dilate", "erode", "close"] = "dilate"
) -> np.ndarray:
    """使用矩形结构元素和默认中心锚点进行形态学运算。"""
    from .foundation._image_numeric import morphology as implementation

    return implementation(image, kernel, operation=operation)


def draw_line(
    image: np.ndarray,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    value: int = 255,
    width: int = 1,
    antialias: bool = False,
) -> np.ndarray:
    """在副本上栅格化线段，推理抗锯齿与普通诊断路径显式区分。"""
    from .foundation._image_numeric import draw_line as implementation

    return implementation(image, start, end, value=value, width=width, antialias=antialias)


def draw_lines(
    image: np.ndarray, lines: np.ndarray, *, value: int = 255, width: int = 1, antialias: bool = False
) -> np.ndarray:
    """在独立数组上批量绘制 xyxy 线段，热循环由原生后端一次处理。"""
    from .foundation._image_numeric import draw_lines as implementation

    return implementation(image, lines, value=value, width=width, antialias=antialias)


def contour_area(points: np.ndarray) -> float:
    """计算任意点轮廓的非负面积。"""
    from .foundation._image_numeric import contour_area as implementation

    return implementation(points)


def contour_length(points: np.ndarray, *, closed: bool = True) -> float:
    """计算开放或闭合轮廓的线段周长。"""
    from .foundation._image_numeric import contour_length as implementation

    return implementation(points, closed=closed)


def rotation_matrix(center: tuple[float, float], angle: float, scale: float = 1.0) -> np.ndarray:
    """构造图像坐标系中绕中心旋转的仿射矩阵。"""
    from .foundation._image_numeric import rotation_matrix as implementation

    return implementation(center, angle, scale)


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """变换最后一维为二维坐标的点阵，并保留输入数值类型。"""
    from .foundation._image_numeric import transform_points as implementation

    return implementation(points, matrix)


__all__ = [
    "Interpolation",
    "Border",
    "WarpInterpolation",
    "decode_image",
    "gray_image",
    "resize_image",
    "warp_image",
    "perspective_matrix",
    "affine_matrix",
    "trace_contours",
    "minimum_rectangle",
    "rectangle_corners",
    "simplify_contour",
    "rasterize_polygons",
    "label_components",
    "morphology",
    "draw_line",
    "draw_lines",
    "contour_area",
    "contour_length",
    "rotation_matrix",
    "transform_points",
]
