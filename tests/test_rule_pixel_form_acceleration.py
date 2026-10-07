"""字节和像素批量内核与原 pypdf/Pillow 规则逐项差分。"""

from io import BytesIO
import random

import pytest
from PIL import Image
from pypdf.generic import ContentStream, DecodedStreamObject, read_object

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf.inline.glyph_weight import _glyph_mask


@pytest.fixture
def native():
    """纯 Python 任务跳过内核差分，强制 Rust 任务要求兼容二进制。"""
    extension = get_native()
    if extension is None:
        pytest.skip("Rust backend required")
    return extension


def test_glyph_nearest_masks_match_pillow_including_outside_crop(native):
    """随机宽高、越界黑色填充和像素临界值保持原最近邻取样结果。"""
    rng = random.Random(1513)
    with Image.frombytes("L", (193, 141), bytes(rng.choice([0, 127, 128, 255]) for _ in range(193 * 141))) as image:
        boxes = []
        for _ in range(200):
            x, y = rng.randrange(-20, 195), rng.randrange(-15, 144)
            boxes.append((x, y, x + rng.randrange(1, 205), y + rng.randrange(1, 200)))
        values = native.glyph_masks(image.tobytes(), image.width, image.height, boxes)
        for box, actual in zip(boxes, values):
            expected = _glyph_mask(image, box, 1)
            if expected is None:
                assert actual is None
            else:
                bits, aspect = actual
                assert int.from_bytes(bits, "little") == sum(1 << i for i in expected[0]), box
                assert aspect == expected[1]


@pytest.mark.parametrize("top", [0, 2, 3, 7, 12, 13, 100])
@pytest.mark.parametrize("value", [249, 250])
def test_blank_rgb_threshold_and_margin_boundaries(native, top, value):
    """全宽首个可见像素决定顶边，近白阈值和百分比端点不放宽。"""
    with Image.new("RGB", (20, 100), "white") as image:
        if top < 100:
            image.putpixel((19, top), (value, 255, 255))
        expected = top / 100 if value < 250 and 3 <= top <= 12 else 0.0
        assert native.blank_top_rgb(image.tobytes(), 20, 100) == expected


def test_form_events_match_pypdf_without_parsing_text_operands(native):
    """正文中的操作符、嵌套字符串和数组不能伪造状态或 Form 调用。"""
    payload = b"/P << /MCID 0 /Lang(en-US) /Nested << /A [true false null 1 0] >> >> BDC q 1 0 0 1 -.5 +2 cm /F#6Frm Do [(q Q cm Do) 42 <446f> [(escaped\\(Do\\))]] TJ % Do\nQ"
    stream = DecodedStreamObject()
    stream.set_data(payload)
    expected = [(operands, op) for operands, op in ContentStream(stream, None).operations if op in (b"q", b"Q", b"cm", b"Do")]
    events = native.form_state_events(payload)
    assert events is not None
    actual = [
        ([read_object(BytesIO(payload[a:b]), None) for a, b in intervals], (b"q", b"Q", b"cm", b"Do")[kind])
        for kind, intervals in events
    ]
    assert actual == expected


def test_form_program_numeric_conversion_matches_reference(native):
    """整数负零、浮点负零、长小数与舍入临界值逐项比较，包含浮点字节而非仅数值相等。"""
    import struct

    rng = random.Random(1301)
    words = [
        b"-0",
        b"-0.0",
        b"+.0",
        b".500000000000000055511151231257827021181583404541015625",
        b"9007199254740993",
        b"-.00000000000000000000000000000000000000000000000000001",
    ]
    words += [f"{rng.randrange(-(10**18), 10**18)}.{rng.randrange(10**17):017d}".encode() for _ in range(150)]
    for word in words:
        payload = b"q " + b" ".join([word] * 6) + b" cm /F#6Frm Do Q"
        expected = float(read_object(BytesIO(word), None))
        program = native.form_state_program(payload)
        if len(word) >= 48:
            assert program is None
            continue
        assert program is not None
        assert all(struct.pack("d", value) == struct.pack("d", expected) for value in program[1][1])
        assert program[2][2] is not None
    assert native.form_state_program(b"1" * 65 + b" 0 0 1 0 0 cm") is None


