"""验证页面 Form 展开、真实整图保护和调用实例的成员隔离。"""

from collections import Counter
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject
from reportlab.pdfgen.canvas import Canvas

from docvortex.analyzers.native import PdfModel
from docvortex.document.pdf import PDFDocument

FIXTURES = Path(__file__).parent / "pdfs/form_containers"


def _predict(data: bytes | Path) -> list[list[dict]]:
    """通过真实原生入口解析原件，保留全文上下文。"""
    with PDFDocument(str(data) if isinstance(data, Path) else data) as document:
        return PdfModel().predict(document)


def _visible(value) -> str:
    """提取样式包装中的可见文字，以便核对成员而非块计数。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_visible(item) for item in value)
    return _visible(value.get("content", "")) if isinstance(value, dict) else ""


def _body_pdf(
    depth: int, *, rotation: int = 0, shift: float = 0, font: str = "Helvetica", size: float = 11, columns: bool = False
) -> bytes:
    """生成直接绘制或多层 Form 包装的同一正文、图形和表格页面。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(600, 800))
    canvas.setFont(font, size)
    for index in range(12):
        if columns:
            for x in (50, 330):
                canvas.drawString(x + shift, 730 - index * 16, f"Column {index} contains native prose.")
        else:
            canvas.drawString(
                50 + shift, 730 - index * 16, f"Native paragraph row {index}: independently readable page content."
            )
    canvas.drawString(50 + shift, 490, "A second paragraph remains outside the chart and table.")
    canvas.drawString(50 + shift, 474, "Its source members must not become figure labels.")
    canvas.beginForm("figure", 0, 0, 180, 100)
    canvas.setFont(font, 9)
    canvas.rect(0, 0, 180, 100)
    canvas.line(20, 20, 20, 85)
    canvas.line(20, 20, 160, 20)
    canvas.line(20, 20, 60, 70)
    canvas.line(60, 70, 160, 35)
    canvas.drawString(25, 80, "Actual chart label")
    canvas.drawString(25, 60, "A second chart label")
    canvas.endForm()
    canvas.saveState()
    canvas.translate(50 + shift, 310)
    canvas.doForm("figure")
    canvas.restoreState()
    canvas.setFont(font, 10)
    canvas.drawString(50 + shift, 292, "Figure 1: independent chart")
    for y in (80, 110, 140, 170):
        canvas.line(50 + shift, y, 350 + shift, y)
    for x in (50, 200, 350):
        canvas.line(x + shift, 80, x + shift, 170)
    for row in range(3):
        for column in range(2):
            canvas.drawString(60 + shift + 150 * column, 90 + 30 * row, f"Cell {row}/{column}")
    canvas.save()
    wrapped = _wrap_page(stream.getvalue(), depth)
    if not rotation:
        return wrapped
    writer = PdfWriter(clone_from=PdfReader(BytesIO(wrapped)))
    writer.pages[0].rotate(rotation)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _wrap_page(data: bytes, depth: int) -> bytes:
    """把真实页面内容封装为多层调用，避免 ReportLab 不支持嵌套 beginForm 的限制。"""
    if depth == 0:
        return data
    writer = PdfWriter(clone_from=PdfReader(BytesIO(data)))
    page = writer.pages[0]
    for level in range(depth):
        form = DecodedStreamObject()
        form.set_data(page.get_contents().get_data())
        form.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([FloatObject(value) for value in page.mediabox]),
                NameObject("/Resources"): page["/Resources"],
            }
        )
        key = NameObject(f"/PageContainer{level}")
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({key: writer._add_object(form)})}
        )
        contents = DecodedStreamObject()
        contents.set_data(f"q {key} Do Q".encode())
        page[NameObject("/Contents")] = writer._add_object(contents)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_page_form_wrapping_preserves_semantic_blocks(depth: int) -> None:
    """页面包装不得改变正文、整图、表格、图注或成员的公开模型。"""
    direct = _predict(_body_pdf(0))
    wrapped = _predict(_body_pdf(depth))
    assert wrapped == direct
    assert Counter(block["type"] for block in wrapped[0])["image"] == 1
    assert any(block["type"] == "table" for block in wrapped[0])


