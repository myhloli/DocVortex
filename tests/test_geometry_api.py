"""验证几何 SDK 的显式坐标语义及导入依赖边界。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from docvortex.geometry import convert_bbox, rotate_bbox


def test_geometry_import_does_not_load_image_or_pdf_runtime() -> None:
    """独立解释器中的几何导入不隐式加载图像解码或 PDFium。"""
    code = """
import sys
from docvortex.geometry import convert_bbox
for name in ('cv2', 'PIL', 'pypdfium2', 'torch', 'transformers'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("source", "target", "bbox", "expected"),
    [
        ("unit", "point", (0.1, 0.2, 0.9, 0.8), (10, 40, 90, 160)),
        ("unit", "pixel", (0.1, 0.2, 0.9, 0.8), (20, 80, 180, 320)),
        ("pixel", "point", (20, 80, 180, 320), (10, 40, 90, 160)),
        ("point", "unit", (10, 40, 90, 160), (0.1, 0.2, 0.9, 0.8)),
        ("point", "pixel", (10, 40, 90, 160), (20, 80, 180, 320)),
    ],
)
def test_coordinate_spaces(source: str, target: str, bbox: tuple, expected: tuple) -> None:
    """相同数值只有配合明确空间才能解释，避免自动判断单位。"""
    assert convert_bbox(bbox, source_space=source, target_space=target, page_size=(100, 200), render_scale=2) == pytest.approx(
        expected
    )


def test_clipping_and_degenerate_bbox() -> None:
    """显式裁剪不改变合法区域，退化框不进入后续图像处理。"""
    assert convert_bbox((-1, -1, 2, 2), source_space="unit", target_space="point", page_size=(100, 200), clip=True) == (
        0,
        0,
        100,
        200,
    )
    assert convert_bbox((1, 1, 1, 2), source_space="point", target_space="point", page_size=(100, 200)) is None
    with pytest.raises(ValueError, match="coordinate space"):
        convert_bbox((0, 0, 1, 1), source_space="unknown", target_space="point", page_size=(100, 200))


@pytest.mark.parametrize(
    ("angle", "expected"),
    [
        (0, (10, 20, 30, 40)),
        (90, (20, 70, 40, 90)),
        (180, (70, 160, 90, 180)),
        (270, (160, 10, 180, 30)),
    ],
)
def test_rotated_rectangle(angle: int, expected: tuple) -> None:
    """四种旋转保持与现有视觉块方向一致的坐标变换。"""
    assert rotate_bbox((10, 20, 30, 40), 100, 200, angle) == expected
