"""对原生内核作独立差分，覆盖几何阈值、Unicode 和公开对象只读契约。"""

import pickle
import random
from pathlib import Path

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import _script_geometry as scripts
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.text import Bbox


@pytest.fixture
def native():
    """纯 Python 任务允许跳过，强制 Rust 任务缺失扩展时必须失败。"""
    extension = get_native()
    if extension is None:
        pytest.skip("native backend is not selected")
    return extension


@pytest.mark.parametrize("seed", range(40))
def test_script_geometry_random_parity(native, seed):
    """随机改变字体、尺寸、基线和字符类别，覆盖组件分组及角色精炼。"""
    rng = random.Random(seed)
    alphabet = "Ab09０９中文 αβ²₃,-—/⁄()\n\t\ufffd"
    for _ in range(12):
        chars, tight, origins = [], {}, {}
        for i in range(rng.randrange(1, 100)):
            x = i * rng.choice([3.0, 5.0, 12.0])
            y = rng.choice([10.0, 10.35, 9.5, 12.0, 14.0])
            h = rng.choice([4.0, 6.0, 10.0])
            chars.append(
                {
                    "char": rng.choice(alphabet),
                    "char_idx": i,
                    "bbox": Bbox([x, y - h, x + 4, y + 1]),
                    "font": {"name": rng.choice(["A", "B", ""]), "flags": 0, "weight": 400},
                }
            )
            if rng.random() > 0.04:
                tight[i] = (x, y - h + 1, x + 3, y)
                origins[i] = (x, y)
        protected = {i for i in range(len(chars)) if rng.random() < 0.04}
        before = pickle.dumps((chars, tight, origins))
        expected = scripts._classify_char_script_roles_python(
            chars, tight_bboxes=tight, origins=origins, protected_body_indices=protected
        )
        assert (
            scripts.classify_char_script_roles(chars, tight_bboxes=tight, origins=origins, protected_body_indices=protected)
            == expected
        )
        assert pickle.dumps((chars, tight, origins)) == before


@pytest.mark.parametrize("name", ["caibao1", "demo1", "demo2"])
def test_real_page_script_parity(native, name):
    """逐页比较真实字符和 side-map，源文件和输入对象保持不变。"""
    path = Path(__file__).parents[1] / "demo/pdfs" / f"{name}.pdf"
    with PDFDocument(str(path)) as document:
        for index in range(len(document)):
            page = document[index]
            geometry = page.get_chars_with_geometry()
            expected = scripts._classify_char_script_roles_python(
                geometry.chars, tight_bboxes=geometry.tight_bboxes, origins=geometry.origins
            )
            assert (
                scripts.classify_char_script_roles(geometry.chars, tight_bboxes=geometry.tight_bboxes, origins=geometry.origins)
                == expected
            )


def test_native_script_path_is_exercised(native, monkeypatch):
    """确认普通输入真实执行扩展，不能用参考实现冒充原生通过。"""
    chars = [{"char": "a", "char_idx": 0, "bbox": Bbox([0, 0, 5, 10])}]

    def reject(*args, **kwargs):
        """阻止本测试悄悄使用参考计算。"""
        raise AssertionError("reference path executed")

    monkeypatch.setattr(scripts, "_classify_char_script_roles_python", reject)
    assert scripts.classify_char_script_roles(chars, tight_bboxes={0: (0, 0, 5, 9)}, origins={0: (0, 9)}) == ["body"]