def test_glyph_font_cache_distinguishes_types_and_field_mutations():
    """页内缓存不能混淆同值异型字体名，修改名字、flags 或字重后必须重新计算。"""
    from docvortex.analyzers.native.pdf.inline.glyph_weight import _glyph_font_metadata
    from docvortex.analyzers.native.pdf.inline.detection import _pdf_font_metadata

    cache = {}
    char = {"font": {"name": 1, "flags": 0, "weight": None}}
    for name, flags, weight in [(1, 0, None), (True, 0, None), (1.0, 0, None), ("ABCDEF+Body", 64, 700), ("Body", 0, None)]:
        char["font"].update(name=name, flags=flags, weight=weight)
        assert _glyph_font_metadata(char, cache) == _pdf_font_metadata(char)


@pytest.mark.parametrize("mode", ["RGB", "L", "RGBA", "P"])
@pytest.mark.parametrize("transparent", [False, True])
def test_blank_top_buffers_match_full_pillow_composition(native, monkeypatch, mode, transparent):
    """灰度、RGB、alpha 与调色板颜色键均与完整 Pillow 白底合成逐像素差分。"""
    from docvortex.document.pdf.native_objects import _blank_image_top_fraction
    import docvortex._compute_backend as backend

    with Image.new(mode, (100, 100), "white" if mode != "P" else 255) as image:
        if mode == "P":
            image.putpalette([v for value in range(256) for v in (value, value, value)])
        image.putpixel((99, 7), (249, 255, 255, 255) if mode == "RGBA" else (249, 255, 255) if mode == "RGB" else 249)
        if transparent:
            if mode == "RGBA":
                image.putpixel((99, 7), (249, 255, 255, 0))
            else:
                image.info["transparency"] = (249, 255, 255) if mode == "RGB" else 249
        actual = _blank_image_top_fraction(image)
        with monkeypatch.context() as context:
            context.setattr(backend, "get_native", lambda: None)
            expected = _blank_image_top_fraction(image)
        assert actual == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"<< /MCID >> BDC q Q",
        b"BI /W 1 /H 1 ID q Do EI Q",
        b"q [(broken] Q",
        b"1 2 cm",
        b"12 Do",
        b"q ] Q",
        b"/Bad#zz 1 gs q /Good Do Q",
        b"true 1 0 0 1 0 0 cm",
        b"<< /A [1 0 X] >> q Q",
        b"<< /A [1 0 R] >> q Q",
    ],
)
def test_form_special_or_malformed_streams_request_complete_reference(native, payload):
    """内联图片、损坏字典与异常操作数不能返回部分事件，整个流使用原解析器。"""
    assert native.form_state_events(payload) is None


@pytest.mark.parametrize("mode", ["L", "RGB", "BGR", "RGBX", "BGRX"])
@pytest.mark.parametrize("top", [0, 2, 3, 5, 12, 13, 99, 100])
def test_blank_bitmap_preserves_padding_and_pillow_color_semantics(native, monkeypatch, mode, top):
    """逐格式对照完整 Pillow 白底合成，行尾与 X 通道的非白填充不应影响白边。"""
    from docvortex.document.pdf import native_objects

    channels = 1 if mode == "L" else 3 if mode in ("RGB", "BGR") else 4
    width, height = 13, 100
    stride = width * channels + 7
    data = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * channels
            data[offset : offset + min(channels, 3)] = bytes([255 if y < top else 249]) * min(channels, 3)
            if channels == 4:
                data[offset + 3] = 0
    destination = "RGB" if mode == "BGR" else "RGBX" if mode == "BGRX" else mode
    with Image.frombytes(destination, (width, height), bytes(data), "raw", mode, stride, 1) as image:
        monkeypatch.setattr("docvortex._compute_backend.get_native", lambda: None)
        expected = native_objects._blank_image_top_fraction(image)
    assert native.blank_top_bitmap(bytes(data), width, height, stride, mode) == expected


def test_blank_bitmap_unknown_or_transparent_formats_fall_back(native):
    """透明度和损坏步长不能当作无透明图片，完整保留旧解码与合成路径。"""
    assert native.blank_top_bitmap(bytes(400), 10, 10, 40, "BGRA") is None
    assert native.blank_top_bitmap(bytes(400), 10, 10, 40, "RGBA") is None
    assert native.blank_top_bitmap(bytes(300), 10, 10, 1, "RGB") is None
    assert native.blank_top_bitmap(bytes(300), 10, 11, 30, "RGB") is None
