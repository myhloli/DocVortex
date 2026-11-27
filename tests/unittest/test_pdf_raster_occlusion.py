"""使用真实 PDF 对象验证完全遮挡与近形反例。"""

from PIL import Image, ImageDraw
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf.graphics import _build_raster_image_blocks
from docvortex.analyzers.native.pdf.pipeline import _collect_document_sources
from docvortex.document.pdf._document import PDFDocument


def _pattern(color, *, mode="RGB"):
    """创建有独立图案的资源，可按用例写入真实软蒙版。"""
    image = Image.new(mode, (120, 100), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 15, 45, 95), fill=(175, 79, 19))
    draw.ellipse((68, 3, 105, 56), fill=(75, 60, 140))
    return image


def _paper(path, case, *, left=65, bottom=301, scale=1):
    """画出有同等外接几何但真实像素归属不同的位图组合。"""
    canvas = Canvas(str(path), pagesize=(620, 650))
    inner = (left + 17 * scale, bottom + 15 * scale, 160 * scale, 123 * scale)
    outer = (left, bottom, 197 * scale, 155 * scale)
    if case == "tight":
        inner = (left + 7 * scale, bottom + 6 * scale, 183 * scale, 143 * scale)
    if case == "protruding":
        inner = (left + 38 * scale, *inner[1:])
    if case == "scan":
        inner = (81, 380, 160, 110)
        outer = (0, 0, 620, 650)
    top = _pattern((31, 94, 210))
    low = _pattern((185, 50, 40))
    if case == "scan":
        top = Image.new("RGB", (620, 650), "white")
        logo = ImageDraw.Draw(top)
        logo.ellipse((95, 173, 218, 252), fill=(32, 129, 87))
        logo.rectangle((139, 191, 167, 231), fill=(233, 211, 42))
    if case == "masked-rim":
        top = top.convert("RGBA")
        mask = Image.new("L", top.size, 0)
        ImageDraw.Draw(mask).rectangle((4, 4, 115, 95), fill=255)
        top.putalpha(mask)
    if case == "soft-mask":
        top = top.convert("RGBA")
        ImageDraw.Draw(top).rectangle((36, 26, 80, 68), fill=(31, 94, 210, 0))

    def print_outer():
        """按用例应用路径剪切或混合模式，再绘制实心前层。"""
        canvas.saveState()
        if case == "clip-hole":
            clip = canvas.beginPath()
            clip.rect(*outer)
            clip.rect(left + 70 * scale, bottom + 45 * scale, 35 * scale, 37 * scale)
            canvas.clipPath(clip, stroke=0, fill=0, fillMode=0)
        if case == "multiply":
            canvas.setBlendMode("Multiply")
        if case == "distinct-tags":
            canvas._code.append("/Figure << /MCID 5 >> BDC")
        canvas.drawImage(ImageReader(top), *outer, mask="auto")
        if case == "distinct-tags":
            canvas._code.append("EMC")
        canvas.restoreState()

    if case == "reverse":
        print_outer()
    if case == "scan":
        canvas.drawInlineImage(Image.new("RGB", (1, 1), "white"), *inner)
    else:
        if case == "distinct-tags":
            canvas._code.append("/Figure << /MCID 4 >> BDC")
        canvas.drawImage(ImageReader(low), *inner)
        if case == "distinct-tags":
            canvas._code.append("EMC")
    if case != "reverse":
        print_outer()
    if case == "reused":
        canvas.drawImage(ImageReader(low), 370, 306, 160, 123)
    if case == "inside-text":
        canvas.drawString(left + 26, bottom + 50, "Foreground native label.")
    if case != "scan":
        for row in range(5):
            canvas.drawString(42, 68 + row * 16, f"Words independently editable outside the figure, row {row}.")
    canvas.save()


def _bodies(result):
    """从实际公开中间结果读取图体及其可编辑文字。"""
    return [
        child
        for block in result.to_dict()["pages"][0]["blocks"]
        if block["type"] == "image"
        for child in block["content"]
        if child["type"] == "image_body"
    ]


