"""OFD字形批量几何的逐位浮点差分、旧调用合同和公开入口回归。"""

from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import random
from unittest.mock import Mock

from lxml import etree
import pytest

import _ofd_text_oracle as oracle
from _ofd_test_utils import build_ofd_package, page_xml, text_object
from docvortex import parse
from docvortex._compute_backend import get_native
from docvortex.analyzers.native.ofd import scene, text
from docvortex.analyzers.native.ofd.errors import OfdResourceLimitError
from docvortex.analyzers.native.ofd.geometry import Affine
from docvortex.analyzers.native.ofd.models import FontResource, ResourceRegistry
from docvortex.analyzers.native.ofd.package import OfdPackage

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _exact(value):
    """用十六进制比较每个浮点值，保留正负零，禁止近似容差掩盖坐标改变。"""
    if isinstance(value, float):
        return value.hex()
    if isinstance(value, (tuple, list)):
        return tuple(_exact(item) for item in value)
    if isinstance(value, dict):
        return {key: _exact(item) for key, item in value.items()}
    return value


def _expected_geometry(direction, rotation, origins, boxes, clip):
    """使用冻结Python矩阵和外接框助手计算独立预期，保持原点及quad所有字段。"""
    output = []
    for origin, bbox in zip(origins, boxes, strict=True):
        transform = oracle.Affine(*direction).compose(oracle.Affine.translation(*origin)).compose(oracle.Affine(*rotation))
        quad = oracle.transform_quad(oracle.rect_quad(bbox), transform)
        box = oracle.quad_bbox(quad)
        if box is not None:
            box = oracle.bbox_intersection(box, clip)
        output.append(None if box is None else (box, quad, transform.apply((0.0, 0.0))))
    return output


@pytest.mark.parametrize("seed", range(128))
def test_native_geometry_matches_frozen_float_operations(seed: int) -> None:
    """固定种子覆盖仿射、旋转、抵消、巨大/极小值及裁剪，逐位比较每个返回坐标。"""
    rng = random.Random(seed)
    scale = rng.choice([1.0, 1e-150, 1e150])
    direction = tuple(rng.uniform(-2, 2) for _ in range(6))
    angle = rng.choice([0.0, 30.0, 89.9, 90.0, 180.0, 270.0, 359.9])
    rotation = oracle.Affine.rotation(angle)
    rotation = tuple(getattr(rotation, name) for name in "abcdef")
    origins = [(rng.uniform(-10, 10) * scale, rng.uniform(-10, 10) * scale) for _ in range(48)]
    boxes = [
        (rng.uniform(-2, 0) * scale, rng.uniform(-2, 0) * scale, rng.uniform(0, 2) * scale, rng.uniform(0, 2) * scale)
        for _ in origins
    ]
    clip = (-5 * scale, -5 * scale, 5 * scale, 5 * scale)
    actual = get_native().ofd_glyph_geometries(direction, rotation, origins, boxes, clip)
    assert _exact(actual) == _exact(_expected_geometry(direction, rotation, origins, boxes, clip))


@pytest.mark.parametrize(
    "direction,rotation,origin,bbox,clip",
    [
        (IDENTITY, IDENTITY, (-0.0, 0.0), (-0.0, -0.0, 1.0, 1.0), (0.0, 0.0, 2.0, 2.0)),
        ((-1.0, -0.0, 0.0, 1.0, -0.0, 0.0), IDENTITY, (0.0, -0.0), (-1.0, -1.0, 0.0, 0.0), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (0.0, 0.0), (0.0, 0.0, 1.0, 1.0), (1.0, 0.0, 2.0, 1.0)),
        (IDENTITY, IDENTITY, (0.0, 0.0), (0.0, 0.0, 1.0, 1.0), (math.nextafter(1.0, 0.0), 0.0, 2.0, 1.0)),
        ((0.0,) * 6, IDENTITY, (0.0, 0.0), (0.0, 0.0, 1.0, 1.0), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (math.inf, 0.0), (0.0, 0.0, 1.0, 1.0), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (math.nan, 0.0), (0.0, 0.0, 1.0, 1.0), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (0.0, 0.0), (0.0, -math.inf, 1.0, math.inf), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (0.0, 0.0), (0.0, 0.0, 1.0, 1.0), (math.nan, math.nan, math.nan, math.nan)),
        ((1e308, 1e308, -1e308, -1e308, 0.0, 0.0), IDENTITY, (1.0, 1.0), (0.0, 0.0, 1.0, 1.0), (-2.0, -2.0, 2.0, 2.0)),
        (IDENTITY, IDENTITY, (0.0, 0.0), (1.0, 1.0, -1.0, -1.0), (-2.0, -2.0, 2.0, 2.0)),
    ],
)
def test_native_geometry_directed_boundaries(direction, rotation, origin, bbox, clip) -> None:
    """定向覆盖正负零、相切与一ULP交集、奇异矩阵、非有限数据和反向局部框。"""
    actual = get_native().ofd_glyph_geometries(direction, rotation, [origin], [bbox], clip)
    assert _exact(actual) == _exact(_expected_geometry(direction, rotation, [origin], [bbox], clip))


