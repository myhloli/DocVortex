"""用等像素的 PDF 原件验证 Form 包装、独立图题及实色流程图。"""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

from docvortex.document.pdf import PDFDocument
from test_form_native_delivery import parse


PAGE_WIDTH = 600
PAGE_HEIGHT = 800
PAGE_BOX = (0, 0, PAGE_WIDTH, PAGE_HEIGHT)
CAPTION = "Figure 1. A complete visual example"
FIGURE_INK_BOX = (39.5, 469.5, 560.5, 724.5)
FLOWCHART_INK_BOX = (120, 285, 480, 683)


def _text_line(x, y, text, *, size=11):
    """生成一行标准 Helvetica PDF 文本指令。"""
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET"


def _prose(marker):
    """生成有独立身份且依次排列的十二条完整源行。"""
    lines = [(f"{marker} {index:02d}", 690 - index * 17) for index in range(1, 13)]
    return [_text_line(60, y, f"{label} explains how the document workflow should proceed.") for label, y in lines]


def _form(writer, content, bounds, resources):
    """注册 Form XObject，并保留它自己的 BBox 和资源表。"""
    stream = DecodedStreamObject()
    stream.set_data("\n".join(content).encode("ascii"))
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(FloatObject(value) for value in bounds),
            NameObject("/Resources"): resources,
        }
    )
    return writer._add_object(stream)


