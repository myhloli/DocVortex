"""以真实 PDF 与公开导出覆盖图体和图例的共同所有权。"""

from io import BytesIO

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.content.spans import inline_span_plain_text


_LABELS = ("Fern creek", "Cedar grove", "High ridge", "City station", "Hill path", "Main terminal")
_OUTSIDE = "Keep the independent text outside the drawing."
_TAIL = "Distances shown on a separate line"
_TITLE = "Independent collection title"
_FOOTER = "Prepared by the municipal archive"
_ACROSS = ("Xb", "Wc", "Vd", "Ue", "Tf", "Sg", "Rh")
_DOWN = ("Rb", "Sc", "Td", "Ue", "Vf", "Wg", "Xh")


def _synthetic_partition(panel_type, obstruction, *, scale, offset, index=False):
    """构造带图内白底或图外横规则的矢量图及原生标签。"""
    raw = BytesIO()
    canvas = Canvas(raw, pagesize=(560 * scale, 520 * scale))
    canvas.scale(scale, scale)
    canvas.translate(*offset)
    canvas.setLineWidth(1)
    left, bottom, right, top = (35, 55, 510 if panel_type == "inset" else 365, 445)
    if panel_type == "side":
        # 有延伸填色的实际图主体，与仅排正文的边框不一样。
        for i in range(5):
            path = canvas.beginPath()
            coords = [
                (left, bottom),
                (right, bottom),
                (right, top),
                (left + 70, top),
                (left + 50, top - 20 + i),
                (left + 35, top + 3 - i),
                (left, top),
            ]
            path.moveTo(*coords[0])
            for x, y in coords[1:]:
                path.lineTo(x, y)
            path.close()
            canvas.setFillColorRGB(0.81, 0.91 - i * 0.06, 0.89)
            canvas.drawPath(path, stroke=1, fill=1)
    for i in range(126):
        x = left + 6 + (i % 13) * (right - left - 30) / 13
        y = bottom + 10 + (i // 13) * 37
        canvas.setStrokeColorRGB(0.25 + 0.3 * (i % 2), 0.35, 0.65)
        path = canvas.beginPath()
        path.moveTo(x, y)
        if panel_type == "side":
            for dx, dy in ((2, 4), (5, 9), (9, 12), (11, 17), (12, 18)):
                path.lineTo(x + dx, y + dy)
        path.lineTo(x + 13, y + 14 + i % 4)
        path.lineTo(x + 21, y + 2 - i % 4)
        canvas.drawPath(path)
        if i % 6 == 0 and not (obstruction == "explicit-index" and 83 <= y <= 245):
            canvas.setFillColorRGB(0.15, 0.18, 0.25)
            canvas.setFont("Helvetica", 8)
            canvas.drawString(x + 2, y + 18, f"Station {i}")
    canvas.setStrokeColorRGB(0.1, 0.15, 0.2)
    if panel_type == "inset":
        panel_left, panel_bottom, panel_right, panel_top = (365, 245, right + 8, top + 8)
        canvas.setFillColorRGB(1, 1, 1)
        canvas.rect(panel_left, panel_bottom, panel_right - panel_left, panel_top - panel_bottom, stroke=0, fill=1)
        canvas.rect(panel_left, panel_bottom, panel_right - panel_left, panel_top - panel_bottom, stroke=1, fill=0)
        frame = canvas.beginPath()
        frame.rect(left, bottom, right - left, top - bottom)
        frame.rect(left + 2, bottom + 2, right - left - 4, top - bottom - 4)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.drawPath(frame, stroke=0, fill=1)
    else:
        panel_left, panel_bottom, panel_right, panel_top = (374, 230, 511, 410)
        canvas.line(panel_left, panel_top, panel_right, panel_top)
        canvas.line(panel_left, panel_bottom, panel_right, panel_bottom)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColorRGB(0.1, 0.15, 0.2)
    canvas.drawString(panel_left + 25, panel_top - 28, "Routes and services")
    for i, label in enumerate(_LABELS):
        y = panel_top - 54 - i * 17
        if not obstruction == "missing_marks":
            canvas.setStrokeColorRGB(0.2 + 0.55 * (i % 2), 0.17, 0.75 - 0.47 * (i % 2))
            canvas.setLineWidth(2)
            canvas.line(panel_left + 6, y + 3, panel_left + 22, y + 3)
        canvas.drawString(panel_left + 27, y, label)
    canvas.drawString(panel_left + 12, panel_bottom + 12, _TAIL)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(left, 24, _OUTSIDE)
    if obstruction == "ambiguous-corners":
        _draw_ambiguous_corner_index(canvas, left, bottom, right, top)
    elif index:
        _draw_printed_index(canvas, left, bottom, right, top, obstruction)
    if obstruction == "explicit-index":
        # 目录使用 PDF 原生字和重复页码列，在新图候选之前获得归属。
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawCentredString(280, 225, "Table of Contents")
        canvas.setFont("Courier", 9)
        for i, label in enumerate(
            ("Northern area", "Park information", "Creek access", "Eastern area", "Depot information", "Town center")
        ):
            canvas.drawString(62, 202 - 17 * i, f"{label:.<68}{11 + i}")
    if obstruction == "crossing":
        y = bottom + 70 if panel_type == "inset" else panel_bottom + 12
        canvas.drawString(
            left - 18 if panel_type == "inset" else panel_left - 24, y, "Text beginning outside crosses the drawing boundary."
        )
    canvas.save()
    return raw.getvalue()


def _draw_printed_index(canvas, left, bottom, right, top, obstruction):
    """绘制双轴配对索引，以及位于整幅印刷边界外的独立题名。"""
    canvas.setFillColorRGB(0.2, 0.14, 0.38)
    canvas.rect(left - 16, top, right - left + 32, 16, stroke=0, fill=1)
    canvas.rect(left - 16, bottom - 16, right - left + 32, 16, stroke=0, fill=1)
    canvas.rect(left - 16, bottom, 16, top - bottom, stroke=0, fill=1)
    canvas.rect(right, bottom, 16, top - bottom, stroke=0, fill=1)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColorRGB(0.95, 0.91, 0.85)
    for i, label in enumerate(_ACROSS):
        x = left + (right - left) * (i + 0.5) / len(_ACROSS)
        canvas.drawCentredString(x, top + 6, label)
        if obstruction != "one-sided":
            canvas.drawCentredString(x, bottom - 10, label)
    for i, label in enumerate(_DOWN):
        y = bottom + (top - bottom) * (i + 0.5) / len(_DOWN)
        for x in (left - 8, right + 8):
            canvas.drawCentredString(x, y - 2, label)
    if obstruction == "footer":
        canvas.drawString(left + 44, bottom - 10, _FOOTER)
    canvas.setFont("Helvetica-Bold", 14)
    canvas.setFillColorRGB(0.1, 0.15, 0.2)
    canvas.drawString(left, top + 33, _TITLE)


def _draw_ambiguous_corner_index(canvas, left, bottom, right, top):
    """四个交角重复匹配，同时放四个跨内框的未匹配字行制造总数抵消。"""
    canvas.setFillColorRGB(0.2, 0.14, 0.38)
    canvas.rect(left - 16, top, right - left + 32, 16, stroke=0, fill=1)
    canvas.rect(left - 16, bottom - 16, right - left + 32, 16, stroke=0, fill=1)
    canvas.rect(left - 16, bottom - 16, 16, top - bottom + 32, stroke=0, fill=1)
    canvas.rect(right, bottom - 16, 16, top - bottom + 32, stroke=0, fill=1)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColorRGB(0.95, 0.91, 0.85)
    for i, label in enumerate(("Q", *_ACROSS[:6], "Q")):
        x = left - 8 + (right - left + 16) * i / 7
        for y in (top + 6, bottom - 10):
            canvas.drawCentredString(x, y, label)
    for i, label in enumerate(_DOWN[:6], start=1):
        y = bottom - 8 + (top - bottom + 16) * i / 7
        for x in (left - 8, right + 8):
            canvas.drawCentredString(x, y - 2, label)
    canvas.setFillColorRGB(0.1, 0.15, 0.2)
    for i, (x, y) in enumerate(((145, top - 3), (255, top - 3), (145, bottom - 3), (255, bottom - 3))):
        canvas.drawString(x, y, f"Archive reference {i}")


def _text(block):
    """获取公共图体或块中的全文，供所有权核对。"""
    value = block.get("content", "")
    return value if isinstance(value, str) else inline_span_plain_text(value)


@pytest.mark.parametrize(("scale", "offset"), [(1.0, (0, 0)), (0.8, (9, 11))])
@pytest.mark.parametrize("panel_type", ["inset", "side"])
def test_graphic_partitions_export_complete_legend_once(tmp_path, panel_type, scale, offset):
    """原页改变坐标和字号后，完整图例仍唯一归属图像，并通过 HTML 公开可下载。"""
    data = _synthetic_partition(panel_type, None, scale=scale, offset=offset)
    result = parse(data, parse_mode="txt", keep_model_json=True)
    model = result.model_json.pages[0]
    image_owners = [block for block in model if block["type"] == "image" and all(label in _text(block) for label in _LABELS)]
    assert len(image_owners) == 1
    (owner,) = image_owners
    assert _TAIL in _text(owner) and "Routes and services" in _text(owner)
    assert all(label not in _text(block) for block in model if block is not owner for label in _LABELS)
    assert any(_OUTSIDE in _text(block) and block["type"] != "image" for block in model)
    public_images = [
        child
        for block in result.to_dict()["pages"][0]["blocks"]
        if block["type"] == "image"
        for child in block["content"]
        if child["type"] == "image_body"
    ]
    public_owner = next(child for child in public_images if child["bbox"] == owner["bbox"])
    html = tmp_path / "map.html"
    result.export(html, output_format="html")
    assert html.read_text().count(public_owner["image_path"]) == 1
    with Image.open(tmp_path / public_owner["image_path"]) as image:
        image.verify()
    if panel_type == "side":
        assert any(child["bbox"][2] < owner["bbox"][0] and "Station" in child["content"] for child in public_images)
        assert all(
            not (child["bbox"][0] < owner["bbox"][2] and owner["bbox"][0] < child["bbox"][2])
            for child in public_images
            if child is not public_owner
        )
    else:
        assert len(public_images) == 1
        assert "Station" in _text(owner)


@pytest.mark.parametrize(("panel_type", "obstruction"), [("side", "missing_marks"), ("inset", "crossing")])
def test_graphic_partitions_require_symbols_and_preserve_crossing_text(panel_type, obstruction):
    """无配对色标的文字侧栏和跨印刷边界的正文不得被新图例成组认领。"""
    data = _synthetic_partition(panel_type, obstruction, scale=1, offset=(0, 0))
    result = parse(data, parse_mode="txt", keep_model_json=True)
    model = result.model_json.pages[0]
    assert any(_OUTSIDE in _text(block) and block["type"] != "image" for block in model)
    if obstruction == "crossing":
        # 原图形分支可能包住延伸字，但只有新规则使用精确的双矩形边界。
        assert not any(block["type"] == "image" and block["bbox"] == [0.062, 0.144, 0.911, 0.894] for block in model)
        assert sum(_text(block).count("Text beginning outside crosses") for block in model) == 1
    else:
        assert not any(block["type"] == "image" and all(label in _text(block) for label in _LABELS) for block in model)


@pytest.mark.parametrize("obstruction", [None, "one-sided", "footer"])
def test_printed_index_frame_requires_paired_coordinates_and_no_independent_footer(tmp_path, obstruction):
    """明暗底板的双轴索引随图输出，单边刻度或真实页脚阻止外扩，图外题名仍独立。"""
    result = parse(
        _synthetic_partition("inset", obstruction, scale=1, offset=(0, 0), index=True),
        keep_model_json=True,
    )
    model = result.model_json.pages[0]
    (owner,) = [block for block in model if block["type"] == "image" and all(label in _text(block) for label in _LABELS)]
    assert all(_TITLE not in _text(block) for block in model if block["type"] == "image")
    assert sum(_text(block).count(_TITLE) for block in model) == 1
    assert sum(_text(block).count(_OUTSIDE) for block in model) == 1
    if obstruction:
        assert owner["bbox"] == [0.062, 0.144, 0.911, 0.894]
        assert all(label not in _text(owner) for label in _ACROSS)
        if obstruction == "footer":
            assert sum(_text(block).count(_FOOTER) for block in model if block["type"] != "image") == 1
        return
    assert owner["bbox"] == [0.034, 0.113, 0.939, 0.925]
    assert all(_text(owner).count(label) == 2 for label in {*_ACROSS, *_DOWN} - {"Ue"})
    assert _text(owner).count("Ue") == 4
    assets = [
        child["image_path"]
        for block in result.to_dict()["pages"][0]["blocks"]
        if block["type"] == "image"
        for child in block["content"]
        if child["type"] == "image_body"
    ]
    (asset,) = assets
    html = tmp_path / "indexed-map.html"
    result.export(html, output_format="html")
    assert html.read_text().count(asset) == 1
    with Image.open(tmp_path / asset) as rendered:
        clear_corner = rendered.convert("RGB").getpixel((4, 4))
    assert max(abs(value - expected) for value, expected in zip(clear_corner, (51, 36, 97), strict=True)) <= 12


def test_printed_border_does_not_trade_unowned_source_lines_for_duplicate_corners():
    """角落重复、跨框字行遗漏即使数量相抵，公开产物也不能把所有框外字行据为一图。"""
    result = parse(
        _synthetic_partition("inset", "ambiguous-corners", scale=1, offset=(0, 0)), parse_mode="txt", keep_model_json=True
    )
    images = [b for b in result.model_json.pages[0] if b["type"] == "image"]
    assert not any(b["bbox"] == [0.034, 0.113, 0.939, 0.925] for b in images)


@pytest.mark.parametrize("route", ["default", "native"])
def test_confirmed_contents_claims_rows_before_graphic_partition(tmp_path, route):
    """绝对目录框阻止闭框候选再次认领条目，公开目录可搜索且出现一次。"""
    data = _synthetic_partition("inset", "explicit-index", scale=1, offset=(0, 0))
    options = {"parse_mode": "txt"} if route == "native" else {}
    result = parse(data, keep_model_json=True, **options)
    model = result.model_json.pages[0]
    (contents,) = [block for block in model if block["type"] == "index"]
    assert contents["bbox"] == pytest.approx([0.111, 0.597, 0.786, 0.780], abs=0.003)
    labels = ("Northern area", "Park information", "Creek access", "Eastern area", "Depot information", "Town center")
    assert all(label in _text(contents) for label in labels)
    assert all(label not in _text(block) for block in model if block["type"] == "image" for label in labels)
    html_path = tmp_path / "contents.html"
    result.export(html_path, output_format="html")
    html = html_path.read_text()
    assert all(html.count(label) == 1 for label in labels)
