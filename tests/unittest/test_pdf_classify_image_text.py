"""真实图像、附加文字与背景图的分类回归，不依赖 OCR 模型或字体名称。"""

from io import BytesIO
from importlib import import_module

import pytest
from PIL import Image
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument

classification = import_module("docvortex.document.pdf.classify")
BODY = [
    "This page contains readable document text at its original position.",
    "The existing search layer must not replace recognition of a scan.",
    "A digital document can use a large decorative background image.",
    "Classification should depend on which objects actually paint text.",
]


def _paint_text(canvas: Canvas, mode: int = 0, *, alpha: float = 1, color: tuple = (0, 0, 0)) -> None:
    """使用普通字体写入足够正文，并独立控制可见模式与透明度。"""
    canvas.saveState()
    canvas.setFillAlpha(alpha)
    canvas.setFillColorRGB(*color)
    text = canvas.beginText(20, 260)
    text.setFont("Helvetica", 9)
    text.setTextRenderMode(mode)
    for line in BODY:
        text.textLine(line)
    canvas.drawText(text)
    canvas.restoreState()


def image_text_pdf(kind: str, *, nested: bool = False, rotation: int = 0) -> bytes:
    """生成可见正文背景页，或附有隐藏、透明、被图覆盖文字的扫描页。"""
    source = BytesIO()
    painter = Canvas(source, pagesize=(400, 300))
    _paint_text(painter)
    painter.save()
    with pdfium.PdfDocument(source.getvalue()) as doc:
        page = doc[0]
        bitmap = page.render(scale=2)
        try:
            scan = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
            page.close()
    # 蓝色底图含少量纹理，避免将纯色图片特征误当成判定条件。
    background = Image.new("RGB", (800, 600), (225, 235, 250))
    for y in range(0, 600, 16):
        for x in range(800):
            background.putpixel((x, y), (210, 225, 245))
    result = BytesIO()
    painter = Canvas(result, pagesize=(400, 300))
    painter.setPageRotation(rotation)
    image = background if kind.startswith("background") else scan
    if kind == "covered":
        _paint_text(painter)
    painter.saveState()
    if kind == "cropped_scan":
        clip = painter.beginPath()
        clip.rect(0, 210, 400, 90)
        painter.clipPath(clip, stroke=0)
    if nested:
        painter.beginForm("page_image", 0, 0, 200, 150)
        painter.drawImage(ImageReader(image), 0, 0, 200, 150)
        painter.endForm()
        painter.saveState()
        painter.scale(2, 2)
        painter.doForm("page_image")
        painter.restoreState()
    else:
        painter.drawImage(ImageReader(image), 0, 0, 400, 300)
    painter.restoreState()
    if kind.startswith("background"):
        _paint_text(painter, color=(226 / 255, 236 / 255, 251 / 255) if kind == "background_low_contrast" else (0, 0, 0))
        if kind == "background_with_hidden_copy":
            _paint_text(painter, 3)
    elif kind in {"hidden", "transparent", "hidden_with_header", "cropped_scan"}:
        _paint_text(painter, 3 if kind != "transparent" else 0, alpha=0 if kind == "transparent" else 1)
    if kind == "hidden_with_header":
        painter.setFont("Helvetica", 8)
        painter.drawString(20, 290, "Visible header and page number should not turn a scanned document into a text PDF.")
    painter.save()
    scan.close()
    background.close()
    return result.getvalue()


@pytest.mark.parametrize("kind", ["hidden", "transparent", "covered", "hidden_with_header", "scan", "cropped_scan"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("rotation", [0, 90])
def test_scan_with_supplemental_text_remains_ocr(kind: str, nested: bool, rotation: int) -> None:
    """附加文本、少量可见页眉、Form 缩放和页面旋转均不能将扫描件放行 txt。"""
    with PDFDocument(image_text_pdf(kind, nested=nested, rotation=rotation)) as doc:
        assert doc.classify() == "ocr"


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("kind", ["background", "background_with_hidden_copy", "background_low_contrast"])
def test_visible_native_text_over_background_is_txt(kind: str, nested: bool, rotation: int) -> None:
    """覆盖整页的有色图片和纹理不影响真正可见的原生正文。"""
    with PDFDocument(image_text_pdf(kind, nested=nested, rotation=rotation)) as doc:
        assert doc.classify() == "txt"


@pytest.mark.parametrize("kind", ["background", "covered", "hidden"])
def test_classification_preserves_page_render_and_original_text(kind: str) -> None:
    """分类前后页面像素、原始字符和模式保持相同，不能污染调用方后续解析。"""
    with PDFDocument(image_text_pdf(kind)) as doc:
        before = doc.render_page(0, scale=1).pil_image
        before_bytes = before.tobytes()
        before.close()
        raw = "".join(char["char"] for char in doc.get_page_chars(0))
        doc.classify()
        after = doc.render_page(0, scale=1).pil_image
        assert after.tobytes() == before_bytes
        after.close()
        assert "".join(char["char"] for char in doc.get_page_chars(0)) == raw


def test_public_auto_parse_uses_native_text_for_digital_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """公共 auto 入口对背景图走真实原生解析，不能初始化 OCR 推理。"""
    from docvortex import parse
    from docvortex.analyzers.ocr import pdf as ocr_pdf

    def forbidden(*args: object, **kwargs: object) -> None:
        """背景页若误走 OCR，立即暴露真实公共路由错误。"""
        raise AssertionError("Digital background must stay native")

    monkeypatch.setattr(ocr_pdf, "analyze_pdf_ocr", forbidden)
    result = parse(image_text_pdf("background"), file_suffix="pdf", parse_mode="auto")
    assert "readable document text" in result.middle_json.to_json()


def test_render_probe_failure_restores_live_page_text_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二次渲染失败时仍恢复同一存活页面的绘制状态，不能依赖重新打开文档掩盖污染。"""
    from docvortex.document.pdf import classification_visuals as visuals
    from docvortex.document.pdf.native_objects import _clipped_objects_of_type
    import pypdfium2.raw as raw

    original = visuals._render_rgb
    calls = 0

    def fail_second(page, scale):
        """在文字已被屏蔽后制造渲染失败，核验 finally 的真实恢复路径。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("probe failed")
        return original(page, scale)

    with pdfium.PdfDocument(image_text_pdf("background")) as doc:
        page = doc[0]
        try:
            members = list(_clipped_objects_of_type(page, raw.FPDF_PAGEOBJ_TEXT))
            before = [raw.FPDFTextObj_GetTextRenderMode(member.raw) for member in members]
            monkeypatch.setattr(visuals, "_render_rgb", fail_second)
            with pytest.raises(RuntimeError, match="probe failed"):
                visuals._text_paint_difference(page)
            assert [raw.FPDFTextObj_GetTextRenderMode(member.raw) for member in members] == before
        finally:
            page.close()


def test_native_pages_cannot_outvote_supplemental_text_scan() -> None:
    """混合文档中的明确扫描正文不能被其他数字页的多数票掩盖，整体 auto 需走 OCR。"""
    writer = PdfWriter()
    for kind in ["background", "background", "hidden", "background"]:
        writer.add_page(PdfReader(BytesIO(image_text_pdf(kind))).pages[0])
    stream = BytesIO()
    writer.write(stream)
    with PDFDocument(stream.getvalue()) as doc:
        assert doc.classify() == "ocr"
