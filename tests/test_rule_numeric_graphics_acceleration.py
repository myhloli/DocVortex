"""数字文字批处理对照 Python 的 Unicode 语法及输入状态失效。"""
import copy
import re
import sys
from pathlib import Path
from random import Random
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import graphics
from docvortex.analyzers.native.pdf.models import _LineItem

PATTERNS = (r"[+−-]?\d+(?:\.\d+)?", r"\s*[+−-]?\d+(?:[,.]\d+)?%?\s*", r"\s*[-+]?\d+(?:[,.]\d+)*%?\s*")


def line(text):
    """构造无几何副作用的真实内部行对象。"""
    return _LineItem(text=text, bbox=(0.0, 0.0, 3.0, 10.0), angle=0, source_index=1, effective_height=10.0)


@pytest.fixture
def native():
    """Python 模式跳过内核差分，Rust 模式调用真实扩展。"""
    kernel = get_native()
    if kernel is None:
        pytest.skip("native backend is not selected")
    return kernel


@pytest.mark.parametrize("seed", range(24))
def test_numeric_grammar_and_stable_sources(native, seed):
    """随机 ASCII、Unicode 数字、空白和负号逐模式与原正则比较。"""
    rng = Random(seed)
    alphabet = "0123456789+−-.,% abc\t\n\x1c\u00a0\u2003０１٢٣中é²"
    texts = ["", "1", "−1", "１.٢", "\x1c1\x1f", "1,2,3%", "123456789", "\u20031\u2003"]
    texts += ["".join(rng.choices(alphabet, k=rng.randrange(30))) for _ in range(300)]
    lines = [line(text) for text in texts]
    for mode in (2, 0, 1, 0, 2):
        expected = [item for item in lines if re.fullmatch(PATTERNS[mode], item.text.strip() if mode == 0 else item.text)
                    and (mode != 0 or len(item.text.strip()) <= 8)]
        actual = native.graphic_numeric_lines(lines, mode, _LineItem, re.fullmatch)
        assert [id(item) for item in actual] == [id(item) for item in expected]


def test_numeric_cache_text_mutation_copy_and_unknown(native, monkeypatch):
    """文字变化使缓存失效，语义和几何不写入缓存，未知类型及规则修改完整回退。"""
    item = line("12")
    assert graphics._numeric_graphic_lines([item], 0) == [item]
    item.text = "abc"
    assert graphics._numeric_graphic_lines([item], 0) == []
    item.text = "−12"
    assert graphics._numeric_graphic_lines([item], 1) == [item]
    cloned = copy.deepcopy(item)
    cloned.text = "other"
    assert graphics._numeric_graphic_lines([cloned], 1) == []
    item.semantic_type = "formula"
    item.effective_height = 0.0
    assert graphics._numeric_graphic_lines([item], 1) == [item]
    assert native.graphic_numeric_lines([SimpleNamespace(text="1")], 0, _LineItem, re.fullmatch) is None
    assert native.graphic_numeric_lines([line("\ud800")], 0, _LineItem, re.fullmatch) is None
    monkeypatch.setattr(re, "fullmatch", lambda *args: None)
    assert graphics._numeric_graphic_lines([item], 1) is None


def test_numeric_raster_reads_current_geometry(native, monkeypatch):
    """文字缓存命中时仍重新判断角度、语义、字号及宽度。"""
    item = line("12")
    source = SimpleNamespace(lines=[item], image_bboxes=[])
    observed = []
    monkeypatch.setattr(graphics, "_raster_axis_tick_groups", lambda values: observed.append(values) or [])
    graphics._detect_native_raster_axis_graphics(source)
    assert observed[-1] == [item]
    for attr, value in (("angle", 90), ("semantic_type", "formula"), ("effective_height", 0.0), ("bbox", (0.0, 0.0, 100.0, 10.0))):
        previous = getattr(item, attr)
        setattr(item, attr, value)
        graphics._detect_native_raster_axis_graphics(source)
        assert observed[-1] == []
        setattr(item, attr, previous)


def test_numeric_graphics_import_with_legacy_regex_module(monkeypatch):
    """模拟 Python 3.10 没有 re._compiler 的模块形状，导入和私有筛选均可正常执行。"""
    legacy = SimpleNamespace(**{key: value for key, value in vars(re).items() if key != "_compiler"})
    namespace = {"__name__": graphics.__name__, "__package__": graphics.__package__}
    monkeypatch.setitem(sys.modules, "re", legacy)
    path = Path(graphics.__file__)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    item = line("12")
    assert namespace["_numeric_graphic_lines"]([item], 0) == ([item] if get_native() is not None else None)
