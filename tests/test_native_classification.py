"""原始分类统计与 Python 参考的字段级差分，禁止从去重字符反推分类结果。"""

from contextlib import closing
from io import BytesIO
from pathlib import Path
from importlib import import_module

import pytest
import reportlab
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import pypdfium2 as pdfium

from docvortex._compute_backend import get_native
from docvortex.document.pdf.pdfium import pdfium_guard
from docvortex.document.pdf import classification_bridge

classify = import_module("docvortex.document.pdf.classify")


@pytest.fixture
def native():
    """Rust 作业必须使用本次原始统计扩展，纯 Python 作业明确跳过。"""
    extension = get_native()
    if extension is None:
        pytest.skip("Python backend")
    assert hasattr(extension, "read_pdfium_classification")
    return extension


def pdf_bytes():
    """包含字体子集、连字、PUA、控制字符、隐藏副本及空页的真实 PDF。"""
    pdfmetrics.registerFont(TTFont("ClassificationVera", str(Path(reportlab.__file__).parent / "fonts/Vera.ttf")))
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(400, 300))
    canvas.setFont("ClassificationVera", 12)
    canvas.drawString(20, 250, "A ﬁ B \ue000\ue001 中文 \x02 C")
    canvas.drawString(20, 220, "Two lines 123")
    text = canvas.beginText(20, 250)
    text.setFont("ClassificationVera", 12)
    text.setTextRenderMode(3)
    text.textOut("Hidden OCR duplicate 123")
    canvas.drawText(text)
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("page_index", [0, 1])
@pytest.mark.parametrize("alternate_rules", [False, True])
def test_raw_classification_matches_reference(native, monkeypatch, page_index, alternate_rules):
    """所有统计字段及字体键顺序一致，运行时允许字符集配置也必须传入 Rust。"""
    if alternate_rules:
        monkeypatch.setattr(classify, "_ALLOWED_CONTROL_CODES", {0, 2, 10})
        monkeypatch.setattr(classify, "CJK_TEXT_RANGES", ((65, 90), (0x4E00, 0x9FFF)))
        monkeypatch.setattr(classify, "_PRIVATE_USE_AREA_START", 0)
        monkeypatch.setattr(classify, "_PRIVATE_USE_AREA_END", 100)
    before = native.classification_snapshot_stats()
    with pdfium_guard(), pdfium.PdfDocument(pdf_bytes()) as document, closing(document[page_index]) as page:
        actual = classify._collect_pdfium_text_sample_from_page(page_index, page)
        assert native.classification_snapshot_stats() == before + 1
        with monkeypatch.context() as patcher:
            patcher.setattr(classification_bridge, "get_native", lambda: None)
            expected = classify._collect_pdfium_text_sample_from_page(page_index, page)
    assert actual == expected
    assert list(actual) == list(expected)
    for key in ["font_name_counts", "font_non_generated_char_counts", "font_non_generated_cjk_char_counts"]:
        assert list(actual[key].items()) == list(expected[key].items())


def test_raw_classification_respects_custom_font_reader(native, monkeypatch):
    """替换字符字体读取规则时必须执行调用方函数，不静默使用原始 PDFium 字体。"""
    before = native.classification_snapshot_stats()
    monkeypatch.setattr(classify, "_get_pdfium_char_font_name", lambda *_: "CUSTOM")
    with pdfium_guard(), pdfium.PdfDocument(pdf_bytes()) as document, closing(document[0]) as page:
        result = classify._collect_pdfium_text_sample_from_page(0, page)
    assert result["font_name_counts"] == {"CUSTOM": result["char_count"]}
    assert native.classification_snapshot_stats() == before


def test_classification_snapshot_survives_text_page_close(native):
    """统计所有权独立于文本页；导出字典的修改不会污染后续读取。"""
    with (
        pdfium_guard(),
        pdfium.PdfDocument(pdf_bytes()) as document,
        closing(document[0]) as page,
        closing(page.get_textpage()) as textpage,
    ):
        snapshot = classification_bridge.read_classification_snapshot(
            textpage,
            textpage.count_chars(),
            classify.CJK_TEXT_RANGES,
            classify._ALLOWED_CONTROL_CODES,
            (classify._PRIVATE_USE_AREA_START, classify._PRIVATE_USE_AREA_END),
            classify._normalize_pdf_font_name,
        )
    expected = snapshot.to_dict()
    changed = snapshot.to_dict()
    changed["font_name_counts"].clear()
    changed["null_char_count"] = -1
    assert snapshot.to_dict() == expected


