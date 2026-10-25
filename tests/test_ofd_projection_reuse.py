"""OFD阅读方向复用的逐位浮点、回调顺序、缓存边界和完整公开输出回归。"""

from decimal import Decimal
from fractions import Fraction
import cProfile
from copy import deepcopy
import math
from pathlib import Path
import pstats
import random

import pytest

import _ofd_assembly_oracle as oracle
from _ofd_test_utils import build_ofd_package, page_xml, text_object
from docvortex import parse
from docvortex.analyzers.native.ofd import assembly, reading_order, table
from docvortex.analyzers.native.ofd.models import GlyphItem, TextLine
from docvortex.schema import BlockType
from test_native_ofd_text_geometry import _exact, _public_output

ROOT = Path(__file__).resolve().parents[1]


def _line(origins, angle=0):
    """构造具有独立原点的标准字形行，所有预期由冻结算法计算。"""
    glyphs = [GlyphItem(str(i), (0.0, 0.0, 1.0, 1.0), ((0.0, 0.0),) * 4, origin) for i, origin in enumerate(origins)]
    return TextLine("正文内容", (0.0, 0.0, 10.0, 5.0), glyphs, angle, 3.0, 0, 1, "Body", None)


def _outcome(function, *args):
    """同时比较精确返回值及异常类型/消息，避免优化改变畸形输入的错误优先级。"""
    try:
        return "value", _exact(function(*args))
    except Exception as error:
        return "error", type(error).__name__, str(error)


@pytest.mark.parametrize("angle", range(360))
def test_projection_matches_all_canonical_integer_angles(angle: int) -> None:
    """逐个整数角度比较bbox及奇偶中位数，保留正负零和原始浮点顺序。"""
    rng = random.Random(angle)
    values = [-0.0, 0.0, -1e150, 1e150, -1e-150, 1e-150] + [rng.uniform(-100, 100) for _ in range(18)]
    for index in range(0, 20, 4):
        box = tuple(values[index : index + 4])
        assert _exact(assembly.upright_box(box, angle)) == _exact(oracle.upright_box(box, angle))
    for count in (7, 8, 9, 64):
        origins = [(rng.choice(values), rng.choice(values)) for _ in range(count)]
        line = _line(origins, angle)
        assert _exact(assembly.line_baseline(line)) == _exact(oracle.line_baseline(line))


@pytest.mark.parametrize("angle", [0, 30, 90, 180, 270, 359, -90, 360, 0.0, True, 10**400])
@pytest.mark.parametrize(
    "box",
    [
        (-0.0, -0.0, 1.0, 1.0),
        (0.0, 0.0, 1.0, 1.0),
        (-math.inf, math.nan, math.inf, 1.0),
        (0, 0, 2**70, 1),
        (Decimal("1"), 0.0, 2.0, 3.0),
        (Fraction(1, 3), 0.0, 2.0, 3.0),
        [0.0, 0.0, 1.0, 1.0],
        (),
        (0.0,),
    ],
)
def test_projection_keeps_nonstandard_inputs_and_errors(angle, box) -> None:
    """非标准角度、容器、数值和不完整框保留逐点算法及原始异常顺序。"""
    assert _outcome(assembly.upright_box, box, angle) == _outcome(oracle.upright_box, box, angle)


@pytest.mark.parametrize(
    "origins",
    [
        [(-0.0, -0.0)] * 8,
        [(math.inf, 1.0)] * 8,
        [(1.0, math.nan)] * 8,
        [(2**70, 1)] * 8,
        [(2**1000, 1)] * 8,
        [(2**10000, 1)] * 8,
        [(Decimal("1"), 1.0)] * 8,
        [(0.0, 1.0, 2.0)] * 8,
    ],
)
def test_baseline_preserves_unusual_origin_values(origins) -> None:
    """原点异常、超大整数及不完整/多余字段仍得到原结果或相同异常。"""
    line = _line(origins)
    assert _outcome(assembly.line_baseline, line) == _outcome(oracle.line_baseline, line)


