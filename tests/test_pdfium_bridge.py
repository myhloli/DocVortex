"""核对同库 PDFium 批量读取与 ctypes 参考的原始字符和生命周期。"""

from _native_pdf_golden import native_pdf_golden

from io import BytesIO
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
import ctypes
from concurrent.futures import ThreadPoolExecutor
from collections import UserDict

import pytest
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf.pdfium import pdfium_guard
from docvortex.document.pdf.text import extract
from docvortex.document.pdf.text import _pdfium_bridge as bridge


@pytest.fixture
def native():
    """扩展缺失或协议不匹配时明确失败，不跳过桥接验证。"""
    value = get_native()
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
                before = bridge.bridge_info()
                count = textpage.count_chars()
                expected = native_pdf_golden("_get_chars_python:68")
                actual = extract.get_chars(textpage, list(page.get_bbox()), page.get_rotation(), include_geometry=extended)
                after = bridge.bridge_info()
                assert after["pdfium_bridge_calls"] == before["pdfium_bridge_calls"] + int(count > 0)
                assert after["pdfium_empty_pages"] == before["pdfium_empty_pages"] + int(count == 0)
                assert character_state(actual) == character_state(expected)


@pytest.mark.parametrize("name", ("caibao1", "demo1", "demo2"))
def test_real_font_and_geometry_bridge_parity(native, name):
    """真实中文字体与数学字符在解码、代理对恢复和去重前已经完全相同。"""
    path = Path(__file__).parents[1] / "demo/pdfs" / f"{name}.pdf"
    with pdfium_guard(), pdfium.PdfDocument(path) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            expected = native_pdf_golden("_get_chars_python:85")
            actual = extract.get_chars(textpage, box, page.get_rotation(), include_geometry=True)
            assert character_state(actual) == character_state(expected)


