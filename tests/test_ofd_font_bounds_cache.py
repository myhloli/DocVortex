"""原始字形边界复用的逐位几何、失败重试、缓存上限与公开入口回归。"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
import pytest

import _ofd_font_oracle as previous
from _ofd_test_utils import build_ofd_package, page_xml
from docvortex import parse
from docvortex.analyzers.native.ofd import scene, text
from docvortex.analyzers.native.ofd.models import FontResource
from docvortex.analyzers.native.ofd.package import OfdPackage
from test_native_ofd_text_geometry import _exact, _public_output

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = FontResource(1, "Fixture", "Fixture", "font.ttf")


@pytest.fixture(scope="module", params=["glyf", "cff"])
def font_bytes(request: pytest.FixtureRequest) -> bytes:
    """构造含复合字形、空格和共享cmap名称的小字体，不依赖系统字体。"""
    is_ttf = request.param == "glyf"
    builder = FontBuilder(1000, isTTF=is_ttf)
    builder.setupGlyphOrder([".notdef", "A", "B", "space"])
    glyphs = {}
    for name in (".notdef", "A", "space"):
        pen = TTGlyphPen(None) if is_ttf else T2CharStringPen(600, None)
        if name != "space":
            pen.moveTo((-20, -30))
            pen.lineTo((500, -30))
            if is_ttf:
                pen.qCurveTo((650, 400), (400, 720))
            else:
                pen.curveTo((650, 100), (650, 400), (400, 720))
            pen.lineTo((-20, 720))
            pen.closePath()
        glyphs[name] = pen.glyph() if is_ttf else pen.getCharString()
    if is_ttf:
        pen = TTGlyphPen(glyphs)
        pen.addComponent("A", (1, 0, 0, 1, 100, 25))
        glyphs["B"] = pen.glyph()
        builder.setupGlyf(glyphs)
    else:
        glyphs["B"] = glyphs["A"]
        builder.setupCFF("BoundsFixture", {"FullName": "Bounds Fixture"}, glyphs, {})
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupCharacterMap({65: "A", 97: "A", 66: "B", 32: "space"})
    builder.setupNameTable({"familyName": "BoundsFixture", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


def _pair(font_bytes: bytes):
    """用独立字体解析器读取同一字节，避免共享FontTools对象干扰差分。"""
    actual_package = SimpleNamespace(read_part=Mock(return_value=font_bytes))
    expected_package = SimpleNamespace(read_part=Mock(return_value=font_bytes))
    return text.FontMetricResolver(actual_package), previous.FontMetricResolver(expected_package)


@pytest.mark.parametrize("size", [0.0, -2.0, 3.5, 1e-200, 1e150])
@pytest.mark.parametrize("hscale", [0.0, -1.0, 1.0, 1e-100])
@pytest.mark.parametrize("hint", [None, 0.0, -1.0, 2.0, float("inf"), float("nan")])
def test_cached_bounds_keep_scaling_advance_and_float_bits(font_bytes: bytes, size, hscale, hint) -> None:
    """先填充缓存再改变缩放及advance，复合、空白与错误ID仍逐位等同旧实现。"""
    actual, expected = _pair(font_bytes)
    try:
        for resolver in (actual, expected):
            resolver.glyph_bbox(RESOURCE, None, "A", size=1.0, hscale=1.0, advance_hint=None)
        for glyph_id, character in ((None, "a"), (2, "¤"), (3, " "), (999, "A"), (-1, ""), (None, "缺")):
            options = dict(size=size, hscale=hscale, advance_hint=hint)
            assert _exact(actual.glyph_bbox(RESOURCE, glyph_id, character, **options)) == _exact(
                expected.glyph_bbox(RESOURCE, glyph_id, character, **options)
            )
        assert set(actual._cache["font.ttf"].bounds_cache) == {"A", "B"}
        assert actual.package.read_part.call_count == expected.package.read_part.call_count == 1
    finally:
        actual.close()
        expected.close()


class _Glyph:
    """记录描边调用，并可模拟空字形和暂时失败。"""

    def __init__(self, failures: int = 0, empty: bool = False) -> None:
        """设置失败次数和路径形状。"""
        self.calls = 0
        self.failures = failures
        self.empty = empty

    def draw(self, pen) -> None:
        """按调用顺序失败或向标准BoundsPen写入路径。"""
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("temporary draw failure")
        if not self.empty:
            pen.moveTo((-0.0, -2.0))
            pen.lineTo((4.0, 5.0))
            pen.endPath()


def _fake_resolver(glyphs: dict, *, enabled: bool = True):
    """显式创建允许缓存的只读字体替身，默认构造路径另行检查。"""
    resolver = text.FontMetricResolver(None)
    loaded = text._LoadedFont(
        font=SimpleNamespace(close=Mock()),
        glyph_order=list(glyphs),
        glyph_set=glyphs,
        units_per_em=1000.0,
        advances={},
        char_to_name={65: next(iter(glyphs))},
        name_to_char={},
        bounds_cache={} if enabled else None,
    )
    resolver._cache["font.ttf"] = loaded
    return resolver, loaded


def _box(resolver, glyph_id=None, character="A", **options):
    """统一调用字体指标，同时允许逐次改动缩放和advance。"""
    return resolver.glyph_bbox(RESOURCE, glyph_id, character, **{**dict(size=4.0, hscale=1.0, advance_hint=None), **options})


def test_success_reuse_empty_and_failure_retry() -> None:
    """成功路径只描一次，空路径及失败均重试，恢复后再复用成功结果。"""
    successful, empty, retry = _Glyph(), _Glyph(empty=True), _Glyph(failures=2)
    resolver, loaded = _fake_resolver({"A": successful, "space": empty, "retry": retry})
    for _ in range(4):
        _box(resolver, 0)
        _box(resolver, 1)
        _box(resolver, 2)
    assert (successful.calls, empty.calls, retry.calls) == (1, 4, 3)
    assert set(loaded.bounds_cache) == {"A", "retry"}


def test_admission_bound_and_cross_resolver_isolation() -> None:
    """缓存满后保留已有1024项，额外字形重画，另一解析器不能复用条目。"""
    glyphs = {f"g{index}": _Glyph() for index in range(text._MAX_CACHED_GLYPH_BOUNDS + 1)}
    resolver, loaded = _fake_resolver(glyphs)
    for index in range(len(glyphs)):
        _box(resolver, index)
    assert len(loaded.bounds_cache) == text._MAX_CACHED_GLYPH_BOUNDS
    assert "g0" in loaded.bounds_cache and "g1024" not in loaded.bounds_cache
    _box(resolver, 0)
    _box(resolver, 1024)
    assert glyphs["g0"].calls == 1 and glyphs["g1024"].calls == 2
    other, _ = _fake_resolver(glyphs)
    _box(other, 0)
    assert glyphs["g0"].calls == 2


def test_close_releases_cache_and_disables_post_close_hits() -> None:
    """关闭先清空并禁用边界缓存，随后关闭字体且保留旧的重复关闭行为。"""
    glyph = _Glyph()
    resolver, loaded = _fake_resolver({"A": glyph})
    _box(resolver)
    cache = loaded.bounds_cache
    resolver.close()
    assert cache == {} and loaded.bounds_cache is None
    loaded.font.close.assert_called_once_with()
    _box(resolver)
    assert glyph.calls == 2
    resolver.close()
    assert loaded.font.close.call_count == 2


def test_custom_pen_and_manual_loaded_font_keep_draw_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换BoundsPen或未显式启用缓存的字体沿用原描边路径。"""
    glyph = _Glyph()
    resolver, loaded = _fake_resolver({"A": glyph})
    _box(resolver)
    replacement = Mock(side_effect=text.BoundsPen)
    monkeypatch.setattr(text, "BoundsPen", replacement)
    for _ in range(2):
        _box(resolver)
    assert replacement.call_count == 2 and glyph.calls == 3
    assert len(loaded.bounds_cache) == 1
    manual, loaded_manual = _fake_resolver({"A": glyph}, enabled=False)
    monkeypatch.undo()
    _box(manual)
    _box(manual)
    assert glyph.calls == 5 and loaded_manual.bounds_cache is None


