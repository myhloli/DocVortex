"""通过真实 PDF 绘制流验证局部阴影、卡面与独立图像的归属。"""

from PIL import Image, ImageDraw, ImageFilter
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf.graphics import _build_raster_image_blocks
from docvortex.analyzers.native.pdf.pipeline import _collect_document_sources
from docvortex.document.pdf._document import PDFDocument

_PAGE = (620, 650)


def _pattern(color):
    """生成三段有明显颜色区隔的内容图，检查裁片确实保留了原图。"""
    im = Image.new("RGB", (160, 120), color)
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 159, 29), fill=(36, 140, 127))
    draw.rectangle((0, 90, 159, 119), fill=(183, 61, 101))
    return ImageReader(im)


def _soft_shadow():
    """生成真实低透明度阴影；ReportLab 在 PDF 中写独立软蒙版。"""
    alpha = Image.new("L", (160, 120))
    ImageDraw.Draw(alpha).rounded_rectangle((10, 9, 149, 110), radius=12, fill=144)
    dark = Image.new("RGBA", alpha.size, (24, 24, 24))
    dark.putalpha(alpha.filter(ImageFilter.GaussianBlur(4)))
    return ImageReader(dark)


def _native_prose(canvas):
    """增加真正的原生正文，让默认公共路由也使用可编辑文字。"""
    for idx in range(6):
        canvas.drawString(70, 93 + 14 * idx, f"Editable source text on the printed diagram sheet, line {idx}.")


def _layered_page(path, *, case="shadow", x=65, y=301, scale=1):
    """通过同一张纸上的图层顺序、裁剪和合法复用制造相近正反例。"""
    canvas = Canvas(str(path), pagesize=_PAGE)
    subject = _pattern((30, 80, 180))
    alternate = _pattern((195, 100, 45))
    outer = (x, y, 198 * scale, 155 * scale)
    inner = (x + 18 * scale, y + 16 * scale, 162 * scale, 123 * scale)

    def shadow_layer():
        """按用例绘制真实软阴影或不带蒙版的独立照片。"""
        canvas.drawImage(alternate if case == "opaque" else _soft_shadow(), *outer, mask="auto")

    def primary_layer():
        """画入局部圆角剪裁，不能用矩形重叠代替真实外观。"""
        canvas.saveState()
        clip = canvas.beginPath()
        clip.roundRect(*inner, radius=9 * scale)
        canvas.clipPath(clip, stroke=0, fill=0)
        canvas.drawImage(subject, *inner)
        canvas.restoreState()

    if case == "reverse":
        primary_layer()
        shadow_layer()
    else:
        shadow_layer()
        primary_layer()
    if case == "coincident-source":
        canvas.drawImage(alternate, *inner)
    if case == "reused":
        canvas.drawImage(subject, 382, 326, 162, 123)
    _native_prose(canvas)
    canvas.save()
    return outer, inner


def _draw_card(canvas, x, y, label, baseline):
    """在给定位置绘制透明内孔印刷边框和两笔圆角原生卡面。"""
    w, h = 151, 112
    outer = Image.new("RGB", (128, 96), (193, 194, 196))
    draw = ImageDraw.Draw(outer)
    draw.rectangle((3, 3, 124, 92), outline=(160, 163, 165), width=4)
    canvas.saveState()
    clip = canvas.beginPath()
    clip.rect(x, y, w, h)
    clip.roundRect(x + 15, y + 13, w - 30, h - 26, radius=8)
    canvas.clipPath(clip, stroke=0, fill=0, fillMode=0)
    canvas.drawImage(ImageReader(outer), x, y, w, h)
    canvas.restoreState()
    for amount, rgb in [(11, (250, 251, 252)), (11.5, (247, 248, 248))]:
        canvas.saveState()
        clip = canvas.beginPath()
        inner = (x + 15, y + amount, w - 30, h - amount * 2)
        clip.roundRect(*inner, radius=8)
        canvas.clipPath(clip, stroke=0, fill=0)
        canvas.drawImage(ImageReader(Image.new("RGB", (78, 60), rgb)), *inner)
        canvas.restoreState()
    canvas.setFillColorRGB(0.65, 0.09, 0.21)
    canvas.drawString(x + 22, baseline, label)
    canvas.setFillColorRGB(0, 0, 0)


def _card_page(path):
    """绘制一张卡片，供公共图体的文字与画面归属检验。"""
    canvas = Canvas(str(path), pagesize=_PAGE)
    _draw_card(canvas, 70, 340, "NATIVE CARD LABEL", 391)
    _native_prose(canvas)
    canvas.save()


