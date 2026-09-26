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
from docvortex.document.pdf.native_objects import _walk_clipped_objects
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