def test_raw_font_bytes_and_negative_pdfium_flags(native):
    """人工同 ABI 回调覆盖无效 UTF-8、规范化字体碰撞及 PDFium 负返回值的原真值语义。"""
    import ctypes as ct

    convention = getattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE)
    codes = [65, 0, 0x4E00, 0xE001, 0xFFFD, 0xFFFFFFFF, 0x4E01]
    names = [b"ABCDEF+F\xff\x00", b"F\x00", b"G\x00", b"G\x00", b"F\x00", b"\x00", b"F\x00"]
    generated = [1, 1, 0, 0, -1, 0, 0]
    errors = [-1, 0, 0, 1, 0, 0, 0]
    scalar = convention(ct.c_int, ct.c_void_p, ct.c_int)
    unicode_function = convention(ct.c_uint, ct.c_void_p, ct.c_int)(lambda _page, index: codes[index])
    generated_function = scalar(lambda _page, index: generated[index])
    error_function = scalar(lambda _page, index: errors[index])

    @convention(ct.c_ulong, ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_ulong, ct.POINTER(ct.c_int))
    def font_info(_page, index, buffer, capacity, flags):
        """遵循先查询长度再写入的 PDFium 约定，模拟原始字体字节而非 Python 字符串。"""
        flags[0] = 0
        value = names[index]
        if buffer and capacity >= len(value):
            ct.memmove(buffer, value, len(value))
        return len(value)

    functions = [unicode_function, generated_function, error_function, font_info]
    snapshot = native.read_pdfium_classification(
        [ct.cast(function, ct.c_void_p).value for function in functions],
        1,
        len(codes),
        [(0x4E00, 0x9FFF)],
        [9, 10, 13],
        (0xE000, 0xF8FF),
        classify._normalize_pdf_font_name,
    )
    assert snapshot.to_dict() == {
        "null_char_count": 1,
        "replacement_char_count": 1,
        "control_char_count": 0,
        "private_use_char_count": 1,
        "unicode_map_error_count": 2,
        "non_generated_char_count": 5,
        "font_name_counts": {"F": 4, "G": 2},
        "font_non_generated_char_counts": {"G": 2, "F": 2},
        "font_non_generated_cjk_char_counts": {"G": 1, "F": 1},
    }

    assert list(snapshot.to_dict()["font_non_generated_char_counts"]) == ["G", "F"]
    assert list(snapshot.to_dict()["font_non_generated_cjk_char_counts"]) == ["G", "F"]


@pytest.mark.parametrize("addresses,handle,count", [([0, 1, 1, 1], 1, 0), ([1, 1, 1, 1], 0, 0), ([1, 1, 1, 1], 1, 2**31)])
def test_classification_rejects_invalid_abi_before_dereference(native, addresses, handle, count):
    """空函数、空句柄和超范围索引必须在读取任何指针前被拒绝。"""
    with pytest.raises(ValueError, match="classification ABI or character count"):
        native.read_pdfium_classification(addresses, handle, count, [], [], (0xE000, 0xF8FF), classify._normalize_pdf_font_name)


def test_native_classification_failure_is_not_silently_ocr(native, monkeypatch):
    """原生计算失败保留异常原因，不进入分类器原有的通用 OCR 回退。"""

    def fail(*args):
        """模拟已选中原生入口后的计算错误。"""
        raise RuntimeError("native computation failed")

    monkeypatch.setattr(classification_bridge, "read_classification_snapshot", fail)
    payload = pdf_bytes()
    with pdfium_guard(), pdfium.PdfDocument(payload) as document:
        with pytest.raises(classification_bridge.NativeClassificationError) as raised:
            classify.classify(document, payload)
    assert isinstance(raised.value.__cause__, RuntimeError)
