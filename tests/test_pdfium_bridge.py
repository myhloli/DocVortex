"""核对同库 PDFium 批量读取与 ctypes 参考的原始字符和生命周期。"""

from io import BytesIO
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf.pdfium import pdfium_guard
from docvortex.document.pdf.text import extract
from docvortex.document.pdf.text import _pdfium_bridge as bridge


@pytest.fixture
def native():
    """强制原生环境缺扩展时明确失败，纯 Python 测试任务允许跳过桥接。"""
    value = get_native()
    if value is None:
        pytest.skip("native backend is not selected")
    return value


def character_state(chars):
    """比较所有原始字段及字体别名关系，不依赖矩形实例的对象地址。"""
    fonts = {}
    return [
        (
            {key: value.bbox if key == "bbox" else value for key, value in char.items()},
            fonts.setdefault(id(char["font"]), len(fonts)),
        )
        for char in chars
    ]


def sample_pdf(rotation):
    """构造可见、隐藏和重复绘制文本，并覆盖页面旋转及空白页。"""
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.setPageRotation(rotation)
    canvas.drawString(50, 100, "Repeated text 123")
    canvas.drawString(50.2, 100.2, "Repeated text 123")
    text = canvas.beginText(50, 100)
    text.setTextRenderMode(3)
    text.textOut("Repeated text 123")
    canvas.drawText(text)
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
@pytest.mark.parametrize("extended", (False, True))
def test_raw_character_bridge_parity(native, rotation, extended):
    """在同一 textpage 上逐字段核对，字体分组、隐藏文字和旋转结果均不得变化。"""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(rotation)) as document:
        for page_index in range(len(document)):
            with closing(document[page_index]) as page, closing(page.get_textpage()) as textpage:
                before = bridge.bridge_info()["pdfium_bridge_calls"]
                expected = extract._get_chars_python(
                    textpage, list(page.get_bbox()), page.get_rotation(), include_geometry=extended
                )
                actual = extract.get_chars(textpage, list(page.get_bbox()), page.get_rotation(), include_geometry=extended)
                assert bridge.bridge_info()["pdfium_bridge_calls"] == before + 1
                assert character_state(actual) == character_state(expected)


@pytest.mark.parametrize("name", ("caibao1", "demo1", "demo2"))
def test_real_font_and_geometry_bridge_parity(native, name):
    """真实中文字体与数学字符在解码、代理对恢复和去重前已经完全相同。"""
    path = Path(__file__).parents[1] / "demo/pdfs" / f"{name}.pdf"
    with pdfium_guard(), pdfium.PdfDocument(path) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            expected = extract._get_chars_python(textpage, box, page.get_rotation(), include_geometry=True)
            actual = extract.get_chars(textpage, box, page.get_rotation(), include_geometry=True)
            assert character_state(actual) == character_state(expected)


def test_bridge_unavailable_and_error_are_distinct(native, monkeypatch):
    """能力缺失明确回退，已进入原生计算后的错误直接传播而不重复读取页面。"""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            with monkeypatch.context() as context:
                context.setattr(bridge.raw, "FPDFText_GetFontInfo", Mock())
                assert bridge.read_native_chars(textpage, True) is None
                assert "FPDFText_GetFontInfo" in bridge.bridge_info()["pdfium_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_chars", Mock(side_effect=ValueError("calculation failure")))
                with pytest.raises(ValueError, match="calculation failure"):
                    bridge.read_native_chars(textpage, True)
        assert bridge.read_native_chars(textpage, True) is None


def test_bridge_rejects_null_arguments_before_ffi(native):
    """不允许空句柄或空函数地址进入 unsafe 调用。"""
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([0] * 10, 1, 1, True)
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([1] * 10, 0, 1, True)
