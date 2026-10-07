"""核对 Rust 对象树遍历的矩阵、裁剪、顺序和异常边界。"""

from contextlib import closing
import ctypes
from io import BytesIO
from unittest.mock import Mock

import pytest
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf import _object_bridge as bridge
from docvortex.document.pdf.native_objects import _text_object_visibility, _walk_clipped_objects
from docvortex.document.pdf.native_objects import _read_raw_path_subpaths, _read_raw_path_subpaths_python
from docvortex.document.pdf.pdfium import pdfium_guard


@pytest.fixture
def native():
    """强制 Rust 任务不能静默跳过，纯 Python 任务保持参考测试模式。"""
    extension = get_native()
    if extension is None:
        pytest.skip("native backend is not selected")
    return extension


def nested_pdf():
    """构建带嵌套 Form、旋转、斜切、曲线裁剪与不可见区域的真实 PDF。"""
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.beginForm("inner")
    canvas.drawString(10, 20, "Nested text")
    canvas.rect(0, 0, 60, 60)
    canvas.endForm()
    canvas.beginForm("outer")
    canvas.translate(40, 30)
    canvas.rotate(15)
    path = canvas.beginPath()
    path.moveTo(0, 0)
    path.curveTo(80, 0, 80, 80, 0, 80)
    path.close()
    canvas.clipPath(path, stroke=0)
    canvas.doForm("inner")
    canvas.endForm()
    canvas.translate(100, 100)
    canvas.skew(5, -10)
    canvas.doForm("outer")
    canvas.doForm("inner")
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_visibility_collects_only_root_paths(native, monkeypatch, rotation):
    """同次遍历的额外记录与参考树顶层路径完全相同，Form 中的路径不能作为遮挡证据。"""
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(10, 10, "visible")
    canvas.rect(0, 0, 60, 60, fill=1)
    canvas.beginForm("inner")
    canvas.rect(0, 0, 30, 30, fill=1)
    canvas.drawString(10, 10, "nested")
    canvas.endForm()
    canvas.doForm("inner")
    canvas.save()
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            page.set_rotation(rotation)
            values, roots, paint = bridge.read_text_visibility(page, page.get_bbox(), rotation, 15, with_roots=True)
            assert values == bridge.read_text_visibility(page, page.get_bbox(), rotation, 15)
            expected = [
                (order, (ctypes.cast(p.raw, ctypes.c_void_p).value, p.matrix, p.parent_matrix, p.depth, p.clip))
                for order, p in enumerate(_walk_clipped_objects(page))
                if p.depth == 0 and raw.FPDFPageObj_GetType(p.raw) == raw.FPDF_PAGEOBJ_PATH
            ]
            assert roots == expected
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(stream.getvalue()))
            actual = _text_object_visibility(page, page.get_bbox(), rotation, lambda: reader.pages[0])
            with monkeypatch.context() as context:
                context.setattr(bridge, "read_text_visibility", lambda *args, **kwargs: None)
                reference = _text_object_visibility(page, page.get_bbox(), rotation, lambda: reader.pages[0])
            assert actual == reference
            assert any(not value[0] for value in actual.values())


@pytest.mark.parametrize("kind", ["early", "stroke", "transparent", "late"])
def test_cover_prefilter_preserves_visibility_and_skips_impossible_bounds(native, monkeypatch, kind):
    """早绘制、仅描边及透明路径不读文字外框；后绘制覆盖保留全量成员及原资源审计。"""
    from pypdf import PdfReader

    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(200, 200))
    if kind == "early":
        canvas.rect(0, 0, 100, 100, fill=1, stroke=0)
    canvas.drawString(10, 10, "earlier")
    if kind != "early":
        if kind == "transparent":
            canvas.setFillAlpha(0.5)
        canvas.rect(0, 0, 100, 100, fill=int(kind != "stroke"), stroke=int(kind == "stroke"))
        canvas.setFillAlpha(1)
    canvas.drawString(10, 10, "later")
    canvas.save()
    reader = PdfReader(BytesIO(stream.getvalue()))
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            _, roots, paint = bridge.read_text_visibility(page, page.get_bbox(), 0, 15, with_roots=True)
            if kind == "late":
                assert len(roots) == len(paint) == 1
                assert paint[0][0] < roots[0][0]
            else:
                assert roots == paint == []
            actual = _text_object_visibility(page, page.get_bbox(), 0, lambda: reader.pages[0])
            with monkeypatch.context() as scoped:
                scoped.setattr(bridge, "read_text_visibility", lambda *args, **kwargs: None)
                expected = _text_object_visibility(page, page.get_bbox(), 0, lambda: reader.pages[0])
            assert actual == expected