def _blocks(count=80):
    """构造会多次观察邻域的正文块，内容列表在合段后仍需独立拥有。"""
    return [
        dict(
            type=BlockType.TEXT,
            content=[dict(type="text", content=f"这是第{i}行正文内容")],
            text_line=f"这是第{i}行正文内容",
            bbox_mm=(10.0, 10.0 + i * 4, 90.0, 13.0 + i * 4),
            angle=0,
            font_size_mm=3.0,
            baseline_mm=13.0 + i * 4,
            text_scope=("Body", None),
            line_bboxes_mm=[(10.0, 10.0 + i * 4, 90.0, 13.0 + i * 4)],
        )
        for i in range(count)
    ]


@pytest.mark.parametrize("seed", range(64))
def test_paragraph_cache_keeps_complete_order_and_ownership(seed: int) -> None:
    """随机栏宽、字号、间隔、列表、辅助块及旋转范围，完整输出和输入列表所有权不变。"""
    rng = random.Random(seed)
    blocks = _blocks(50)
    for index, block in enumerate(blocks):
        if rng.randrange(8) == 0:
            block["type"] = rng.choice([BlockType.IMAGE, BlockType.TABLE, BlockType.HEADER])
        block["angle"] = rng.choice([0, 0, 0, 90, 270])
        block["font_size_mm"] = rng.choice([2.0, 3.0, 4.0])
        block["text_scope"] = ("Body", rng.choice([None, None, 5]))
        x = rng.choice([10.0, 13.0, 110.0])
        y = 10.0 + index * rng.choice([4.0, 5.0, 10.0])
        block["bbox_mm"] = (x, y, x + rng.choice([30.0, 80.0]), y + 3.0)
        block["line_bboxes_mm"] = [block["bbox_mm"]]
        block["baseline_mm"] = y + 3.0
        if rng.randrange(8) == 0:
            block["text_line"] = "1. 列表正文"
    snapshot = deepcopy(blocks)
    actual = assembly.merge_paragraph_blocks(blocks)
    expected = oracle.merge_paragraph_blocks(blocks)
    assert _exact(actual) == _exact(expected)
    assert blocks == snapshot
    for block in actual:
        if block["type"] == BlockType.TEXT:
            assert all(
                block["content"] is not source["content"] and block["line_bboxes_mm"] is not source["line_bboxes_mm"]
                for source in blocks
            )


def _cos_calls(function, *args):
    """通过cProfile计数而非替换math函数，保证默认复用路径实际命中。"""
    profile = cProfile.Profile()
    profile.enable()
    result = function(*args)
    profile.disable()
    count = sum(record[1] for (file, line, name), record in pstats.Stats(profile).stats.items() if "math.cos" in name)
    return result, count


def test_reuse_reduces_trigonometry_without_cross_call_cache() -> None:
    """bbox四次降为一次、长行逐字降为一次，合段重复调用仍使用全新的本次缓存。"""
    box = (-0.0, 1.0, 30.0, 20.0)
    actual, current = _cos_calls(assembly.upright_box, box, 30)
    expected, previous = _cos_calls(oracle.upright_box, box, 30)
    assert _exact(actual) == _exact(expected) and (current, previous) == (1, 4)
    line = _line([(float(i), 3.0) for i in range(64)], 30)
    actual, current = _cos_calls(assembly.line_baseline, line)
    expected, previous = _cos_calls(oracle.line_baseline, line)
    assert _exact(actual) == _exact(expected) and (current, previous) == (1, 64)
    blocks = _blocks(120)
    actual, current = _cos_calls(assembly.merge_paragraph_blocks, blocks)
    expected, previous = _cos_calls(oracle.merge_paragraph_blocks, blocks)
    again, again_count = _cos_calls(assembly.merge_paragraph_blocks, blocks)
    assert _exact(actual) == _exact(expected) == _exact(again)
    assert 0 < current < previous / 4 and current == again_count


