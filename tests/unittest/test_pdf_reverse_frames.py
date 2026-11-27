"""用真实 PDF 绘制和公开资产验证屏框、编辑文字与独立图层不互相吞并。"""

from collections.abc import Iterable
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from docvortex import parse
from docvortex.analyzers.native.pdf import pipeline
from docvortex.document.pdf import PDFDocument

PAGE = (480, 568)
FRAME = (92, 182, 332, 382)
SCREEN = (100, 203, 323, 361)
TIGHT = (98, 189, 326, 375)
INSET = (161, 224, 229, 293)
TEST_COLORS = {(17, 86, 154), (241, 135, 30), (21, 173, 121)}


def batch_image(kind):
    """绘制可逐色验证的独立主体与四边透明边框。"""
    if kind in {"frame", "decorated"}:
        image = Image.new("RGBA", (240, 200))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, 239, 199), radius=13, outline=(17, 86, 154, 255), width=6)
        if kind == "decorated":
            draw.ellipse((15, 33, 118, 153), fill=(17, 86, 154, 255))
        return image
    if kind == "subject":
        image = Image.new("RGBA", (280, 340))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((28, 41, 247, 255), radius=20, fill=(17, 86, 154, 255))
        draw.ellipse((150, 78, 228, 154), fill=(241, 135, 30, 255))
        draw.rectangle((126, 247, 163, 339), fill=(21, 173, 121, 255))
        return image
    bg, spot = ((241, 135, 30), (21, 173, 121)) if kind == "screen" else ((17, 86, 154), (21, 173, 121))
    image = Image.new("RGB", (240, 200), bg)
    ImageDraw.Draw(image).ellipse((135, 45, 212, 123), fill=spot)
    return image


def paint(pdf, image, box):
    """使用左上坐标放置位图，保留带透明度资源。"""
    x1, y1, x2, y2 = box
    pdf.drawImage(ImageReader(image), x1, PAGE[1] - y2, width=x2 - x1, height=y2 - y1, mask="auto")


def draw_phone(path, mode):
    """建立不同几何、绘制顺序和外层内容的手机原页。"""
    pdf = canvas.Canvas(str(path), pagesize=PAGE)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(92, PAGE[1] - 156, "Devices on display")
    pdf.setFont("Times-Roman", 12)
    pdf.drawString(90, PAGE[1] - 423, "A short description outside the image frame.")
    inner = TIGHT if mode == "tight" else INSET if mode == "inset" else SCREEN
    if mode == "opaque":
        paint(pdf, batch_image("opaque"), FRAME)
        paint(pdf, batch_image("screen"), inner)
    else:
        paint(pdf, batch_image("screen"), inner)
        paint(pdf, batch_image("decorated" if mode == "decorated" else "frame"), FRAME)
    if mode == "extra-inset":
        paint(pdf, batch_image("opaque"), INSET)
    pdf.save()
    return inner


def backdrop_image(mark):
    """制作原像素中仅亮度相差 12 的微印图标，对照曾缩图的背景验证。"""
    levels = np.linspace(212, 242, 1550, dtype=np.uint8)
    image = Image.fromarray(np.repeat(levels[:, None], 1120, axis=1)).convert("RGB")
    if mark == "glyph":
        draw = ImageDraw.Draw(image)
        gray = int(levels[166]) - 12
        font = ImageFont.load_default(size=11)
        draw.text((384, 166), "xy", font=font, fill=(gray,) * 3)
    if mark == "side":
        y = 700
        draw = ImageDraw.Draw(image)
        gray = int(levels[y]) - 12
        draw.rectangle((4, y, 14, y + 17), fill=(gray,) * 3)
    return image


def draw_backdrop(path, side, header, mark=None):
    """在可移动的左右印刷栏里建立底图、内图及真实原生标题。"""
    x = 13 if side == "left" else 230
    outer = (x, 17, x + 236, 554)
    inner = (x + 5, 153, x + 233, 550)
    pdf = canvas.Canvas(str(path), pagesize=PAGE)
    paint(pdf, backdrop_image(mark), outer)
    paint(pdf, batch_image("subject"), inner)
    if header:
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(x + 34, PAGE[1] - 82, header)
    pdf.save()
    return outer, inner


def model_images(result):
    """从实际 ModelJson 返回页面中输出的图像范围。"""
    return [block["bbox"] for block in result.model_json.pages[0] if block["type"] == "image"]


def places(boxes: Iterable):
    """按解析器公开坐标精度归一原始测量框。"""
    return sorted(tuple(round(v / PAGE[i % 2], 3) for i, v in enumerate(box)) for box in boxes)