@pytest.mark.parametrize(
    "rotation,shift,font", [(0, 20, "Courier"), (90, 0, "Helvetica"), (180, 0, "Times-Roman"), (270, 0, "Helvetica")]
)
def test_page_form_wrapping_generalizes_layout(rotation: int, shift: float, font: str) -> None:
    """独立改变字体、位置和旋转后，页面包装仍与直接绘制等价。"""
    assert _predict(_body_pdf(2, rotation=rotation, shift=shift, font=font)) == _predict(
        _body_pdf(0, rotation=rotation, shift=shift, font=font)
    )


def test_issue23_report_keeps_six_charts_and_native_tables() -> None:
    """六张独立图和原生正文表格恢复，原本栅格化的表格仍是一张图。"""
    pages = _predict(FIXTURES / "research-report.pdf")
    assert len(pages) == 5
    assert sum(block["type"] == "image" for block in pages[1]) == 6
    assert sum(block["type"] == "caption" for block in pages[1]) == 6
    text = "".join(_visible(b["content"]) for b in pages[2] if b["type"] != "image")
    assert "盈利预测调整说明" in text
    assert "预计" in text
    assert sum(block["type"] == "image" for block in pages[2]) == 1
    for number, page in enumerate(pages[3:], 4):
        assert any(block["type"] == "table" for block in page)
        if number == 4:
            assert any(block["type"] == "text" for block in page)
        else:
            # 年份表头已归入完整表格，不能再用原先游离表头的 text 数量作为正文存在性断言。
            assert any(block["type"] == "paragraph_title" and "财务预测与估值" in _visible(block["content"]) for block in page)
            tables = [block for block in page if block["type"] == "table"]
            assert len(tables) == 4
            assert all("2022" in _visible(block["content"]) and "2026E" in _visible(block["content"]) for block in tables)
        assert not any(block["type"] == "image" and block["bbox"][3] - block["bbox"][1] > 0.7 for block in page)


def test_issue23_paper_preserves_body_tables_and_real_figures() -> None:
    """全文不再整页认领为图，三个图形页的真实图形保持完整。"""
    pages = _predict(FIXTURES / "2022.emnlp-main.614.pdf")
    assert len(pages) == 18
    for page in pages:
        assert any(block["type"] in {"text", "table"} for block in page)
        assert not any(block["type"] == "image" and block["bbox"][3] - block["bbox"][1] > 0.75 for block in page)
    for index, expected in [(3, 1), (8, 1), (15, 2)]:
        assert sum(block["type"] == "image" for block in pages[index]) == expected
    assert any(block["type"] == "table" for block in pages[2])
    assert "Chelsea Finn" in "".join(_visible(b["content"]) for b in pages[10] if b["type"] == "text")


def _dense_figure_pdf(kind: str) -> bytes:
    """生成具有整页声明框和连续长标签的图，验证标签不能充当展开正文。"""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    output = BytesIO()
    canvas = Canvas(output, pagesize=(600, 800))
    canvas.setFont("Helvetica", 11)
    for index in range(12):
        canvas.drawString(50, 730 - index * 16, f"Dense figure label {index}: descriptive text inside the drawing.")
    full = kind.startswith("full_")
    kind = kind.removeprefix("full_")
    conflict = kind == "conflict"
    if conflict:
        kind = "plot"
    if kind in {"plot", "flowchart"}:
        canvas.rect(*((0, 0, 600, 800) if full else (30, 250, 540, 520)), fill=0)
        if kind == "plot":
            path = canvas.beginPath()
            path.moveTo(40, 270)
            for x, y in [(80, 520), (160, 330), (220, 680), (300, 470), (420, 720), (550, 390)]:
                path.lineTo(x, y)
            canvas.drawPath(path)
        else:
            for index in range(6):
                canvas.rect(45 + 70 * index, 280 + 25 * index, 60, 40)
                canvas.line(105 + 70 * index, 300 + 25 * index, 115 + 70 * index, 345 + 25 * index)
    elif kind in {"raster", "panels"}:
        image = ImageReader(Image.new("RGB", (40, 40), "lightgray"))
        for index in range(4):
            canvas.drawImage(image, *((150 * index, 0, 150, 800) if full else (30 + 140 * index, 250, 130, 520)))
        if kind == "panels":
            canvas.drawString(50, 230, "Figure 1: common caption for all panels")
    if conflict:
        image = ImageReader(Image.new("RGB", (40, 40), "lightgray"))
        for index, x in enumerate((60, 300)):
            canvas.drawImage(image, x, 300, 130, 100)
            canvas.drawString(x, 280, f"Figure {index + 1}: panel inside unified core")
    canvas.save()
    return _wrap_page(output.getvalue(), 1)


