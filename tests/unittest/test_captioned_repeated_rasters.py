"""在公共 Flash 路径锁定带独立图题的重复位图及附近的混淆结构。"""

from hashlib import sha256
from io import BytesIO
from os import environ
from pathlib import Path

from docvortex import parse
from docvortex.analyzers.native.pdf.pipeline import _collect_document_sources, _detect_repeated_raster_watermark_fingerprints
from docvortex.content.spans import inline_span_plain_text
from docvortex.document.pdf._document import PDFDocument
from PIL import Image, ImageDraw
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

_PAGE = (600, 780)
_PRIMARY = (78, 232, 309, 456)
_SIBLING = (350, 232, 527, 456)
_BLUE = (43, 92, 175)
_ORANGE = (175, 92, 43)


def _pattern(accent):
    """生成分层颜色证据，便于在实际 JPEG 中核查是原位图而非空裁片或邻图。"""
    image = Image.new("RGB", (170, 151), (231, 235, 222))
    paint = ImageDraw.Draw(image)
    paint.rectangle((0, 0, 170, 37), fill=(62, 137, 78))
    paint.rectangle((0, 38, 170, 113), fill=accent)
    paint.rectangle((0, 114, 170, 151), fill=(162, 49, 100))
    paint.rectangle((22, 50, 26, 100), fill=(255, 255, 255))
    original = BytesIO()
    image.save(original, "PNG")
    return ImageReader(BytesIO(original.getvalue()))


def _draw_image(canvas, image, box):
    """以源 PDF 固定版心点坐标绘制位图，并原样复用 PDF Image XObject。"""
    x0, y0, x1, y1 = box
    canvas.drawImage(image, x0, _PAGE[1] - y1, x1 - x0, y1 - y0)


def _draw_label(canvas, content, x, y, *, size=12):
    """写入可搜索的原生 PDF 单行，使用页面左上点坐标。"""
    canvas.setFont("Helvetica", size)
    canvas.drawString(x, _PAGE[1] - y, content)


def _make_pdf(path, mode):
    """固定同一位图的四页位置，逐例改变独立图题文本、竞争或正文覆盖。"""
    canvas = Canvas(str(path), pagesize=_PAGE)
    primary, sibling = _pattern(_BLUE), _pattern(_ORANGE)
    for page in range(4):
        if mode == "stacked":
            _draw_image(canvas, primary, (78, 56, 309, 226))
            if page < 2:
                _draw_label(canvas, f"Figure {42 + page * 2}: Top valve response", 79, 248)
                _draw_image(canvas, sibling, (78, 256, 309, 464))
                _draw_label(canvas, f"Figure {43 + page * 2}: Secondary unit", 79, 482)
            _draw_label(canvas, "Pressure calibration procedures for portable equipment.", 78, 612)
            _draw_label(canvas, "Read the calibration reference before servicing the valve.", 78, 632)
            _draw_label(canvas, "Record observations alongside the original control results.", 78, 652)
            canvas.showPage()
            continue
        _draw_image(canvas, primary, _PRIMARY)
        _draw_label(canvas, "Pressure calibration procedures for portable equipment.", 78, 140)
        if mode == "positive" and page < 2:
            _draw_label(
                canvas,
                ("Figure 1: Hydraulic output", "Fig. B.1: Response in the auxiliary circuit")[page],
                83,
                476,
            )
            if page == 1:
                # 这一不同素材只使用一次，供公开父图与资产颜色的对照。
                _draw_image(canvas, sibling, _SIBLING)
        elif mode == "reference":
            _draw_label(canvas, "Figure 1 shows the response in the auxiliary circuit.", 80, 476)
        elif mode == "table":
            _draw_label(canvas, "Table 1: Hydraulic calibration data", 82, 476)
        elif mode == "cover-text":
            _draw_label(canvas, "Figure 1: Hydraulic output", 84, 476)
            _draw_label(canvas, "The equation and instructions remain editable.", 90, 321)
        elif mode == "competing":
            _draw_image(canvas, sibling, _SIBLING)
            _draw_label(
                canvas,
                "Figure 1: Measurements from the left and right circuits under common thermal conditions",
                77,
                476,
            )
        canvas.showPage()
    canvas.save()


