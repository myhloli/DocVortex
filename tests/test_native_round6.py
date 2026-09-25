"""第六轮字符批量分类和分式空间索引的独立差分。"""

import random
import math
from copy import deepcopy

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf import _script_geometry
from docvortex.analyzers.native.pdf.inline import scripts
from docvortex.analyzers.native.pdf.models import _LineItem


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("seed", range(16))
def test_script_batch_keeps_python_roles_and_sources(angle, seed, monkeypatch):
    """对分段、字号及旋转字符比较原始来源与完整脚本角色。"""

    if get_native() is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    chars, tight, origins = [], {}, {}
    alphabet = "A1αβ²₃中文/⁄[] x"
    for index in range(50):
        x = float(index * 4)
        y = rng.choice([10.0, 10.35, 9.0, 12.0])
        height = rng.choice([3.0, 5.0, 10.0])
        chars.append(
            {
                "char": rng.choice(alphabet),
                "char_idx": index,
                "bbox": (x, y, x + 3.0, y + height),
                "font": {"name": rng.choice(["A", "B"]), "flags": 0, "weight": 400},
            }
        )
        tight[index] = (x, y + 0.5, x + 3.0, y + height - 0.5)
        origins[index] = (x, y + height)
    line = _LineItem("".join(char["char"] for char in chars), (0.0, 0.0, 220.0, 30.0), angle, 0, chars=chars)
    line.inline_math_regions = [(60.0, 0.0, 130.0, 30.0)]
    original = deepcopy((line, tight, origins))
    actual = scripts._script_line_char_roles(line, (300.0, 300.0), tight, origins, set())
    with monkeypatch.context() as context:
        context.setattr(scripts, "get_native", lambda: None, raising=False)
        context.setattr(_script_geometry, "get_native", lambda: None)
        context.setattr("docvortex._compute_backend.get_native", lambda: None)
        expected = scripts._script_line_char_roles(line, (300.0, 300.0), tight, origins, set())
    assert actual == expected
    assert (line, tight, origins) == original


@pytest.mark.parametrize("seed", range(24))
def test_fraction_index_matches_exhaustive(seed, monkeypatch):
    """用原全字符扫描比较分式成员，覆盖远处横线、边界和退化框。"""

    rng = random.Random(seed)
    chars, tight = [], {}
    for index in range(100):
        x = float(rng.randrange(0, 140))
        y = float(rng.randrange(0, 30))
        chars.append({"char": rng.choice("A1α-/ "), "char_idx": index})
        tight[index] = (x, y, x + rng.choice([0.0, 2.0, 5.0]), y + 4.0)
    rules = [(float(x), 12.0, float(x + width), 12.1) for x, width in [(0, 3), (20, 10), (50, 25), (100, 80)]]
    result = scripts._fraction_member_indices((200.0, 200.0), chars, tight, rules, 0)
    with monkeypatch.context() as context:
        context.setattr(scripts._FractionCharIndex, "query", lambda self, _rule, _scale: self.chars)
        expected = scripts._fraction_member_indices((200.0, 200.0), chars, tight, rules, 0)
    assert result == expected


def test_fraction_rules_are_reused_without_changing_cell_scale():
    """预旋转横线不能把单元格尺度替换为整页尺度。"""

    rules = [(0.0, 10.0, 8.0, 10.1), (20.0, 11.0, 30.0, 11.1)]
    prepared = scripts._prepare_fraction_rules(rules, (100.0, 100.0), 90)
    for heights in ([4.0, 4.0], [10.0, 10.0]):
        chars = [{"char": "A", "char_idx": 0}, {"char": "1", "char_idx": 1}]
        tight = {i: (0.0, float(i * 8), 4.0, float(i * 8) + heights[i]) for i in range(2)}
        assert scripts._fraction_member_indices((100.0, 100.0), chars, tight, rules, 90, prepared) == scripts._fraction_member_indices(
            (100.0, 100.0), chars, tight, rules, 90
        )


@pytest.mark.parametrize("invalid", [0, math.nan, math.inf, 10**400])
def test_plain_script_batch_rejects_non_float_geometry(invalid):
    """整数、非有限值与超大整数不能静默改变原 Python 几何准入。"""

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    assert native.script_roles_plain_batch(
        [(0.0, 0.0, 5.0, 10.0)], [(0.0, 1.0, 5.0, 9.0)], [(0.0, 9.0)], [4], [0], [0, 1], lambda value: value
    ) == [b"\x00"]
    assert native.script_roles_plain_batch(
        [(0.0, 0.0, 5.0, invalid)], [(0.0, 1.0, 5.0, 9.0)], [(0.0, 9.0)], [4], [0], [0, 1], lambda value: value
    ) is None


def test_script_batches_preserve_line_order_across_chunk_boundary(monkeypatch):
    """超过 64 行时仍按原来源顺序返回全部 sidecar，含公式段。"""

    if get_native() is None:
        pytest.skip("native backend is not selected")
    lines, tight, origins = [], {}, {}
    for number in range(130):
        chars = []
        for offset, text in enumerate("A12B"):
            index = number * 4 + offset
            left = float(offset * 5)
            chars.append({"char": text, "char_idx": index, "bbox": (left, 10.0, left + 4.0, 20.0), "font": {"name": "A"}})
            tight[index] = (left, 11.0, left + 4.0, 19.0)
            origins[index] = (left, 19.0)
        line = _LineItem("A12B", (0.0, 10.0, 20.0, 20.0), 0, number, chars=chars)
        if number % 3 == 0:
            line.inline_math_regions = [(5.0, 10.0, 15.0, 20.0)]
        lines.append(line)
    actual = scripts.detect_pdf_text_script_lines(lines, (100.0, 100.0), tight, origins)
    with monkeypatch.context() as context:
        context.setattr(_script_geometry, "get_native", lambda: None)
        context.setattr("docvortex._compute_backend.get_native", lambda: None)
        expected = scripts.detect_pdf_text_script_lines(lines, (100.0, 100.0), tight, origins)
    assert actual == expected
    assert [line.source_index for line in actual] == list(range(130))
