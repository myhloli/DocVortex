"""验证同位置叠印与异位置复用位图的最终可见裁片。"""

from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse


def _overprinted_pdf(scale, y, *, near_layer=False):
    """在两个物理位置复用同一底图，只在第一处叠印带透明区的第二个图层。"""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(600, 600))
    base = Image.new("RGB", (72, 56), (22, 50, 180))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle((36, 0, 71, 55), fill=(225, 38, 32, 255))
    w, h = 100 * scale, 80 * scale
    for x in (60, 318):
        canvas.drawImage(ImageReader(base), x, y, w, h)
    if near_layer:
        # 字面宽度极近但两边交叉，原三层仍需作为一组经过现有切片合并器。
        canvas.drawImage(ImageReader(base), 59.999, y + 0.1, w - 0.02, h - 0.1)
    canvas.drawImage(ImageReader(overlay), 60, y, w, h, mask="auto")
    canvas.drawString(61, y + h * 0.4, "Native label")
    for n in range(7):
        canvas.drawString(60, 95 + n * 14, f"Independent report notes about the printed drawing, item {n}.")
    canvas.save()
    return buffer.getvalue()


@pytest.mark.parametrize(("scale", "y"), [(0.85, 250), (1.35, 342)])
@pytest.mark.parametrize("mode", ["auto", "txt"])
def test_overprint_has_one_composited_crop_per_physical_placement(scale, y, mode, tmp_path):
    """公开输出须只出现两图，第一图保留底色与叠印，第二图只显示原底色。"""
    path = tmp_path / "layers.pdf"
    path.write_bytes(_overprinted_pdf(scale, y))
    result = parse(path, keep_model_json=True, **({} if mode == "auto" else {"parse_mode": mode}))
    result.export(tmp_path / "document.html", output_format="html")
    result.export(tmp_path / "document.md", output_format="markdown")
    blocks = [b for b in result.to_dict()["pages"][0]["blocks"] if b["type"] == "image"]
    children = sorted((c for b in blocks for c in b["content"] if c["type"] == "image_body"), key=lambda c: c["bbox"][0])
    assert len(children) == 2
    assert (tmp_path / "document.md").read_text().count("](images/") == 2
    for index, child in enumerate(children):
        assert child["image_path"] in (tmp_path / "document.html").read_text()
        with Image.open(tmp_path / child["image_path"]) as image:
            left, right = (image.getpixel((int(image.width * x), int(image.height * 0.2))) for x in (0.2, 0.8))
            assert left[2] > left[0] + 100
            if index == 0:
                assert right[0] > right[2] + 100
            else:
                assert right[2] > right[0] + 100
    assert sum("Native label" in str(b["content"]) for b in result.model_json.to_dict()["pages"][0]) == 1


def test_tile_grouping_precedes_same_place_deduplication(tmp_path):
    """一组包含完全重合和微偏移三层，组合后左图应只出现一次。"""
    path = tmp_path / "print-layers.pdf"
    path.write_bytes(_overprinted_pdf(1.4, 300, near_layer=True))
    result = parse(path, parse_mode="txt", keep_model_json=True)
    images = [b for b in result.to_dict()["pages"][0]["blocks"] if b["type"] == "image"]
    assert len(images) == 2
    assert len({tuple(b["bbox"]) for b in images}) == 2
