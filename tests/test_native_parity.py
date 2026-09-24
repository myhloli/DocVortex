"""对原生内核作独立差分，覆盖几何阈值、Unicode 和公开对象只读契约。"""

import pickle
import random
from copy import deepcopy
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


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_visual_runs_and_typography_parity(native, monkeypatch, angle):
    """随机字距、空白、反向框及字体权重下比较完整行对象和引用关系。"""
    from docvortex import _compute_backend
    from docvortex.analyzers.native.pdf import native_text
    from docvortex.analyzers.native.pdf.models import _LineItem

    rng = random.Random(angle + 7)
    for _ in range(60):
        chars = []
        x = 0
        for i in range(rng.randrange(1, 90)):
            x += rng.choice([2, 5, 8, 20])
            char = {
                "char": rng.choice("中文aA12 ,\t\n"),
                "char_idx": i,
                "bbox": Bbox([x, 20, x + 4, 30]),
                "font": {"name": rng.choice(["Abc", "Def", ""]), "flags": 0, "weight": rng.choice([0, 400, 700])},
            }
            chars.append(char)
        line = _LineItem("".join(c["char"] for c in chars), (0, 20, x + 5, 30), angle, 0, chars=chars)
        before = pickle.dumps(line)
        with monkeypatch.context() as context:
            context.setattr(_compute_backend, "get_native", lambda: None)
            expected = native_text._split_native_visual_runs_python(deepcopy(line), (1000, 1000))
        actual = native_text._split_native_visual_runs(line, (1000, 1000))
        assert pickle.dumps(actual) == pickle.dumps(expected)
        assert all(any(c is source for source in chars) for item in actual for c in item.chars)
        assert pickle.dumps(line) == before


@pytest.mark.parametrize("seed", range(20))
def test_dedup_layers_and_hidden_parity(native, monkeypatch, seed):
    """对混合重复绘制、平移阴影和隐藏副本逐值比较，并检查来源引用不被改写。"""
    from docvortex.document.pdf.text import dedup
    from test_pdf_text_dedup import _char, _indexed

    rng = random.Random(seed)
    chars = []
    text = "ABCD 中文123 ab"
    for layer in range(rng.randrange(1, 5)):
        dx, dy = rng.choice([(0, 0), (0.08, 0.08), (1.0, 1.0), (8, 0)])
        for i, c in enumerate(text):
            chars.append(_char(c, i * 10 + dx, 20 + dy, obj=layer, mode=3 if layer == 2 else 0))
    chars = _indexed(chars)
    before = pickle.dumps(chars)
    with monkeypatch.context() as context:
        context.setattr(dedup, "get_native", lambda: None)
        expected = dedup.deduplicate_chars(chars)
    actual = dedup.deduplicate_chars(chars)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    assert pickle.dumps(chars) == before


def test_table_coverage_batch_parity(native):
    """区间乱序、反向端点、重复线和精确相接端点的覆盖率必须逐值一致。"""
    from docvortex.analyzers.native.pdf._table_recovery.geometry import covered_interval_ratio

    rng = random.Random(93)
    rules = [(rng.randrange(2), float(rng.randrange(5)), rng.uniform(-5, 15), rng.uniform(-5, 15)) for _ in range(80)]
    queries = [
        (rng.randrange(2), (float(rng.randrange(5)),), float(rng.randrange(-2, 3)), float(rng.randrange(5, 12)), 0.5)
        for _ in range(90)
    ]
    expected = [
        covered_interval_ratio(
            [(a, b) for o, c, a, b in rules if o == orientation and any(abs(c - v) <= tolerance for v in aliases)], start, end
        )
        for orientation, aliases, start, end, tolerance in queries
    ]
    assert native.coverage_batch(rules, queries) == expected


@pytest.mark.parametrize("indexed", [False, True])
def test_cell_assignment_batch_parity(native, indexed):
    """覆盖边界平局、出界和跨格字符，保持现有索引路径的选择与歧义位。"""
    from docvortex.analyzers.native.pdf._table_recovery import candidate as c
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph

    specs = tuple(
        c.GridCellSpec(r, col, 1, 1, (col * 10.0, r * 10.0, col * 10.0 + 10.0, r * 10.0 + 10.0))
        for r in range(3)
        for col in range(4)
    )
    index = c._build_grid_spec_index(3, 4, specs) if indexed else None
    rng = random.Random(12)
    glyphs = []
    for i in range(300):
        x, y = rng.uniform(-5, 45), rng.uniform(-5, 35)
        glyphs.append(NativeTableGlyph(i, i, "a", (x, y, x + rng.choice([0.0, 5.0, 10.0, 20.0]), y + 5.0), 0))
    expected = [
        c._choose_cell_for_glyph_indexed(g, specs, index) if index is not None else c._choose_cell_for_glyph(g, specs)
        for g in glyphs
    ]
    assert (
        native.assign_cells(
            [g.bbox for g in glyphs],
            [s.bbox for s in specs],
            (index.x_tracks, index.y_tracks, index.owners) if index is not None else None,
        )
        == expected
    )


def test_invalid_parent_cycle_is_rejected(native):
    """损坏的私有数组不能让 Rust 陷入无限循环。"""
    with pytest.raises(ValueError, match="cyclic"):
        native.component_specs([1, 0], 1, 2)