def test_replaced_cover_rule_keeps_complete_native_root_evidence(native, monkeypatch):
    """遮挡规则被替换时禁用必要条件裁剪，原可见性接口仍可返回全部路径和文字外框。"""
    from docvortex.document.pdf import text_occlusion

    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.rect(0, 0, 100, 100, fill=0)
    canvas.drawString(10, 10, "later")
    canvas.save()

    def rectangle(*args):
        """模拟调用方替换的规则，必须使原生预筛选失效。"""
        return None

    monkeypatch.setattr(text_occlusion, "_opaque_rectangle", rectangle)
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            _, roots, paint = bridge.read_text_visibility(page, page.get_bbox(), 0, 15, with_roots=True)
            assert len(roots) == len(paint) == 1


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_full_page_image_evidence_reuses_decode_without_changing_pixels(monkeypatch, mode):
    """真实嵌入图片逐项比较背景与白边裁决，同时确认两项检查只借用一次位图。"""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from docvortex.document.pdf import native_objects

    stream = BytesIO()
    with Image.new(mode, (300, 300), (255, 255, 255) if mode == "RGB" else (255, 255, 255, 0)) as image:
        ImageDraw.Draw(image).rectangle((0, 15, 299, 299), fill=(30, 120, 190) if mode == "RGB" else (30, 120, 190, 255))
        canvas = Canvas(stream, pagesize=(300, 300))
        canvas.drawImage(ImageReader(image), 0, 0, width=300, height=300, mask="auto")
        canvas.save()
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            member = next(
                item for item in _walk_clipped_objects(page) if raw.FPDFPageObj_GetType(item.raw) == raw.FPDF_PAGEOBJ_IMAGE
            )
            box = (0.0, 0.0, 300.0, 300.0)
            expected = (
                native_objects._is_smooth_page_background(member.raw, page, box, box),
                native_objects._native_image_blank_top_bbox(member.raw, page, member.matrix, box, box, 0),
            )
            getter = raw.FPDFImageObj_GetBitmap
            calls = []

            def counted(obj):
                """记录借用次数并使用原有 PDFium 函数和句柄生命周期。"""
                calls.append(obj)
                return getter(obj)

            monkeypatch.setattr(raw, "FPDFImageObj_GetBitmap", counted)
            assert native_objects._image_visual_evidence(member.raw, page, member.matrix, box, box, 0) == expected
            assert len(calls) == 1


@pytest.mark.parametrize("kind", [raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_IMAGE])
def test_object_bridge_matches_reference(native, kind):
    """同一页面逐字段比较借用对象地址、矩阵、裁剪和遍历顺序。"""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            expected = [
                (ctypes.cast(item.raw, ctypes.c_void_p).value, item.matrix, item.parent_matrix, item.depth, item.clip)
                for item in _walk_clipped_objects(page)
                if raw.FPDFPageObj_GetType(item.raw) == kind
            ]
            assert list(bridge.read_clipped_objects(page, kind, 15)) == expected


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_visibility_bridge_matches_reference(native, monkeypatch, rotation):
    """Rust 同批读取 TEXT 状态/裁剪，并与 Python 属性读取逐字段一致。"""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            page.set_rotation(rotation)
            before = bridge.bridge_info()["pdfium_text_visibility_bridge_calls"]
            actual = bridge.read_text_visibility(page, page.get_bbox(), rotation, 15)
            info = bridge.bridge_info()
            assert actual is not None
            assert info["pdfium_text_visibility_bridge_calls"] == before + 1
            assert info["pdfium_text_visibility_bridge_unavailable_reason"] is None
            with monkeypatch.context() as context:
                context.setattr(bridge, "read_text_visibility", lambda *args, **kwargs: None)
                expected = _text_object_visibility(page, page.get_bbox(), rotation)
            assert {address: (visible, clip) for address, visible, clip in actual} == expected
            assert any(visible for _, visible, _ in actual)
            assert any(clip is not None for _, _, clip in actual)