def test_bridge_unavailable_and_error_are_distinct(native, monkeypatch):
    """ABI 不匹配明确失败，原生计算错误直接传播而不重复读取页面。"""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            with monkeypatch.context() as context:
                context.setattr(bridge.raw, "FPDFText_GetFontInfo", Mock())
                with pytest.raises(RuntimeError):
                    bridge.read_native_chars(textpage, True)
                assert "FPDFText_GetFontInfo" in bridge.bridge_info()["pdfium_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                reader_name = "read_pdfium_char_batches" if hasattr(native, "read_pdfium_char_batches") else "read_pdfium_chars"
                context.setattr(native, reader_name, Mock(side_effect=ValueError("calculation failure")))
                with pytest.raises(ValueError, match="calculation failure"):
                    bridge.read_native_chars(textpage, True)
        with pytest.raises(RuntimeError):
            bridge.read_native_chars(textpage, True)


def test_bridge_rejects_null_arguments_before_ffi(native):
    """不允许空句柄或空函数地址进入 unsafe 调用。"""
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([0] * 11, 1, 1, True)
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([1] * 11, 0, 1, True)
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_char_batches([0] * 11, 1, 1, True)


def test_batched_records_preserve_boundary_indices_and_legacy_reader(native, monkeypatch):
    """跨物化批次的代理对和来源序号保持不变，协议 4 的旧列表读取仍可兼容。"""
    stream = BytesIO()
    canvas = Canvas(stream)
    for row in range(24):
        canvas.drawString(30, 780 - row * 14, "boundary 1234567890 " * 3)
    canvas.save()
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    unicode_reader = bridge.raw.FPDFText_GetUnicode

    def unicode_value(handle, index):
        """将成对代理项放在数值批次边界，检查全页索引不会在新批次重置。"""
        return {1023: 0xD83D, 1024: 0xDE00}.get(index, unicode_reader(handle, index))

    monkeypatch.setattr(
        bridge.raw, "FPDFText_GetUnicode", factory(unicode_reader.restype, *unicode_reader.argtypes)(unicode_value)
    )
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            assert textpage.count_chars() > 1024
            box = list(page.get_bbox())
            expected = character_state(native_pdf_golden("_get_chars_python:137"))
            assert character_state(extract.get_chars(textpage, box, 0, include_geometry=True)) == expected
            assert bridge.bridge_info()["pdfium_record_batch_size"] == 1024


def test_bridge_long_font_callback_and_surrogates(native, monkeypatch):
    """真实句柄复用同一 ctypes 回调，覆盖长字体重读、非法 UTF8 和代理对码值。"""
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    font_reader = bridge.raw.FPDFText_GetFontInfo
    unicode_reader = bridge.raw.FPDFText_GetUnicode
    font_name = b"Synthetic-" + b"A" * 300 + b"\xff\0"

    def font_info(handle, index, buffer, capacity, flags):
        """按 PDFium 长度协议填写缓冲区，确保第二次读取路径真正执行。"""
        flags[0] = 32
        if capacity >= len(font_name):
            ctypes.memmove(buffer, font_name, len(font_name))
        return len(font_name)

    def unicode_value(handle, index):
        """分别注入高低代理项和缺失字形标记，其他码值使用实际 PDFium。"""
        return {0: 0xD83D, 1: 0xDE00, 2: 0}.get(index, unicode_reader(handle, index))

    monkeypatch.setattr(bridge.raw, "FPDFText_GetFontInfo", factory(font_reader.restype, *font_reader.argtypes)(font_info))
    monkeypatch.setattr(
        bridge.raw, "FPDFText_GetUnicode", factory(unicode_reader.restype, *unicode_reader.argtypes)(unicode_value)
    )
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            expected = native_pdf_golden("_get_chars_python:174")
            before = bridge.bridge_info()["pdfium_bridge_calls"]
            actual = extract.get_chars(textpage, box, 0, include_geometry=True)
            assert bridge.bridge_info()["pdfium_bridge_calls"] == before + 1
            assert character_state(actual) == character_state(expected)
            assert any("\ufffd" in char["font"]["name"] for char in actual)


def test_bridge_charbox_failure_propagates(native, monkeypatch):
    """已进入桥接的 PDFium 读取失败保持同一异常，不悄悄重试或返回半页字符。"""
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    reader = bridge.raw.FPDFText_GetLooseCharBox

    def failed_box(handle, index, rectangle):
        """模拟合法 ABI 下的 PDFium 失败返回值。"""
        return 0

    monkeypatch.setattr(bridge.raw, "FPDFText_GetLooseCharBox", factory(reader.restype, *reader.argtypes)(failed_box))
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            with pytest.raises(pdfium.PdfiumError, match="Failed to get charbox"):
                extract.get_chars(textpage, list(page.get_bbox()), 0, include_geometry=True)


def test_bridge_concurrent_requests_keep_existing_guard(native):
    """多个请求使用原有可重入锁串行读取，每次请求都独立拥有和关闭句柄。"""
    payload = sample_pdf(0)

    def capture(_index):
        """在锁内完成打开、两条路径读取和关闭，并返回独立的可比较数据。"""
        with pdfium_guard(), pdfium.PdfDocument(payload) as document:
            with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
                box = list(page.get_bbox())
                expected = native_pdf_golden("_get_chars_python:208")
                actual = extract.get_chars(textpage, box, 0, include_geometry=True)
                assert character_state(actual) == character_state(expected)
                return character_state(actual)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(capture, range(6)))
    assert all(result == results[0] for result in results)


def test_special_visibility_mapping_preserves_reference_access(native, monkeypatch):
    """映射和整数页框经对象适配后进入原生字符读取，保持冻结的字段输出。"""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            visibility = UserDict()
            expected = native_pdf_golden("_get_chars_python:226")
            actual = extract.get_chars(textpage, box, 0, include_geometry=True, visibility_by_object=visibility)
            assert character_state(actual) == character_state(expected)
            integer_box = [int(value) for value in box]
            expected = native_pdf_golden("_get_chars_python:230")
            assert character_state(extract.get_chars(textpage, integer_box, 0, include_geometry=True)) == character_state(
                expected
            )


def test_empty_page_skips_character_ffi(native, monkeypatch):
    """空页不打包字符函数地址，也不把空快照谎报为实际 Rust 读取。"""

    def forbidden(*args):
        """空页若进入字符读取或坐标内核则明确失败。"""
        raise AssertionError("empty page entered character kernel")

    monkeypatch.setattr(native, "read_pdfium_char_batches", forbidden)
    monkeypatch.setattr(native, "read_pdfium_chars", forbidden)
    monkeypatch.setattr(native, "materialize_geometry", forbidden)
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[1]) as page, closing(page.get_textpage()) as textpage:
            before = bridge.bridge_info()
            assert extract.get_chars(textpage, list(page.get_bbox()), 0, include_geometry=True) == []
            after = bridge.bridge_info()
            assert after["pdfium_bridge_calls"] == before["pdfium_bridge_calls"]
            assert after["pdfium_empty_pages"] == before["pdfium_empty_pages"] + 1
