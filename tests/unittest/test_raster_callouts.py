"""验证完整 PDF 里跨页复用的内容插图、原生引线和不可放宽的水印保护。"""

from io import BytesIO
from pathlib import Path

from docvortex import parse
from docvortex.analyzers.native.pdf.pipeline import _collect_document_sources, _detect_repeated_raster_watermark_fingerprints
from docvortex.document.pdf._document import PDFDocument
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
import pytest


def _texture() -> bytes:
    """生成复用的位图面板，避免均匀底色触发单独的背景过滤。"""
    im = Image.new("RGB", (180, 151), "steelblue")
    d = ImageDraw.Draw(im)
    for y in range(5, 145, 11):
        d.rectangle((8 + y % 20, y, 161, y + 5), fill=(60 + y, 30, 200 - y))
    buffer = BytesIO()
    im.save(buffer, "PNG")
    return buffer.getvalue()


def _draw_callout(page, height, x, y, w, *, right, jog, linked, single_row):
    """画左右两种真实原生标注，分别支持直连、转折和不接触目标的反例。"""
    frame = (x + 6, y + 32, x + 0.78 * w, y + 82)
    yy = y + 55
    if right:
        panel = (x + w + 24, yy - 24, x + w + 112, yy + 24)
        points = ((panel[0], yy), (frame[2] + (12 if not linked else 0), yy))
    else:
        py = yy + 42 if jog else yy - 24
        panel = (x - 90, py, x - 20, py + 48)
        endpoint = (frame[0] - (12 if not linked else 0), yy)
        if jog:
            points = ((panel[0] + 30, panel[1]), (panel[0] + 30, yy), endpoint)
        else:
            points = ((panel[2], yy), endpoint)
    page.setStrokeColorRGB(0.8, 0, 0)
    page.setLineWidth(1.3)
    page.rect(frame[0], height - frame[3], frame[2] - frame[0], frame[3] - frame[1], stroke=1, fill=0)
    page.setStrokeColorRGB(0.2, 0.1, 0.3)
    page.setFillColorRGB(0.94, 0.94, 0.97)
    page.setLineWidth(1.1)
    page.rect(panel[0], height - panel[3], panel[2] - panel[0], panel[3] - panel[1], stroke=1, fill=1)
    line = page.beginPath()
    line.moveTo(points[0][0], height - points[0][1])
    for px, py in points[1:]:
        line.lineTo(px, height - py)
    page.drawPath(line, stroke=1, fill=0)
    page.setFillColorRGB(0, 0, 0)
    page.setFont("Courier" if right else "Helvetica", 10)
    page.drawString(panel[0] + 7, height - (panel[1] + 17), "Local detail")
    if not single_row:
        page.drawString(panel[0] + 7, height - (panel[1] + 34), "Tune value")


def _create_pdf(path, mode):
    """构造完整文档，并独立改动位置、字体、栏宽、路径端点或正文叠压。"""
    widths = [235, 276, 258, 235]
    x_positions = [160, 160, 126, 142]
    y_positions = [222, 310, 148, 222]
    if mode == "stable":
        widths, x_positions, y_positions = [235] * 4, [160] * 4, [222] * 4
    if mode == "mixed-stable":
        widths, x_positions, y_positions = [235] * 3 + [276, 258], [160] * 3 + [160, 126], [222] * 3 + [310, 148]
    bitmap = ImageReader(BytesIO(_texture()))
    canvas = Canvas(str(path), pagesize=(595, 800))
    height, raster_height = 800, 230
    for i, (x, y, width) in enumerate(zip(x_positions, y_positions, widths, strict=True)):
        canvas.drawImage(bitmap, x, height - y - raster_height, width, raster_height)
        if mode == "mixed-stable":
            side = None if i < 3 else (i == 4)
        else:
            side = None if i not in {1, 2} or mode == "no-link" else (i == 2)
        if side is not None:
            _draw_callout(
                canvas, height, x, y, width, right=side, jog=not side, linked=mode != "broken", single_row=mode == "single"
            )
        base_y = y - 19 if i != 2 else y + raster_height + 34
        if mode == "overlay":
            base_y = y + 123
        canvas.setFont("Helvetica", 11)
        canvas.drawString(73, height - base_y + 17, "Operators record the active channel and verify the panel settings below.")
        canvas.drawString(73, height - base_y, "Read the technical reference and preserve the searchable instruction.")
        canvas.linkURL("https://example.org/reference", (73, height - base_y - 1, 121, height - base_y + 12))
        canvas.showPage()
    canvas.save()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("placed", [1, 1, 1, 1]),
        ("stable", [0, 0, 0, 0]),
        ("overlay", [0, 0, 0, 0]),
        ("broken", [0, 0, 0, 0]),
        ("single", [0, 0, 0, 0]),
        ("no-link", [0, 0, 0, 0]),
        ("mixed-stable", [0, 0, 0, 1, 1]),
    ],
)
def test_same_raster_requires_clear_moving_slot_and_linked_panel(tmp_path, mode, expected):
    """候选先由旧指纹门命中，只有明确复用插图才重新进入位图源列表。"""
    path = tmp_path / "raster.pdf"
    _create_pdf(path, mode)
    with PDFDocument(str(path)) as pdf:
        sizes = [pdf.page_size(i) for i in range(pdf.page_count)]
        raw = [pdf.get_page_image_infos(i) for i in range(pdf.page_count)]
        old_watermarks = _detect_repeated_raster_watermark_fingerprints(raw, sizes)
        assert len(old_watermarks) == 1
        sources = _collect_document_sources(pdf).page_sources
    assert [len(s.image_bboxes) for s in sources] == expected
    if mode == "placed":
        assert [len(s.raster_callout_bboxes) for s in sources] == [0, 1, 1, 0]
        assert sources[1].image_bboxes[0][0] < raw[1][0].bbox[0]
        assert sources[2].image_bboxes[0][2] > raw[2][0].bbox[2]


def test_public_parse_keeps_link_body_and_single_complete_asset(tmp_path):
    """公共导出必须保留同一图片、完整原生标注，以及外部正文和 URI。"""
    path = tmp_path / "raster.pdf"
    _create_pdf(path, "placed")
    result = parse(path, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    middle = result.to_dict()
    for page in middle["pages"]:
        images = [b for b in page["blocks"] if b["type"] == "image"]
        assert len(images) == 1
        visible = str(page["blocks"])
        assert visible.count("searchable instruction") == 1
        assert visible.count("https://example.org/reference") == 1
    for index in (1, 2):
        images = [b for b in middle["pages"][index]["blocks"] if b["type"] == "image"]
        assert images[0]["content"][0]["content"].splitlines() == ["Local detail", "Tune value"]
    exported = tmp_path / "document.html"
    result.export(exported, output_format="html")
    html = exported.read_text()
    for page in middle["pages"]:
        [body] = next(b for b in page["blocks"] if b["type"] == "image")["content"]
        assert isinstance(body["image_path"], str)
        assert body["image_path"] in html
        with Image.open(tmp_path / Path(body["image_path"])) as im:
            im.verify()
    assert html.count("https://example.org/reference") == 4