def test_advance_table_changes_do_not_reuse_scaled_or_fallback_box() -> None:
    """退化缩放仍使用当前advance表和hint，原始边界缓存不冻结最终框。"""
    resolver, loaded = _fake_resolver({"A": _Glyph()})
    _box(resolver)
    loaded.advances["A"] = (5000, 0)
    assert _box(resolver, hscale=-1.0, advance_hint=30.0) == (0.0, -3.4, 30.0, 0.8)
    assert _box(resolver, hscale=0.0, advance_hint=50.0) == (0.0, -3.4, 50.0, 0.8)
    assert len(loaded.bounds_cache) == 1


def test_public_real_font_output_matches_frozen_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实OFD公开入口的完整输出和资源诊断与冻结字体实现相同。"""
    path = ROOT / "demo/ofd/ofd_01.ofd"
    payload = path.read_bytes()
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    monkeypatch.setattr(scene, "FontMetricResolver", previous.FontMetricResolver)
    expected = parse(payload, file_suffix="ofd", keep_model_json=True)
    assert _exact(_public_output(actual)) == _exact(_public_output(expected))


def test_default_font_loading_and_missing_resource_retry_contract(font_bytes: bytes) -> None:
    """资源加载仍按part复用，缺失字体仍缓存空结果，回退字体不创建描边条目。"""
    resolver, _ = _pair(font_bytes)
    assert resolver._load(None) is None
    assert resolver._load(FontResource(2, "NoFont", "NoFont", None)) is None
    resolver.package.read_part.return_value = None
    assert resolver._load(RESOURCE) is None
    assert resolver._load(RESOURCE) is None
    assert resolver.package.read_part.call_count == 1
    with OfdPackage(build_ofd_package([("Pages/Page_0/Content.xml", page_xml(""))])) as package:
        fallback = text.FontMetricResolver(package)
        assert _box(fallback) == (0.0, -3.4, 2.0, 0.8)
        assert fallback._cache == {"font.ttf": None}