@pytest.mark.parametrize(
    "kind", ["plot", "flowchart", "raster", "panels", "full_plot", "full_flowchart", "full_raster", "conflict"]
)
def test_full_page_graphic_with_dense_labels_stays_whole(kind: str) -> None:
    """统一绘图核心、多栅格图和共同图题均优先保护，满页图不能凭长标签拆分。"""
    page = _predict(_dense_figure_pdf(kind))[0]
    assert sum(block["type"] == "image" for block in page) == 1
    assert not any(block["type"] == "text" and "Dense figure label" in _visible(block["content"]) for block in page)


def _repeated_figure_pdf() -> bytes:
    """同一资源调用两次，并在图内空间叠放不属于 Form 的外部正文。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(600, 800))
    canvas.beginForm("Reusable", 0, 0, 180, 100)
    canvas.rect(0, 0, 180, 100)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(10, 80, "Internal label one")
    canvas.drawString(10, 60, "Internal label two")
    canvas.endForm()
    for x in (50, 330):
        canvas.saveState()
        canvas.translate(x, 300)
        canvas.doForm("Reusable")
        canvas.restoreState()
    canvas.setFont("Helvetica", 8)
    canvas.drawString(60, 330, "External body overlaps the first figure")
    canvas.save()
    return stream.getvalue()


def test_repeated_calls_have_disjoint_owned_members_after_close() -> None:
    """重复 Do 的字符和路径编号独立，快照在文档关闭后仍可安全读取。"""
    with PDFDocument(_repeated_figure_pdf()) as document:
        forms = document._extract_native_page(0).form_infos
    assert len(forms) == 2
    assert all(form.structure_valid for form in forms)
    assert forms[0].occurrence != forms[1].occurrence
    assert forms[0].text_indices and forms[1].text_indices
    assert forms[0].text_indices.isdisjoint(forms[1].text_indices)
    assert forms[0].path_indices.isdisjoint(forms[1].path_indices)
    assert forms[0].matrix != forms[1].matrix


def test_external_overlap_is_not_claimed_by_form_image() -> None:
    """对象之外的叠放正文即使完全位于图框内，也不能被空间规则吞并。"""
    page = _predict(_repeated_figure_pdf())[0]
    assert sum(block["type"] == "image" for block in page) == 2
    assert "External body overlaps" in "".join(_visible(block["content"]) for block in page if block["type"] == "text")


@pytest.mark.parametrize("clipped,scale", [(False, 1), (True, 1), (False, 0.75)])
def test_page_form_crop_scale_and_clip_preserve_direct_output(clipped: bool, scale: float) -> None:
    """CropBox、缩放与显式剪裁下仍与直接绘制等价，不重新引入框外成员。"""
    from pypdf import Transformation

    variants = []
    for depth in (0, 2):
        writer = PdfWriter(clone_from=PdfReader(BytesIO(_body_pdf(depth))))
        page = writer.pages[0]
        page.add_transformation(Transformation().scale(scale))
        if scale != 1:
            page.mediabox.upper_right = (600 * scale, 800 * scale)
        if clipped:
            page.cropbox.lower_left = (20, 20)
            page.cropbox.upper_right = (580, 780)
            stream = DecodedStreamObject()
            stream.set_data(b"q 20 20 560 760 re W n\n" + page.get_contents().get_data() + b"\nQ")
            page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        variants.append(_predict(output.getvalue()))
    assert variants[0] == variants[1]


def test_broken_resources_disable_expansion_without_losing_native_content() -> None:
    """未解析资源令结构关联失败时保留整图，PDFium 恢复的内容仍参与旧流程。"""
    writer = PdfWriter(clone_from=PdfReader(BytesIO(_body_pdf(1))))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b"\n/MissingResource Do")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    with PDFDocument(output.getvalue()) as document:
        forms = document._extract_native_page(0).form_infos
        assert forms and not any(form.structure_valid for form in forms)
    assert sum(block["type"] == "image" for block in _predict(output.getvalue())[0]) == 1


def test_python_and_rust_character_ownership_are_equivalent() -> None:
    """真实重复调用页的 Rust 批量关联与逐字符参考路径得到相同自有证据。"""
    from unittest.mock import patch

    from docvortex._compute_backend import get_native

    if get_native() is None:
        pytest.skip("当前环境没有原生扩展")
    with PDFDocument(_repeated_figure_pdf()) as document:
        native = document._extract_native_page(0).form_infos
    with patch("docvortex.document.pdf.form_structure.get_native", return_value=None):
        with PDFDocument(_repeated_figure_pdf()) as document:
            reference = document._extract_native_page(0).form_infos
    assert native == reference


def test_underlay_rectangle_is_removed_but_public_geometry_is_preserved() -> None:
    """确认底层简单填色无需输出图片，原始 Form 查询仍保留该矩形。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(600, 800))
    canvas.beginForm("Underlay", 0, 0, 600, 800)
    canvas.setFillColorRGB(0.95, 0.95, 0.95)
    canvas.rect(0, 0, 600, 800, stroke=0, fill=1)
    canvas.endForm()
    canvas.doForm("Underlay")
    canvas.setFont("Helvetica", 11)
    for index in range(8):
        canvas.drawString(50, 700 - 16 * index, f"External paragraph {index} remains readable over the underlay.")
    canvas.save()
    with PDFDocument(stream.getvalue()) as document:
        before = document._extract_native_page(0).form_bboxes
        page = PdfModel().predict(document)[0]
        assert document._extract_native_page(0).form_bboxes == before
    assert before
    assert not any(block["type"] == "image" for block in page)
    assert "External paragraph 7" in "".join(_visible(block["content"]) for block in page)