def test_cache_has_fixed_capacity_and_keeps_zero_sign_identity() -> None:
    """缓存保留原tuple防止ID重用，最多64条，同值但零符号不同的框独立计算。"""
    cache = {}
    boxes = [(-0.0, -0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)]
    for box in boxes:
        assert _exact(assembly._paragraph_box(box, 0, cache)) == _exact(oracle.upright_box(box, 0))
    assert len(cache) == 2
    for i in range(1000):
        box = (float(i), 1.0, float(i + 1), 2.0)
        assert _exact(assembly._paragraph_box(box, 30, cache)) == _exact(oracle.upright_box(box, 30))
        assert len(cache) <= 64
        assert cache[(id(box), 30)][0] is box


@pytest.mark.parametrize(
    "hook", ["point", "box", "cos", "sin", "radians", "round", "median", "line-getattribute", "glyph-getattribute"]
)
def test_dynamic_hooks_preserve_original_calls(monkeypatch: pytest.MonkeyPatch, hook: str) -> None:
    """动态数学/投影/中位数和属性访问钩子不被提前复用或缓存绕过。"""
    module = assembly
    if hook in {"cos", "sin", "radians"}:
        module = math
        name = hook
    elif hook in {"point", "box"}:
        name = {"point": "upright_point", "box": "upright_box"}[hook]
    elif hook == "round":
        name = "round"
    elif hook == "median":
        module = assembly.statistics
        name = "median"
    else:
        module = TextLine if hook == "line-getattribute" else GlyphItem
        name = "__getattribute__"
    original = getattr(module, name, round if hook == "round" else None)
    calls = []

    def observe(*args, **kwargs):
        """只记录原调用并返回旧结果，确保检查的是执行合同而非改写输出。"""
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, observe, raising=False)
    if hook in {"point", "box", "round"}:
        monkeypatch.setattr(oracle, name, observe, raising=False)
    line = _line([(float(i), 3.0) for i in range(12)])
    # 构造阶段属性访问不计入待比较调用。
    calls.clear()
    actual = assembly.line_baseline(line)
    first_count = len(calls)
    calls.clear()
    expected = oracle.line_baseline(line)
    second_count = len(calls)
    assert _exact(actual) == _exact(expected) and first_count == second_count
    if hook not in {"line-getattribute", "glyph-getattribute", "median"}:
        blocks = _blocks(20)
        calls.clear()
        actual = assembly.merge_paragraph_blocks(blocks)
        first_count = len(calls)
        calls.clear()
        expected = oracle.merge_paragraph_blocks(blocks)
        second_count = len(calls)
        assert _exact(actual) == _exact(expected) and first_count == second_count


@pytest.mark.parametrize("kind", ["real", "fixture"])
def test_public_ofd_keeps_complete_output_and_reading_order(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """真实文档及倾斜多行通过公开入口，与冻结投影/组行/组段算法完整对照。"""
    payload = (
        (ROOT / "demo/ofd/ofd_01.ofd").read_bytes()
        if kind == "real"
        else build_ofd_package(
            [
                (
                    "Pages/Page_0/Content.xml",
                    page_xml(
                        "".join(
                            text_object(
                                i + 1,
                                "AB中文0123" * 8,
                                boundary=f"10 {10 + i * 3} 100 4",
                                size=0.7,
                                x=1,
                                y=2,
                                delta_x="g 63 0.7",
                                ctm="1 0.05 -0.03 1 0 0",
                            )
                            for i in range(12)
                        )
                    ),
                )
            ]
        )
    )
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    monkeypatch.setattr(reading_order, "line_baseline", oracle.line_baseline)
    monkeypatch.setattr(reading_order, "merge_same_baseline_lines", oracle.merge_same_baseline_lines)
    monkeypatch.setattr(reading_order, "merge_paragraph_blocks", oracle.merge_paragraph_blocks)
    monkeypatch.setattr(table, "merge_same_baseline_lines", oracle.merge_same_baseline_lines)
    expected = parse(payload, file_suffix="ofd", keep_model_json=True)
    assert _exact(_public_output(actual)) == _exact(_public_output(expected))