def _document(content, *, form_box, depth, matrix=(1, 0, 0, 1, 0, 0), outside=()):
    """在画布上原位绘制或额外包装同一内容，保持页根的外部图题。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    font_resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    resources = font_resources
    painted = content
    for level in range(depth):
        key = NameObject(f"/Fm{level}")
        reference = _form(writer, painted, form_box if level == 0 else PAGE_BOX, resources)
        resources = DictionaryObject(font_resources)
        resources[NameObject("/XObject")] = DictionaryObject({key: reference})
        painted = [f"{key} Do"]
    layout = " ".join(str(value) for value in matrix)
    stream = DecodedStreamObject()
    stream.set_data("\n".join((f"q {layout} cm", *painted, "Q", *outside)).encode("ascii"))
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _captioned_figure(depth):
    """生成有真实背景边框、十二行正文式文字和独立外置图题的整图。"""
    frame = "0.89 0.94 0.99 rg 0.2 0.35 0.6 RG 1 w 40 470 520 254 re B"
    inside = ["q", frame, "Q", *_prose("Inset line")]
    return _document(inside, form_box=(39, 469, 561, 725), depth=depth, outside=(_text_line(75, 447, CAPTION),))


def _flowchart():
    """生成四个纯填充节点和三个可见箭头，每个节点各含两条文字。"""
    content = []
    colors = ((0.8, 0.9, 1), (0.78, 0.93, 0.83), (0.98, 0.88, 0.76), (0.9, 0.83, 0.96))
    for index, rgb in enumerate(colors):
        bottom = 625 - 113 * index
        fill = " ".join(str(value) for value in rgb)
        content.extend(
            (
                f"q {fill} rg 120 {bottom} 360 58 re f Q",
                _text_line(145, bottom + 35, f"Stage {index + 1} confirms the next planned workflow action."),
                _text_line(145, bottom + 18, "The operator records the outcome before moving to the next step."),
            )
        )
        if index + 1 < len(colors):
            end = bottom - 55
            content.append(
                f"q 0.14 0.18 0.25 RG 2 w 300 {bottom} m 300 {end} l 294 {end + 8} m 300 {end} l 306 {end + 8} l S Q"
            )
    return _document(content, form_box=(118, 281, 482, 687), depth=1)


def _pixels(data):
    """渲染真实 PDF 并返回可做逐像素等价断言的位图副本。"""
    with PDFDocument(data) as document:
        rendered = document.render_page(0, scale=1).pil_image
        try:
            return rendered.size, rendered.mode, rendered.tobytes()
        finally:
            rendered.close()


def _visible_text(blocks):
    """读取全部非 image 输出的可见文本，同时容许原生类型和样式组织改变。"""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, list):
        return " ".join(_visible_text(item) for item in blocks)
    if isinstance(blocks, dict) and blocks.get("type") != "image":
        return _visible_text(blocks.get("content"))
    return ""


def _assert_whole_image(blocks, source_bbox, original):
    """要求一个图框覆盖完整绘制区，并限制超出原绘图区的裁片。"""
    left, bottom, right, top = source_bbox
    expected = (left, PAGE_HEIGHT - top, right, PAGE_HEIGHT - bottom)
    matches = []
    image_boxes = []
    for block in blocks:
        if block["type"] != "image":
            continue
        bbox = tuple(value * size for value, size in zip(block["bbox"], (PAGE_WIDTH, PAGE_HEIGHT) * 2, strict=True))
        image_boxes.append(bbox)
        if all(abs(actual - target) <= 2.0 for actual, target in zip(bbox, expected, strict=True)):
            matches.append(bbox)
    assert len(matches) == 1, (
        f"{original}: expected one complete image within 2 PDF points of {expected}, "
        f"got image boxes {image_boxes} and block types {[block['type'] for block in blocks]}"
    )


@pytest.mark.parametrize("depth", [1, 2], ids=["direct-figure-form", "wrapped-figure-form"])
def test_external_caption_keeps_text_rich_figure_intact(tmp_path, depth):
    """外置题注及十二行图内文字应保留原图边界，多包一层时渲染等像素。"""
    baseline = _captioned_figure(1)
    data = _captioned_figure(depth)
    original = tmp_path / f"captioned-figure-depth-{depth}.pdf"
    original.write_bytes(data)
    assert _pixels(data) == _pixels(baseline), original
    pages, _ = parse(data)
    _assert_whole_image(pages[0], FIGURE_INK_BOX, original)
    outside = _visible_text(pages[0])
    assert outside.count(CAPTION) == 1, (original, outside)
    assert "Inset line" not in outside, (original, outside)


@pytest.mark.parametrize(
    "depth, matrix",
    [(2, (1, 0, 0, 1, 0, 0)), (1, (1, 0, 0, 1, 24, -36)), (1, (0.8, 0, 0, 0.8, 48, 90))],
    ids=["extra-wrapper", "translated-wrapper", "scaled-wrapper"],
)
def test_equivalent_body_wrappers_deliver_native_text(tmp_path, depth, matrix):
    """纯正文在多一层 Form、平移或缩放后保持与同版直接绘制的文字及顺序。"""
    content = _prose("Reading marker")
    direct = _document(content, form_box=(40, 450, 570, 730), depth=0, matrix=matrix)
    wrapped = _document(content, form_box=(40, 450, 570, 730), depth=depth, matrix=matrix)
    control = tmp_path / "body-direct.pdf"
    original = tmp_path / "body-wrapped.pdf"
    control.write_bytes(direct)
    original.write_bytes(wrapped)
    assert _pixels(wrapped) == _pixels(direct), (original, control)
    wrapped_pages, _ = parse(wrapped)
    direct_pages, _ = parse(direct)
    assert not any(block["type"] == "image" for block in wrapped_pages[0]), (original, wrapped_pages[0])
    direct_text = " ".join(_visible_text(direct_pages[0]).split())
    wrapped_text = " ".join(_visible_text(wrapped_pages[0]).split())
    assert wrapped_text == direct_text, (original, wrapped_text, direct_text)
    markers = [f"Reading marker {index:02d}" for index in range(1, 13)]
    assert [wrapped_text.count(marker) for marker in markers] == [1] * len(markers), (original, wrapped_text)
    positions = [wrapped_text.index(marker) for marker in markers]
    assert positions == sorted(positions), (original, wrapped_text)


def test_uncaptioned_filled_flowchart_stays_one_image(tmp_path):
    """无图题且节点有两条文字的实色流程图应维持单个完整图框。"""
    data = _flowchart()
    original = tmp_path / "filled-flowchart-without-caption.pdf"
    original.write_bytes(data)
    pages, _ = parse(data)
    _assert_whole_image(pages[0], FLOWCHART_INK_BOX, original)
    assert "confirms the next planned workflow action" not in _visible_text(pages[0]), original


def test_local_bond_diagram_is_not_a_text_wrapper(tmp_path):
    """无图题的局部键线图即使所有标签可提取，也应保留图形整体。"""
    content = []
    for index in range(4):
        y = 650 - 45 * index
        content.extend(
            [
                _text_line(100, y, "CH3", size=10),
                _text_line(220, y, "C", size=10),
                _text_line(330, y, "OH", size=10),
                f"124 {y + 4} m 215 {y + 4} l S",
                f"230 {y + 4} m 325 {y + 4} l S",
            ]
        )
    data = _document(content, form_box=(90, 490, 370, 680), depth=1)
    (tmp_path / "local-bond-diagram.pdf").write_bytes(data)
    pages, _ = parse(data)
    assert any(block["type"] == "image" for block in pages[0])
    assert not any(block["type"] == "text" and "CH3" in str(block.get("content", "")) for block in pages[0])


@pytest.mark.parametrize("offset", [0, 12])
def test_adjacent_panel_with_page_sized_form_keeps_its_credit(offset):
    """相邻同比例卡片有完整底板时，声明覆盖全页也要保留单幅裁图及卡内来源续行。"""
    x = 55 + offset
    panel = [f"q .92 .92 .92 rg {x} 390 220 300 re f Q", f"q .25 .42 .65 rg {x + 55} 410 120 70 re f Q"]
    panel += [_text_line(x + 20, 640 - i * 26, f"Visual label {i}") for i in range(5)]
    outside = [
        f"q .80 .85 .90 rg {x + 238} 390 220 300 re f Q",
        _text_line(x + 38, 495, "Source: Editorial archive", size=9),
        _text_line(x + 38, 485, "additional attribution", size=9),
        _text_line(x, 340, "An independent paragraph outside the panel."),
    ]
    doc = _document(panel, form_box=PAGE_BOX, depth=1, outside=outside)
    pages, confidences = parse(doc)
    img = [b for b in pages[0] if b["type"] == "image" and "Visual label" in str(b.get("content"))]
    assert len(img) == 1 and all(word in img[0]["content"] for word in ("Source: Editorial archive", "additional attribution"))
    assert "Editorial archive" not in _visible_text(pages[0]) and "outside the panel" in _visible_text(pages[0])
    assert confidences[0] == 1.0


@pytest.mark.parametrize("as_prose", [False, True])
def test_ruled_symbol_sample_and_ruled_prose(as_prose):
    """同样的上下栏线，重复分立字形构成字体示例，普通连续正文仍可展开。"""
    lines = [
        _text_line(
            70,
            637 - i * 19,
            "A true paragraph reading continuously across the lane." if as_prose else "A     B     C     D     E     F",
            size=11,
        )
        for i in range(8)
    ]
    # 横线依据实际绘制边缘放置，区域之外的正文仍归入正常文本块。
    from docvortex.document.pdf import PDFDocument

    direct = _document(lines, form_box=PAGE_BOX, depth=1)
    with PDFDocument(direct) as pdf:
        f = pdf._extract_native_page(0).form_infos[0]
        x0, y0, x1, y1 = f.bbox
    edges = [
        f"{x0} {PAGE_HEIGHT - y0 + 4} m {x1} {PAGE_HEIGHT - y0 + 4} l S",
        f"{x0} {PAGE_HEIGHT - y1 - 4} m {x1} {PAGE_HEIGHT - y1 - 4} l S",
        _text_line(70, 385, "Ordinary text below the example."),
    ]
    pages, _ = parse(_document(lines, form_box=PAGE_BOX, depth=1, outside=edges))
    assert any(b["type"] == "image" for b in pages[0]) is (not as_prose)
    assert "Ordinary text below" in _visible_text(pages[0])
