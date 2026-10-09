"""图上字符与差分像素批量路径的独立 Python 参考及原生所有权验收。"""

from contextlib import closing
import ctypes as ct
import random
from types import SimpleNamespace

import pytest
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from PIL import Image

from docvortex._compute_backend import get_native
from docvortex.document.pdf import PDFDocument, classification_bridge as bridge, classification_visuals as visuals
from docvortex.document.pdf.native_objects import _text_object_visibility
from docvortex.document.pdf.native_coordinates import _drawing_page_size, _transform_drawing_point
from test_pdf_classify_image_text import image_text_pdf


@pytest.fixture
def native():
    """原生作业必须加载协议 33，纯 Python 作业明确跳过相关扩展断言。"""
    value = get_native()
    if value is None:
        pytest.skip("Python backend")
    assert value.PROTOCOL_VERSION == 33
    return value


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("kind", ["background", "hidden", "cropped_scan"])
@pytest.mark.parametrize("nested", [False, True])
def test_batch_candidates_and_pixels_match_reference(native, rotation, kind, nested):
    """真实旋转、Form 和裁剪下候选顺序、可见性及像素计数都与独立参考相同。"""
    with pdfium.PdfDocument(image_text_pdf(kind, rotation=rotation, nested=nested)) as doc, closing(doc[0]) as page:
        frame, angle = page.get_bbox(), page.get_rotation()
        width, height = _drawing_page_size(frame, angle)
        images = [
            visuals._transform_object_bbox(box, lambda point: _transform_drawing_point(point, frame, angle))
            for box in visuals._image_rectangles(page)
        ]
        visibility = _text_object_visibility(page, frame, angle)
        before = native.image_text_snapshot_stats()
        with closing(page.get_textpage()) as textpage:
            snapshot = bridge.read_image_text_snapshot(textpage, frame, angle, images, visibility, 0.8)
            boxes, paints = visuals._image_text_candidates_python(textpage, frame, angle, images, visibility)
        assert snapshot is not None
        assert [tuple(box) for box in snapshot.boxes()] == boxes
        assert snapshot.count == len(boxes)
        assert snapshot.paints is paints
        assert native.image_text_snapshot_stats()[0] == before[0] + 1
    # 页面已经关闭，候选快照只消费独立数据；极小像素值也不能被阈值化丢弃。
    rng = random.Random(42)
    pixels = Image.new("L", (47, 61))
    pixels.putdata([rng.choice([0, 0, 0, 1, 255]) for _ in range(47 * 61)])
    try:
        assert snapshot.painted_count(pixels.tobytes(), 47, 61, width, height) == visuals._painted_count_python(
            pixels, boxes, width, height
        )
    finally:
        pixels.close()
    copied = snapshot.boxes()
    copied.clear()
    assert snapshot.count == len(boxes)


def test_raw_unicode_callbacks_match_python_filtering(native, monkeypatch):
    """人工 ABI 回调覆盖控制空白、代理码点、非法码点和缺失对象，不能由 UTF-8 转换替代。"""
    codes = [28, 31, 32, 65, 0xD800, 0x110000, 0, 0x85, 0x3000, 0x4E2D, 66]
    convention = getattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE)
    uni = convention(ct.c_uint, ct.c_void_p, ct.c_int)(lambda _page, i: codes[i])
    obj = convention(ct.c_void_p, ct.c_void_p, ct.c_int)(lambda _page, i: 0 if i == 10 else 1 + i % 2)
    double_pointer = ct.POINTER(ct.c_double)

    @convention(ct.c_int, ct.c_void_p, ct.c_int, double_pointer, double_pointer, double_pointer, double_pointer)
    def bounds(_page, index, left, right, bottom, top):
        """读取固定字形框，保持 PDFium 的 left/right/bottom/top 参数顺序。"""
        left[0], right[0], bottom[0], top[0] = index + 0.25, index + 1.0, 1.5, 3.75
        return 1

    class Textpage:
        """仅用于参考路径的 ctypes 参数适配，不向原生桥伪装成真实 PdfTextPage。"""

        _as_parameter_ = ct.c_void_p(1)

        def count_chars(self):
            """与人工原生输入采用完全相同的字符序列长度。"""
            return len(codes)

    functions = [uni, obj, bounds]
    frame = (0.0, 0.0, 20.0, 10.0)
    images = [frame, (1.0, 1.0, 4.0, 5.0)]
    visibility = {1: (False, None), 2: (True, (0.0, 0.0, 20.0, 10.0))}
    snapshot = native.read_pdfium_image_text(
        [ct.cast(f, ct.c_void_p).value for f in functions],
        1,
        len(codes),
        frame,
        0,
        images,
        [(k, *v) for k, v in visibility.items()],
        0.8,
    )
    monkeypatch.setattr(raw, "FPDFText_GetUnicode", uni)
    monkeypatch.setattr(raw, "FPDFText_GetTextObject", obj)
    monkeypatch.setattr(raw, "FPDFText_GetCharBox", bounds)
    boxes, paints = visuals._image_text_candidates_python(Textpage(), frame, 0, images, visibility)
    assert snapshot.count == 3
    assert [tuple(box) for box in snapshot.boxes()] == boxes
    assert snapshot.paints is paints


