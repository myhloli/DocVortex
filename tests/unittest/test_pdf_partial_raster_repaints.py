"""使用真实局部 PDF 绘制验证重绘，保留可见独立图形和半透明覆盖。"""

from contextlib import closing
from io import BytesIO

from PIL import Image, ImageDraw
import pypdfium2 as pdfium
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from docvortex.document.pdf.native_objects import _extract_page_image_infos
from docvortex.document.pdf.raster_repaints import verify_raster_repaint


def _paint_source(left, *, horizontal=False, hole=False, overhang=False, reverse=False):
    """生成一对边缘重绘的真实位图，按控制变量添加透明孔或独立矢量外伸墨迹。"""
    data = BytesIO()
    sheet = canvas.Canvas(data, pagesize=(420, 320), pageCompression=0)
    bottom, width, height = 80, 160, 120
    over_width = 70 if horizontal else width
    over_height = height if horizontal else 50
    over_bottom = bottom + height - over_height
    base = Image.new("RGB", (160, 120), (33, 91, 150))
    overlay = Image.new("RGBA", (over_width, over_height), (177, 49, 79, 255))
    if hole:
        ImageDraw.Draw(overlay).rectangle((15, 15, 45, 39), fill=(0, 0, 0, 0))
    drawings = [(base, left, bottom, width, height), (overlay, left, over_bottom, over_width, over_height)]
    for image, x, y, w, h in reversed(drawings) if reverse else drawings:
        sheet.drawImage(ImageReader(image), x, y, w, h, mask="auto")
    sheet.setStrokeColorRGB(0, 0, 0)
    sheet.setLineWidth(3)
    # 这笔线实际在入选区域内。第二条在冲突框外沿露出时，该框不允许被丢弃。
    sheet.line(left + 10, 178, left + 40, 188)
    if overhang:
        sheet.line(left + 10, 205, left + 40, 217)
    sheet.save()
    return data.getvalue(), (left + 8, 100, left + 48, 162)


@pytest.mark.parametrize(
    ("left", "options", "expected"),
    [
        (45, {}, True),
        (200, {}, True),
        (45, {"horizontal": True}, True),
        (45, {"hole": True}, False),
        (45, {"overhang": True}, False),
        (45, {"reverse": True}, False),
    ],
    ids=["top-band", "translated-top-band", "left-band", "transparent-hole", "independent-ink", "reverse-paint"],
)
def test_partial_repaint_requires_real_order_and_paper_support(left, options, expected):
    """同样的候选几何只在旧层确实被部分替换且矢量框没有独立墨迹时接受。"""
    data, vector = _paint_source(left, **options)
    with pdfium.PdfDocument(data) as document, closing(document[0]) as page:
        boxes = [entry.bbox for entry in _extract_page_image_infos(page, page.get_bbox(), page.get_rotation())]
        overlay, base = sorted(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
        assert verify_raster_repaint(page, base, overlay, vector) is expected
