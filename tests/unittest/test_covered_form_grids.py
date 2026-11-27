"""验证完整父裁片只替代同纸面确证为纯网格的 Form。"""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from reportlab.lib.colors import Color, darkblue, red
from reportlab.pdfgen.canvas import Canvas

from docvortex import load_bundle, parse
from docvortex.analyzers.native.pdf import form_roles, graphics, pipeline
from docvortex.document.pdf import PDFDocument


def _clipped_gradient(canvas: Canvas, x: float, y: float, width: float, height: float) -> None:
    """用矩形裁剪限制 PDF 原生着色器，不将渐变转成位图。"""
    canvas.saveState()
    rectangle = canvas.beginPath()
    rectangle.rect(x, y, width, height)
    canvas.clipPath(rectangle, stroke=0, fill=0)
    canvas.linearGradient(x, y, x, y + height, (Color(0.95, 0.85, 0.9), Color(0.98, 0.75, 0.72)), extend=True)
    canvas.restoreState()


def _poster(case: str = "grid", *, scale: float = 1.0, font: str = "Helvetica", shift: int = 0) -> bytes:
    """按实际绘制顺序合成底色、图形、剪裁网格与可抽取图外文字。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(600 * scale, 820 * scale), pageCompression=0)
    canvas.scale(scale, scale)
    top = 505 if case == "partial_parent" else 745
    canvas.beginForm("underlay", 0, 55, 600, top)
    _clipped_gradient(canvas, 0, 55, 600, top - 55)
    canvas.endForm()
    has_grid_child = case in {"grid_with_empty_child", "grid_with_nested_text"}
    if has_grid_child:
        canvas.beginForm("unknown-child", 10, 15, 190, 72)
        if case == "grid_with_nested_text":
            canvas.setFont(font, 11)
            canvas.drawString(24, 58, "Nested source text")
        canvas.endForm()
    canvas.beginForm("grid", 0, 0, 205, 105)
    if case == "opaque":
        canvas.setFillColor(Color(0.1, 0.82, 0.5))
        canvas.rect(0, 0, 205, 105, fill=1, stroke=0)
    if case in {"clipped", "clipped_curve"}:
        limit = canvas.beginPath()
        width, height = (190, 95) if case == "clipped_curve" else (105, 70)
        limit.rect(0, 0, width, height)
        canvas.clipPath(limit, stroke=0, fill=0)
    if case == "wide_strokes":
        canvas.setLineWidth(13)
    grid = canvas.beginPath()
    for y in (8, 35, 60, 85):
        grid.moveTo(0, y)
        if case == "unequal":
            grid.lineTo(180 + y / 4, y)
        else:
            grid.lineTo(205, y)
    if case == "clipped_curve":
        grid.moveTo(5, 13)
        grid.curveTo(40, 63, 90, 11, 200, 75)
    canvas.drawPath(grid)
    if has_grid_child:
        canvas.doForm("unknown-child")
    canvas.endForm()
    if case in {"nested", "same_frame_real"}:
        canvas.beginForm("nested-chart", 0, 0, 205, 105)
        canvas.doForm("grid")
        canvas.setFont(font, 11)
        canvas.drawString(22, 65, "A distinct drawing")
        canvas.endForm()
    canvas.doForm("underlay")
    canvas.setFont(font, 11)
    canvas.drawString(25, 785, "An independent heading outside the gradient")
    canvas.drawString(25, 21, "Independent prose remains editable")
    for idx, origin in enumerate((55, 285, 515)):
        if idx != 1:
            canvas.saveState()
            canvas.translate(342 + shift, origin + 44)
            canvas.doForm("nested-chart" if case == "nested" else "grid")
            if case == "same_frame_real":
                canvas.doForm("nested-chart")
            canvas.restoreState()
        for series, color in enumerate((darkblue, red)):
            canvas.setStrokeColor(color)
            canvas.setLineWidth(1.4)
            chart = canvas.beginPath()
            y = origin + 75 + series * 40
            x = 37 + shift
            chart.moveTo(x, y)
            chart.curveTo(x + 50, y + 35, x + 75, y - 20, x + 120, y + 16)
            chart.curveTo(x + 170, y + 50, x + 195, y - 14, x + 235, y + 30)
            canvas.drawPath(chart)
        for label in range(4):
            canvas.drawString(37 + shift + label * 48, origin + 180 + 10 * (label % 2), f"Key {idx}.{label}")
    _clipped_gradient(canvas, 0, 284.5, 600, 1)
    canvas.showPage()
    canvas.save()
    writer = PdfWriter(clone_from=PdfReader(stream))
    resources = writer.pages[0]["/Resources"]
    background = resources["/XObject"]["/FormXob.underlay"]
    # ReportLab 将 Shading 注册在页资源；这里同时补齐实际调用流自己的资源字典。
    background["/Resources"][NameObject("/Shading")] = resources["/Shading"]
    background.set_data(background.get_data().replace(b"/Sh0 sh", b"/Sh1 sh"))
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def _inspect(data: bytes):
    """走真实 PDFium 采集和区域选择，返回可审候选及 Form 摘要。"""
    with PDFDocument(data) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
        form_roles.normalize_page_forms([source])
        return graphics._select_form_image_bboxes(source), source


def _form_image_boxes(case: str):
    """取经过最终 Form 认领的图裁片，为所有权回归核对实际坐标。"""
    candidates, source = _inspect(_poster(case))
    blocks, _claims = graphics._build_form_image_blocks(source, candidates, set())
    return candidates, source.form_infos, {block["bbox"] for block in blocks}


def test_covered_same_page_grid_crops_do_not_repeat_foreground_pixel_regions():
    """合成大图包含所有曲线，两个纯网格的局部物理裁片各只出现一次。"""
    candidates, forms, boxes = _form_image_boxes("grid")
    assert len(candidates) == 3
    assert len(boxes) == 1
    grids = [form for form in forms if form.parallel_rules_only]
    assert len(grids) == 2
    parent = next(iter(boxes))
    assert all(graphics._bbox_overlap_in_first(grid.bbox, parent) > 0.999 for grid in grids)


def test_same_xobject_drawn_outside_parent_is_kept():
    """等字节的 Form 在另一实际位置独立绘制，未被完整大框覆盖就保留。"""
    candidates, forms, boxes = _form_image_boxes("partial_parent")
    assert len(candidates) == 3
    grids = [form for form in forms if form.path_indices]
    assert len(grids) == 2 and grids[0].matrix != grids[1].matrix
    outside = min(grids, key=lambda grid: grid.bbox[1]).bbox
    inside = max(grids, key=lambda grid: grid.bbox[1]).bbox
    assert outside in boxes and inside not in boxes


@pytest.mark.parametrize("case", ["opaque", "clipped", "clipped_curve", "nested", "same_frame_real", "wide_strokes", "unequal"])
def test_distinct_or_incompletely_proven_form_paints_are_never_dropped(case):
    """保留异源同框、裁剪、宽笔画、不同端点统计条、底色或文字成员。"""
    candidates, forms, boxes = _form_image_boxes(case)
    assert set(candidates) == boxes
    if case == "clipped_curve":
        curve_forms = [form for form in forms if form.path_indices]
        assert len(curve_forms) == 2 and all(form.bbox in boxes and form.clip for form in curve_forms)
        assert all(not form.parallel_rules_only for form in curve_forms)
    if case == "same_frame_real":
        assert any(
            first.parent_id is None
            and second.parent_id is None
            and first.bbox == second.bbox
            and first.parallel_rules_only
            and bool(second.text_indices)
            for first in forms
            for second in forms
        )


@pytest.mark.parametrize("case", ["grid_with_empty_child", "grid_with_nested_text"])
def test_a_direct_grid_path_and_a_sibling_form_preserve_the_root_crop(case: str):
    """根 Form 同时有直接网格和嵌套调用时，缺少子层叶子也不能当作单个 Path。"""
    candidates, forms, boxes = _form_image_boxes(case)
    roots = [form for form in forms if form.parent_id is None and form.path_indices]
    assert len(roots) == 2
    for root in roots:
        children = [form for form in forms if form.parent_id == root.instance_id]
        assert len(root.path_indices) == 1 and len(children) == 1
        assert root.structure_valid and children[0].structure_valid
        assert bool(children[0].text_indices) == (case == "grid_with_nested_text")
        assert not children[0].path_indices and not children[0].image_bboxes
        assert not root.parallel_rules_only
        assert root.bbox in candidates and root.bbox in boxes


@pytest.mark.parametrize(
    "case,native,scale,font,shift",
    (
        ("grid", True, 1, "Helvetica", 0),
        ("grid", False, 1.4, "Courier", -15),
        ("partial_parent", True, 1, "Helvetica", 0),
        ("clipped_curve", True, 1, "Helvetica", 0),
        ("same_frame_real", False, 1, "Helvetica", 0),
        ("grid_with_empty_child", False, 1, "Helvetica", 0),
        ("grid_with_nested_text", True, 1, "Helvetica", 0),
    ),
)
def test_public_parse_preserves_proven_placements_and_editable_prose(
    case: str, native: bool, scale: float, font: str, shift: int, tmp_path: Path
):
    """真实公开路由需保留各独立摆放、图外文本与可回读图像结果包。"""
    data = _poster(case, scale=scale, font=font, shift=shift)
    candidates, source = _inspect(data)
    form_boxes, _claims = graphics._build_form_image_blocks(source, candidates, set())
    options = {"parse_mode": "txt", "pdf_structure": "auto"} if native else {}
    product = parse(data, keep_model_json=True, **options)
    middle = product.to_dict()
    assert len(middle["pages"]) == 1
    page = middle["pages"][0]
    published_crops = [
        part
        for block in page["blocks"]
        if block["type"] in {"image", "chart"}
        for part in block["content"]
        if part["type"] in {"image_body", "chart_body"}
    ]
    expected = {
        tuple(round(value / source.page_size[axis % 2], 3) for axis, value in enumerate(block["bbox"])) for block in form_boxes
    }
    actual = {tuple(part["bbox"]) for part in published_crops}
    assert expected <= actual
    if case == "grid":
        assert len(expected) == len(actual) == 1
    if case in {"grid_with_empty_child", "grid_with_nested_text"}:
        assert len(expected) == len(actual) == 3
    exported = product.export(tmp_path / "poster.html", output_format="html")
    html = exported.path.read_text()
    assert "An independent heading outside the gradient" in html
    assert "Independent prose remains editable" in html
    if case == "grid_with_nested_text":
        assert sum(part.get("content", "").count("Nested source text") for part in published_crops) == 2
        assert "Nested source text" in html
    for asset in exported.asset_paths:
        with Image.open(asset) as rendered:
            rendered.verify()
    bundle = product.save_bundle(tmp_path / "poster-bundle")
    assert load_bundle(bundle.path.parent).to_dict() == middle