def test_native_geometry_batch_bounds() -> None:
    """原生边界限制并列数组和最大批次，空批次保持空结果。"""
    native = get_native()
    assert native.ofd_glyph_geometries(IDENTITY, IDENTITY, [], [], (0.0,) * 4) == []
    for origins, boxes in [([(0.0, 0.0)], []), ([(0.0, 0.0)] * 257, [(0.0, 0.0, 1.0, 1.0)] * 257)]:
        with pytest.raises(ValueError, match="batch"):
            native.ofd_glyph_geometries(IDENTITY, IDENTITY, origins, boxes, (-2.0, -2.0, 2.0, 2.0))


def _build_both(source: bytes, *, parent_transform: Affine = Affine(), parent_clip=(-1000.0, -1000.0, 1000.0, 1000.0)):
    """对同一XML和字体环境执行当前与冻结实现，返回完整内部字形及全部预算字段。"""
    results = []
    package_bytes = build_ofd_package([("Pages/Page_0/Content.xml", page_xml(""))])
    for build in (text.build_text_lines, oracle.build_text_lines):
        with OfdPackage(package_bytes) as package:
            metrics = text.FontMetricResolver(package)
            budget = text.OfdTextBudget()
            lines = build(
                etree.fromstring(source),
                parent_transform=parent_transform,
                parent_clip=parent_clip,
                resources=ResourceRegistry(fonts={1: FontResource(1, "Fixture", "Fixture", None)}),
                package=package,
                font_metrics=metrics,
                budget=budget,
                paint_order=3,
                layer_type="Body",
                template_id=7,
            )
            results.append(([asdict(line) for line in lines], asdict(budget)))
            metrics.close()
    return results