def _recover_image_candidates(path, watermark_count=1):
    """先证实测试输入命中原水印条件，再从真实源收集入口读取候选。"""
    with PDFDocument(str(path)) as pdf:
        sizes = [pdf.page_size(n) for n in range(pdf.page_count)]
        images = [pdf.get_page_image_infos(n) for n in range(pdf.page_count)]
        marks = _detect_repeated_raster_watermark_fingerprints(images, sizes)
        assert len(marks) == watermark_count
        return _collect_document_sources(pdf).page_sources


def _selected_boxes(page):
    """合并图参与早期分析的框和仅收集了证据、等待晚物化的裸位图。"""
    return set(page.image_bboxes) | getattr(page, "raster_caption_rows", {}).keys()


def _text(block):
    """统一输出公开原生段落和图题的 Span 文字。"""
    return inline_span_plain_text(block["content"])


def _assert_raster_asset(folder, body, accent):
    """验证三处实际裁图颜色和像素大小，确保输出是完整预期图像。"""
    with Image.open(folder / body["image_path"]) as raw:
        image = raw.convert("RGB")
        assert image.width > 100 and image.height > 100
        for part, expect in [(0.12, (62, 137, 78)), (0.5, accent), (0.88, (162, 49, 100))]:
            rgb = image.getpixel((image.width // 2, round(part * image.height)))
            assert max(abs(a - b) for a, b in zip(rgb, expect, strict=True)) <= 12, (body, rgb, expect)


def test_known_caption_owns_fixed_repeated_bitmap_without_callout(tmp_path):
    """普通及附录编号图题在正确图内只出现一次，公开资产完整且裸水印位置继续排除。"""
    path = tmp_path / "captioned.pdf"
    _make_pdf(path, "positive")
    sources = _recover_image_candidates(path)
    assert [len(_selected_boxes(page)) for page in sources] == [1, 2, 0, 0]
    assert all(not page.raster_callout_bboxes for page in sources)
    result = parse(path, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    pages = result.to_dict()["pages"]
    html = tmp_path / "captioned.html"
    result.export(html, output_format="html")
    html_content = html.read_text()
    assert all(not [b for b in pages[page]["blocks"] if b["type"] == "image"] for page in (2, 3))
    for page_index, caption in enumerate(("Figure 1: Hydraulic output", "Fig. B.1: Response in the auxiliary circuit")):
        figures = [block for block in pages[page_index]["blocks"] if block["type"] == "image"]
        owner = min(figures, key=lambda block: block["bbox"][0])
        body = next(b for b in owner["content"] if b["type"] == "image_body")
        assert body["bbox"] == [0.13, 0.297, 0.515, 0.585]
        captions = [b for b in owner["content"] if b["type"] == "image_caption"]
        assert [_text(b) for b in captions] == [caption]
        assert html_content.count(caption) == 1
        assert html_content.count(body["image_path"]) == 1
        _assert_raster_asset(tmp_path, body, _BLUE)
        if page_index == 1:
            assert len(figures) == 2
            neighbour = max(figures, key=lambda block: block["bbox"][0])
            [neighbour_body] = neighbour["content"]
            _assert_raster_asset(tmp_path, neighbour_body, _ORANGE)


@pytest.mark.parametrize("mode", ["reference", "table", "cover-text", "competing"])
def test_caption_rejects_ambiguous_or_nonvisual_evidence(tmp_path, mode):
    """正文引用、表题、墨迹覆盖及两个同距离候选都不得豁免重复图像。"""
    path = tmp_path / f"{mode}.pdf"
    _make_pdf(path, mode)
    sources = _recover_image_candidates(path, watermark_count=2 if mode == "competing" else 1)
    assert all(not _selected_boxes(page) for page in sources)
    result = parse(path, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    for page in result.to_dict()["pages"]:
        assert all(block["type"] != "image" for block in page["blocks"])
        if mode == "reference":
            assert any(block["type"] == "text" and "shows the response" in _text(block) for block in page["blocks"])
        if mode == "cover-text":
            assert any("instructions remain editable" in str(block["content"]) for block in page["blocks"])


def _assert_stacked_figures(page, html, expected):
    """按输出顺序核对每张图只有自己的原图题，HTML 图片后紧跟该图题。"""
    figures = [block for block in page["blocks"] if block["type"] == "image"]
    assert len(figures) == len(expected)
    previous = -1
    bodies = []
    for figure, (bbox, caption) in zip(figures, expected, strict=True):
        body = next(block for block in figure["content"] if block["type"] == "image_body")
        assert body["bbox"] == bbox
        assert [_text(block) for block in figure["content"] if block["type"] == "image_caption"] == [caption]
        image_index = html.index(body["image_path"])
        caption_index = html.index(caption)
        assert previous < image_index < caption_index
        previous = caption_index
        bodies.append(body)
    return bodies


def test_repeated_raster_caption_keeps_upper_parent_even_when_lower_image_is_closer(tmp_path):
    """上图先经图题图形物化时，较近的下图仍只能得到自己的题名。"""
    path = tmp_path / "stacked.pdf"
    _make_pdf(path, "stacked")
    sources = _recover_image_candidates(path)
    assert [len(_selected_boxes(page)) for page in sources] == [2, 2, 0, 0]
    result = parse(path, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    html = tmp_path / "stacked.html"
    result.export(html, output_format="html")
    contents = html.read_text()
    for page_index, page in enumerate(result.to_dict()["pages"][:2]):
        upper, lower = _assert_stacked_figures(
            page,
            contents,
            [
                ([0.13, 0.072, 0.515, 0.29], f"Figure {42 + page_index * 2}: Top valve response"),
                ([0.13, 0.328, 0.515, 0.595], f"Figure {43 + page_index * 2}: Secondary unit"),
            ],
        )
        _assert_raster_asset(tmp_path, upper, _BLUE)
        _assert_raster_asset(tmp_path, lower, _ORANGE)


def test_libreoffice_neighbour_caption_owners(tmp_path):
    """全本原件复核四对连续编号的父图、顺序、HTML 资源和邻图题名。"""
    filename = environ.get("DOCVORTEX_TEST_LIBREOFFICE_DRAW_GUIDE")
    if not filename:
        pytest.skip("Set DOCVORTEX_TEST_LIBREOFFICE_DRAW_GUIDE to the original LibreOffice Draw Guide PDF")
    source = Path(filename)
    assert sha256(source.read_bytes()).hexdigest() == "acc41bb2ddd44224d6baac834311cd92b254a8f59ed244a1eab55b97984fd5c4"
    result = parse(source, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    html = tmp_path / "guide.html"
    result.export(html, output_format="html")
    contents = html.read_text()
    cases = {
        24: [
            ([0.136, 0.067, 0.898, 0.209], "Figure 11: Line and Filling toolbar"),
            ([0.136, 0.238, 0.898, 0.364], "Figure 12: Text Formatting toolbar"),
        ],
        30: [
            ([0.177, 0.444, 0.857, 0.612], "Figure 16: Drawing toolbar"),
            ([0.136, 0.641, 0.898, 0.777], "Figure 17: Status Bar"),
        ],
        33: [
            ([0.303, 0.067, 0.731, 0.375], "Figure 21: Line panel in Properties deck on Sidebar"),
            ([0.136, 0.405, 0.898, 0.546], "Figure 22: Line and Filling toolbar"),
        ],
        201: [
            ([0.211, 0.067, 0.823, 0.219], "Figure 206: Drawing toolbar"),
            ([0.397, 0.248, 0.636, 0.315], "Figure 207: Text toolbar"),
        ],
    }
    pages = result.to_dict()["pages"]
    for physical, expected in cases.items():
        for body in _assert_stacked_figures(pages[physical - 1], contents, expected):
            with Image.open(tmp_path / body["image_path"]) as image:
                image.load()
                assert min(image.size) > 30

    commands = "13) Insert Row Above 14) Insert Row Below 15) Insert Column Before 16) Insert Column After 17) Delete Row 18) Delete Column 19) Delete Table 20) Select Table 21) Select Column 22) Select Row 23) Table Design 24) Table Properties"
    page = pages[373]
    candidates = [
        block for block in page["blocks"] if block["type"] in {"text", "paragraph_title"} and "17) Delete Row" in _text(block)
    ]
    assert [(block["type"], _text(block)) for block in candidates] == [("text", commands)]
    figure = next(
        block
        for block in page["blocks"]
        if block["type"] == "image"
        and any(
            child["type"] == "image_caption" and _text(child) == "Figure 362: Text Formatting toolbar"
            for child in block["content"]
        )
    )
    assert candidates[0]["index"] < figure["index"]
    assert figure["bbox"] == [0.136, 0.738, 0.898, 0.863]
    result.export(tmp_path / "guide.md", output_format="markdown")
    markdown = (tmp_path / "guide.md").read_text().splitlines()
    assert markdown.count(commands) == 1