@pytest.mark.parametrize("size,columns", [(8, False), (12, False), (9, True)])
def test_font_size_and_columns_do_not_define_container_role(size: float, columns: bool) -> None:
    """字号和栏宽独立改变后仍保持原生页面包装等价，资源名称仅用于解析 Do。"""
    direct = _body_pdf(0, size=size, columns=columns)
    wrapped = _body_pdf(2, size=size, columns=columns)
    assert _predict(direct) == _predict(wrapped)
    writer = PdfWriter(clone_from=PdfReader(BytesIO(wrapped)))
    page = writer.pages[0]
    objects = page["/Resources"]["/XObject"]
    old = next(iter(objects))
    objects[NameObject("/ArbitraryResourceZ")] = objects.pop(old)
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data().replace(old.encode(), b"/ArbitraryResourceZ"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    assert _predict(direct) == _predict(output.getvalue())


def test_sparse_page_needs_matching_layout_framework() -> None:
    """同文档仅有相同整页声明框不足以展开位置和栏缘都不同的稀疏内容。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(600, 800))
    canvas.setFont("Helvetica", 9)
    for index in range(3):
        canvas.drawString(350, 500 - index * 20, "An ambiguous sparse label within a Form")
    canvas.save()
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(_body_pdf(1))))
    writer.append(PdfReader(BytesIO(_wrap_page(stream.getvalue(), 1))))
    output = BytesIO()
    writer.write(output)
    assert sum(block["type"] == "image" for block in _predict(output.getvalue())[1]) == 1
