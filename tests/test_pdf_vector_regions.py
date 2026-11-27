"""通过真实 PDF 操作验证相邻填色轮廓与短标签的所有权。"""

from io import BytesIO

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.content.spans import inline_span_plain_text


PAGE_WIDTH, PAGE_HEIGHT = 420, 560
LABELS = ("Cedar", "Elm", "Pine", "Birch", "Holly", "Alder")
TABLE_LABELS = ("Acer", "Juniper", "Taxus", "Lot A", "Lot B", "Lot C")
BODY = "The surrounding text stays independently searchable."


def _region_pdf(x, y, em, color, separate, mode, *, page_size=(PAGE_WIDTH, PAGE_HEIGHT)):
    """按测试输入改动图的位置、字级、填色及绘制方式，并构造几何反例。"""
    output = BytesIO()
    page = Canvas(output, pagesize=page_size)
    grid = (
        ((0, 0), (58, 3), (116, 0), (174, 4)),
        ((0, 42), (62, 39), (112, 44), (174, 42)),
        ((0, 84), (57, 82), (118, 85), (174, 84)),
    )
    page.setLineWidth(0.65)
    page.setFillColorRGB(*color)
    page.setStrokeColorRGB(0.96, 0.96, 0.96)
    paths = []
    for row in range(2):
        for column in range(3):
            points = (grid[row][column], grid[row][column + 1], grid[row + 1][column + 1], grid[row + 1][column])
            path = page.beginPath()
            path.moveTo(x + points[0][0], y + points[0][1])
            for (ax, ay), (bx, by) in zip(points, (*points[1:], points[0])):
                # 每条边的中间弯折为相邻面共用的边界。
                path.lineTo(x + (ax + bx) / 2 + (2 if ax == bx else 0), y + (ay + by) / 2 + 1)
                path.lineTo(x + (ax + 2 * bx) / 3, y + (ay + 2 * by) / 3)
                path.lineTo(x + bx, y + by)
            path.close()
            paths.append(path)
    if mode == "white-blank":
        paths.clear()
    if mode == "rectangular":
        for row in range(2):
            for column in range(3):
                page.rect(x + column * 58, y + row * 42, 58, 42, fill=1, stroke=1)
    elif mode == "unoutlined":
        for path in paths:
            page.drawPath(path, fill=1, stroke=0)
    elif separate:
        for path in paths:
            page.drawPath(path, fill=1, stroke=0)
        for path in reversed(paths):
            page.drawPath(path, fill=0, stroke=1)
    else:
        for path in paths:
            page.drawPath(path, fill=1, stroke=1)
    if mode != "decoration":
        page.setFillColorRGB(1, 1, 1)
        page.setFont("Helvetica", em)
        for index, label in enumerate(TABLE_LABELS if mode == "titled-table" else LABELS):
            row, column = divmod(index, 3)
            if mode == "concentrated":
                page.drawString(x + 21, y + 8 + index * 5.5, label)
            else:
                page.drawCentredString(x + 58 * column + 29, y + 42 * row + 19, label)
    page.setFillColorRGB(0, 0, 0)
    page.setFont("Times-Roman", em + 3)
    if mode == "titled-table":
        page.drawString(x, y + 102, "Table 1. Nursery species assigned to each lot")
    elif mode == "table-reference":
        page.drawString(x, y + 102, "Table 1 shows how the lists are grouped.")
    elif mode == "side-table":
        page.drawString(18, y + 102, "Table 1. Plant lots")
        page.setStrokeColorRGB(0, 0, 0)
        for row in range(3):
            for col in range(2):
                left, bottom = 18 + col * 65, y + 18 * (2 - row)
                page.rect(left, bottom, 65, 18)
                page.drawString(left + 4, bottom + 5, (("Lot", "Species"), ("1", "Acer"), ("2", "Taxus"))[row][col])
    if mode == "prose":
        page.drawString(x - 8, y + 40, "A paragraph runs uninterrupted across the printed shapes.")
        page.drawString(x - 8, y + 28, "The lines belong to readable continuous body text.")
    page.drawString(45, y - 17, BODY)
    page.drawString(45, y - 30, "This second line provides ordinary body context.")
    page.save()
    return output.getvalue()


