"""嵌入字体短批次分流的完整几何、预算和原生调用回归。"""

from dataclasses import asdict
from pathlib import Path

from lxml import etree
import pytest

import _ofd_r3b_text_oracle as previous
import _ofd_text_oracle as scalar
from _ofd_test_utils import build_ofd_package, page_xml
from docvortex import parse
from docvortex._compute_backend import get_native
from docvortex.analyzers.native.ofd import scene, text
from docvortex.analyzers.native.ofd.geometry import Affine
from docvortex.analyzers.native.ofd.models import FontResource, ResourceRegistry
from docvortex.analyzers.native.ofd.package import OfdPackage
from test_native_ofd_text_geometry import _exact, _public_output

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize("count", [7, 8, 16, 24, 31, 32, 33, 255, 256, 257])
def test_dispatch_keeps_complete_glyphs_and_independent_budgets(embedded: bool, count: int) -> None:
    """只改变原生分流门槛，三个实现的字形、坐标、类型、样式及全部预算保持一致。"""
    content = (
        f'<TextObject ID="1" Boundary="0 0 1000 20" Font="1" Size="3"><TextCode X="1" Y="5" DeltaX="g {count - 1} 0.7">'
        + "中" * count
        + '</TextCode><CGTransform CodePosition="1" CodeCount="3"><Glyphs>2 3</Glyphs></CGTransform></TextObject>'
    ).encode()
    font = FontResource(1, "Fixture", "Fixture", "missing.ttf" if embedded else None)
    results = []
    native = get_native()
    before = native.ofd_glyph_geometry_stats()
    for build in (text.build_text_lines, previous.build_text_lines, scalar.build_text_lines):
        with OfdPackage(build_ofd_package([("Pages/Page_0/Content.xml", page_xml(""))])) as package:
            budget = text.OfdTextBudget()
            metrics = (
                previous.FontMetricResolver(package) if build is previous.build_text_lines else text.FontMetricResolver(package)
            )
            lines = build(
                etree.fromstring(content),
                parent_transform=Affine(),
                parent_clip=(0.0, 0.0, 1000.0, 20.0),
                resources=ResourceRegistry(fonts={1: font}),
                package=package,
                font_metrics=metrics,
                budget=budget,
                paint_order=2,
                layer_type="Body",
                template_id=3,
            )
            results.append(([asdict(line) for line in lines], asdict(budget)))
            if build is text.build_text_lines:
                after = native.ofd_glyph_geometry_stats()
            metrics.close()
    assert _exact(results[0]) == _exact(results[1]) == _exact(results[2])
    assert after[1] - before[1] == (count if count >= (32 if embedded else 8) else 0)


def test_real_embedded_font_uses_scalar_short_text_and_keeps_public_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实字体短文本不再产生小原生批次，公开完整结果与R3B及更早逐字路径相同。"""
    payload = (ROOT / "demo/ofd/ofd_01.ofd").read_bytes()
    native = get_native()
    before = native.ofd_glyph_geometry_stats()
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    assert native.ofd_glyph_geometry_stats() == before
    for build in (previous.build_text_lines, scalar.build_text_lines):
        monkeypatch.setattr(scene, "build_text_lines", build)
        monkeypatch.setattr(
            scene,
            "FontMetricResolver",
            previous.FontMetricResolver if build is previous.build_text_lines else text.FontMetricResolver,
        )
        old_count = native.ofd_glyph_geometry_stats()
        expected = parse(payload, file_suffix="ofd", keep_model_json=True)
        assert _exact(_public_output(actual)) == _exact(_public_output(expected))
        if build is previous.build_text_lines:
            assert native.ofd_glyph_geometry_stats()[1] > old_count[1]
