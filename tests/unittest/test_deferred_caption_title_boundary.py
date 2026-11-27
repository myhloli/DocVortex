"""保护有独立图题的重复位图仅在完成标题判定后才进入正文版面。"""

from docvortex import parse
from docvortex.analyzers.native.pdf import pipeline
from docvortex.content.spans import inline_span_plain_text
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

_PAGE_SIZE = (595.3, 841.89)
_EARLY_RASTER = (70.85, 227.85, 544.3, 361.55)
_DEFERRED_RASTER = (80.75, 620.9, 534.45, 726.95)
_COMMANDS = (
    "Insert Row Above",
    "Insert Row Below",
    "Insert Column Before",
    "Insert Column After",
    "Delete Row",
    "Delete Column",
    "Delete Table",
    "Select Table",
    "Select Column",
    "Select Row",
    "Table Design",
    "Table Properties",
)


def _bitmap(color):
    """生成带颜色分界的非均匀真实 PDF Image XObject。"""
    image = Image.new("RGB", (180, 92), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 12, 180, 71), fill=color)
    draw.rectangle((24, 23, 49, 61), fill="black")
    return ImageReader(image)


def _place_raster(canvas, bbox, image):
    """按左上原点的源几何放置一张位图。"""
    x0, y0, x1, y1 = bbox
    canvas.drawImage(image, x0, _PAGE_SIZE[1] - y1, x1 - x0, y1 - y0)


def _label(canvas, x, y, value, size=10, font="Helvetica"):
    """写入原生工具命令和题名，保留真实的字号、坐标与基线。"""
    canvas.setFont(font, size)
    canvas.drawString(x, _PAGE_SIZE[1] - y, value)


def _make_toolbar_pdf(path):
    """同一源位图在四页复用，目标页独有题名，图前有同样式的双栏编号命令。"""
    canvas = Canvas(str(path), pagesize=_PAGE_SIZE)
    reusable = _bitmap("steelblue")
    ordinary = _bitmap("darksalmon")
    for page in range(4):
        _place_raster(canvas, _DEFERRED_RASTER, reusable)
        if page == 2:
            _place_raster(canvas, _EARLY_RASTER, ordinary)
            _label(canvas, 72, 130, "Viewing and updating the editor configuration", 12)
            _label(canvas, 72, 378, "Figure 6: Default toolbar", 11, "Helvetica-Oblique")
            _label(canvas, 72, 404, "Editing tools", 14, "Helvetica-Bold")
            _label(canvas, 72, 424, "The toolbar provides creation and formatting tools for the current drawing.", 11)
            _label(canvas, 72, 439, "Controls can be enabled before editing starts.", 11)
            _label(canvas, 72, 466, "Available commands follow their on-screen order.", 11)
            left = (
                "New",
                "Copy",
                "Add Border",
                "Change Border",
                "Fill Area",
                "Move Element",
                "Align Left",
                "Align Right",
                "Group Objects",
                "Keep Original Shape",
                "Merge Visible Groups",
                "Split Selected Group",
            )
            for index, command in enumerate(left, 1):
                y = 486 + (index - 1) * 11.5
                _label(canvas, 76 if index >= 10 else 82, y, f"{index})")
                _label(canvas, 107, y, command)
            for index, command in enumerate(_COMMANDS, 13):
                y = 486 + (index - 13) * 11.5
                _label(canvas, 313, y, f"{index})")
                _label(canvas, 344, y, command)
            _label(canvas, 81, 743, "Figure 7: Editing toolbar", 11, "Helvetica-Oblique")
        else:
            for row in range(10):
                _label(
                    canvas,
                    72,
                    89 + 18 * row,
                    f"Step {row + 1} establishes the editor configuration for section {page + 1}.",
                    11,
                )
        canvas.showPage()
    canvas.save()


def _same_box(box, expected):
    """比较出自 PDF 单精度的点坐标，容许小数序列化误差。"""
    return all(abs(value - saved) < 0.01 for value, saved in zip(box, expected, strict=True))


def test_captioned_repeated_bitmap_cannot_enter_title_container_pool(tmp_path, monkeypatch):
    """标题分析只看原有容器，最终公开图体及图题与连续命令同时存在。"""
    original = pipeline._classify_page_titles
    snapshots = []

    def inspect(lines, size, **kwargs):
        """记下目标页实际进入标题判定的占位，继续运行生产算法。"""
        if kwargs["page_index"] == 2:
            snapshots.extend(kwargs["container_bboxes"])
        return original(lines, size, **kwargs)

    monkeypatch.setattr(pipeline, "_classify_page_titles", inspect)
    source = tmp_path / "commands.pdf"
    _make_toolbar_pdf(source)
    result = parse(source, parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    pages = result.to_dict()["pages"]
    page = pages[2]
    figure = next(
        block
        for block in page["blocks"]
        if block["type"] == "image"
        and any(
            child["type"] == "image_caption" and inline_span_plain_text(child["content"]) == "Figure 7: Editing toolbar"
            for child in block["content"]
        )
    )
    commands = " ".join(f"{number}) {word}" for number, word in enumerate(_COMMANDS, 13))
    matching = [
        (block["type"], inline_span_plain_text(block["content"]), block["index"])
        for block in page["blocks"]
        if block["type"] in {"text", "paragraph_title"} and "17) Delete Row" in inline_span_plain_text(block["content"])
    ]
    assert [(kind, text) for kind, text, _ in matching] == [("text", commands)]
    assert matching[0][2] < figure["index"]
    assert any(_same_box(box, _EARLY_RASTER) for box in snapshots)
    assert not any(_same_box(box, _DEFERRED_RASTER) for box in snapshots)