def _block_text(block):
    """读取图片文字或普通文本的可搜索字符。"""
    content = block.get("content", "")
    return content if isinstance(content, str) else inline_span_plain_text(content)


@pytest.mark.parametrize(
    "x,y,em,color,separate",
    [(208, 468, 8, (0.15, 0.55, 0.53), False), (54, 298, 10, (0.58, 0.27, 0.41), True)],
)
def test_labeled_filled_outlines_become_one_public_image(x, y, em, color, separate, tmp_path):
    """复合填描及分开绘制的图均只导出一份，标签归图，外部正文仍能读取。"""
    result = parse(_region_pdf(x, y, em, color, separate, "labels"), parse_mode="txt", keep_model_json=True)
    model = result.model_json.pages[0]
    images = [block for block in model if block["type"] == "image"]
    assert len(images) == 1
    assert set(images[0]["content"].split()) == set(LABELS)
    assert not any(
        label in _block_text(block) for block in model if block["type"] not in {"image", "table"} for label in LABELS
    )
    assert any(BODY in _block_text(block) for block in model if block["type"] not in {"image", "table"})
    assert images[0]["bbox"] == pytest.approx(
        (x / PAGE_WIDTH, 1 - (y + 86) / PAGE_HEIGHT, (x + 176) / PAGE_WIDTH, 1 - y / PAGE_HEIGHT), abs=0.004
    )
    result.export(tmp_path / "map.html", output_format="html")
    html = (tmp_path / "map.html").read_text()
    assert BODY in html
    parent = next(block for block in result.to_dict()["pages"][0]["blocks"] if block["type"] == "image")
    assert result.assets[parent["content"][0]["image_path"]]


@pytest.mark.parametrize("mode", ["prose", "decoration", "rectangular", "unoutlined", "concentrated"])
def test_labeled_filled_outlines_reject_nonfigure_regions(mode):
    """贯穿正文、同色无标签装饰和规则底色表单均不得走新图体路径。"""
    result = parse(_region_pdf(186, 440, 8, (0.15, 0.55, 0.53), True, mode), parse_mode="txt", keep_model_json=True)
    model = result.model_json.pages[0]
    images = [block for block in model if block["type"] == "image"]
    assert not images
    if mode == "rectangular":
        assert any(block["type"] == "table" and "Cedar" in block["content"] for block in model)
    assert any(BODY in _block_text(block) for block in model if block["type"] not in {"image", "table"})
    if mode == "prose":
        assert any(
            "Aparagraphrunsuninterrupted" in _block_text(block).replace(" ", "")
            for block in model
            if block["type"] not in {"image", "table"}
        )


@pytest.mark.parametrize("separate", [False, True])
def test_nearby_table_title_protects_cut_corner_cell_text(separate):
    """有明确表题、同栏近邻的切角表格保留六个可搜索文本单元。"""
    result = parse(
        _region_pdf(180, 350, 8, (0.15, 0.55, 0.53), separate, "titled-table"), parse_mode="txt", keep_model_json=True
    )
    model = result.model_json.pages[0]
    assert all(block["type"] != "image" for block in model)
    extracted = " ".join(_block_text(block) for block in model)
    assert "Table 1. Nursery species" in extracted
    assert all(label in extracted for label in TABLE_LABELS)


@pytest.mark.parametrize("mode", ["table-reference", "side-table"])
def test_table_title_competition_keeps_figure_in_its_own_region(mode):
    """同行旁栏表题和普通叙述引用都不能否决真正的分面图。"""
    result = parse(_region_pdf(186, 350, 8, (0.15, 0.55, 0.53), False, mode), parse_mode="txt", keep_model_json=True)
    model = result.model_json.pages[0]
    (figure,) = (block for block in model if block["type"] == "image")
    assert set(figure["content"].split()) == set(LABELS)
    if mode == "side-table":
        assert any(block["type"] == "table" and "Taxus" in block["content"] for block in model)
