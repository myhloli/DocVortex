"""从单独渲染的 PDF 位图透明度反推出边框开孔。"""

from math import ceil

import numpy as np

from ...schema import BBox


def _inner_wall(edge: np.ndarray, *, notch: bool = False) -> float | None:
    """由多条截面估计内沿，横边容许局部摄像头开槽。"""
    active = edge.any(axis=1)
    if np.mean(active) < 0.78:
        return None
    extent = np.max(edge * np.arange(1, edge.shape[1] + 1), axis=1)[active]
    variation = 0.5 if notch else 0.3
    if np.quantile(extent, 0.9) - np.quantile(extent, 0.1) > max(2, variation * edge.shape[1]):
        return None
    return float(np.quantile(extent, 0.25 if notch else 0.5))


def find_frame_opening(
    alpha: np.ndarray,
    bbox: BBox,
    *,
    cut_top: bool = False,
    cut_bottom: bool = False,
) -> BBox | None:
    """起点为实际墨迹框。只有明确跨纸边时允许开孔缺失上边或下边。"""
    if alpha.ndim != 2 or min(alpha.shape) < 16 or cut_top and cut_bottom:
        return None
    ink = alpha >= 32
    ys, xs = np.nonzero(ink)
    if not len(xs):
        return None
    left, top = xs.min().item(), ys.min().item()
    right, bottom = xs.max().item() + 1, ys.max().item() + 1
    tight = ink[top:bottom, left:right]
    rows, cols = tight.shape
    if min(rows, cols) < 16 or not 0.02 <= np.mean(tight) <= 0.36:
        return None

    by, bx = ceil(rows * 0.2), ceil(cols * 0.2)
    core = tight[by:-by, bx:-bx]
    if np.mean(core) > 0.02 or any(np.max(np.mean(core, axis=axis)) > 0.26 for axis in (0, 1)):
        return None
    vertical = tight[by:-by]
    horizontal = tight[:, bx:-bx].T
    lwall = _inner_wall(vertical[:, :bx])
    rwall = _inner_wall(vertical[:, -bx:][:, ::-1])
    twall = _inner_wall(horizontal[:, :by], notch=True)
    bwall = _inner_wall(horizontal[:, -by:][:, ::-1], notch=True)
    if lwall is None or rwall is None:
        return None
    if twall is None:
        if not (cut_top and top <= 1 and tight[:2, :bx].any() and tight[:2, -bx:].any()):
            return None
        twall = 0.0
    if bwall is None:
        if not (cut_bottom and bottom >= len(alpha) - 1 and tight[-2:, :bx].any() and tight[-2:, -bx:].any()):
            return None
        bwall = 0.0

    source_rows, source_cols = alpha.shape
    dx, dy = (bbox[2] - bbox[0]) / source_cols, (bbox[3] - bbox[1]) / source_rows
    return (
        bbox[0] + (left + lwall) * dx,
        bbox[1] + (top + twall) * dy,
        bbox[0] + (right - rwall) * dx,
        bbox[1] + (bottom - bwall) * dy,
    )


def screen_fits_opening(opening: BBox, screen: BBox) -> bool:
    """屏幕需覆盖开孔的全宽和大部分高度，且只允许轻微压住边缘。"""
    width, height = opening[2] - opening[0], opening[3] - opening[1]
    sw, sh = screen[2] - screen[0], screen[3] - screen[1]
    if not 0 < width or not 0 < height or sw < 0.83 * width or sh < 0.77 * height:
        return False
    return all(
        -0.05 * span <= margin <= inset * span
        for margin, span, inset in (
            (screen[0] - opening[0], width, 0.09),
            (screen[1] - opening[1], height, 0.14),
            (opening[2] - screen[2], width, 0.09),
            (opening[3] - screen[3], height, 0.14),
        )
    )
