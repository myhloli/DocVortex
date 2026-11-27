"""验证薄边、透明边距、纸边截断及独立内嵌图的几何。"""

import numpy as np
from PIL import Image, ImageDraw
import pytest

from docvortex.document.pdf.frame_apertures import find_frame_opening, screen_fits_opening


def make_bezel(*, seam=False, clipped=False):
    """制作完全透明内孔的位图蒙版，保留明显的左右透明边距。"""
    image = Image.new("L", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((9, 1, 55, 77 if clipped else 61), radius=8, outline=255, width=3)
    draw.rectangle((26, 2, 39, 6), fill=255)
    if seam:
        draw.line((32, 2, 32, 61), fill=255)
    return np.array(image)


def test_closed_opening_in_padded_frame():
    """用实际墨迹测量左右边，透明留白不增大联合屏幕。"""
    box = (50, 100, 178, 292)
    opening = find_frame_opening(make_bezel(), box)
    assert opening == pytest.approx((74, 112, 156, 277))
    assert screen_fits_opening(opening, (75, 116, 156, 272))
    assert not screen_fits_opening(opening, (87, 153, 145, 231))
    assert not screen_fits_opening(opening, (96, 113, 175, 276))
    assert not screen_fits_opening(opening, (62, 106, 168, 285))


def test_paper_edge_is_the_only_allowed_missing_wall():
    """开放侧仅可来自实证的纸边裁切，邻侧轮廓必须一直延续到裁切线。"""
    box = (50, 100, 178, 292)
    alpha = make_bezel(clipped=True)
    assert find_frame_opening(alpha, box) is None
    result = find_frame_opening(alpha, box, cut_bottom=True)
    assert result == pytest.approx((74, 112, 156, 292))
    assert screen_fits_opening(result, (75, 116, 155, 291))
    alpha[-2:, 9:13] = 0
    alpha[-2:, 53:57] = 0
    assert find_frame_opening(alpha, box, cut_bottom=True) is None


@pytest.mark.parametrize("mode", ["solid", "seam", "decorated"])
def test_not_single_transparent_opening(mode):
    """绘制本身的主体、双孔分隔和内有装饰的掩模不构成单屏框。"""
    alpha = make_bezel(seam=mode == "seam")
    if mode == "solid":
        alpha[8:-8, 11:-11] = 255
    if mode == "decorated":
        alpha[18:30, 21:27] = 255
    assert find_frame_opening(alpha, (50, 100, 178, 292)) is None
