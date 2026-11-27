"""用真实绘制顺序验证独立注框以外的可见底图、开孔及整本公开输出。"""

from base64 import b64decode
from contextlib import closing
from hashlib import file_digest
from io import BytesIO
import json
from os import environ
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
import pypdfium2 as pdfium
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from docvortex import parse


def _draw_test_page(shift, overlap):
    """在已知位置先画多色底图，后画有不透明、开孔、半透明三种状态的说明牌。"""
    data = BytesIO()
    page = canvas.Canvas(data, pagesize=(450, 320), pageCompression=0)
    picture = Image.new("RGB", (240, 150), (56, 137, 93))
    paint = ImageDraw.Draw(picture)
    paint.rectangle((8, 90, 50, 125), fill=(174, 41, 75))
    paint.rectangle((84, 30, 121, 70), fill=(255, 15, 234))
    left, top = 45 + shift, 60
    page.drawImage(ImageReader(picture), left, 320 - top - 150, 240, 150)
    note = (left + 70, top + 21, left + 140, top + 86)
    page.saveState()
    path = page.beginPath()
    path.rect(note[0], 320 - note[3], 70, 65)
    if overlap == "hole":
        path.rect(note[0] + 21, 320 - note[3] + 15, 28, 27)
    if overlap == "transparent":
        page.setFillAlpha(0.5)
    page.setFillColorRGB(0.1, 0.35, 0.9)
    page.drawPath(path, stroke=0, fill=1, fillMode=0)
    page.restoreState()
    page.save()
    return data.getvalue(), (left, top, left + 240, top + 150), note


@pytest.mark.parametrize("shift", [0, 117])
def test_only_visible_underlay_ink_is_exported(shift):
    """平移底图和注框后，保住独立底图颜色，注框内早画的紫色和后画蓝色均不应进入导出。"""
    from docvortex.document.pdf.callout_projection import visible_callout_underlay

    data, box, note = _draw_test_page(shift, "opaque")
    with pdfium.PdfDocument(data) as doc, closing(doc[0]) as page:
        normalized = tuple(v / (450, 320)[i % 2] for i, v in enumerate(box))
        uri = visible_callout_underlay(page, box, note, normalized)
        assert uri and uri.startswith("data:image/png;base64,")
    with Image.open(BytesIO(b64decode(uri.split(",", 1)[1]))) as image:
        rgb = np.array(image.convert("RGB"))
    assert (rgb[:, :, :3] == [56, 137, 93]).all(axis=2).mean() > 0.5
    assert (rgb == [174, 41, 75]).all(axis=2).mean() > 0.015
    assert (rgb[int(0.35 * rgb.shape[0]), int(0.42 * rgb.shape[1])] == 255).all()
    assert not ((rgb[:, :, 0] > 200) & (rgb[:, :, 2] > 190) & (rgb[:, :, 1] < 80)).any()


@pytest.mark.parametrize("overlap", ["hole", "transparent"])
def test_underlay_visible_in_the_annotation_is_not_removed(overlap):
    """源图在说明框内仍有印刷像素时，不能用白纸替代这部分。"""
    from docvortex.document.pdf.callout_projection import visible_callout_underlay

    data, box, note = _draw_test_page(30, overlap)
    with pdfium.PdfDocument(data) as doc, closing(doc[0]) as page:
        normalized = tuple(v / (450, 320)[i % 2] for i, v in enumerate(box))
        assert visible_callout_underlay(page, box, note, normalized) is None


@pytest.mark.parametrize("options", [{}, {"parse_mode": "txt"}], ids=["default", "native"])
def test_published_thunderstorm_map_retains_its_note_and_regions(options, tmp_path):
    """按官方整册验证阿拉斯加和美国本土同时可见，蓝色说明框与独立原生文字各自保留。"""
    base = Path(__file__).resolve().parents[1] / "fixtures"
    src = json.loads((base / "flash_r21_callout_underlay.json").read_bytes())
    if not (path := environ.get("DOCVORTEX_TEST_NWS_GUIDE")):
        pytest.skip(f"Set DOCVORTEX_TEST_NWS_GUIDE to the reviewed source: {src['source_url']}")
    with Path(path).open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == src["sha256"]
    result = parse(Path(path), **options)
    assert len(result.middle_json.pages) == src["pages"]
    n = src["physical_page"] - 1
    blocks = result.to_dict()["pages"][n]["blocks"]
    images = [b["content"][0] for b in blocks if b["type"] == "image"]

    def at_source_region(key):
        """使用原件点坐标选中唯一图体，容许公开坐标归一化到三位小数。"""
        size = src["source_page_size"]
        expected = [coord / size[i % 2] for i, coord in enumerate(src[key])]
        (body,) = [b for b in images if max(abs(a - z) for a, z in zip(b["bbox"], expected, strict=True)) < 0.003]
        return body

    map_body = at_source_region("map_bbox_pt")
    note_body = at_source_region("note_bbox_pt")
    assert map_body["image_path"] != note_body["image_path"]
    sentence = src["note_text"]
    assert sum(sentence in " ".join(c["content"] for c in b["content"]) for b in blocks if b["type"] == "text") == 1
    for ext, fmt in [("html", "html"), ("md", "markdown")]:
        f = tmp_path / f"document.{ext}"
        result.export(f, output_format=fmt)
        text = f.read_text()
        assert sentence in text and map_body["image_path"] in text and note_body["image_path"] in text
    with Image.open(tmp_path / map_body["image_path"]) as pic:
        crop = np.array(pic.convert("RGB"))
    with pdfium.PdfDocument(path) as doc, closing(doc[n]) as page, closing(page.render(scale=200 / 72)) as bitmap:
        source = np.array(bitmap.to_pil().convert("RGB"))
    h, w = source.shape[:2]
    left, top, right, bottom = map_body["bbox"]
    x, y = int(np.floor(left * w)), int(np.floor(top * h))
    right_px, bottom_px = (int(np.floor(v)) for v in (right * w, bottom * h))
    assert abs(crop.shape[1] - (right_px - x)) <= 2 and abs(crop.shape[0] - (bottom_px - y)) <= 2
    for region in src["visible_map_windows"]:
        x1, y1, x2, y2 = [round(v * (w, h)[i % 2]) for i, v in enumerate(region)]
        original = source[y1:y2, x1:x2]
        projected = crop[y1 - y : y2 - y, x1 - x : x2 - x]
        color = np.ptp(original.astype(int), axis=2) > 30
        assert color.sum() > 2000
        assert (np.abs(projected.astype(int) - original).max(axis=2)[color] <= 5).mean() > 0.95
    # 两区域同比对之外，再用说明框内部的实际范围排除恢复底图时夹带说明牌。
    nl, nt, nr, nb = [round(v * (w, h)[i % 2]) for i, v in enumerate(src["note_interior"])]
    note = crop[nt - y : nb - y, nl - x : nr - x]
    assert np.all(note == 255)
