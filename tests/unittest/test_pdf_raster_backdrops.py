"""用真 PDF 保护可编辑正文、独立卡片，以及源底图中的可见浅色标记。"""

from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.document.pdf import PDFDocument


def draw_source_card(pdf, x, y, number):
    """画入透明内孔与不同圆角裁剪的卡片，标签仍由 PDF 字形描画。"""
    w, h = 149, 46
    pdf.saveState()
    clip = pdf.beginPath()
    clip.rect(x, y, w, h)
    clip.roundRect(x + 12, y + 6, w - 24, h - 12, radius=5)
    pdf.clipPath(clip, stroke=0, fill=0, fillMode=0)
    pdf.drawImage(ImageReader(Image.new("RGB", (150, 55), (195, 198, 201))), x, y, w, h)
    pdf.restoreState()
    for amt, color in [(5, (246, 248, 250)), (5.5, (253, 253, 254))]:
        pdf.saveState()
        inner = (x + 12, y + amt, w - 24, h - 2 * amt)
        path = pdf.beginPath()
        path.roundRect(*inner, radius=6)
        pdf.clipPath(path, stroke=0, fill=0)
        pdf.drawImage(ImageReader(Image.new("RGB", (135, 45), color)), *inner)
        pdf.restoreState()
    pdf.setFont("Helvetica", 9)
    pdf.setFillColorRGB(0.55, 0.06, 0.11)
    pdf.drawCentredString(x + w / 2, y + 19, f"CARD LABEL {number}")


def create_sheet(path, variant, mirror):
    """分别改变文字栏、背景自身小字或软蒙版，保持三张卡的源几何一致。"""
    w, h = 622, 730
    pdf = Canvas(str(path), pagesize=(w, h))
    layer = Image.new("RGB", (400, 173))
    layer.putdata(
        [
            (225 + int(y / 12 + x / 40), 230 + int(y / 15 + x / 50), 236 + int(y / 21 + x / 62))
            for y in range(173)
            for x in range(400)
        ]
    )
    if variant == "ink":
        ImageDraw.Draw(layer).text((192, 33), "FOREGROUND MARK", fill=(199, 205, 210))
    elif variant == "alpha":
        mask = Image.new("L", layer.size, 255)
        ImageDraw.Draw(mask).text((192, 33), "MASK MARK", fill=35)
        layer.putalpha(mask)
    pdf.drawImage(ImageReader(layer), 0, 0, w, 252, mask="auto")
    for n in range(3):
        draw_source_card(pdf, 405 if mirror else 42, 37 + 64 * n, n)
    if variant != "no-prose":
        pdf.setFont("Times-Roman", 11)
        pdf.setFillColorRGB(0, 0, 0)
        for num in range(7):
            pdf.drawString(
                38 if mirror else 286, 173 - 13 * num, f"Native paragraph has editable printed words on line number {num}."
            )
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(35, 625, "PAGE WITH INDEPENDENT ELEMENTS")
    pdf.save()


@pytest.mark.parametrize(
    "variant,mirror", [("gradient", False), ("gradient", True), ("ink", False), ("alpha", False), ("no-prose", True)]
)
def test_panel_backdrop_keeps_only_independent_visual_owners(tmp_path, variant, mirror):
    """真实印刷浅色标记、软蒙版和无正文的排版均阻止删除背景图。"""
    path = tmp_path / "cards-and-body.pdf"
    create_sheet(path, variant, mirror)
    with PDFDocument(str(path)) as pdf:
        infos = pdf.get_page_image_infos(0)
    assert len([o for o in infos if o.composite_inner_bboxes]) == 3
    parsed = parse(path, parse_mode="txt", keep_model_json=True)
    bodies = [b for b in parsed.model_json.pages[0] if b["type"] == "image"]
    bands = [b for b in bodies if b["bbox"][0] == 0.0 and b["bbox"][2] == 1.0]
    assert bool(bands) == (variant != "gradient")
    assert {b["content"] for b in bodies if b not in bands} == {f"CARD LABEL {i}" for i in range(3)}
    if variant == "gradient":
        page_text = [b for b in parsed.model_json.pages[0] if b["type"] == "text"]
        assert "Native paragraph has editable printed words on line number 0" in str(page_text)
        parsed.export(tmp_path / "document.html", output_format="html")
        for image_data in parsed.assets.values():
            with Image.open(BytesIO(image_data)) as image:
                image.verify()
