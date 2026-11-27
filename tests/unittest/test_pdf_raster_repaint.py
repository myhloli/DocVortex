"""以两笔真实原生裁剪区分同位置换图、透明墨迹重叠与需要独立保留的文字。"""

from io import BytesIO

from PIL import Image, ImageDraw
import pypdfium2 as pdfium
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from docvortex import parse
from docvortex.document.pdf.native_objects import _extract_page_image_infos


def _paint_product(sheet, left, bottom, width, *, later=False, hole=False):
    """绘制横向略错位的两个产品版本，保留下层独有的竖边。"""
    image = Image.new("RGBA", (120, 100), (225, 225, 210, 255))
    painter = ImageDraw.Draw(image)
    color = (198, 47, 52, 255) if later else (45, 65, 174, 255)
    painter.rectangle((5, 5, 115, 95), fill=color)
    painter.line((3, 3, 3, 96), fill=(35, 35, 35, 255), width=2)
    if hole:
        painter.rectangle((25, 15, 100, 80), fill=(0, 0, 0, 0))
    sheet.saveState()
    path = sheet.beginPath()
    path.rect(left, bottom, width, 85)
    sheet.clipPath(path, stroke=0, fill=0)
    sheet.drawImage(ImageReader(image), left - 3, bottom - 3, width + 6, 91, mask="auto")
    sheet.restoreState()


def _source(shift, *, hole=False, label=False):
    """构造同一槽位有两笔不同图纹的 PDF，并给邻区添加独立可编辑段落。"""
    out = BytesIO()
    sheet = canvas.Canvas(out, pagesize=(420, 320), pageCompression=0)
    _paint_product(sheet, shift, 160, 150)
    _paint_product(sheet, shift + 2, 160.5, 150, later=True, hole=hole)
    if label:
        sheet.drawString(shift + 20, 195, "Independent printed legend")
    for index in range(5):
        sheet.drawString(36, 32 + 16 * index, f"Editable paragraph outside the illustration {index}.")
    sheet.save()
    return out.getvalue()


def _figures(result):
    """按公开中间页读取可解码的图体及其从属区域。"""
    return [
        child
        for block in result.to_dict()["pages"][0]["blocks"]
        if block["type"] == "image"
        for child in block["content"]
        if child["type"] == "image_body"
    ]


@pytest.mark.parametrize("shift", [50, 180])
def test_repaint_joins_both_surviving_edges(shift):
    """同位换图须收齐两笔绘制的外缘，留下最终红色正面并保持图外文字。"""
    result = parse(_source(shift), parse_mode="txt")
    (body,) = _figures(result)
    # MiddleJson 的公开归一化坐标固定到三位小数，此误差小于半个单位。
    left, top, right, bottom = body["bbox"]
    assert left * 420 == pytest.approx(shift, abs=0.21)
    assert right * 420 == pytest.approx(shift + 152, abs=0.21)
    assert top * 320 == pytest.approx(74.5, abs=0.21)
    assert bottom * 320 == pytest.approx(160, abs=0.21)
    with Image.open(BytesIO(result.assets[body["image_path"]])) as image:
        red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    assert red > 2 * green and red > 2 * blue


def test_cutout_keeps_different_visible_graphics():
    """后画大孔仍显露前图大块图纹，两幅源图都需有独立成品。"""
    data = _source(90, hole=True)
    with pdfium.PdfDocument(data) as doc:
        page = doc[0]
        try:
            infos = _extract_page_image_infos(page, page.get_bbox(), page.get_rotation())
        finally:
            page.close()
    assert not any(getattr(info, "repainted_inner_bboxes", ()) for info in infos)
    assert len(_figures(parse(data, parse_mode="txt"))) == 2


def test_native_legend_protects_separate_owners():
    """交叠区域有独立原生图例时沿用原来的文字归属。"""
    result = parse(_source(90, label=True), parse_mode="txt")
    assert len(_figures(result)) == 2