@pytest.mark.parametrize("count", [1, 7, 8, 255, 256, 257, 513])
def test_textcode_batch_edges_and_clipped_glyphs(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """跨批次末字仍使用下一字的原距离，最多256字，裁剪后完整字段和预算逐位相同。"""
    native = get_native()
    original = native.ofd_glyph_geometries
    lengths = []

    def observe(direction, rotation, origins, boxes, clip):
        """记录真实批次长度并执行原生数值计算，避免只凭函数名判断命中。"""
        lengths.append(len(origins))
        return original(direction, rotation, origins, boxes, clip)

    monkeypatch.setattr(native, "ofd_glyph_geometries", observe)
    source = (
        f'<TextObject ID="1" Boundary="0 0 100 20" Font="1" Size="3"><TextCode X="1" Y="5" DeltaX="g {max(0, count - 1)} 0.7">'
        + "A" * count
        + "</TextCode></TextObject>"
    ).encode()
    actual, expected = _build_both(source)
    assert _exact(actual) == _exact(expected)
    assert all(length <= 256 for length in lengths)
    assert sum(lengths) == (count if count >= 8 else 0)


@pytest.mark.parametrize(
    "attributes",
    [
        "",
        'CTM="1 0.1 -0.05 1 2 0"',
        'ReadDirection="90" CharDirection="30"',
        'ReadDirection="180" CharDirection="90"',
        'HScale="0.01" Size="inf"',
    ],
)
def test_textcode_affine_direction_mapping_and_inheritance(attributes: str) -> None:
    """包含Unicode、首尾空白、跨TextCode坐标继承及CGTransform，比较完整内部几何。"""
    source = (
        f'<TextObject ID="1" Boundary="0 0 180 100" Font="1" {attributes}>'
        '<TextCode X="2" Y="6" DeltaX="g 15 1"> AB中文0123456789 </TextCode>'
        '<TextCode Y="12" DeltaX="g 15 1">继承坐标ABCDEFGH0123</TextCode>'
        '<CGTransform CodePosition="2" CodeCount="8"><Glyphs>1 2 3</Glyphs></CGTransform></TextObject>'
    ).encode()
    actual, expected = _build_both(source)
    assert _exact(actual) == _exact(expected)


def test_integer_clip_preserves_internal_bbox_value_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """手工传入整数裁剪边界时，原max/min选中的整数坐标不能被强制变成浮点数。"""
    source = b'<TextObject Boundary="0 0 100 20" Size="3"><TextCode X="1.5" Y="5" DeltaX="g 15 3">ABCDEFGHIJKLMNOP</TextCode></TextObject>'
    spy = Mock(side_effect=AssertionError("integer clip must retain Python values"))
    monkeypatch.setattr(get_native(), "ofd_glyph_geometries", spy)
    actual, expected = _build_both(source, parent_clip=(2, 3, 80, 20))
    assert _exact(actual) == _exact(expected)
    assert type(actual[0][0]["glyphs"][0]["bbox"][0]) is int
    spy.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        "compose",
        "apply",
        "translation",
        "rotation",
        "quad",
        "bounds",
        "clip",
        "glyph-constructor",
        "font-method",
        "font-subclass",
        "trig",
    ],
)
def test_custom_geometry_contract_keeps_scalar_calls(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    """自定义几何、字体解释或构造钩子仍逐字执行，不能被批量计算绕过。"""
    if override in {"compose", "apply"}:
        original = getattr(Affine, override)

        def changed(self, other):
            """保持返回结果，标记定制的实例几何方法。"""
            return original(self, other)

        monkeypatch.setattr(Affine, override, changed)
    elif override in {"translation", "rotation"}:
        original = getattr(Affine, override)
        monkeypatch.setattr(Affine, override, Mock(wraps=original))
    elif override in {"quad", "bounds", "clip"}:
        name = {"quad": "transform_quad", "bounds": "quad_bbox", "clip": "bbox_intersection"}[override]
        monkeypatch.setattr(text, name, Mock(wraps=getattr(text, name)))
    elif override == "glyph-constructor":
        original_init = text.GlyphItem.__init__

        def initialize(self, *args, **kwargs):
            """保持原始字形字段，标记自定义构造方法不能被提前批量绕过。"""
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(text.GlyphItem, "__init__", initialize)
    elif override == "font-method":
        original_font = text.FontMetricResolver.glyph_bbox

        def glyph_bbox(self, *args, **kwargs):
            """保持字体解释结果，验证用户替换的方法仍走原逐字路径。"""
            return original_font(self, *args, **kwargs)

        monkeypatch.setattr(text.FontMetricResolver, "glyph_bbox", glyph_bbox)
    elif override == "font-subclass":

        class CustomResolver(text.FontMetricResolver):
            """模拟用户字体解析器子类，保留其原有逐字行为。"""

        monkeypatch.setattr(text, "FontMetricResolver", CustomResolver)
    else:
        monkeypatch.setattr(math, "cos", Mock(wraps=math.cos))
    spy = Mock(side_effect=AssertionError("unexpected native glyph geometry"))
    monkeypatch.setattr(get_native(), "ofd_glyph_geometries", spy)
    source = b'<TextObject Boundary="0 0 100 20" Size="3"><TextCode X="1" Y="5" DeltaX="g 15 1">ABCDEFGHIJKLMNOP</TextCode></TextObject>'
    _build_both(source)
    spy.assert_not_called()


@pytest.mark.parametrize("field", ["glyph_count", "delta_token_count", "glyph_mapping_count"])
def test_text_budget_failures_precede_native_geometry(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """各类文字预算仍在几何之前失败，保持原异常消息、计数和字体加载顺序。"""
    source = b'<TextObject Boundary="0 0 100 20" Size="3"><TextCode X="1" Y="5" DeltaX="g 15 1">ABCDEFGHIJKLMNOP</TextCode><CGTransform CodePosition="0" CodeCount="1"><Glyphs>1</Glyphs></CGTransform></TextObject>'
    spy = Mock(side_effect=AssertionError("geometry before budget check"))
    monkeypatch.setattr(get_native(), "ofd_glyph_geometries", spy)
    values = []
    for build in (text.build_text_lines, oracle.build_text_lines):
        with OfdPackage(build_ofd_package([("Pages/Page_0/Content.xml", page_xml(""))])) as package:
            budget = text.OfdTextBudget(**{field: 2_000_000})
            with pytest.raises(OfdResourceLimitError) as caught:
                build(
                    etree.fromstring(source),
                    parent_transform=Affine(),
                    parent_clip=(0.0, 0.0, 100.0, 100.0),
                    resources=ResourceRegistry(),
                    package=package,
                    font_metrics=text.FontMetricResolver(package),
                    budget=budget,
                    paint_order=0,
                    layer_type="Body",
                    template_id=None,
                )
            values.append((str(caught.value), asdict(budget)))
    assert values[0] == values[1]
    spy.assert_not_called()


def _public_output(result):
    """保留完整模型、MiddleJson、原始素材字节和诊断，不忽略浮点坐标字段。"""
    return (
        result.model_json.model_dump(mode="json"),
        result.middle_json.model_dump(mode="json"),
        dict(result.assets),
        [asdict(item) for item in result.diagnostics],
    )


@pytest.mark.parametrize("kind", ["fixture", "real"])
def test_public_ofd_text_uses_native_and_matches_frozen_parser(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """真实OFD与长TextCode均经公开入口命中原生批次，并与冻结原始解析完整对照。"""
    payload = (
        (ROOT / "demo/ofd/ofd_01.ofd").read_bytes()
        if kind == "real"
        else build_ofd_package(
            [
                (
                    "Pages/Page_0/Content.xml",
                    page_xml(
                        text_object(1, "ABC中文0123" * 30, boundary="0 0 100 20", size=0.6, x=1, y=2, delta_x="g 269 0.5")
                    ),
                )
            ]
        )
    )
    native = get_native()
    before = native.ofd_glyph_geometry_stats()
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    after = native.ofd_glyph_geometry_stats()
    if kind == "fixture":
        assert after[0] > before[0] and after[1] > before[1]
    else:
        # 真实样本的嵌入字体TextCode均短于32字，新分流保留逐字路径。
        assert after == before
    monkeypatch.setattr(scene, "build_text_lines", oracle.build_text_lines)
    expected = parse(payload, file_suffix="ofd", keep_model_json=True)
    assert _exact(_public_output(actual)) == _exact(_public_output(expected))