def test_text_visibility_bridge_fallback_and_compute_failure(native, monkeypatch):
    """ABI 不兼容回退 Python；已进入 Rust 的异常必须原样传播。"""
    assert hasattr(native, "read_pdfium_text_visibility")
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            bbox, rotation = page.get_bbox(), 0
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFTextObj_GetTextRenderMode", Mock())
                assert bridge.read_text_visibility(page, bbox, rotation, 15) is None
                assert "ABI" in bridge.bridge_info()["pdfium_text_visibility_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_text_visibility", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_text_visibility(page, bbox, rotation, 15)


def test_text_visibility_python_backend_reports_reference_choice(monkeypatch):
    """纯 Python 后端只记录能力选择，不把扩展不存在伪装成执行成功。"""

    before = bridge.bridge_info()["pdfium_text_visibility_bridge_calls"]
    monkeypatch.setattr(bridge, "get_native", lambda: None)
    assert bridge.read_text_visibility(object(), (0, 0, 10, 10), 0, 15) is None
    info = bridge.bridge_info()
    assert info["pdfium_text_visibility_bridge_calls"] == before
    assert info["pdfium_text_visibility_bridge_unavailable_reason"] == "python backend or unsupported native extension"


def test_text_visibility_bridge_rejects_invalid_addresses(native):
    """无效地址或几何在 unsafe 前拒绝，防止借用悬挂页面对象。"""
    for addresses, handle, rotation, frame in [
        ([], 1, 0, (0.0, 0.0, 10.0, 10.0)),
        ([0] * 14, 1, 0, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 0, 0, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 1, 45, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 1, 0, (0.0, 0.0, 0.0, 10.0)),
    ]:
        with pytest.raises(ValueError, match="invalid PDFium text visibility"):
            native.read_pdfium_text_visibility(addresses, handle, frame, rotation, 15)


def test_object_bridge_abi_fallback_and_compute_failure(native, monkeypatch):
    """仅能力不兼容允许参考回退，已进入 Rust 的异常必须传播。"""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFPageObj_GetMatrix", Mock())
                assert bridge.read_clipped_objects(page, raw.FPDF_PAGEOBJ_TEXT, 15) is None
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_objects", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_clipped_objects(page, raw.FPDF_PAGEOBJ_TEXT, 15)


def test_object_bridge_rejects_invalid_addresses(native):
    """无效地址在进入 unsafe 调用前拒绝，不触发原生崩溃。"""
    for addresses, handle, depth in [([], 1, 15), ([0] * 11, 1, 15), ([1] * 11, 0, 15), ([1] * 11, 1, 65)]:
        with pytest.raises(ValueError, match="invalid PDFium"):
            native.read_pdfium_objects(addresses, handle, 1, depth)


def test_drawing_bridge_fallback_and_compute_failure(native, monkeypatch):
    """复杂 Form 与 ABI 不兼容回退；进入 Rust 后的计算错误原样传播。"""
    assert hasattr(native, "read_pdfium_drawing_lines")
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            bbox = page.get_bbox()
            assert bridge.read_drawing_lines(page, bbox, 0) is None
            assert "complex" in bridge.bridge_info()["pdfium_drawing_line_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFPageObj_GetStrokeWidth", Mock())
                assert bridge.read_drawing_lines(page, bbox, 0) is None
                assert "ABI" in bridge.bridge_info()["pdfium_drawing_line_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_drawing_lines", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_drawing_lines(page, bbox, 0)


def test_native_subpaths_match_reference_and_share_endpoints(native):
    """原生解码保留子路径、曲线控制点、闭合边和同一点对象的引用。"""
    stream = BytesIO()
    canvas = Canvas(stream)
    path = canvas.beginPath()
    path.moveTo(10, 10)
    path.lineTo(50, 50)
    path.curveTo(70, 20, 80, 30, 50, 70)
    path.close()
    path.moveTo(100, 100)
    path.lineTo(120, 110)
    canvas.drawPath(path)
    canvas.save()
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            paths = [
                item.raw for item in _walk_clipped_objects(page) if raw.FPDFPageObj_GetType(item.raw) == raw.FPDF_PAGEOBJ_PATH
            ]
            assert paths
            for path in paths:
                expected = _read_raw_path_subpaths_python(path)
                actual = _read_raw_path_subpaths(path)
                assert actual == expected
                for subpath in actual:
                    for first, last in subpath.straight_segments:
                        assert any(first is point for point in subpath.points)
                        assert any(last is point for point in subpath.points)