@pytest.mark.parametrize("addresses,handle,count", [([0, 1, 1], 1, 0), ([1, 1, 1], 0, 0), ([1, 1, 1], 1, 2**31)])
def test_image_reader_rejects_invalid_addresses_before_dereference(native, addresses, handle, count):
    """空函数、空句柄及超范围字符数必须在借用任何原生地址之前拒绝。"""
    with pytest.raises(ValueError, match="image text ABI"):
        native.read_pdfium_image_text(addresses, handle, count, (0.0, 0.0, 100.0, 100.0), 0, [], [], 0.8)


@pytest.mark.parametrize("count,cleaned,needed", [(49, 98, False), (50, 100, True), (50, 101, False), (51, 102, True)])
def test_impossible_ocr_bound_skips_pixel_probe(monkeypatch, count, cleaned, needed):
    """候选全不可见仍不够门槛时不能渲染；达到 50 和全文一半的边界仍要核验。"""
    calls = []
    snapshot = SimpleNamespace(count=count, paints=True, painted_count=lambda *_: 0)
    monkeypatch.setattr(bridge, "read_image_text_snapshot", lambda *_: snapshot)

    def probe(_page):
        """像素完全不变代表候选由扫描图承载，核验是否被正确跳过。"""
        calls.append(True)
        return Image.new("L", (1, 1))

    monkeypatch.setattr(visuals, "_text_paint_difference", probe)
    with pdfium.PdfDocument(image_text_pdf("background")) as doc, closing(doc[0]) as page:
        result = visuals.image_text_signal(page, cleaned, 50)
    assert bool(calls) is needed
    assert result["pixel_probe_performed"] is needed
    assert result["image_backed_ocr"] is needed


def test_classifier_shares_page_image_geometry(monkeypatch):
    """覆盖率和图上字符核验共用同次提取，不能再次遍历图片。"""
    calls = []
    original = visuals._image_rectangles

    def counted(page):
        """统计实际图片集合构建，而非仅统计页面加载。"""
        calls.append(True)
        return original(page)

    monkeypatch.setattr(visuals, "_image_rectangles", counted)
    with PDFDocument(image_text_pdf("background")) as doc:
        assert doc.classify() == "txt"
    assert len(calls) == 1


def test_nonstandard_character_reader_uses_reference(native, monkeypatch):
    """替换 ctypes 读取器后不得绕过调用方回调，必须明确返回参考选择。"""
    before = native.image_text_snapshot_stats()
    with (
        pdfium.PdfDocument(image_text_pdf("background")) as doc,
        closing(doc[0]) as page,
        closing(page.get_textpage()) as textpage,
    ):
        monkeypatch.setattr(raw, "FPDFText_GetCharBox", lambda *_: 0)
        assert bridge.read_image_text_snapshot(textpage, page.get_bbox(), 0, [], {}, 0.8) is None
    assert native.image_text_snapshot_stats() == before