@pytest.mark.parametrize(("left", "bottom", "scale", "route"), [(65, 301, 1, "auto"), (69, 317, 1.13, "txt")])
@pytest.mark.parametrize("case", ["positive", "masked-rim"])
def test_later_solid_print_keeps_one_owner_and_native_prose(tmp_path, left, bottom, scale, route, case):
    """真正被完全遮掉的源图层无需再裁一次，独立可编辑正文和外图色彩仍在。"""
    path = tmp_path / "opaque-print.pdf"
    _paper(path, case, left=left, bottom=bottom, scale=scale)
    result = parse(path, keep_model_json=True, **({"parse_mode": route} if route == "txt" else {}))
    (body,) = _bodies(result)
    for suffix, output_format in (("html", "html"), ("md", "markdown")):
        output = tmp_path / f"result.{suffix}"
        result.export(output, output_format=output_format)
        rendered = output.read_text()
        assert body["image_path"] in rendered
        assert rendered.count("Words independently editable outside the figure") == 5
    with Image.open(tmp_path / body["image_path"]) as crop:
        r, g, b = crop.getpixel((crop.width // 2, crop.height // 2))
        assert b > max(r, g) + 90


@pytest.mark.parametrize("case", ["soft-mask", "clip-hole", "multiply", "reverse", "protruding", "inside-text"])
def test_retains_visible_or_independently_owned_inner_raster(tmp_path, case):
    """保留透明开孔、非普通混合、前置主体、突出边缘和独立原生文字。"""
    path = tmp_path / f"{case}.pdf"
    _paper(path, case)
    result = parse(path, parse_mode="txt", keep_model_json=True)
    assert len(_bodies(result)) == 2
    if case == "inside-text":
        assert [b["content"] for b in _bodies(result)].count("Foreground native label.") == 1


@pytest.mark.parametrize("case", ["positive", "tight"])
def test_filtered_owner_leaves_inner_candidate_untouched(tmp_path, case):
    """高优先级容器挡住外层后，下层候选仍可独立保留。"""
    path = tmp_path / "container.pdf"
    _paper(path, case)
    with PDFDocument(str(path)) as document:
        source = _collect_document_sources(document).page_sources[0]
    (relation,) = [info for info in source.raster_composite_infos if info.occluded_inner_bboxes]
    (inner,) = relation.occluded_inner_bboxes
    left, upper, right, _ = relation.bbox
    blocker = {"bbox": (left, upper - 2, right, (upper + inner[1]) / 2)}
    blocks, _ = _build_raster_image_blocks(source, [blocker], set())
    assert [block["bbox"] for block in blocks] == [inner]


def test_different_placement_of_hidden_resource_remains_independent(tmp_path):
    """同一资源在另一处真正在纸上绘制时，那个位置继续独立导出。"""
    path = tmp_path / "reuse.pdf"
    _paper(path, "reused")
    result = parse(path, parse_mode="txt", keep_model_json=True)
    bodies = _bodies(result)
    assert len(bodies) == 2
    assert any(box["bbox"][0] > 0.5 for box in bodies)


def test_small_scan_region_keeps_printed_logo_asset(tmp_path):
    """整页扫描覆盖单像素占位时，框内还有可独立使用的印刷图标，应保留局部裁片。"""
    from io import BytesIO

    path = tmp_path / "scan.pdf"
    _paper(path, "scan")
    result = parse(path, parse_mode="txt", keep_model_json=True)
    bodies = _bodies(result)
    assert len(bodies) == 2
    logo = min(bodies, key=lambda body: body["bbox"][2] - body["bbox"][0])
    with Image.open(BytesIO(result.assets[logo["image_path"]])) as bitmap:
        red, green, blue = bitmap.convert("RGB").getpixel((bitmap.width // 2, bitmap.height // 2))
    assert red > 170 and green > 165 and blue < 100


def test_marked_numbers_without_structure_tree_do_not_create_ownership(tmp_path):
    """单独的 MCID 没有真实结构图体时，仍按纸面判定重复裁片。"""
    path = tmp_path / "distinct-figures.pdf"
    _paper(path, "distinct-tags")
    with PDFDocument(str(path)) as document:
        assert document._get_structure() is None
        assert any(info.occluded_inner_bboxes for info in document.get_page_image_infos(0))
    result = parse(path, parse_mode="txt", keep_model_json=True)
    assert len(_bodies(result)) == 1


def test_independent_tagged_figure_retains_alt_and_its_original_asset():
    """真实结构树认领下层时，公开输出保留它的 Alt 和独立源图像。"""
    from io import BytesIO

    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject
    from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary

    builder = TaggedPdfBuilder(1)
    native_page = builder.pages[0]
    resources = pdf_dictionary()
    nodes = []
    for index, (label, rgb, rectangle) in enumerate(
        [
            ("Early source figure", (210, 18, 24), (66, 145, 127, 108)),
            ("Later source figure", (12, 45, 227), (55, 137, 150, 128)),
        ]
    ):
        image = DecodedStreamObject()
        image.set_data(bytes(rgb * 20))
        image.update(
            pdf_dictionary(
                Type=NameObject("/XObject"),
                Subtype=NameObject("/Image"),
                ColorSpace=NameObject("/DeviceRGB"),
                Height=NumberObject(4),
                Width=NumberObject(5),
                BitsPerComponent=NumberObject(8),
            )
        )
        resources[NameObject(f"/Im{index}")] = builder.writer._add_object(image)
        left, bottom, width, height = rectangle
        builder.commands[0].extend(
            f"/Figure <</MCID {index}>> BDC q {width} 0 0 {height} {left} {bottom} cm /Im{index} Do Q EMC\n".encode()
        )
        builder.next_mcid[0] += 1
        if index == 0:
            reference = pdf_dictionary(Type=NameObject("/MCR"), Pg=native_page.indirect_reference, MCID=NumberObject(index))
            nodes.append(builder.node("Figure", [reference], alt=label))
    native_page["/Resources"][NameObject("/XObject")] = resources
    nodes.append(builder.node("P", [builder.text("Native words outside the image.", x=25, y=58)]))
    result = parse(builder.finish(nodes), parse_mode="txt", keep_model_json=True)
    bodies = _bodies(result)
    assert len(bodies) == 2
    images = [block for block in result.model_json.pages[0] if block["type"] == "image"]
    (tagged,) = [block for block in images if block.get("_pdf_structure", {}).get("image_ready")]
    identity = tagged["_pdf_structure"]["node_id"]
    assert result.middle_json.extensions["docvortex_pdf_structure"]["nodes"][identity]["alt"] == "Early source figure"
    top_colors = []
    for body in bodies:
        with Image.open(BytesIO(result.assets[body["image_path"]])) as image:
            top_colors.append(image.getpixel((image.width // 2, image.height // 2)))
    assert any(r > max(g, b) + 130 for r, g, b in top_colors)
    assert any(b > max(r, g) + 130 for r, g, b in top_colors)


@pytest.mark.parametrize("obstacle", ["none", "body", "table", "large-photo", "plain-backdrop"])
def test_dominant_raster_accepts_only_subordinate_labelled_insets(obstacle):
    """同一印刷图面的图例与局部矢量标注可合成，独立正文、表及大幅照片不能被接管。"""
    from docvortex.analyzers.native.pdf.models import _LineItem, _PageSource
    from docvortex.analyzers.native.pdf.raster_repaints import resolve_raster_insets

    base = (10, 10, 290, 390)
    legend = (12, 160, 45, 370) if obstacle != "large-photo" else (12, 100, 150, 370)
    inset = {"type": "image", "bbox": (220, 300, 286, 380)}
    label = _LineItem("North", (230, 325, 263, 337), 0, 0)
    source = _PageSource((300, 400), [label], [], [], image_bboxes=[base, legend])
    source.inset_base_bboxes = frozenset([base]) if obstacle != "plain-backdrop" else frozenset()
    protected = [(50, 80, 150, 140)] if obstacle == "table" else []
    if obstacle == "body":
        source.lines.append(_LineItem("An independent paragraph", (50, 70, 190, 82), 0, 1))
    layers, graphics = resolve_raster_insets(source, source.image_bboxes, [inset], {id(inset): {0}}, protected)
    assert (layers, graphics) == (({legend}, {id(inset)}) if obstacle == "none" else (set(), set()))