def _bodies(result):
    """读取公开 MiddleJson 的图体，而非根据候选内部的数量判断。"""
    return [
        child
        for block in result.to_dict()["pages"][0]["blocks"]
        if block["type"] == "image"
        for child in block["content"]
        if child["type"] == "image_body"
    ]


@pytest.mark.parametrize(("scale", "x", "y", "route"), [(1, 65, 301, "txt"), (1.27, 95, 248, "auto")])
def test_soft_shadow_matches_one_rounded_photo(tmp_path, scale, x, y, route):
    """主图和外部可见软阴影各保留，整幅合成图只导出一个公开资产。"""
    path = tmp_path / "rounded.pdf"
    _layered_page(path, x=x, y=y, scale=scale)
    with PDFDocument(str(path)) as doc:
        image_infos = doc.get_page_image_infos(0)
        evidence = [info for info in image_infos if info.composite_inner_bboxes]
        assert len(evidence) == 1 and evidence[0].shadow_ink_bbox is not None
    result = parse(path, keep_model_json=True, **({} if route == "auto" else {"parse_mode": route}))
    (body,) = _bodies(result)
    result.export(tmp_path / "document.html", output_format="html")
    result.export(tmp_path / "document.md", output_format="markdown")
    assert body["image_path"] in (tmp_path / "document.html").read_text()
    assert body["image_path"] in (tmp_path / "document.md").read_text()
    with Image.open(tmp_path / body["image_path"]) as crop:
        width, height = crop.size
        midpoint, edge = crop.getpixel((width // 2, height // 2)), crop.getpixel((round(width * 0.065), height // 2))
        assert midpoint[2] - midpoint[0] > 120
        assert 120 < edge[0] < 242 and max(edge) - min(edge) < 10


@pytest.mark.parametrize("case", ["opaque", "reverse", "edge", "coincident-source", "reused"])
def test_close_geometry_preserves_independent_image_placements(tmp_path, case):
    """普通无蒙版照片、反序覆盖、页面截断与同源资源复用都保留独立放置。"""
    path = tmp_path / "independent.pdf"
    _layered_page(path, case=case, x=-6 if case == "edge" else 65)
    with PDFDocument(str(path)) as doc:
        found = [info for info in doc.get_page_image_infos(0) if info.composite_inner_bboxes]
        assert len(found) == (1 if case == "reused" else 0)
    result = parse(path, parse_mode="txt", keep_model_json=True)
    assert len(_bodies(result)) == 2


def test_filtered_outer_does_not_consume_foreground(tmp_path):
    """走真实位图提取和图容器构建；竞争容器盖住外层一侧后，主体仍可独立导出。"""
    path = tmp_path / "competing-form.pdf"
    _layered_page(path)
    with PDFDocument(str(path)) as doc:
        image_infos = doc.get_page_image_infos(0)
        source = _collect_document_sources(doc).page_sources[0]
    (outer,) = [info for info in image_infos if info.composite_inner_bboxes]
    (inner,) = outer.composite_inner_bboxes
    left, top, right, bottom = outer.bbox
    # 模拟前一阶段已保留的高优先级图形，覆盖外框而与主体交叠低于容器阈值。
    blocker = {"bbox": (left, top - 4, right, (top + inner[1]) / 2)}
    raster_blocks, _ = _build_raster_image_blocks(source, [blocker], set())
    assert [block["bbox"] for block in raster_blocks] == [inner]


def test_transparent_card_hole_and_native_word_are_one_card(tmp_path):
    """卡面与印刷阴影只出一图，原生文字在公开图体中仍可提取。"""
    path = tmp_path / "card.pdf"
    _card_page(path)
    with PDFDocument(str(path)) as doc:
        infos = doc.get_page_image_infos(0)
        composites = [info for info in infos if info.composite_inner_bboxes]
        assert len(composites) == 1 and composites[0].shadow_ink_bbox is None
        assert len(composites[0].composite_inner_bboxes) == 2
    result = parse(path, parse_mode="txt", keep_model_json=True)
    (body,) = _bodies(result)
    assert body["content"] == "NATIVE CARD LABEL"
    result.export(tmp_path / "document.html", output_format="html")
    result.export(tmp_path / "document.md", output_format="markdown")
    for name in ("document.html", "document.md"):
        assert "NATIVE CARD LABEL" in (tmp_path / name).read_text()


def test_nearby_printed_label_panel_stays_outside_the_shadow_crop(tmp_path):
    """原生标签文字虽在图外，稍有重叠的原生填充底板也不能进入组合裁片。"""
    path = tmp_path / "panel.pdf"
    canvas = Canvas(str(path), pagesize=_PAGE)
    canvas.drawImage(_soft_shadow(), 65, 300, 198, 155, mask="auto")
    canvas.drawImage(_pattern((30, 80, 180)), 83, 316, 162, 123)
    canvas.setFillColorRGB(0.75, 0, 0)
    canvas.roundRect(78, 452, 174, 27, 4, stroke=0, fill=1)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.drawString(105, 460, "Printed banner")
    canvas.setFillColorRGB(0, 0, 0)
    _native_prose(canvas)
    canvas.save()
    with PDFDocument(str(path)) as doc:
        source = _collect_document_sources(doc).page_sources[0]
    (photo,), _ = _build_raster_image_blocks(source, [], set())
    assert photo["bbox"] == (65.0, 198.0, 263.0, 350.0)
    assert "Printed banner" not in str(photo["content"])
    result = parse(path, parse_mode="txt", keep_model_json=True)
    result.export(tmp_path / "document.html", output_format="html")
    assert "Printed banner" in (tmp_path / "document.html").read_text()


def test_source_card_label_rows_sort_left_to_right_despite_shadow_top(tmp_path):
    """真实三列卡片外框纵向错位时，公开图体按原生标签来源逐行从左向右。"""
    path = tmp_path / "card-rows.pdf"
    canvas = Canvas(str(path), pagesize=_PAGE)
    expected = []
    for row, base in enumerate((415, 270)):
        for column, (x, shift) in enumerate(zip((70, 230, 390), (0, 3, 6), strict=True)):
            label = f"Source card {row + 1} / {column + 1}"
            _draw_card(canvas, x, base + shift, label, base + 52)
            expected.append(label)
    _native_prose(canvas)
    canvas.save()
    result = parse(path, parse_mode="txt", keep_model_json=True)
    assert [body["content"] for body in _bodies(result)] == expected
    result.export(tmp_path / "document.md", output_format="markdown")
    exported = (tmp_path / "document.md").read_text()
    starts = [exported.index(label) for label in expected]
    assert starts == sorted(starts)


@pytest.mark.parametrize("case", ["different-label-row", "without-card-evidence"])
def test_different_rows_and_ordinary_images_keep_existing_geometric_order(case):
    """两幅独立图即使外框接近，也须同时有卡片和同排字行证据才改变排序。"""
    from docvortex.analyzers.native.pdf.pipeline import _sort_blocks_with_visual_row_groups

    left = {"type": "image", "bbox": (50, 54, 200, 155), "content": "Left image", "angle": 0}
    right = {"type": "image", "bbox": (200, 50, 350, 153), "content": "Right image", "angle": 0}
    baseline = _sort_blocks_with_visual_row_groups([left, right], _PAGE)
    assert [b["content"] for b in baseline] == ["Right image", "Left image"]
    if case == "different-label-row":
        left["_raster_card_label_bbox"] = (60, 105, 120, 115)
        right["_raster_card_label_bbox"] = (220, 80, 280, 90)
    else:
        left["_raster_card_label_bbox"] = (60, 105, 120, 115)
    candidate = _sort_blocks_with_visual_row_groups([left, right], _PAGE)
    assert [b["content"] for b in candidate] == ["Right image", "Left image"]


@pytest.mark.parametrize("mode", ["txt", "auto"])
def test_centered_card_labels_with_different_line_counts_share_physical_rows(tmp_path, mode):
    """同排一、二、三行标签的首基线交错，公开结果仍按完整字框逐行从左到右。"""
    path = tmp_path / "mixed-card-labels.pdf"
    canvas = Canvas(str(path), pagesize=_PAGE)
    expected = []
    for row, baseline in enumerate((415, 270)):
        line_counts = (2, 1, 3) if row == 0 else (3, 2, 1)
        for column, (x, shift, lines) in enumerate(zip((70, 230, 390), (0, 3, 6), line_counts, strict=True)):
            content = [f"Topic {row + 1}.{column + 1}"]
            if lines > 1:
                content.append("Spring edition")
            if lines > 2:
                content.append("Next update")
            _draw_card(canvas, x, baseline + shift, "", baseline + 53)
            canvas.setFillColorRGB(0.65, 0.09, 0.21)
            for index, text in enumerate(content):
                y = baseline + 53 + (len(content) - 1) * 7 - index * 14
                canvas.drawCentredString(x + 75.5, y, text)
            canvas.setFillColorRGB(0, 0, 0)
            expected.append("\n".join(content))
    _native_prose(canvas)
    canvas.save()

    opts = {} if mode == "auto" else {"parse_mode": mode}
    result = parse(path, keep_model_json=True, **opts)
    assert [body["content"] for body in _bodies(result)] == expected