def verify_assets(result, html):
    """解码每个被链接的实际图像，汇总其可周边复核的像素代表。"""
    assert result.assets
    colors = set()
    result.export(html, output_format="html", overwrite=True)
    for relative, payload in result.assets.items():
        assert (html.parent / relative).read_bytes() == payload
        with Image.open(html.parent / relative) as image:
            rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
            colors.update(color for color in TEST_COLORS if np.any(np.max(abs(rgb - color), axis=2) <= 7))
    return colors


@pytest.mark.parametrize("mode", ("screen", "opaque", "decorated", "inset", "extra-inset"))
def test_phone_frame_and_distinct_raster(tmp_path, mode):
    """只归并完整开孔中的主屏，保留独立全色外图与小型内嵌图。"""
    pdf = tmp_path / "screen.pdf"
    inner = draw_phone(pdf, mode)
    result = parse(str(pdf), parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    expected = [FRAME]
    if mode in {"opaque", "decorated", "inset"}:
        expected += [inner]
    if mode == "extra-inset":
        expected += [INSET]
    assert sorted(map(tuple, model_images(result))) == places(expected)
    assert verify_assets(result, tmp_path / "document.html") == TEST_COLORS
    assert "Devices on display" in (tmp_path / "document.html").read_text()


@pytest.mark.parametrize("container_type", ("table", "image"))
def test_reverse_frame_after_container_filter(tmp_path, container_type):
    """覆盖只筛掉外框的表格、Form 容器，旧近框阈值面积也不可吞掉先画主体。"""
    pdf = tmp_path / "screen.pdf"
    inner = draw_phone(pdf, "tight")
    before = pipeline._build_raster_image_blocks
    observed = []

    def intercept(source, blocks, members):
        """只在真实进图容器入口注入占用边条，记录最终的图像块。"""
        reservation = {"type": container_type, "bbox": (94, 180, 330, 186), "content": ""}
        images, claimed = before(source, [*blocks, reservation], members)
        observed.extend(block["bbox"] for block in images)
        return images, claimed

    with patch.object(pipeline, "_build_raster_image_blocks", intercept):
        result = parse(str(pdf), parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    assert places(observed) == places((inner,))
    assert sorted(map(tuple, model_images(result))) == places((inner,))
    assert {(241, 135, 30), (21, 173, 121)} <= verify_assets(result, tmp_path / "document.html")


@pytest.mark.parametrize(
    ("side", "header", "mark", "collapse"),
    (
        ("left", "AI", None, True),
        ("right", "Vue", None, True),
        ("right", "Vue", "glyph", False),
        ("left", "AI", "side", False),
        ("right", None, None, False),
    ),
)
def test_backdrop_discard_requires_blank_ink_and_native_header(tmp_path, side, header, mark, collapse):
    """保住会在缩图中消失的暗纹、窄边中的独立标志和无题名的背景。"""
    pdf = tmp_path / "poster.pdf"
    outer, inner = draw_backdrop(pdf, side, header, mark)
    result = parse(str(pdf), parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    np.testing.assert_allclose(sorted(model_images(result)), places((inner,) if collapse else (inner, outer)), atol=0.0011)
    if mark:
        # 旧版用缩图验证，微印在缩图中的亮度差低于生产旧阈值 22。
        scaled = [
            np.array(backdrop_image(case).resize((256, 256), Image.Resampling.BOX), dtype=np.int16) for case in (mark, None)
        ]
        assert 0 < np.abs(scaled[0] - scaled[1]).max() < 22
    assert verify_assets(result, tmp_path / "document.html") == TEST_COLORS
    if collapse:
        matches = [b for b in result.model_json.pages[0] if b["type"] != "image" and header in str(b["content"])]
        assert len(matches) == 1
        assert matches[0]["bbox"][3] < inner[1] / PAGE[1]


def test_screen_frames_do_not_certify_page_wide_card_background(tmp_path):
    """三组屏框及图中原生字不成为 R18 卡片色带的前景证明。"""
    pdf_path = tmp_path / "screen-board.pdf"
    pdf = canvas.Canvas(str(pdf_path), pagesize=PAGE)
    band = (0, 175, PAGE[0], 390)
    paint(pdf, Image.new("RGB", (360, 240), (240, 240, 242)), band)
    for x in (24, 139, 254):
        paint(pdf, batch_image("screen"), (x + 5, 221, x + 85, 344))
        paint(pdf, batch_image("frame"), (x, 209, x + 90, 355))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(x + 10, PAGE[1] - 247, "Specimen")
    pdf.save()
    with PDFDocument(str(pdf_path)) as doc:
        infos = doc._extract_native_page(0).image_infos
    assert sum(info.raster_pair is not None for info in infos) == 3
    assert sum(bool(info.composite_inner_bboxes) for info in infos) == 0
    assert all(not info.quiet_band_backdrop for info in infos)
